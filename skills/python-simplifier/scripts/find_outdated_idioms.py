#!/usr/bin/env python3
"""
Detect outdated Python idioms that should be modernized.

Finds constructs that modern Python (3.9+/3.10+) replaces with cleaner
built-in syntax: old-style string formatting, legacy typing aliases,
super() with explicit arguments, and os.path usage that pathlib replaces.

Finds:
  - percent_format      : "..." % x  — use an f-string
  - str_format_call     : "...".format(...)  — use an f-string
  - old_typing_alias    : typing.List/Dict/Optional/Union etc. — use builtins
  - super_with_args     : super(Cls, self) — use bare super()
  - os_path_join        : os.path.join/exists/isfile/isdir/dirname/basename
                          — use pathlib.Path
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


# typing names whose subscripted forms should be replaced with builtins (3.9+)
_TYPING_BUILTIN_ALIASES = {"List", "Dict", "Set", "FrozenSet", "Tuple", "Type"}
# typing names whose union forms should be replaced (3.10+)
_TYPING_UNION_ALIASES = {"Optional", "Union"}
_ALL_TYPING_ALIASES = _TYPING_BUILTIN_ALIASES | _TYPING_UNION_ALIASES

_OS_PATH_FUNCS = {"join", "exists", "isfile", "isdir", "dirname", "basename"}


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


def _collect_typing_imports(tree):
    """Return the set of names imported from typing (and whether 'typing' is imported)."""
    imported_from_typing = set()
    typing_module_imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "typing":
                for alias in node.names:
                    imported_from_typing.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "typing":
                    typing_module_imported = True
    return imported_from_typing, typing_module_imported


def detect(tree, filename, lines, ignore):
    issues = []

    def add(line, st, desc, sug, sev):
        if st in ignore:
            return
        issues.append(CodeSmell(filename, line, st, desc, sug, sev, _get_line(lines, line)))

    typing_imports, typing_module_imported = _collect_typing_imports(tree)

    for node in ast.walk(tree):

        # percent_format: <str constant> % <anything>
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                add(node.lineno, "percent_format",
                    "Old-style '%' string formatting",
                    "Replace with an f-string for clarity and performance.",
                    "low")

        # str_format_call: "literal".format(...)
        elif isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and func.attr == "format"
                    and isinstance(func.value, ast.Constant)
                    and isinstance(func.value.value, str)):
                add(node.lineno, "str_format_call",
                    "Old-style '.format()' string formatting on a string literal",
                    "Replace with an f-string for clarity and performance.",
                    "low")

            # super_with_args: super(ClassName, self)
            elif (isinstance(func, ast.Name)
                  and func.id == "super"
                  and len(node.args) >= 1):
                add(node.lineno, "super_with_args",
                    "super() called with explicit class and instance arguments",
                    "Use bare super() — Python 3 resolves the class automatically.",
                    "low")

            # os.path.join / exists / isfile / isdir / dirname / basename
            elif (isinstance(func, ast.Attribute)
                  and func.attr in _OS_PATH_FUNCS
                  and isinstance(func.value, ast.Attribute)
                  and func.value.attr == "path"
                  and isinstance(func.value.value, ast.Name)
                  and func.value.value.id == "os"):
                add(node.lineno, "os_path_join",
                    f"os.path.{func.attr}() is verbose for path operations",
                    "Use pathlib.Path — it offers an object-oriented, cross-platform API "
                    f"(e.g., replace os.path.{func.attr} with Path / operator or .exists()/.is_file()/.name etc.).",
                    "low")

        # old_typing_alias: Name in typing aliases that was imported from typing,
        # appearing in a subscript (annotation context) or as a standalone annotation.
        elif isinstance(node, ast.Subscript):
            value = node.value
            # typing.List[...] — attribute form
            if (typing_module_imported
                    and isinstance(value, ast.Attribute)
                    and value.attr in _ALL_TYPING_ALIASES
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "typing"):
                alias = value.attr
                if alias in _TYPING_BUILTIN_ALIASES:
                    suggestion = (f"Use the builtin '{alias.lower()}' directly (Python 3.9+), "
                                  "e.g., list[int] instead of List[int].")
                else:
                    suggestion = ("Use '|' union syntax (Python 3.10+), "
                                  "e.g., 'int | None' instead of Optional[int].")
                add(node.lineno, "old_typing_alias",
                    f"Deprecated typing alias 'typing.{alias}' used as a generic annotation",
                    suggestion,
                    "low")
            # from-typing import form: bare Name
            elif (isinstance(value, ast.Name)
                  and value.id in _ALL_TYPING_ALIASES
                  and value.id in typing_imports):
                alias = value.id
                if alias in _TYPING_BUILTIN_ALIASES:
                    suggestion = (f"Use the builtin '{alias.lower()}' directly (Python 3.9+), "
                                  "e.g., list[int] instead of List[int].")
                else:
                    suggestion = ("Use '|' union syntax (Python 3.10+), "
                                  "e.g., 'int | None' instead of Optional[int].")
                add(node.lineno, "old_typing_alias",
                    f"Deprecated typing alias '{alias}' (imported from typing) used as a generic annotation",
                    suggestion,
                    "low")

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
    parser = argparse.ArgumentParser(description="Detect outdated Python idioms")
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
            print("✅ No outdated idioms found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} outdated idiom(s):\n\nSummary:")
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
