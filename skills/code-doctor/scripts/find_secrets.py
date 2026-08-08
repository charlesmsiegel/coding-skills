#!/usr/bin/env python3
"""
Credentials committed to the repository.

Key material and recognisable cloud credentials are findings: the format is
distinctive enough to be evidence on its own. A high-entropy value assigned to
a secret-shaped name is a candidate — it is routinely a test fixture, a public
key identifier, or a hash, and telling those apart needs context this skill
does not have.
"""

import math
import re
import sys
from pathlib import Path

from common import (Reporter, ScanPathError, build_parser, committed_or_staged_text,
                    configure_output, coverage_gaps, emit, fail_on_bad_path, parse_ignore,
                    read_text, tracked_paths, walk_files, warn_detector_error,
                    warn_unreadable)

# OpenPGP armor is "PGP PRIVATE KEY BLOCK", not "PGP PRIVATE KEY" — the
# shorter form matches nothing a real key ever writes. "ENCRYPTED PRIVATE
# KEY" is standard PKCS#8 armor for a password-protected key and is just as
# much a secret worth rotating as an unencrypted one.
KEY_BLOCK = re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
                      r"|-----BEGIN PGP PRIVATE KEY BLOCK-----")
KEY_END = re.compile(r"-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
                    r"|-----END PGP PRIVATE KEY BLOCK-----")
BASE64_LINE = re.compile(r"[A-Za-z0-9+/=]{32,}")

CLOUD_CREDENTIALS = (
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key ID"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "AWS temporary access key ID"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub personal access token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), "GitHub fine-grained token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), "API secret key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "JWT"),
)

# Values every vendor publishes in its own documentation. They are not
# credentials, and the wide walk deliberately reaches the docs and fixtures
# that quote them.
DOCUMENTED_EXAMPLES = frozenset({
    "AKIAIOSFODNN7EXAMPLE",
    "ASIAIOSFODNN7EXAMPLE",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
})

# The value may be quoted or bare. `.env` files and YAML — the two formats
# most likely to hold a real credential — normally write it unquoted, so a
# quotes-only pattern misses exactly the highest-value cases.
SECRET_NAME = re.compile(
    r"\b(\w*(?:secret|passwd|password|token|apikey|api_key|access_key|private_key)\w*)\b"
    r"\s*[:=]\s*"
    r"""(?:['"]([^'"]{16,})['"]|([^\s'"#;,]{16,}))""",
    re.IGNORECASE,
)

MIN_ENTROPY_BITS = 3.5

# also_caused_by prose, shared between the private-key and cloud-credential
# paths since the underlying tracking states (and the reasoning behind them)
# are identical — only the header line naming what was found differs.
_UNTRACKED_REASONS = (
    "it is gitignored local material that was never pushed",
    "it is a scratch file outside the repository's history",
)
_UNKNOWN_TRACKING_REASONS = (
    "git is unavailable here, so committed/staged content could not be checked",
)
_NOT_YET_COMMITTED_REASONS = (
    "it was just added to a tracked file and has not yet been staged or committed",
    "the committed/staged version of this file does not contain this value",
)


class _Lazy:
    """Calls ``fn`` at most once and caches the result, including a cached None.

    Committed/staged content is only worth fetching from git when a match
    actually needs verifying — most scanned files contain no credential at
    all, and spawning a `git show` per tracked file regardless would multiply
    the walk's subprocess count by the size of the repository for no benefit.
    """

    __slots__ = ("_fn", "_value", "_done")

    def __init__(self, fn):
        self._fn = fn
        self._value = None
        self._done = False

    def __call__(self):
        if not self._done:
            self._value = self._fn()
            self._done = True
        return self._value


