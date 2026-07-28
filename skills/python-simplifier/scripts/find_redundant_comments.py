#!/usr/bin/env python3
"""
Detect narration comments that merely restate the code.

The most reliable tell of AI-generated code is comments that describe *what*
the code does rather than *why*. This detector pairs each comment with its
associated code line and flags it when the comment is a low-information
restatement of the code.

This detector is intentionally noisy so it defaults to LOW severity. It skips
aggressively: directives, URLs, long explanatory comments, and any comment that
carries real information (because/why/note/hack/workaround/warning/todo/fixme).

Finds:
  - redundant_comment : a # comment that restates the code on the same or next line
"""

import ast
import re
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


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


# ---------------------------------------------------------------------------
# Text processing helpers
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "the", "a", "an", "to", "of", "for", "this", "that", "is", "are",
    "it", "we", "then", "and", "in", "on", "with", "current", "new",
    "value", "val",
}

_SNAKE_SPLIT_RE = re.compile(r"[_\W]+")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _split_words(text: str) -> list:
    """
    Split a text into lowercase words, handling snake_case and camelCase,
    removing non-alphanumeric characters.
    """
    # camelCase split first
    text = _CAMEL_SPLIT_RE.sub(" ", text)
    # then split on non-alphanumeric
    parts = _SNAKE_SPLIT_RE.split(text)
    return [p.lower() for p in parts if p]


def _significant_words(text: str) -> list:
    """Words after splitting and removing stopwords."""
    return [w for w in _split_words(text) if w and w not in _STOPWORDS]


# ---------------------------------------------------------------------------
# Narration-verb detection
# ---------------------------------------------------------------------------

# Each entry: (verb_phrase, word_that_must_appear_in_code)
# The verb_phrase is matched at the START of the comment (after stripping).
# We check that at least one non-stopword from the comment's "object" words
# appears in the code's word set.

_NARRATION_VERBS = [
    "increment",
    "decrement",
    "loop over",
    "iterate over",
    "iterate",
    "return",
    "returns",
    "set",
    "get",
    "gets",
    "create",
    "creates",
    "initialize",
    "initialise",
    "define",
    "import",
    "call",
    "calls",
    "check if",
    "assign",
    "add",
    "append",
    "remove",
    "delete",
    "update",
    "print",
    "instantiate",
    "declare",
    "increase",
    "decrease",
]
# Sort longest first so "loop over" matches before "loop"
_NARRATION_VERBS_SORTED = sorted(_NARRATION_VERBS, key=len, reverse=True)


def _narration_verb_match(comment_words: list, comment_lower: str, code_words: set) -> bool:
    """
    Return True if the comment starts with a narration verb AND the verb's
    object word also appears in the code line's word set.
    """
    for verb in _NARRATION_VERBS_SORTED:
        if comment_lower.startswith(verb):
            # The "object" is the rest of the comment after the verb
            rest = comment_lower[len(verb):].strip()
            obj_words = [w for w in _split_words(rest) if w and w not in _STOPWORDS]
            if obj_words and any(w in code_words for w in obj_words):
                return True
    return False


# ---------------------------------------------------------------------------
# Skip conditions
# ---------------------------------------------------------------------------

_DIRECTIVE_PREFIXES = (
    "type:", "noqa", "pragma:", "pylint:", "fmt:", "isort:",
)
_INFO_KEYWORDS = re.compile(
    r"\b(because|why|note|hack|workaround|warning|todo|fixme|see |e\.g\.)\b",
    re.IGNORECASE,
)
_MOSTLY_PUNCT_RE = re.compile(r"^[=\-#*~^+.]{3,}$")
# Matches section dividers like "---- text ----" or "==== TEXT ===="
_SECTION_HEADER_RE = re.compile(r"^[-=*#~^+.]{2,}\s+\S.*\S\s+[-=*#~^+.]{2,}$")
_CODING_RE = re.compile(r"coding[:=]", re.IGNORECASE)


