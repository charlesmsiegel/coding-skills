#!/usr/bin/env python3
"""
Detect comment smells via tokenisation + AST validation.

Finds:
  - commented_out_code : a comment on a line of its own whose contents parse as
                         real Python code (dead code left behind; delete it, git
                         remembers). A trailing comment annotates the code it
                         follows — `update(cid, body="x")  # diverge (materializes)`
                         — and was never a line somebody disabled. A lone
                         commented import is a candidate: a comment quoting the
                         import syntax nearby code matches reads the same way.
  - todo_comment       : TODO / FIXME / HACK / XXX / BUG markers (an inventory of
                         deferred work to triage)

(Overlaps Ruff's eradicate (ERA) and flake8-todos (TD); included so the suite is
self-contained.)
"""

import ast
import io
import json
import re
import tokenize
import warnings
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import (sort_findings, cached_source, SEVERITY_ICONS, configure_output, find_python_files,
                    warn_detector_error, warn_unparseable)


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


_LONE_IMPORT_REASONS = (
    "the comment quotes import syntax to document what nearby code matches or "
    "expects (an AST guard, an import rewriter), rather than disabling an import",
)


_PRAGMA_PREFIXES = ("!", "type:", "noqa", "pragma", "pylint:", "mypy:",
                    "isort:", "fmt:", "nopep8", "coding:", "-*-")
_TODO_MARKERS = ("TODO", "FIXME", "HACK", "XXX", "BUG", "OPTIMIZE", "REFACTOR", "DEPRECATED")
# A marker is a *tag*, not a word: it opens the comment (`# TODO rewrite`,
# `# -- HACK --`, `# todo: later`) or is tagged anywhere with `:`/`(`
# (`# see TODO: budget`, `# FIXME(alice)`). Inside prose the same letters are
# vocabulary — `evals todo` names a command, "the bug this fixes" explains one,
# `\uXXXX` is escape notation — and a substring search scores every one.
_MARKER_WORDS = "|".join(_TODO_MARKERS)
_MARKER_OPENS_COMMENT = re.compile(rf"^\W*({_MARKER_WORDS})\b(?P<tag>\s*[:(\-])?", re.IGNORECASE)
_MARKER_TAGGED = re.compile(rf"\b({_MARKER_WORDS})\b(?=\s*[:(])")


def task_marker(text: str) -> str | None:
    """The marker this comment carries, or None when it merely uses the word."""
    opening = _MARKER_OPENS_COMMENT.match(text)
    if opening and (opening.group(1).isupper() or opening.group("tag")):
        return opening.group(1).upper()
    tagged = _MARKER_TAGGED.search(text)
    return tagged.group(1) if tagged else None

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


def _code_statement(text: str) -> ast.stmt | None:
    """The first statement ``text`` parses to, when that reads as code."""
    # The text is a comment, usually prose, so CPython's complaints about it
    # ("500ing" is a numeric literal glued to `in`; "\d" an invalid escape) are
    # about nothing in the user's code. Left unfiltered they reach stderr as
    # `<unknown>:1: SyntaxWarning` and land in the middle of a report.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            warnings.simplefilter("ignore", DeprecationWarning)
            tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    if not tree.body:
        return None
    first = tree.body[0]
    # Structural statements are convincing on their own.
    if isinstance(first, _STRUCTURAL):
        return first
    # Calls / subscripts etc. need a code-ish character to avoid matching prose
    # ("return early" parses as a Return of a Name -- not real code).
    has_code_char = any(c in text for c in "=(){}[].")
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Call):
        return first if has_code_char else None
    if isinstance(first, _CODE_NODES):
        return first if has_code_char else None
    return None


def _looks_like_code(text: str) -> bool:
    return _code_statement(text) is not None


def detect(source: str, filename: str, ignore: set):
    issues = []
    lines = source.splitlines()

    def add(line, st, desc, sug, sev, reasons=()):
        if st in ignore:
            return
        snippet = lines[line - 1].strip()[:80] if 0 < line <= len(lines) else ""
        if reasons:
            issues.append(CodeSmell(filename, line, st, desc, "", "low", snippet,
                                    kind="candidate", also_caused_by=tuple(reasons)))
        else:
            issues.append(CodeSmell(filename, line, st, desc, sug, sev, snippet))

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, Exception):
        return issues

    code_lines: dict[int, ast.stmt] = {}
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        line = tok.start[0]
        text = tok.string.lstrip("#").strip()
        if not text:
            continue
        if any(text.lower().startswith(p) for p in _PRAGMA_PREFIXES):
            continue

        marker = task_marker(text)
        if marker:
            add(line, "todo_comment",
                f"{marker} marker: deferred work that should be tracked",
                "Convert to a ticket (or resolve it). A backlog living in comments is invisible to planning.",
                "low")
            continue

        # Disabled code is a line of its own; a comment after code on the same
        # line annotates that code, however much it looks like code itself.
        standalone = 0 < line <= len(lines) and not lines[line - 1][:tok.start[1]].strip()
        statement = _code_statement(text) if standalone else None
        if statement is not None:
            code_lines[line] = statement

    for line, statement in code_lines.items():
        in_run = (line - 1) in code_lines or (line + 1) in code_lines
        lone_import = isinstance(statement, (ast.Import, ast.ImportFrom)) and not in_run
        add(line, "commented_out_code",
            "Commented-out code left in the source",
            "Delete it -- version control already preserves the history. Dead comments rot and mislead.",
            "medium", reasons=_LONE_IMPORT_REASONS if lone_import else ())

    issues.sort(key=lambda x: (x.severity != "high", x.severity != "medium", x.line))
    return issues


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source = cached_source(filepath)
        return detect(source, str(filepath), ignore)
    except (SyntaxError, ValueError, OSError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect comment smells")
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
        icons = SEVERITY_ICONS
        for i in all_issues:
            if i.kind == "candidate":
                print(f"? [CANDIDATE] {i.file}:{i.line}")
            else:
                print(f"{icons[i.severity]} [{i.severity.upper()}] {i.file}:{i.line}")
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