def shannon_entropy(value: str) -> float:
    """Bits of entropy per character. A real key scores well above a placeholder."""
    if not value:
        return 0.0
    counts = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _key_payload_lines(lines: list[str], start: int) -> list[str]:
    """The base64 body lines between a BEGIN marker (exclusive) and END (exclusive).

    A bare `-----BEGIN RSA PRIVATE KEY-----` with nothing after it is what a
    documentation example or a fixture looks like, and the wide walk reaches
    both. The END marker itself is not payload, though: a doc block of
    `BEGIN` / `<redacted>` / `END` has both markers and no key, and must not
    draw rotate-and-purge advice over nothing. Stop scanning at END, then
    require actual base64 body BETWEEN the markers.
    """
    window = []
    for candidate in lines[start:start + 40]:
        if KEY_END.search(candidate):
            break
        window.append(candidate)
    return [c.strip() for c in window if BASE64_LINE.fullmatch(c.strip())]


def _single_line_key_payload(line: str, header_end: int) -> list[str]:
    """Base64 body on the SAME physical line as the BEGIN marker.

    Service-account credentials in JSON or env files often put the whole key
    on one physical line with literal `\\n` escapes standing in for real
    newlines. `_key_payload_lines` only looks at subsequent physical *lines*,
    so a complete, committed key in that shape was silently downgraded to a
    header-only candidate with no remediation. Split the remainder of this
    line on the literal two-character `\\n` escape to recover the key's
    logical lines, stop at a same-line END marker the same way
    `_key_payload_lines` does, and look for a base64 chunk within each piece
    (`search`, not `fullmatch` — surrounding JSON quoting and a trailing comma
    are expected here, unlike a real PEM file where each physical line is
    pure base64).
    """
    body = []
    for piece in line[header_end:].split("\\n"):
        if KEY_END.search(piece):
            break
        body.append(piece)
    found = []
    for piece in body:
        match = BASE64_LINE.search(piece)
        if match:
            found.append(match.group(0))
    return found


def _find_credential_matches(line: str) -> list[tuple[tuple[int, int], str, str]]:
    """Every distinct credential span on the line, across all known patterns.

    A single `break` after the first match leaves every other credential on a
    minified JSON blob or a multi-token shell line unreported and unrevoked.
    Every pattern's matches are collected; a span already claimed by an
    earlier (and therefore, by list order, more specific) pattern is skipped
    so the same characters are never reported twice under two labels.
    """
    found = []
    claimed: list[tuple[int, int]] = []
    for pattern, label in CLOUD_CREDENTIALS:
        for match in pattern.finditer(line):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in claimed):
                continue
            claimed.append(span)
            found.append((span, match.group(0), label))
    found.sort(key=lambda item: item[0])
    return found


def _tracking_verdict(tracked_here: bool | None, get_committed_text, needle: str) -> str:
    """One of 'committed', 'not_yet_committed', 'untracked', or 'unknown'.

    'committed' is the only verdict that may promote to a finding: the
    matched text must actually appear in git's index or HEAD version of this
    file — tracked-ness of the *path* (`tracked_here`) is not evidence that
    these particular *bytes* were ever staged or committed. 'unknown' covers
    git being unavailable or unable to answer for this file; it gets the same
    conservative (never-a-finding) treatment as 'untracked'/'not_yet_committed'
    but is reported with a different, honest reason.
    """
    if tracked_here is False:
        return "untracked"
    if tracked_here is None:
        return "unknown"
    committed_text = get_committed_text()
    if committed_text is None:
        return "unknown"
    return "committed" if needle in committed_text else "not_yet_committed"


def _uncertain_reasons(verdict: str) -> tuple[str, ...]:
    if verdict == "untracked":
        return _UNTRACKED_REASONS
    if verdict == "unknown":
        return _UNKNOWN_TRACKING_REASONS
    return _NOT_YET_COMMITTED_REASONS  # verdict == "not_yet_committed"


