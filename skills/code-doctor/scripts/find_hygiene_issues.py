#!/usr/bin/env python3
"""
Repository hygiene: the things that are wrong in any language.

Merge markers, oversized files, committed secrets-by-filename, and a TODO
inventory are findings — none of them depends on knowing what language this is.
Commented-out code is a candidate, because deciding that a commented line is
dead code rather than a documentation example needs a reading brain.

Debug-print leftovers are NOT here on purpose: naming the print call of each
language is a per-language table, which this skill does not carry. The
specialists have that check, and so does the project's own linter.
"""

import re
import sys
from pathlib import Path

from common import (Reporter, ScanPathError, build_parser, configure_output,
                    coverage_gaps, emit, fail_on_bad_path, is_probably_binary,
                    tracked_paths, unmerged_paths, walk_files, walk_paths,
                    warn_detector_error, warn_unreadable)

MERGE_MARKERS = ("<<<<<<< ", "=======", ">>>>>>> ")
MAX_FILE_LINES = 1000
MAX_LINE_LENGTH = 300
MAX_COMMITTED_BYTES = 10 * 1024 * 1024

# The skill's one concession to language syntax, used only after string
# literals have been blanked. Five tokens, and no detector branches on which
# language a file is. A mis-classified line loses a candidate, never invents
# a finding.
LINE_COMMENT_PREFIXES = ("//", "#", "--", ";")
# `//` and `--` begin a comment in every language that uses them. `#` and `;`
# do not: `#` opens a C preprocessor directive and a Rust attribute, `;` is an
# instruction separator in assembly. A hit on those two supports a candidate,
# never a finding.
UNAMBIGUOUS_PREFIXES = ("//", "--")

_STRING_LITERAL = re.compile(r"""(["'`])(?:\\.|(?!\1).)*\1""")
_TODO = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
# A commented line that looks like code: ends in a statement terminator or
# opener, or contains an assignment or a call.
_LOOKS_LIKE_CODE = re.compile(r"[;{}]\s*$|\)\s*[;{]?\s*$|=[^=]|\w+\s*\(")

# Filenames whose whole purpose is to hold secrets. `.npmrc` and `.pypirc`
# are deliberately absent: both routinely hold nothing but registry, index,
# and proxy settings, so a name-only rule would call ordinary committed
# config a credential leak. find_secrets.py inspects their contents instead.
SECRET_FILENAMES = frozenset({".env", ".env.local", ".env.production"})


def blank_literals(line: str) -> str:
    """Replace string-literal contents so their punctuation stops parsing as code.

    This runs BEFORE comment stripping, which is what keeps
    `url = "https://x"; work()` from losing its trailing call to a `//` that
    was never a comment.
    """
    return _STRING_LITERAL.sub(lambda m: m.group(1) + " " * (len(m.group(0)) - 2) + m.group(1), line)


def unambiguous_comment(line: str) -> bool:
    """Whether this line's comment marker is one no language repurposes."""
    blanked = blank_literals(line)
    positions = [(blanked.find(pfx), pfx) for pfx in LINE_COMMENT_PREFIXES]
    hits = [(index, pfx) for index, pfx in positions if index != -1]
    if not hits:
        return False
    return min(hits)[1] in UNAMBIGUOUS_PREFIXES


def comment_body(line: str) -> str | None:
    """The text after a line-comment prefix, or None if there is no comment."""
    blanked = blank_literals(line)
    for prefix in LINE_COMMENT_PREFIXES:
        index = blanked.find(prefix)
        if index != -1:
            return line[index + len(prefix):].strip()
    return None


def check_text_file(path: Path, text: str, report: Reporter,
                    unmerged: set[Path] | None) -> None:
    """Checks that are valid in any text file, source or not."""
    is_conflicted = unmerged is not None and path.resolve() in unmerged

    for number, line in enumerate(text.splitlines(), 1):
        # The TODO inventory belongs here, not in the source pass: a TODO in
        # deployment.yaml or settings.toml is debt exactly like one in a .go
        # file, and the design's stated scope for it is all text.
        body = comment_body(line)
        if body is not None:
            todo = _TODO.search(body)
            if todo and unambiguous_comment(line):
                report.finding(
                    number, "todo_inventory",
                    f"{todo.group(1)}: {body[:80]}",
                    "Convert it to a tracked issue or delete it. An undated TODO in the "
                    "source is a decision nobody owns.",
                    severity="low", snippet=line.strip()[:120],
                )
            elif todo:
                # `#` and `;` are syntax, not comments, in several languages —
                # C's `#define TODO 1` is not debt someone forgot to file.
                report.candidate(
                    number, "todo_inventory",
                    f"{todo.group(1)} on a line whose comment prefix is ambiguous",
                    also_caused_by=[
                        "a C preprocessor directive or Rust attribute, where `#` is syntax",
                        "an assembly or ini line, where `;` is not a comment",
                        "a literal string that happens to contain the word",
                    ],
                    severity="low", snippet=line.strip()[:120],
                )

        if not line.startswith(MERGE_MARKERS):
            continue
        if is_conflicted:
            report.finding(
                number, "merge_conflict_marker",
                "Unresolved merge conflict — git reports this path as unmerged",
                "Resolve the conflict and delete the marker. This file does not "
                "parse, build, or run in its current state.",
                severity="high", snippet=line[:120],
            )
        else:
            report.candidate(
                number, "merge_conflict_marker",
                "Merge conflict marker text, in a path git does not report as unmerged",
                also_caused_by=[
                    "a test fixture that exists to exercise conflict handling",
                    "a documentation example showing what a conflict looks like",
                    "a stored snapshot or golden file containing marker text",
                    "git was unavailable, so unmerged state could not be checked",
                ],
                severity="high", snippet=line[:120],
            )

