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
                    looks_minified, parse_ignore, read_text, tracked_paths, unmerged_paths,
                    walk_files, walk_paths, warn_detector_error, warn_unreadable)

MERGE_MARKERS = ("<<<<<<< ", "=======", ">>>>>>> ")
MAX_FILE_LINES = 1000
MAX_LINE_LENGTH = 300
MAX_COMMITTED_BYTES = 10 * 1024 * 1024

# The skill's one concession to language syntax, used only after string
# literals have been blanked. Four tokens, and no detector branches on which
# language a file is.
#
# None of them is an unambiguous comment opener. `//` is floor division in
# Python (`result = total // TODO` is not a TODO comment, it's an expression
# naming a variable TODO), `--` is decrement in C and C++, `#` opens a C
# preprocessor directive and a Rust attribute, and `;` separates instructions
# in assembly. Without a parser this detector cannot establish that any of
# these begins a comment at all — so everything derived from one is a
# candidate, never a finding, and the record names the operator readings the
# prefix allows.
LINE_COMMENT_PREFIXES = ("//", "#", "--", ";")

# `[^\\]`, not `.`: with `.` a backslash could be consumed either as the
# start of an escape or as an ordinary character, and an unterminated quote
# followed by a run of backslashes then backtracks through every split of the
# run — exponential in its length. One committed Vite bundle held the whole
# detector past a ten-minute timeout that way.
_STRING_LITERAL = re.compile(r"""(["'`])(?:\\.|(?!\1)[^\\])*\1""")
# Past this a line is data or a bundle, not a place a person writes a comment.
# Bounding it also bounds the quadratic cost of a line full of unmatched quotes.
MAX_COMMENT_SCAN = 4096
_TODO = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
# A commented line that looks like code: ends in a statement terminator or
# opener, or contains an assignment or a call. A call is a name *touching* its
# parenthesis — `compute(1)` — because prose puts a space there: "every route
# (see the module docstring)" is an aside, not a call — and a name starts with
# a letter, so "ruling 6(c)" is a citation. The control keywords
# that conventionally take a space before theirs are named instead. A closing
# parenthesis at the end of the line is not evidence on its own, for the same
# reason: a sentence ending in an aside ends in one too.
_LOOKS_LIKE_CODE = re.compile(
    r"[;{}]\s*$|=[^=]|\b[A-Za-z_]\w*\(|\b(?:if|for|while|switch|catch|return)\s*\(")

# Commented-out code is a line that was disabled by commenting it out, so the
# prefix opens the line. A prefix found mid-line was overwhelmingly never a
# comment at all: `;` inside `for (i = 0; i < n; i++)`, `--` in `i--`, `#` in
# a CSS colour — and where it is one, it trails live code rather than
# replacing it. `#`, `--` and `;` must also be followed by whitespace or end
# the line. Written flush, each opens something else at the start of a line
# in a common language: a CSS custom property (`--accent: #0a6;`), a CSS id
# selector (`#main {`), a C preprocessor directive (`#include <x.h>`), a
# Python attribute doc comment (`#: what this holds`). `//` has no such
# reading at the start of a line.
_WHOLE_LINE_COMMENT = re.compile(r"^\s*(?://|(?:#|--|;)(?=\s|$))")
# A run of four plain lowercase words is a sentence. Code that has commented
# itself out reads as names, operators and punctuation; a comment that names a
# call — "a direct caller of validate() sees instead" — is prose about code.
# Costs: a commented-out line of English-shaped code (`for item in items if
# ready`) is missed, which a candidate list can afford.
_PROSE_RUN = re.compile(r"(?:\b[a-z]{2,}\b[ ,]+){3}\b[a-z]{2,}\b")
# Documentation comments hold examples by design, and an example is not a
# leftover: `///` and `//!` (Rust, C#, Doxygen) and `#:` (Sphinx).
_DOC_COMMENT = re.compile(r"^\s*(?:///|//!|#:)")

# Filenames whose whole purpose is to hold secrets. `.npmrc` and `.pypirc`
# are deliberately absent: both routinely hold nothing but registry, index,
# and proxy settings, so a name-only rule would call ordinary committed
# config a credential leak. find_secrets.py inspects their contents instead.
#
# `.env.<environment>` is a family, not a fixed list: `.env.staging`,
# `.env.development`, `.env.test`, and any other environment name someone
# invents are exactly as credential-bearing as `.env.production` — matched by
# prefix below rather than enumerated, since enumerating them is a losing
# game the moment a team names an environment nobody anticipated.
SECRET_FILENAMES = frozenset({".env", ".env.local"})
ENV_FAMILY_PREFIX = ".env."

# The documented exception to the family match above: these names exist
# specifically to be a committed, secret-free template for developers to copy
# from, so treating them as leaks would be the false positive this whole
# check exists to avoid on every other `.env.*` name.
ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})


def is_secret_env_filename(name: str) -> bool:
    """Whether ``name`` is a `.env` variant meant to hold real credentials."""
    if name in SECRET_FILENAMES:
        return True
    if name in ENV_TEMPLATE_NAMES:
        return False
    return name.startswith(ENV_FAMILY_PREFIX)


