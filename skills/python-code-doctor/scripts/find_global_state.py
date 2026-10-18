#!/usr/bin/env python3
"""
Detect global-state hazards in Python code via AST analysis.

Module-level mutable state makes code order-dependent, hard to test, and unsafe
under concurrency. This finds the cases that actually bite (mutated shared
globals) rather than every module constant.

Finds:
  - mutated_global : a module-level mutable value (list/dict/set/...) that is
                     mutated at runtime from inside a function. High, unless
                     every detected direct write holds one common, stable
                     module-level threading Lock/RLock. That is a candidate:
                     a process registry or memo has the same shape, and this
                     file cannot establish a lifecycle or reader defect.
  - global_rebind  : a function that uses the `global` statement to reassign
                     module-level variables — one record per function, naming
                     every name it rebinds. A `global` line suppressed with
                     `# noqa: PLW0603 -- <reason>` is a candidate: the repo's
                     gate adjudicated it (an at-fork hook, say, whose return
                     value nobody receives).

A mutation is charged to the module's name only where that name *is* the
module's: a function that binds the same name locally (or inherits it from an
enclosing function) is mutating its own object, whatever it is spelled.
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import Counter, defaultdict
from common import (sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files,
                    reasoned_noqa, warn_detector_error, warn_unparseable)


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""
    # A scored defect by default. "candidate" marks a lead the downstream merge
    # must not score: it carries no fix and names, in also_caused_by, the ways
    # a healthy codebase produces the same observation.
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_MUTABLE_BUILTINS = {"list", "dict", "set", "bytearray", "defaultdict",
                     "OrderedDict", "Counter", "deque"}
_MUTATING_METHODS = {
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse",
    "add", "discard", "update", "setdefault", "popitem",
    "appendleft", "popleft", "extendleft", "rotate",
}


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _is_mutable_value(value) -> bool:
    if isinstance(value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in _MUTABLE_BUILTINS:
        return True
    return False


_LOCK_FACTORIES = {"Lock", "RLock"}


def _same_scope_nodes(stmts):
    # The seeds obey the same rule as the children: a `def` directly in the body
    # is its own scope, and walking into it charged its `global` statements and
    # mutations to the enclosing function as well as to itself.
    stack = [s for s in stmts if not isinstance(s, _SCOPE_BOUNDARIES)]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARIES):
                continue
            stack.append(child)


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


def _mutates_name(node, names):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr in _MUTATING_METHODS and isinstance(node.func.value, ast.Name) and node.func.value.id in names:
            return node.func.value.id
    if isinstance(node, ast.Delete):
        for t in node.targets:
            if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id in names:
                return t.value.id
    if isinstance(node, (ast.Assign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id in names:
                return t.value.id
    return None


def _guarded_nodes(stmts, locks):
    """Same-scope nodes paired with the known module locks held at each node.

    `locks` names the module-level Lock/RLock objects. A nested function is
    its own scope and its own walk: it runs whenever it is called, which need
    not be while the lock that surrounds its definition is held.
    """
    stack = [(s, frozenset()) for s in stmts if not isinstance(s, _SCOPE_BOUNDARIES)]
    while stack:
        node, held = stack.pop()
        yield node, held
        if isinstance(node, ast.With):
            # Context expressions are evaluated before their own __enter__,
            # in order: the first expression is not protected by a later lock.
            children = []
            for item in node.items:
                children.append((item.context_expr, held))
                if isinstance(item.context_expr, ast.Name) and item.context_expr.id in locks:
                    held = held | {item.context_expr.id}
                if item.optional_vars:
                    children.append((item.optional_vars, held))
            children.extend((stmt, held) for stmt in node.body)
            stack.extend(reversed(children))
            continue
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, _SCOPE_BOUNDARIES):
                stack.append((child, held))


def _module_scope_nodes(tree):
    """Nodes evaluated in the module's scope, including definition inputs.

    Bodies of functions and classes have their own namespace. Defaults,
    decorators and bases still run here, where a walrus can replace a lock.
    """
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                stack.extend(node.bases)
                stack.extend(k.value for k in node.keywords)
            else:
                stack.extend(ast.iter_child_nodes(node.args))
                if node.returns:
                    stack.append(node.returns)
            continue
        if isinstance(node, ast.Lambda):
            stack.extend(node.args.defaults)
            stack.extend(v for v in node.args.kw_defaults if v is not None)
            continue
        if isinstance(node, ast.AnnAssign) and node.value is None:
            stack.append(node.annotation)   # a bare annotation assigns no value
            continue
        if isinstance(node, ast.comprehension):
            stack.extend([node.iter] + list(node.ifs))
            continue
        stack.extend(ast.iter_child_nodes(node))


def _module_bindings(tree) -> Counter:
    """Count bindings and deletes of module names, excluding same-named locals."""
    bound = Counter()
    for node in _module_scope_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound[node.name] += 1
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound[node.id] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names if a.name != "*")
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound[node.name] += 1
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound[node.name] += 1
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound[node.rest] += 1
    return bound


def _lock_interference(tree, threading_modules, lock_names):
    """Visible constructor replacements and manual releases defeat the with proof.

    Use the same lexical bindings as the mutation reader: releasing a caller's
    same-named parameter says nothing about the module's guard.
    """
    scopes = [(_module_scope_nodes(tree), frozenset())]
    for fn, enclosing in _function_scopes(tree):
        global_names = {name for node in _same_scope_nodes(fn.body)
                        if isinstance(node, ast.Global) for name in node.names}
        local = (_bound_names(fn) | enclosing) - global_names
        scopes.append((_same_scope_nodes(fn.body), local))
    replaced, released = set(), set()
    for nodes, local in scopes:
        for node in nodes:
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                if (isinstance(node.value, ast.Name) and node.value.id in threading_modules - local
                        and node.attr in _LOCK_FACTORIES):
                    replaced.add((node.value.id, node.attr))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                func = node.func
                if (isinstance(func.value, ast.Name) and func.value.id in lock_names - local
                        and func.attr in {"release", "__exit__"}):
                    released.add(func.value.id)
    return replaced, released


def _module_locks(tree) -> set:
    """Stable names bound once to an imported threading Lock/RLock constructor.

    A global declaration anywhere keeps a name out of this bounded proof: it
    permits a later call to replace the object the name originally identified.
    """
    bindings = _module_bindings(tree)
    global_names = {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}
    stable = {name for name, count in bindings.items() if count == 1} - global_names
    threading_modules, lock_factories = set(), set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == "threading":
                    threading_modules.add(alias.asname or "threading")
        elif isinstance(stmt, ast.ImportFrom) and stmt.module == "threading" and stmt.level == 0:
            for alias in stmt.names:
                if alias.name in _LOCK_FACTORIES:
                    lock_factories.add(alias.asname or alias.name)
    threading_modules &= stable
    lock_factories &= stable
    locks = {}
    for stmt in tree.body:
        value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        is_lock = (
            (isinstance(func, ast.Name) and func.id in lock_factories)
            or (isinstance(func, ast.Attribute) and func.attr in _LOCK_FACTORIES
                and isinstance(func.value, ast.Name) and func.value.id in threading_modules)
        )
        if not is_lock:
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        origin = (func.value.id, func.attr) if isinstance(func, ast.Attribute) else None
        locks.update((t.id, origin) for t in targets if isinstance(t, ast.Name) and t.id in stable)
    if not locks:
        return set()
    replaced, released = _lock_interference(tree, threading_modules, set(locks))
    return {name for name, origin in locks.items() if name not in released and origin not in replaced}


def _bound_names(fn) -> set:
    """Names a function binds in its own scope — its locals, as Python decides
    them: parameters, assignment and loop targets, `as` names, imports, and the
    defs and classes it creates. `global` names are the module's and are left
    out; `nonlocal` names are an enclosing function's, so they are kept (they
    are not the module's either)."""
    args = fn.args
    params = list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs)
    params += [a for a in (args.vararg, args.kwarg) if a is not None]
    bound = {a.arg for a in params}
    declared_global = set()
    stack = [s for s in fn.body]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)     # the name is local; the body is another scope
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Global):
            declared_global.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names if a.name != "*")
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        if isinstance(node, ast.comprehension):
            # A comprehension's loop variable lives in the comprehension's own
            # scope; only what it iterates (and a walrus in it) reaches ours.
            stack.extend([node.iter] + list(node.ifs))
            continue
        stack.extend(ast.iter_child_nodes(node))
    return bound - declared_global


