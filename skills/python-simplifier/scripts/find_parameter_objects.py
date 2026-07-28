#!/usr/bin/env python3
"""
Detect data clumps in Python code via cross-function AST analysis.

A "data clump" is a group of parameters that keep travelling together across
many function signatures. When the same three (or more) names recur, they almost
always belong in a single object (a dataclass / value object), which shortens
signatures and centralises validation.

This analysis is whole-codebase: it collects parameter sets from every function
under the given path, then reports name-groups that recur in >= 3 functions.

Finds:
  - data_clump : a group of parameters appearing together in multiple functions
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Iterator
from itertools import combinations
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


MIN_GROUP = 3        # smallest clump size to consider
MIN_FUNCS = 3        # how many functions must share the group
MAX_PARAMS = 8       # skip very long signatures (already flagged elsewhere)
_SKIP = {"self", "cls"}


def _param_names(args: ast.arguments):
    names = []
    for a in list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs):
        if a.arg not in _SKIP:
            names.append(a.arg)
    return names


def _collect(path: Path):
    """Return list of (frozenset_of_params, file, line, funcname)."""
    out = []
    for filepath in find_python_files(path):
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(filepath))
        except (SyntaxError, Exception):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = _param_names(node.args)
                if MIN_GROUP <= len(names) <= MAX_PARAMS:
                    out.append((frozenset(names), str(filepath), node.lineno, node.name))
    return out


def detect(path: Path, ignore: set):
    if "data_clump" in ignore:
        return []
    functions = _collect(path)

    # Count every MIN_GROUP-sized subset of params, tracking where it occurs.
    triple_locs = defaultdict(list)   # frozenset(3 names) -> [(file, line, name)]
    for names, f, ln, fn in functions:
        for combo in combinations(sorted(names), MIN_GROUP):
            triple_locs[frozenset(combo)].append((f, ln, fn))

    qualifying = {t: locs for t, locs in triple_locs.items() if len(locs) >= MIN_FUNCS}

    # Merge triples that share the exact same support into one (maximal) clump,
    # so a recurring 4-name group is reported once rather than as 4 triples.
    by_support = defaultdict(lambda: {"names": set(), "locs": None})
    for t, locs in qualifying.items():
        key = frozenset(locs)
        by_support[key]["names"].update(t)
        by_support[key]["locs"] = locs

    issues = []
    for key, data in by_support.items():
        names = sorted(data["names"])
        locs = data["locs"]
        f0, ln0, _ = sorted(locs)[0]
        where = ", ".join(f"{Path(f).name}:{ln} {fn}()" for f, ln, fn in sorted(locs)[:5])
        more = "" if len(locs) <= 5 else f" (+{len(locs) - 5} more)"
        issues.append(CodeSmell(
            file=f0, line=ln0, smell_type="data_clump",
            description=f"Parameters {names} appear together in {len(locs)} functions: {where}{more}",
            suggestion="Group them into a dataclass (e.g. a single typed object) and pass that instead of the loose parameters.",
            severity="medium", code_snippet="",
        ))
    issues.sort(key=lambda x: (x.file, x.line))
    return issues


def find_python_files(path: Path) -> Iterator[Path]:
    if path.is_file() and path.suffix == ".py":
        yield path
    elif path.is_dir():
        for p in path.rglob("*.py"):
            if ".venv" not in p.parts and "node_modules" not in p.parts and "__pycache__" not in p.parts:
                yield p


def main():
    parser = argparse.ArgumentParser(description="Detect data clumps (recurring parameter groups)")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    all_issues = detect(Path(args.path), ignore)

    if args.format == "json":
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No data clumps found!")
            return
        print(f"Found {len(all_issues)} data clump(s):\n")
        for i in all_issues:
            print(f"🟡 [{i.severity.upper()}] {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            print(f"   → {i.suggestion}\n")


if __name__ == "__main__":
    main()
