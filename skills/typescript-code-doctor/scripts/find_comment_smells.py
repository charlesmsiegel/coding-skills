#!/usr/bin/env python3
"""
Find commented-out code, the TODO backlog, and JSDoc that restates the types.

Commented-out code is the clearest of the three: git already remembers it, and
while it sits there every reader has to work out whether it matters. JSDoc
`@param {string}` in a TypeScript file is worse than redundant — it is a second
copy of the type that nothing checks and nothing updates.
"""

import re

from common import Reporter, run_file_detector
from tsparse import TsFile

# A comment line that is probably code rather than prose.
_CODE_SHAPES = re.compile(
    r"^\s*(?:const|let|var|function|class|interface|type|enum|import|export|return|if|for|while|"
    r"switch|case|try|catch|throw|await|async|new|delete|yield)\b"
    r"|^\s*[\w.$\[\]]+\s*(?:=|\+=|-=)\s*[^=]"
    r"|^\s*[\w.$]+\([^)]*\)\s*;?\s*$"
    r"|[;{}]\s*$"
    r"|=>\s*[{(]?\s*$"
)
# Prose that happens to end in a brace or look like an assignment. Markdown
# list items and back-quoted identifiers are the two shapes that fooled this
# most often: technical prose is full of `foo()` and trailing semicolons.
_PROSE_SHAPES = re.compile(
    r"^\s*(?:[A-Z][a-z]+\s+){2,}|^\s*(?:e\.g\.|i\.e\.|NOTE|Note:|see |See )|^\s*[-*+]\s|`"
)

_MARKER = re.compile(r"\b(TODO|FIXME|HACK|XXX|WORKAROUND|KLUDGE)\b[:\s]*(.{0,90})", re.IGNORECASE)
_TYPED_JSDOC = re.compile(r"@(?:param|returns?|type|prop(?:erty)?)\s*\{")
_ESLINT = re.compile(r"eslint-|@ts-|prettier-ignore|istanbul ignore|c8 ignore|@jsx|<reference")


def _comment_lines(comment) -> list[tuple[int, str]]:
    """(line, text) for each line of a comment, with the markers stripped."""
    raw = comment.value
    if raw.startswith("//"):
        return [(comment.line, raw[2:])]
    body = raw[2:-2] if raw.endswith("*/") else raw[2:]
    return [(comment.line + offset, line.lstrip().lstrip("*"))
            for offset, line in enumerate(body.splitlines())]


def _check_commented_code(file: TsFile, report: Reporter) -> None:
    runs: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    previous_line = -10
    for comment in file.comments:
        if comment.value.startswith("/**") or _ESLINT.search(comment.value):
            continue
        for line, text in _comment_lines(comment):
            stripped = text.strip()
            looks_like_code = bool(_CODE_SHAPES.search(text)) and not _PROSE_SHAPES.search(text)
            if looks_like_code and len(stripped) > 3:
                if line == previous_line + 1 and current:
                    current.append((line, stripped))
                else:
                    if current:
                        runs.append(current)
                    current = [(line, stripped)]
                previous_line = line
            elif current and line == previous_line + 1:
                previous_line = line  # a blank/prose line inside a commented block
    if current:
        runs.append(current)

    for run in runs:
        # A single commented line is often an example or a disabled option; a
        # run of them is a deleted block someone could not let go of.
        if len(run) < 2:
            continue
        report.add(run[0][0], "commented_out_code",
                   f"{len(run)} consecutive lines of commented-out code",
                   "Delete it. Git has the history, and dead code in a comment is never updated "
                   "with the code around it — it only becomes more misleading.", "medium",
                   related=[line for line, _ in run[1:6]])


def _check_markers(file: TsFile, report: Reporter) -> None:
    for comment in file.comments:
        for line, text in _comment_lines(comment):
            match = _MARKER.search(text)
            if not match:
                continue
            marker, note = match.group(1).upper(), match.group(2).strip()
            severity = "medium" if marker in ("FIXME", "HACK", "XXX", "KLUDGE") else "low"
            report.add(line, "todo_marker",
                       f"{marker}: {note or '(no explanation)'}",
                       "Move it to the tracker with an owner, or do it. A marker with no ticket is "
                       "a decision deferred indefinitely by someone who has since left the file.",
                       severity)


def _check_typed_jsdoc(file: TsFile, report: Reporter) -> None:
    for comment in file.comments:
        if not comment.value.startswith("/**") or not _TYPED_JSDOC.search(comment.value):
            continue
        report.add(comment.line, "jsdoc_repeats_types",
                   "JSDoc carries `{type}` annotations in a TypeScript file",
                   "Drop the braces and keep the prose: `@param items - the rows to render`. The "
                   "signature is the checked source of truth; a second copy in a comment drifts and "
                   "then lies.", "low")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_commented_code(file, report)
    _check_markers(file, report)
    _check_typed_jsdoc(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find commented-out code, TODO markers and JSDoc that restates types",
        "No comment smells found!",
        analyze,
    )
