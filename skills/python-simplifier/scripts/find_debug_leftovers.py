#!/usr/bin/env python3
"""
Detect debugging code left in source files.

Finds:
  - pdb_trace       : calls to pdb/ipdb/pudb set_trace(), IPython.embed(), or bare
                      set_trace(); also stray `import pdb/ipdb/pudb` statements
  - breakpoint_call : calls to the builtin breakpoint()
  - debug_print     : print() calls in files that are NOT CLI/script entrypoints
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


def _is_cli_file(tree, filepath: Path) -> bool:
    """Return True if the file looks like a legitimate CLI/script entrypoint."""
    # Check path segments for script/cli/bin/tools directories
    cli_dirs = {"scripts", "bin", "cli", "tools"}
    if cli_dirs & set(filepath.parts):
        return True
    # Check for `if __name__ == "__main__":` or argparse usage in the AST
    for node in ast.walk(tree):
        # if __name__ == "__main__":
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
                left = test.left
                comparators = test.comparators
                if (
                    isinstance(left, ast.Name) and left.id == "__name__"
                    and len(comparators) == 1
                    and isinstance(comparators[0], ast.Constant)
                    and comparators[0].value == "__main__"
                ):
                    return True
        # argparse usage: import argparse  or  argparse.ArgumentParser(...)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "argparse":
                    return True
        if isinstance(node, ast.ImportFrom):
            if node.module == "argparse":
                return True
    return False


def detect(tree, filename, lines, ignore):
    issues = []

    def add(line, st, desc, sug, sev):
        if st in ignore:
            return
        issues.append(CodeSmell(filename, line, st, desc, sug, sev, _get_line(lines, line)))

    filepath = Path(filename)
    is_cli = _is_cli_file(tree, filepath)

    for node in ast.walk(tree):
        # --- pdb_trace: stray debugger imports ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("pdb", "ipdb", "pudb"):
                    add(
                        node.lineno,
                        "pdb_trace",
                        f"Stray `import {alias.name}` left in source",
                        f"Remove `import {alias.name}` before committing.",
                        "high",
                    )

        # --- pdb_trace / breakpoint_call: call expressions ---
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            func = call.func

            # bare set_trace()
            if isinstance(func, ast.Name) and func.id == "set_trace":
                add(
                    node.lineno,
                    "pdb_trace",
                    "Bare set_trace() call left in source",
                    "Remove set_trace() before committing.",
                    "high",
                )

            # pdb.set_trace() / ipdb.set_trace() / pudb.set_trace()
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "set_trace"
                and isinstance(func.value, ast.Name)
                and func.value.id in ("pdb", "ipdb", "pudb")
            ):
                add(
                    node.lineno,
                    "pdb_trace",
                    f"{func.value.id}.set_trace() call left in source",
                    f"Remove {func.value.id}.set_trace() before committing.",
                    "high",
                )

            # IPython.embed()
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "embed"
                and isinstance(func.value, ast.Name)
                and func.value.id == "IPython"
            ):
                add(
                    node.lineno,
                    "pdb_trace",
                    "IPython.embed() call left in source",
                    "Remove IPython.embed() before committing.",
                    "high",
                )

            # bare embed() — conservative: only flag if IPython was imported
            elif isinstance(func, ast.Name) and func.id == "embed":
                # Check if IPython is imported anywhere in the module
                ipython_imported = False
                for n in ast.walk(tree):
                    if isinstance(n, ast.Import):
                        for alias in n.names:
                            if alias.name == "IPython" or alias.name.startswith("IPython."):
                                ipython_imported = True
                    elif isinstance(n, ast.ImportFrom):
                        if n.module and (n.module == "IPython" or n.module.startswith("IPython.")):
                            ipython_imported = True
                if ipython_imported:
                    add(
                        node.lineno,
                        "pdb_trace",
                        "embed() call (IPython shell) left in source",
                        "Remove embed() before committing.",
                        "high",
                    )

            # breakpoint()
            elif isinstance(func, ast.Name) and func.id == "breakpoint":
                add(
                    node.lineno,
                    "breakpoint_call",
                    "breakpoint() call left in source",
                    "Remove breakpoint() before committing.",
                    "high",
                )

            # print() — only in non-CLI files
            elif not is_cli and isinstance(func, ast.Name) and func.id == "print":
                add(
                    node.lineno,
                    "debug_print",
                    "print() call in non-entrypoint module",
                    "Replace stray print() with logging or remove it.",
                    "low",
                )

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
    parser = argparse.ArgumentParser(description="Detect debugging leftovers in Python")
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
            print("✅ No debugging leftovers found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} debugging leftover(s):\n\nSummary:")
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