def check_metadata(path: Path, report: Reporter, tracked: set[Path] | None) -> None:
    """Checks that read the file's size and tracking state, never its bytes.

    Runs over walk_paths, so binaries reach it — a committed multi-gigabyte
    archive is precisely what this should catch, and it is never text.
    """
    if path.name in SECRET_FILENAMES:
        if tracked is None:
            report.candidate(
                1, "committed_env_file",
                f"`{path.name}` is present and git could not be consulted",
                also_caused_by=[
                    "it is untracked or gitignored, which is the correct arrangement",
                    "git is unavailable here, so tracking state is unknown",
                ],
                severity="high",
            )
        elif path.resolve() in tracked:
            report.finding(
                1, "committed_env_file",
                f"`{path.name}` is tracked by git",
                "Remove it from the index, add it to .gitignore, and rotate anything it "
                "contained — git history keeps the old copy.",
                severity="high",
            )
        # An untracked or ignored .env is correct practice. Say nothing.

    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= MAX_COMMITTED_BYTES:
        return
    if not is_probably_binary(path):
        return  # a large SQL dump or dataset is a different conversation
    megabytes = size // (1024 * 1024)
    if tracked is None:
        report.candidate(
            1, "large_committed_binary",
            f"{megabytes} MB file present, and git could not be consulted",
            also_caused_by=[
                "it is untracked or gitignored — a local build artifact costs no clone time",
                "git is unavailable here, so tracking state is unknown",
            ],
            severity="medium",
        )
    elif path.resolve() in tracked:
        report.finding(
            1, "large_committed_binary",
            f"{megabytes} MB file tracked in git",
            "Move it to release artifacts or an LFS/object store. Git stores every "
            "version forever, so this cost is paid by every clone from now on.",
            severity="medium",
        )


def check_source_file(path: Path, text: str, report: Reporter) -> None:
    """Checks that only make sense on code."""
    lines = text.splitlines()

    if len(lines) > MAX_FILE_LINES:
        report.finding(
            1, "oversized_file",
            f"{len(lines)} lines in one file",
            f"Split it by responsibility. Past roughly {MAX_FILE_LINES} lines a file "
            "stops fitting in a reviewer's head, and edits to it conflict constantly.",
            severity="medium",
        )

    for number, line in enumerate(lines, 1):
        if len(line) > MAX_LINE_LENGTH:
            report.finding(
                number, "oversized_line",
                f"{len(line)}-character line",
                "Break it up. A line this long is unreviewable in a side-by-side diff.",
                severity="low",
            )

        # TODOs are inventoried in the text pass, which covers this file too.
        body = comment_body(line)
        if body is None or _TODO.search(body):
            continue
        # Same discipline as the TODO split above: `;` and `#` are statement
        # separators and preprocessor/attribute syntax in several languages,
        # not comment openers. Without this gate, ordinary semicolon-
        # terminated code (`url := "..."; doWork()`) reads as a "comment"
        # containing a call, which is a false candidate, not a lead.
        if not unambiguous_comment(line):
            continue

        if _LOOKS_LIKE_CODE.search(body):
            report.candidate(
                number, "commented_out_code",
                "Commented-out line that looks like code",
                also_caused_by=[
                    "a documentation example shown as a comment",
                    "a language where this prefix is not a comment "
                    "(`#` opens a Rust attribute and a C preprocessor directive)",
                    "deliberately disabled code with a nearby explanation",
                ],
                severity="low", snippet=line.strip()[:120],
            )


def main() -> int:
    configure_output()
    args = build_parser(__doc__).parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()
    root = Path(args.path)

    findings = []
    unreadable, failed = [], []
    try:
        text_files = set(walk_files(root, source_only=False))
        source_files = set(walk_files(root, source_only=True))
        all_paths = list(walk_paths(root))
    except ScanPathError as exc:
        return fail_on_bad_path(exc)
    unmerged = unmerged_paths(root)
    tracked = tracked_paths(root)

    for filepath in all_paths:
        report = Reporter(filepath, ignore)
        try:
            check_metadata(filepath, report, tracked)
            if filepath in text_files:
                try:
                    text = filepath.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    warn_unreadable(filepath, exc)
                    unreadable.append(str(filepath))
                    findings.extend(report.findings)
                    continue
                check_text_file(filepath, text, report, unmerged)
                if filepath in source_files:
                    check_source_file(filepath, text, report)
        except Exception as exc:
            warn_detector_error(filepath, exc)
            failed.append(str(filepath))
            continue
        findings.extend(report.findings)

    completeness = coverage_gaps(unreadable, failed)
    if unmerged is None:
        completeness["merge_state"] = (
            "git unavailable — conflict markers reported as candidates, "
            "since unmerged state could not be confirmed"
        )
    if tracked is None:
        completeness["tracking_state"] = (
            "git unavailable — tracking state unknown, so .env and large-file "
            "findings are reported conservatively"
        )
    emit(findings, args.format, "No hygiene problems found",
         completeness=completeness or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
