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
from collections import defaultdict
import global_state_evidence as evidence
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


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _is_mutable_value(value) -> bool:
    if isinstance(value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in _MUTABLE_BUILTINS:
        return True
    return False


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
    locks, guarded = evidence.mutation_guards(tree, set(candidates))
    for fn, _ in evidence.function_scopes(tree):
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

    for nm in sorted(guarded):
        common_locks = set.intersection(*(set(held) for held in guarded[nm]))
        if common_locks:
            names = sorted(name for name, mutex in locks.items() if mutex.identity in common_locks)
            add(candidates[nm], "mutated_global",
                f"Every detected direct mutation of module-level mutable '{nm}' holds the same stable "
                f"module lock ({', '.join(names)})",
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
