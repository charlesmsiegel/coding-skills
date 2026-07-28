#!/usr/bin/env python3
"""
Detect missing docstrings on the public API surface in Python code via AST analysis.

Well-documented public APIs reduce onboarding friction and prevent misuse.
This finds modules, functions, methods, and classes that are publicly visible
but lack any docstring.

Finds:
  - module_no_docstring        : module with no docstring that defines public functions/classes
  - public_function_no_docstring : top-level public function (non-trivial) with no docstring
  - public_method_no_docstring   : public method of a public class (non-trivial) with no docstring
  - public_class_no_docstring    : top-level public class with no docstring
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


def _is_test_file(filepath: Path) -> bool:
    parts = filepath.parts
    if "tests" in parts:
        return True
    name = filepath.name
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return False


def _is_trivial(node) -> bool:
    """Return True if the function/method body has fewer than 3 statements."""
    return len(node.body) < 3


def detect(tree, filename, lines, ignore):
    issues = []

    def add(line, st, desc, sug, sev):
        if st in ignore:
            return
        issues.append(CodeSmell(filename, line, st, desc, sug, sev, _get_line(lines, line)))

    # Check module-level docstring
    has_module_docstring = ast.get_docstring(tree) is not None

    # Collect top-level public functions and classes to decide on module_no_docstring
    top_level_public = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                top_level_public.append(node)
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                top_level_public.append(node)

    if not has_module_docstring and top_level_public:
        add(1, "module_no_docstring",
            f"Module '{filename}' defines public functions/classes but has no module docstring",
            "Add a module-level docstring explaining what this module does and why it exists (its intent, not just a restatement of its name).",
            "low")

    # Check top-level public functions
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            if _is_trivial(node):
                continue
            if ast.get_docstring(node) is None:
                add(node.lineno, "public_function_no_docstring",
                    f"Public function '{node.name}' has no docstring",
                    f"Add a one-line docstring to '{node.name}' stating what it does and why it exists (intent, not a restatement of the name).",
                    "low")

    # Check top-level public classes and their public methods
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name.startswith("_"):
            continue

        # Check class docstring
        if ast.get_docstring(node) is None:
            add(node.lineno, "public_class_no_docstring",
                f"Public class '{node.name}' has no docstring",
                f"Add a one-line docstring to '{node.name}' stating what it represents and why it exists (intent, not a restatement of the name).",
                "low")

        # Check public methods directly in the class body (not nested)
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name.startswith("_"):
                continue
            if _is_trivial(item):
                continue
            if ast.get_docstring(item) is None:
                add(item.lineno, "public_method_no_docstring",
                    f"Public method '{node.name}.{item.name}' has no docstring",
                    f"Add a one-line docstring to '{item.name}' stating what it does and why it exists (intent, not a restatement of the name).",
                    "low")

    return issues


def analyze_file(filepath: Path, ignore: set) -> list:
    if _is_test_file(filepath):
        return []
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
    parser = argparse.ArgumentParser(description="Detect missing docstrings on public API surface in Python")
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
            print("✅ No missing-docstring issues found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} missing-docstring issue(s):\n\nSummary:")
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
