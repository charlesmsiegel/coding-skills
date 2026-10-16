#!/usr/bin/env python3
"""
Detect global-state hazards in Python code via AST analysis.

Module-level mutable state makes code order-dependent, hard to test, and unsafe
under concurrency. This finds the cases that actually bite (mutated shared
globals) rather than every module constant.

Finds:
  - mutated_global : a module-level mutable value (list/dict/set/...) that is
                     mutated at runtime from inside a function. High, unless
                     every mutation happens inside `with <module-level Lock>:` —
                     a lock-guarded registry or memo is the factory-with-a-cache
                     shape, safe under threads, and keeps only the
                     process-wide-state cost (medium).
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
from collections import defaultdict
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
    """Same-scope nodes paired with whether they run inside `with <lock>:`.

    `locks` names the module-level Lock/RLock objects. A nested function is
    its own scope and its own walk: it runs whenever it is called, which need
    not be while the lock that surrounds its definition is held.
    """
    stack = [(s, False) for s in stmts if not isinstance(s, _SCOPE_BOUNDARIES)]
    while stack:
        node, held = stack.pop()
        yield node, held
        if isinstance(node, (ast.With, ast.AsyncWith)) and any(
                isinstance(item.context_expr, ast.Name) and item.context_expr.id in locks
                for item in node.items):
            held = True
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, _SCOPE_BOUNDARIES):
                stack.append((child, held))


def _module_locks(tree) -> set:
    """Module-level names bound to `threading.Lock()` / `threading.RLock()`."""
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
    locks = set()
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
        locks.update(t.id for t in targets if isinstance(t, ast.Name))
    return locks


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
    guarded = defaultdict(list)     # name -> [held a module lock?] per mutation site
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
        for n, held in _guarded_nodes(fn.body, locks):
            m = _mutates_name(n, visible)
            if m:
                guarded[m].append(held)

    for nm in sorted(guarded):
        if all(guarded[nm]):
            add(candidates[nm], "mutated_global",
                f"Module-level mutable '{nm}' is shared global state mutated at runtime, always under a "
                "module-level lock",
                "The lock makes the mutation thread-safe; what remains is process-wide state every test and "
                "forked worker inherits. Keep it behind one accessor and give tests a reset, or move it with "
                "its lock into an object the caller owns.",
                "medium")
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
