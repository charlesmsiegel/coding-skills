#!/usr/bin/env python3
"""
Find comments that cost more than they give.

Three kinds. Commented-out code, which git already remembers and which nobody
dares delete because nobody knows if it still matters. TODO/FIXME debt, which is
worth an inventory rather than a lecture — a tracker entry gets scheduled and a
comment does not. And doc comments that restate the signature, which is the
worst case: it looks documented, so nobody writes the documentation.
"""

import re

from common import Reporter, is_test_file, run_file_detector
from rsparse import RsFile, is_doc_comment

_MARKERS = re.compile(r"(?i)\b(todo|fixme|hack|xxx|wip|kludge|refactor me|remove this)\b")

# Text that looks like Rust rather than prose.
_CODE_SHAPES = (
    re.compile(r"^\s*(let|fn|pub|impl|use|match|if|for|while|return|struct|enum)\b.*[;{]"),
    re.compile(r"^\s*\w+\s*\([^)]*\)\s*[;?]"),
    re.compile(r"^\s*[\w:]+\s*=\s*.+;"),
    re.compile(r"^\s*[}{]\s*$"),
    re.compile(r"^\s*\.\w+\([^)]*\)"),
)

# A doc comment that only rearranges the name into a sentence.
_RESTATEMENT = re.compile(r"^(returns?|gets?|sets?|creates?|constructs?|the)\b", re.IGNORECASE)


def _looks_like_code(text: str) -> bool:
    body = text.lstrip("/!* \t")
    if len(body) < 4 or body.startswith(("http", "e.g", "i.e")):
        return False
    return any(pattern.match(body) for pattern in _CODE_SHAPES)


def _check_commented_code(file: RsFile, report: Reporter) -> None:
    run: list = []
    for comment in file.comments:
        if is_doc_comment(comment):
            continue
        lines = comment.value.splitlines()
        if _looks_like_code(lines[0]) or (len(lines) > 1 and any(_looks_like_code(x) for x in lines)):
            if run and comment.line == run[-1] + 1:
                run.append(comment.line)
            else:
                if len(run) >= 2:
                    _report_run(file, report, run)
                run = [comment.line]
        else:
            if len(run) >= 2:
                _report_run(file, report, run)
            run = []
    if len(run) >= 2:
        _report_run(file, report, run)


def _report_run(file: RsFile, report: Reporter, lines: list) -> None:
    report.add(lines[0], "commented_out_code",
               f"{len(lines)} consecutive lines of commented-out code",
               "Delete it. Git has every version this ever had, and a commented block is the one "
               "kind of code nobody dares remove because nobody knows whether it still matters.",
               "low", related=lines[1:])


def _check_debt_markers(file: RsFile, report: Reporter) -> None:
    hits = []
    for comment in file.comments:
        match = _MARKERS.search(comment.value)
        if not match:
            continue
        text = " ".join(comment.value.strip("/!* \t").split())[:100]
        hits.append((comment.line, match.group(1).upper(), text))
    if not hits:
        return
    if len(hits) >= 8:
        report.add(hits[0][0], "todo_debt_inventory",
                   f"{len(hits)} TODO/FIXME markers in this file",
                   "Move them into the tracker with an owner, or delete the ones that no longer "
                   "describe anything. A comment cannot be scheduled, assigned or closed, so a "
                   "file with this many has a backlog nobody can see.", "low",
                   related=[line for line, _, _ in hits[1:]])
        return
    for line, marker, text in hits:
        severity = "medium" if marker in ("FIXME", "XXX", "HACK") else "low"
        report.add(line, "debt_marker", f"{marker}: {text}",
                   "Move it into the tracker, or fix it. If it stays, name what has to be true "
                   "before it can be resolved.", severity)


def _check_doc_restatement(file: RsFile, report: Reporter) -> None:
    docs_by_line = {c.line: c for c in file.comments if is_doc_comment(c)}
    for func in file.functions:
        if func.doc_lines != 1 or func.trait_name is not None:
            continue
        comment = docs_by_line.get(func.line - 1)
        if comment is None:
            continue
        text = comment.value.lstrip("/ ").strip().rstrip(".")
        if not _RESTATEMENT.match(text):
            continue
        words = re.findall(r"[a-z]+", text.lower())
        name_words = set(re.findall(r"[a-z]+", func.name.lower()))
        # A one-word name ("new", "run") matches almost any sentence containing
        # it, so the overlap test says nothing until the name has some shape.
        if len(name_words) < 2:
            continue
        overlap = len(name_words & set(words)) / len(name_words)
        if overlap >= 0.75 and len(words) <= 8:
            report.add(comment.line, "doc_restates_signature",
                       f"`/// {text[:60]}` says what `fn {func.name}` already says",
                       "Document what the signature cannot: the invariant, the failure modes, the "
                       "units, or an example (which `cargo test` will then run). A comment that "
                       "restates the name is a comment that will not be updated when the "
                       "behaviour changes.", "low")


def _check_missing_error_docs(file: RsFile, report: Reporter) -> None:
    """A documented public fallible function should say when it fails."""
    for func in file.functions:
        if not func.is_public or not func.doc_lines or func.trait_name is not None:
            continue
        if not func.return_type.replace(" ", "").startswith("Result<"):
            continue
        docs = "\n".join(c.value for c in file.comments_on_lines(max(1, func.line - func.doc_lines - 2),
                                                                 func.line)
                         if is_doc_comment(c))
        if "# Errors" in docs:
            continue
        report.add(func.line, "documented_result_without_errors_section",
                   f"`{func.qualname}` returns `Result` and its docs have no `# Errors` section",
                   "Say which failures a caller can expect and what they mean — that is the part "
                   "of a fallible signature the type does not carry. "
                   "`clippy::missing_errors_doc` enforces it.", "low")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_commented_code(file, report)
    _check_debt_markers(file, report)
    if not is_test_file(file.path):
        _check_doc_restatement(file, report)
        _check_missing_error_docs(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find comments that cost more than they give",
        "No comment smells found!",
        analyze,
    )
