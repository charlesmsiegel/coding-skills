#!/usr/bin/env python3
"""
Detect imports that are not at the top of the module.

Imports belong at module top level. Imports buried inside functions/methods are
usually a workaround for a circular import (a design smell to fix at the source) or
plain laziness; they hide a module's real dependencies, run on every call, and make
the import graph hard to see. They should be permitted only rarely — for a genuinely
optional dependency or a deliberate lazy/heavy import — so this flags them by default.

Finds:
  - local_import       : an import inside a function/method (a deferred import)
  - import_not_at_top  : a module-level import placed after real code (PEP 8 E402)

Deliberately NOT flagged (the legitimate deferred-import patterns):
  - imports guarded by `if TYPE_CHECKING:` (type-only imports)
  - imports inside a `try: ... except ImportError/ModuleNotFoundError:` (optional dep)
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Iterator
from collections import defaultdict


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


def _is_typechecking_test(test) -> bool:
    """True for `if TYPE_CHECKING:` (as a Name or attribute like typing.TYPE_CHECKING)."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _handler_catches_importerror(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True  # bare except guarding an import — treat as optional-dep pattern
    names = []
    if isinstance(handler.type, ast.Name):
        names = [handler.type.id]
    elif isinstance(handler.type, ast.Tuple):
        names = [e.id for e in handler.type.elts if isinstance(e, ast.Name)]
    return any(n in {"ImportError", "ModuleNotFoundError"} for n in names)


def _collect_guarded(tree) -> set:
    """Import nodes that sit in a legitimate deferred-import context (by id())."""
    guarded = set()
    for node in ast.walk(tree):
        # if TYPE_CHECKING: <imports>
        if isinstance(node, ast.If) and _is_typechecking_test(node.test):
            for n in ast.walk(node):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    guarded.add(id(n))
        # try: <imports> except ImportError: ...
        if isinstance(node, ast.Try) and any(_handler_catches_importerror(h) for h in node.handlers):
            for stmt in node.body:
                for n in ast.walk(stmt):
                    if isinstance(n, (ast.Import, ast.ImportFrom)):
                        guarded.add(id(n))
    return guarded


def _is_dunder_assign(stmt) -> bool:
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, ast.AnnAssign):
        targets = [stmt.target]
    for t in targets:
        if isinstance(t, ast.Name) and t.id.startswith("__") and t.id.endswith("__"):
            return True
    return False


def _only_imports(stmt) -> bool:
    """True if an If/Try statement contains only imports (a top-of-file guard block)."""
    if not isinstance(stmt, (ast.If, ast.Try)):
        return False
    return all(isinstance(n, (ast.Import, ast.ImportFrom, ast.Pass))
               for n in ast.walk(stmt)
               if isinstance(n, (ast.Import, ast.ImportFrom, ast.Expr, ast.Assign,
                                 ast.AnnAssign, ast.Pass))) and any(
               isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(stmt))


def detect(tree, filename, lines, ignore):
    issues = []

    def add(line, st, desc, sug, sev):
        if st in ignore:
            return
        issues.append(CodeSmell(filename, line, st, desc, sug, sev, _get_line(lines, line)))

    guarded = _collect_guarded(tree)

    # Case 1: imports inside functions/methods (deferred imports).
    def walk(node, in_function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                if in_function and id(child) not in guarded:
                    what = ("import " + ", ".join(a.name for a in child.names)
                            if isinstance(child, ast.Import)
                            else "from " + ("." * getattr(child, "level", 0)) + (child.module or "") + " import ...")
                    add(child.lineno, "local_import",
                        f"Import inside a function ('{what}') — a deferred import, usually a circular-import workaround or laziness",
                        "Move it to the top of the module. If it's breaking a real import cycle, fix the cycle; "
                        "if it's a genuinely optional/heavy dependency, guard it with try/except ImportError or TYPE_CHECKING and add a comment.",
                        "medium")
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                walk(child, True)
            else:
                walk(child, in_function)

    walk(tree, False)

    # Case 2: module-level import placed after real code (E402).
    seen_code = False
    for i, stmt in enumerate(tree.body):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            if seen_code and id(stmt) not in guarded:
                add(stmt.lineno, "import_not_at_top",
                    "Module-level import placed after other code instead of at the top of the file",
                    "Move all imports to the top of the module (after the docstring and __future__ imports).",
                    "low")
            continue
        # statements allowed above imports without counting as "real code"
        if i == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            continue  # module docstring
        if _is_dunder_assign(stmt) or _only_imports(stmt):
            continue
        seen_code = True

    return issues


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
        return detect(tree, str(filepath), source.splitlines(), ignore)
    except (SyntaxError, Exception):
        return []


def find_python_files(path: Path) -> Iterator[Path]:
    if path.is_file() and path.suffix == ".py":
        yield path
    elif path.is_dir():
        for p in path.rglob("*.py"):
            if ".venv" not in p.parts and "node_modules" not in p.parts and "__pycache__" not in p.parts:
                yield p


def main():
    parser = argparse.ArgumentParser(description="Detect imports not at the top of the module")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))
    all_issues.sort(key=lambda x: (x.severity != "high", x.severity != "medium", x.file, x.line))

    if args.format == "json":
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No non-top-level imports found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} import-placement issue(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        for i in all_issues:
            print(f"{icons[i.severity]} [{i.severity.upper()}] {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            print(f"   → {i.suggestion}\n")


if __name__ == "__main__":
    main()