def blank_literals(line: str) -> str:
    """Replace string-literal contents so their punctuation stops parsing as code.

    This runs BEFORE comment stripping, which is what keeps
    `url = "https://x"; work()` from losing its trailing call to a `//` that
    was never a comment.
    """
    return _STRING_LITERAL.sub(lambda m: m.group(1) + " " * (len(m.group(0)) - 2) + m.group(1), line)


def comment_body(line: str) -> str | None:
    """The text after a line-comment prefix, or None if there is no comment."""
    if len(line) > MAX_COMMENT_SCAN:
        return None
    blanked = blank_literals(line)
    for prefix in LINE_COMMENT_PREFIXES:
        index = blanked.find(prefix)
        if index != -1:
            return line[index + len(prefix):].strip()
    return None


def whole_line_comment_body(line: str) -> str | None:
    """The text of a line that is nothing but a comment, or None.

    The TODO inventory wants every comment, trailing ones included —
    `retry()  # TODO back off` is debt. Commented-out code is narrower: a
    line somebody disabled, which is a comment from its first token on.
    """
    if _DOC_COMMENT.match(line):
        return None
    opener = _WHOLE_LINE_COMMENT.match(line)
    if opener is None:
        return None
    return line[opener.end():].strip()


def check_text_file(path: Path, text: str, report: Reporter,
                    unmerged: set[Path] | None, *, generated: bool = False) -> None:
    """Checks that are valid in any text file, source or not.

    A ``generated`` file still gets the merge-marker check — a conflict in a
    committed bundle breaks the build like any other — but not the TODO
    inventory: a bundler's output carries its dependencies' TODOs, and none of
    them is this repository's debt.
    """
    is_conflicted = unmerged is not None and path.resolve() in unmerged

    for number, line in enumerate(text.splitlines(), 1):
        # The TODO inventory belongs here, not in the source pass: a TODO in
        # deployment.yaml or settings.toml is debt exactly like one in a .go
        # file, and the design's stated scope for it is all text.
        body = None if generated else comment_body(line)
        if body is not None:
            todo = _TODO.search(body)
            if todo:
                # No prefix here is an unambiguous comment opener, so this is
                # always a candidate — never a finding. `total // TODO` is
                # floor division on a variable named TODO, not debt.
                report.candidate(
                    number, "todo_inventory",
                    f"{todo.group(1)}: {body[:80]}",
                    also_caused_by=[
                        "the prefix is an operator, not a comment — `//` is floor "
                        "division in Python, `--` is decrement in C and C++",
                        "`#` opens a C preprocessor directive or a Rust attribute; "
                        "`;` separates assembly instructions",
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
    if is_secret_env_filename(path.name):
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
        body = whole_line_comment_body(line)
        if body is None or _TODO.search(body):
            continue

        # Matched against the body with its quoted and backticked spans
        # blanked: prose that names code — "the `reload()` calls" — is
        # describing it, and an inline code span is how prose names it.
        blanked = blank_literals(body)
        if _LOOKS_LIKE_CODE.search(blanked) and not _PROSE_RUN.search(blanked):
            report.candidate(
                number, "commented_out_code",
                "Commented-out line that looks like code",
                also_caused_by=[
                    "a documentation example shown as a comment",
                    "the prefix is an operator, not a comment — `//` is floor "
                    "division in Python, `--` is decrement in C and C++, `#` "
                    "opens a preprocessor directive or a Rust attribute, `;` "
                    "separates assembly instructions",
                    "deliberately disabled code with a nearby explanation",
                ],
                severity="low", snippet=line.strip()[:120],
            )


def main() -> int:
    configure_output()
    args = build_parser(__doc__).parse_args()
    ignore = parse_ignore(args.ignore)
    root = Path(args.path)

    findings = []
    unreadable, failed, generated_files = [], [], []
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
                    text = read_text(filepath)
                except OSError as exc:
                    warn_unreadable(filepath, exc)
                    unreadable.append(str(filepath))
                    findings.extend(report.findings)
                    continue
                generated = looks_minified(text)
                if generated:
                    generated_files.append(str(filepath))
                check_text_file(filepath, text, report, unmerged, generated=generated)
                # Line length, file length and commented-out code are
                # questions about what a person wrote; a bundle answers every
                # one of them with noise.
                if filepath in source_files and not generated:
                    check_source_file(filepath, text, report)
        except Exception as exc:
            warn_detector_error(filepath, exc)
            failed.append(str(filepath))
            continue
        findings.extend(report.findings)

    completeness = coverage_gaps(unreadable, failed)
    if generated_files:
        # Named, not silently dropped: a reader comparing two runs should see
        # that these were read for merge markers only.
        names = sorted(generated_files)
        completeness["generated_files_skipped"] = (
            f"{len(names)} generated file(s) checked for merge markers only: "
            + ", ".join(names[:5]) + ("…" if len(names) > 5 else "")
        )
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
