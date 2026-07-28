#!/usr/bin/env python3
"""
Detect boolean-flag parameters in function *definitions* via AST analysis.

A boolean parameter usually means the function does two things; call sites read
as `do_thing(True)` with no clue what True means. This complements the existing
call-site `boolean_blindness` check in find_code_smells by catching the smell at
the definition.

Finds:
  - multiple_boolean_flags : a function with >= 2 boolean-default parameters
  - boolean_positional_param : a boolean parameter that can be passed positionally
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


_SKIP = {"self", "cls"}


def _is_bool_const(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, bool)


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


class BooleanParamDetector(ast.NodeVisitor):
    def __init__(self, filename, lines, ignore):
        self.filename = filename
        self.lines = lines
        self.ignore = ignore
        self.issues = []

    def _add(self, line, st, desc, sug, sev):
        if st in self.ignore:
            return
        self.issues.append(CodeSmell(self.filename, line, st, desc, sug, sev, _get_line(self.lines, line)))

    def _check(self, node):
        args = node.args
        positional = list(getattr(args, "posonlyargs", [])) + list(args.args)
        defaults = list(args.defaults)
        bool_positional = []   # bool-default params that can be passed positionally
        bool_total = 0

        if defaults:
            for arg, default in zip(positional[-len(defaults):], defaults):
                if _is_bool_const(default) and arg.arg not in _SKIP:
                    bool_total += 1
                    bool_positional.append(arg.arg)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            if default is not None and _is_bool_const(default) and arg.arg not in _SKIP:
                bool_total += 1   # keyword-only, so not positional

        if bool_total >= 2:
            self._add(node.lineno, "multiple_boolean_flags",
                f"{node.name} has {bool_total} boolean parameters; it likely does several things",
                "Split into separate functions, or replace the flags with an Enum / small options object.",
                "medium")
        for name in bool_positional:
            self._add(node.lineno, "boolean_positional_param",
                f"Boolean parameter '{name}' in {node.name} can be passed positionally (e.g. {node.name}(..., True))",
                "Make it keyword-only by adding '*,' before it so call sites are self-documenting, or split the function.",
                "low")

    def visit_FunctionDef(self, node):
        self._check(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._check(node)
        self.generic_visit(node)


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
        d = BooleanParamDetector(str(filepath), source.splitlines(), ignore)
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
    parser = argparse.ArgumentParser(description="Detect boolean-flag parameters")
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
            print("✅ No boolean-flag parameters found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} boolean-flag issue(s):\n\nSummary:")
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
