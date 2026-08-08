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

from common import (Reporter, ScanPathError, build_parser, configure_output,
                    coverage_gaps, emit, fail_on_bad_path, tracked_paths,
                    walk_files, warn_detector_error, warn_unreadable)

# OpenPGP armor is "PGP PRIVATE KEY BLOCK", not "PGP PRIVATE KEY" — the
# shorter form matches nothing a real key ever writes.
KEY_BLOCK = re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
                      r"|-----BEGIN PGP PRIVATE KEY BLOCK-----")
KEY_END = re.compile(r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
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


def shannon_entropy(value: str) -> float:
    """Bits of entropy per character. A real key scores well above a placeholder."""
    if not value:
        return 0.0
    counts = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def has_key_payload(lines: list[str], start: int) -> bool:
    """Whether a BEGIN line is followed by a plausible key body.

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
    return any(BASE64_LINE.fullmatch(candidate.strip()) for candidate in window)


def analyze(path: Path, text: str, report: Reporter,
            tracked_here: bool | None = None) -> None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        number = index + 1
        if KEY_BLOCK.search(line):
            if has_key_payload(lines, index + 1) and tracked_here is not False:
                report.finding(
                    number, "private_key_material",
                    "Private key block committed to the repository",
                    "Remove the key, rotate it, and purge it from history. Anyone who has "
                    "ever cloned this repository has the old copy.",
                    severity="high",
                )
            elif has_key_payload(lines, index + 1):
                report.candidate(
                    number, "private_key_material",
                    "Private key block in a file git does not track",
                    also_caused_by=[
                        "it is gitignored local key material that was never pushed",
                        "it is a scratch file outside the repository's history",
                    ],
                    severity="high",
                )
            else:
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

        # One credential must not appear as both a finding and a candidate, so
        # a recognized pattern ends this line's processing entirely.
        matched_credential = False
        for pattern, label in CLOUD_CREDENTIALS:
            match = pattern.search(line)
            if not match:
                continue
            matched_credential = True
            if match.group(0) in DOCUMENTED_EXAMPLES:
                break  # the vendor's own published placeholder
            if tracked_here is False:
                report.candidate(
                    number, "cloud_credential",
                    f"{label} in a file git does not track",
                    also_caused_by=[
                        "it is gitignored local configuration that was never pushed",
                        "it is a scratch file outside the repository's history",
                    ],
                    severity="high", snippet=match.group(0)[:12] + "…",
                )
            else:
                report.finding(
                    number, "cloud_credential",
                    f"{label} committed to the repository",
                    "Revoke it now, then load it from the environment or a secret "
                    "manager. Revoke first — removing the line does not un-leak it.",
                    severity="high", snippet=match.group(0)[:12] + "…",
                )
            break
        if matched_credential:
            continue

        assignment = SECRET_NAME.search(line)
        value = (assignment.group(2) or assignment.group(3)) if assignment else ""
        if assignment and shannon_entropy(value) >= MIN_ENTROPY_BITS:
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
    ignore = set(args.ignore.split(",")) if args.ignore else set()
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
            text = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warn_unreadable(filepath, exc)
            unreadable.append(str(filepath))
            continue
        report = Reporter(filepath, ignore)
        tracked_here = None if tracked is None else (filepath.resolve() in tracked)
        try:
            analyze(filepath, text, report, tracked_here)
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
