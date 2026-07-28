#!/usr/bin/env python3
"""
Detect comment smells via tokenisation + AST validation.

Finds:
  - commented_out_code : a comment whose contents parse as real Python code
                         (dead code left behind; delete it, git remembers)
  - todo_comment       : TODO / FIXME / HACK / XXX / BUG markers (an inventory of
                         deferred work to triage)

(Overlaps Ruff's eradicate (ERA) and flake8-todos (TD); included so the suite is
self-contained.)
"""

import ast
import io
import json
import tokenize
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


_PRAGMA_PREFIXES = ("!", "type:", "noqa", "pragma", "pylint:", "mypy:",
                    "isort:", "fmt:", "nopep8", "coding:", "-*-")
_TODO_MARKERS = ("TODO", "FIXME", "HACK", "XXX", "BUG", "OPTIMIZE", "REFACTOR", "DEPRECATED")

# A comment is treated as code only if its first parsed node is one of these...
_CODE_NODES = (
    ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Import, ast.ImportFrom,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.For, ast.AsyncFor,
    ast.While, ast.If, ast.With, ast.AsyncWith, ast.Return, ast.Raise, ast.Try,
    ast.Assert, ast.Delete, ast.Global, ast.Nonlocal, ast.Break, ast.Continue, ast.Pass,
)
# ...structural keywords whose presence alone is convincing
_STRUCTURAL = (
    ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
    ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
)


def _looks_like_code(text: str) -> bool:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    if not tree.body:
        return False
    first = tree.body[0]
    # Structural statements are convincing on their own.
    if isinstance(first, _STRUCTURAL):
        return True
    # Calls / subscripts etc. need a code-ish character to avoid matching prose
    # ("return early" parses as a Return of a Name -- not real code).
    has_code_char = any(c in text for c in "=(){}[].")
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Call):
        return has_code_char
    if isinstance(first, _CODE_NODES):
        return has_code_char
    return False


def detect(source: str, filename: str, ignore: set):
    issues = []
    lines = source.splitlines()

    def add(line, st, desc, sug, sev):
        if st in ignore:
            return
        snippet = lines[line - 1].strip()[:80] if 0 < line <= len(lines) else ""
        issues.append(CodeSmell(filename, line, st, desc, sug, sev, snippet))

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, Exception):
        return issues

    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        line = tok.start[0]
        text = tok.string.lstrip("#").strip()
        if not text:
            continue
        if any(text.lower().startswith(p) for p in _PRAGMA_PREFIXES):
            continue

        upper = text.upper()
        marker = next((m for m in _TODO_MARKERS if m in upper), None)
        if marker:
            add(line, "todo_comment",
                f"{marker} marker: deferred work that should be tracked",
                "Convert to a ticket (or resolve it). A backlog living in comments is invisible to planning.",
                "low")
            continue

        if _looks_like_code(text):
            add(line, "commented_out_code",
                "Commented-out code left in the source",
                "Delete it -- version control already preserves the history. Dead comments rot and mislead.",
                "medium")

    issues.sort(key=lambda x: (x.severity != "high", x.severity != "medium", x.line))
    return issues


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        return detect(source, str(filepath), ignore)
    except Exception:
        return []


def find_python_files(path: Path) -> Iterator[Path]:
    if path.is_file() and path.suffix == ".py":
        yield path
    elif path.is_dir():
        for p in path.rglob("*.py"):
            if ".venv" not in p.parts and "node_modules" not in p.parts and "__pycache__" not in p.parts:
                yield p


def main():
    parser = argparse.ArgumentParser(description="Detect comment smells")
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
            print("✅ No comment smells found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} comment smell(s):\n\nSummary:")
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
