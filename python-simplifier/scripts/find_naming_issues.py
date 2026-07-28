#!/usr/bin/env python3
"""
Detect naming problems in Python code via AST analysis.

Finds:
  - shadows_builtin          : a name that shadows a Python builtin (list, dict,
                               id, type, ...), which hides the builtin and confuses readers
  - non_snake_case_function  : a function/method not named in snake_case
  - non_pascal_case_class    : a class not named in PascalCase

(Overlaps Ruff's pep8-naming (N) and flake8-builtins (A) rule sets; included so
the suite is self-contained for repos that don't run those.)
"""

import ast
import re
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


# Builtins worth flagging when shadowed (high-traffic / bug-prone ones).
_BUILTINS = {
    "list", "dict", "set", "tuple", "str", "int", "float", "bool", "bytes",
    "type", "id", "input", "file", "object", "property", "super", "open",
    "sum", "min", "max", "map", "filter", "range", "len", "sorted", "reversed",
    "iter", "next", "zip", "enumerate", "all", "any", "format", "hash", "dir",
    "vars", "repr", "hex", "oct", "bin", "abs", "round", "pow", "divmod",
    "chr", "ord", "callable", "getattr", "setattr", "hasattr", "globals",
    "locals", "exec", "eval", "compile", "copyright", "license", "exit", "quit",
}

_SNAKE = re.compile(r"^_{0,2}[a-z][a-z0-9_]*_{0,2}$")
_PASCAL = re.compile(r"^_?[A-Z][a-zA-Z0-9]*$")


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


class NamingDetector(ast.NodeVisitor):
    def __init__(self, filename, lines, ignore):
        self.filename = filename
        self.lines = lines
        self.ignore = ignore
        self.issues = []
        self._reported = set()   # (line, name, type) to avoid duplicate shadow reports

    def _add(self, line, st, desc, sug, sev):
        if st in self.ignore:
            return
        self.issues.append(CodeSmell(self.filename, line, st, desc, sug, sev, _get_line(self.lines, line)))

    def _shadow(self, name, line):
        if name in _BUILTINS and (line, name, "shadows_builtin") not in self._reported:
            self._reported.add((line, name, "shadows_builtin"))
            self._add(line, "shadows_builtin",
                f"'{name}' shadows the Python builtin of the same name",
                f"Rename it (e.g. '{name}_' or a more descriptive name) so the builtin stays available and intent is clear.",
                "medium")

    def visit_FunctionDef(self, node):
        self._check_func(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._check_func(node)
        self.generic_visit(node)

    def _check_func(self, node):
        if not _is_dunder(node.name):
            self._shadow(node.name, node.lineno)
            if not _SNAKE.match(node.name):
                self._add(node.lineno, "non_snake_case_function",
                    f"Function '{node.name}' is not snake_case",
                    "Rename to snake_case (PEP 8), e.g. 'parse_value' not 'parseValue'.",
                    "low")
        for a in list(getattr(node.args, "posonlyargs", [])) + list(node.args.args) + list(node.args.kwonlyargs):
            if a.arg not in {"self", "cls"}:
                self._shadow(a.arg, node.lineno)

    def visit_ClassDef(self, node):
        if not _PASCAL.match(node.name):
            self._add(node.lineno, "non_pascal_case_class",
                f"Class '{node.name}' is not PascalCase",
                "Rename to PascalCase (PEP 8), e.g. 'HttpClient' not 'http_client'.",
                "low")
        self.generic_visit(node)

    def visit_Assign(self, node):
        for t in node.targets:
            if isinstance(t, ast.Name) and not _is_dunder(t.id):
                self._shadow(t.id, node.lineno)
        self.generic_visit(node)

    def visit_For(self, node):
        if isinstance(node.target, ast.Name) and not _is_dunder(node.target.id):
            self._shadow(node.target.id, node.lineno)
        self.generic_visit(node)


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
        d = NamingDetector(str(filepath), source.splitlines(), ignore)
        d.visit(tree)
        return d.issues
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
    parser = argparse.ArgumentParser(description="Detect naming problems")
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
            print("✅ No naming problems found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} naming issue(s):\n\nSummary:")
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