def _function_scopes(tree):
    """Every function, with the names enclosing *functions* bind (a closure
    sees those, not the module's). Class bodies bind nothing a method sees."""
    def visit(node, enclosing):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child, enclosing
                yield from visit(child, enclosing | _bound_names(child))
            else:
                yield from visit(child, enclosing)
    yield from visit(tree, frozenset())


def detect(tree, filename, lines, ignore):
    issues = []

    def add(line, st, desc, sug, sev, also_caused_by=()):
        if st in ignore:
            return
        issues.append(CodeSmell(filename, line, st, desc, "" if also_caused_by else sug, sev,
                                _get_line(lines, line),
                                "candidate" if also_caused_by else "finding", tuple(also_caused_by)))

    # Pass 1: module-level mutable assignments
    candidates = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and _is_mutable_value(stmt.value):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and not _is_dunder(t.id):
                    candidates[t.id] = stmt.lineno
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None and _is_mutable_value(stmt.value):
            if isinstance(stmt.target, ast.Name) and not _is_dunder(stmt.target.id):
                candidates[stmt.target.id] = stmt.lineno

    # Pass 2: scan every function for `global` rebinds and mutations of candidates
    locks = _module_locks(tree)
    guarded = defaultdict(list)     # name -> [stable module lock names held] per mutation site
    candidate_names = set(candidates)
    for fn, enclosing in _function_scopes(tree):
        body = list(_same_scope_nodes(fn.body))
        global_lines = {}
        for n in body:
            if isinstance(n, ast.Global):
                for nm in n.names:
                    global_lines.setdefault(nm, n.lineno)
        globals_here = set(global_lines)
        rebinds = set()
        for n in body:
            if isinstance(n, ast.Assign):
                rebinds.update(t.id for t in n.targets if isinstance(t, ast.Name) and t.id in globals_here)
            elif isinstance(n, ast.AugAssign):
                if isinstance(n.target, ast.Name) and n.target.id in globals_here:
                    rebinds.add(n.target.id)
            elif isinstance(n, ast.AnnAssign):
                if isinstance(n.target, ast.Name) and n.target.id in globals_here and n.value is not None:
                    rebinds.add(n.target.id)
        if rebinds:
            named = ", ".join(f"'{nm}'" for nm in sorted(rebinds))
            plural = "s" if len(rebinds) > 1 else ""
            # Ruff's PLW0603 is reported on the `global` statement; a stated
            # reason there for every name rebound is the repo's adjudication.
            reasons = ()
            if all(reasoned_noqa(lines, global_lines[nm], "PLW0603") for nm in rebinds):
                reasons = ("the repo's PLW0603 lint gate suppresses this `global` with a stated reason — "
                           "e.g. an at-fork or install-once hook whose return value no caller receives",)
            add(fn.lineno, "global_rebind",
                f"Function '{fn.name}' reassigns module-level global{plural} {named} via the 'global' statement",
                "Return the new value and assign it at the call site, or encapsulate the state in a class or closure.",
                "medium", also_caused_by=reasons)
        # Only the module's names: one this function (or an enclosing function)
        # binds locally is a different object that happens to share the spelling.
        local = (_bound_names(fn) | enclosing) - globals_here
        visible = candidate_names - local
        if not visible:
            continue
        for n, held in _guarded_nodes(fn.body, locks - local):
            m = _mutates_name(n, visible)
            if m:
                guarded[m].append(held)

    for nm in sorted(guarded):
        common_locks = set.intersection(*(set(held) for held in guarded[nm]))
        if common_locks:
            add(candidates[nm], "mutated_global",
                f"Every detected direct mutation of module-level mutable '{nm}' holds the same stable "
                f"module lock ({', '.join(sorted(common_locks))})",
                "", "medium", also_caused_by=(
                    "an intentional process-wide registry or memo whose direct writes share one threading Lock/RLock",
                    "reads, mutations through aliases, external consumers and fork/reset lifecycle are not proved "
                    "by this file-level write check",
                ))
        else:
            add(candidates[nm], "mutated_global",
                f"Module-level mutable '{nm}' is shared global state that is mutated at runtime",
                "Encapsulate it in a class or pass it explicitly. Module-level mutable state makes execution "
                "order-dependent and hard to test.",
                "high")

    return issues


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source, tree = cached_parse(filepath)
        return detect(tree, str(filepath), source.splitlines(), ignore)
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def to_record(issue: "CodeSmell") -> dict:
    """The JSON shape this detector emits, shared with the runner.

    `kind` is always present, so a consumer never has to read an absent key as
    a classification; the reasons tuple serialises as a list.
    """
    record = asdict(issue)
    record["also_caused_by"] = list(record["also_caused_by"])
    return record


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect global-state hazards in Python")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))
    sort_findings(all_issues)

    if args.format == "json":
        print(json.dumps([to_record(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No global-state hazards found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        proven = sum(1 for i in all_issues if i.kind != "candidate")
        print(f"Found {proven} global-state issue(s), {len(all_issues) - proven} candidate(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = SEVERITY_ICONS
        for i in all_issues:
            # A severity icon on a lead reads as a verdict the evidence does not support.
            marker = "? [CANDIDATE]" if i.kind == "candidate" else f"{icons[i.severity]} [{i.severity.upper()}]"
            print(f"{marker} {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            if i.suggestion:
                print(f"   → {i.suggestion}")
            for reason in i.also_caused_by:
                print(f"   ? also caused by: {reason}")
            print()


if __name__ == "__main__":
    main()