def analyze(path: Path, text: str, report: Reporter, tracked_here: bool | None = None,
           get_committed_text=lambda: None) -> None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        number = index + 1
        key_match = KEY_BLOCK.search(line)
        if key_match:
            body = (_key_payload_lines(lines, index + 1)
                   + _single_line_key_payload(line, key_match.end()))
            if not body:
                report.candidate(
                    number, "private_key_material",
                    "Private-key header with no key body following it",
                    also_caused_by=[
                        "a documentation example showing the format",
                        "a test fixture that only needs the header line",
                        "a truncated or redacted paste",
                    ],
                    severity="high",
                )
                continue
            verdict = _tracking_verdict(tracked_here, get_committed_text, body[0])
            if verdict == "committed":
                report.finding(
                    number, "private_key_material",
                    "Private key block committed to the repository",
                    "Remove the key, rotate it, and purge it from history. Anyone who has "
                    "ever cloned this repository has the old copy.",
                    severity="high",
                )
            else:
                report.candidate(
                    number, "private_key_material",
                    "Private key block with a body, but not confirmed committed",
                    also_caused_by=list(_uncertain_reasons(verdict)),
                    severity="high",
                )
            continue

        credential_spans: list[tuple[int, int]] = []
        for span, value, label in _find_credential_matches(line):
            credential_spans.append(span)
            if value in DOCUMENTED_EXAMPLES:
                continue  # the vendor's own published placeholder
            verdict = _tracking_verdict(tracked_here, get_committed_text, value)
            if verdict == "committed":
                report.finding(
                    number, "cloud_credential",
                    f"{label} committed to the repository",
                    "Revoke it now, then load it from the environment or a secret "
                    "manager. Revoke first — removing the line does not un-leak it.",
                    severity="high", snippet=value[:12] + "…",
                )
            else:
                report.candidate(
                    number, "cloud_credential",
                    f"{label}, but not confirmed committed",
                    also_caused_by=list(_uncertain_reasons(verdict)),
                    severity="high", snippet=value[:12] + "…",
                )

        assignment = SECRET_NAME.search(line)
        if assignment:
            group_index = 2 if assignment.group(2) else 3
            value = assignment.group(group_index)
            value_span = assignment.span(group_index)
            # Suppress only when this candidate would restate a credential
            # already reported (or ruled out as a documented example) above —
            # a distinct high-entropy value elsewhere on the same line still
            # deserves its own candidate.
            overlaps_reported = any(value_span[0] < end and start < value_span[1]
                                    for start, end in credential_spans)
            if not overlaps_reported and shannon_entropy(value) >= MIN_ENTROPY_BITS:
                report.candidate(
                    number, "hardcoded_secret_assignment",
                    f"High-entropy value assigned to `{assignment.group(1)}`",
                    also_caused_by=[
                        "a test fixture or a deliberately fake credential",
                        "a public identifier (a key ID, a client ID) that is not secret",
                        "a hash, a checksum, or an encoded non-secret value",
                    ],
                    severity="high", snippet=assignment.group(1) + " = …",
                )


def main() -> int:
    configure_output()
    args = build_parser(__doc__).parse_args()
    ignore = parse_ignore(args.ignore)
    root = Path(args.path)

    # Tracking state separates a committed leak from a developer's gitignored
    # local config. Without it every finding would carry revoke-and-purge
    # advice for files that were never pushed anywhere.
    tracked = tracked_paths(root)

    findings, unreadable, failed = [], [], []
    try:
        walked = list(walk_files(root, source_only=False))
    except ScanPathError as exc:
        return fail_on_bad_path(exc)
    for filepath in walked:
        try:
            text = read_text(filepath)
        except OSError as exc:
            warn_unreadable(filepath, exc)
            unreadable.append(str(filepath))
            continue
        report = Reporter(filepath, ignore)
        tracked_here = None if tracked is None else (filepath.resolve() in tracked)
        # Tracked-ness of the path is not tracked-ness of the bytes (A1): a
        # match only promotes to a finding once the matched text is confirmed
        # present in git's own index/HEAD content, fetched lazily below so a
        # file with no matches never pays for a `git show` it doesn't need.
        get_committed_text = _Lazy(
            lambda fp=filepath: committed_or_staged_text(root, fp) if tracked_here else None
        )
        try:
            analyze(filepath, text, report, tracked_here, get_committed_text)
        except Exception as exc:
            warn_detector_error(filepath, exc)
            failed.append(str(filepath))
            continue
        findings.extend(report.findings)

    completeness = coverage_gaps(unreadable, failed)
    if tracked is None:
        completeness["tracking_state"] = (
            "git unavailable — tracking state unknown, so credentials are reported "
            "as committed only where that cannot be ruled out"
        )
    emit(findings, args.format, "No committed credentials found",
         completeness=completeness or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
