#!/usr/bin/env python3
"""
Detect return-statement problems in Python code via AST analysis.

Finds:
  - inconsistent_returns : a function mixes `return <value>` with a bare `return`
                           (or `return None`), so callers can't trust the result
  - return_bool_condition : `if cond: return True` / `return False`, which is just
                            `return bool(cond)`

(Partially overlaps Ruff's RET / SIM103 rules; included so the suite is
self-contained and emits findings in the unified format.)
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


_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


def _same_scope_nodes(stmts):
    stack = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARIES):
                continue
            stack.append(child)


def _is_bare_return(r: ast.Return) -> bool:
    return r.value is None or (isinstance(r.value, ast.Constant) and r.value.value is None)


def _bool_value(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


class ReturnIssueDetector(ast.NodeVisitor):
    def __init__(self, filename, lines, ignore):
        self.filename = filename
        self.lines = lines
        self.ignore = ignore
        self.issues = []

    def _add(self, line, st, desc, sug, sev):
        if st in self.ignore:
            return
        self.issues.append(CodeSmell(self.filename, line, st, desc, sug, sev, _get_line(self.lines, line)))

    def _check_function(self, node):
        scope = list(_same_scope_nodes(node.body))
        # generators legitimately use bare `return`
        if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in scope):
            return
        returns = [n for n in scope if isinstance(n, ast.Return)]
        has_value = any(not _is_bare_return(r) for r in returns)
        has_bare = any(_is_bare_return(r) for r in returns)
        if has_value and has_bare:
            self._add(node.lineno, "inconsistent_returns",
                f"{node.name} mixes 'return <value>' with a bare 'return'/'return None'",
                "Return a consistent type on every path. If 'no result' is meaningful, return None explicitly everywhere and document it.",
                "medium")

    def visit_FunctionDef(self, node):
        self._check_function(node)
        self._scan_block(node.body)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._check_function(node)
        self._scan_block(node.body)
        self.generic_visit(node)

    def _scan_block(self, stmts):
        """Look for `if cond: return True` followed/elsed by `return False`."""
        for idx, stmt in enumerate(stmts):
            if not isinstance(stmt, ast.If):
                continue
            if len(stmt.body) != 1 or not isinstance(stmt.body[0], ast.Return):
                continue
            b = _bool_value(stmt.body[0].value)
            if b is None:
                continue
            # else-branch form
            other = None
            if len(stmt.orelse) == 1 and isinstance(stmt.orelse[0], ast.Return):
                other = _bool_value(stmt.orelse[0].value)
            # fall-through form: next statement returns the opposite bool
            elif not stmt.orelse and idx + 1 < len(stmts) and isinstance(stmts[idx + 1], ast.Return):
                other = _bool_value(stmts[idx + 1].value)
            if other is not None and other != b:
                self._add(stmt.lineno, "return_bool_condition",
                    "An if/else that returns True/False is just 'return <condition>'",
                    "Replace with `return <condition>` (wrap in bool() if the condition may be non-boolean).",
                    "low")
        # NOTE: nested blocks are reached via generic_visit -> visit_* on inner defs,
        # and via the recursive scan below for compound statements in the same function.
        for stmt in stmts:
            for attr in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, attr, None)
                if isinstance(inner, list) and not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    self._scan_block(inner)
            for handler in getattr(stmt, "handlers", []) or []:
                self._scan_block(handler.body)


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
        d = ReturnIssueDetector(str(filepath), source.splitlines(), ignore)
        d.visit(tree)
        # de-duplicate (recursive scan can revisit a block reached two ways)
        seen, unique = set(), []
        for i in d.issues:
            key = (i.line, i.smell_type)
            if key not in seen:
                seen.add(key)
                unique.append(i)
        return unique
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
    parser = argparse.ArgumentParser(description="Detect return-statement problems")
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
            print("✅ No return-statement problems found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} return-statement issue(s):\n\nSummary:")
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