def _should_skip_comment(comment_text: str) -> bool:
    """
    Return True if this comment should be excluded from analysis regardless
    of any code-similarity check.
    """
    ct = comment_text.strip()

    # shebang
    if ct.startswith("!"):
        return True

    # coding declaration
    if _CODING_RE.search(ct):
        return True

    # URL
    if "http" in ct:
        return True

    # directive annotations
    ct_lower = ct.lower()
    for prefix in _DIRECTIVE_PREFIXES:
        if ct_lower.startswith(prefix):
            return True

    # license / copyright
    if "copyright" in ct_lower or "license" in ct_lower or "licence" in ct_lower:
        return True

    # section dividers: comment is mostly punctuation, or "---- label ----" style
    if _MOSTLY_PUNCT_RE.match(ct):
        return True
    if _SECTION_HEADER_RE.match(ct):
        return True

    # carries real information
    if _INFO_KEYWORDS.search(ct):
        return True

    # longer than 10 words → likely explaining WHY
    if len(ct.split()) > 10:
        return True

    return False


# ---------------------------------------------------------------------------
# Core similarity check
# ---------------------------------------------------------------------------

def _is_redundant(comment_text: str, code_line: str) -> bool:
    """
    Return True if comment_text appears to restate code_line.

    Two criteria (either is sufficient):
    1. comment has <= 8 significant words AND >= 0.6 of them appear in code's word set
    2. comment starts with a narration verb AND that verb's object appears in code
    """
    if not code_line.strip():
        return False

    comment_sig = _significant_words(comment_text)
    code_sig = set(_significant_words(code_line))

    if not comment_sig:
        return False

    # Criterion 1: high overlap, short comment
    if len(comment_sig) <= 8:
        overlap = sum(1 for w in comment_sig if w in code_sig)
        ratio = overlap / len(comment_sig)
        if ratio >= 0.6:
            return True

    # Criterion 2: narration verb whose object appears in the code
    comment_lower = comment_text.strip().lower()
    if _narration_verb_match(comment_sig, comment_lower, code_sig):
        return True

    return False


# ---------------------------------------------------------------------------
# Main detection using tokenize
# ---------------------------------------------------------------------------

def detect(tree, filename, lines, ignore):
    issues = []

    def add(line, st, desc, sug, sev):
        if st in ignore:
            return
        issues.append(CodeSmell(filename, line, st, desc, sug, sev, _get_line(lines, line)))

    if "redundant_comment" in ignore:
        return issues

    source = "\n".join(lines)

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        return issues

    # Build a quick lookup: lineno -> stripped source line (for code pairing)
    def get_code_line(lineno):
        """Get the non-blank, non-comment source line at or after lineno."""
        idx = lineno  # lineno is 1-based; lines[lineno] is the NEXT line
        while idx < len(lines):
            raw = lines[idx].strip()
            if raw and not raw.startswith("#"):
                return raw
            idx += 1
        return ""

    for tok_type, tok_string, tok_start, tok_end, tok_line in tokens:
        if tok_type != tokenize.COMMENT:
            continue

        lineno = tok_start[0]
        col = tok_start[1]

        # The comment text without the leading #
        comment_text = tok_string[1:].strip()

        if not comment_text:
            continue

        if _should_skip_comment(comment_text):
            continue

        # Skip section-header labels: a stand-alone comment whose adjacent line
        # is a pure separator (# ---- / # ====).
        def _is_separator_line(ln):
            s = ln.strip()
            if not s.startswith("#"):
                return False
            inner = s[1:].strip()
            return bool(_MOSTLY_PUNCT_RE.match(inner))

        prev_line = lines[lineno - 2] if lineno >= 2 else ""
        next_line = lines[lineno] if lineno < len(lines) else ""
        if _is_separator_line(prev_line) or _is_separator_line(next_line):
            continue

        # Pair with code: same-line code (trailing comment) or next non-blank line
        same_line_code = tok_line[:col].strip()  # code before the comment on same line

        if same_line_code:
            code_line = same_line_code
        else:
            # next non-blank, non-comment source line
            code_line = get_code_line(lineno)  # lineno is 1-based, lines[lineno] = next

        if not code_line:
            continue

        if _is_redundant(comment_text, code_line):
            add(
                lineno,
                "redundant_comment",
                f"Comment appears to restate the code: {comment_text!r}",
                "Delete it — the code already says this; reserve comments for WHY, not WHAT.",
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
    parser = argparse.ArgumentParser(description="Detect redundant narration comments in Python")
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
            print("✅ No redundant comments found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} redundant comment(s):\n\nSummary:")
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
