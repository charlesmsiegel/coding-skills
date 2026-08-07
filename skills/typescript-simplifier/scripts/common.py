#!/usr/bin/env python3
"""Shared plumbing for the detector scripts.

Every detector needs the same four things: a way to enumerate TypeScript
files, the severity icons, a console that can print them, and one policy for
what to do when a file will not parse or a detector crashes. These were
copy-pasted per script; this module is the single copy.
"""

import argparse
import contextlib
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from tsparse import TsSyntaxError, parse_file

# Severity → icon, in report order.
SEVERITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}

# Directory names that are never the user's own code. Matched against path
# segments *below* the scanned root, so a repo that happens to live inside a
# directory with one of these names is still scanned.
EXCLUDE_DIRS = frozenset({
    "node_modules", ".git", ".hg", ".svn",
    "dist", "build", "out", "output", "lib-esm", ".output",
    ".next", ".nuxt", ".svelte-kit", ".astro", ".angular", ".vercel",
    "coverage", ".nyc_output", "storybook-static",
    ".turbo", ".cache", ".parcel-cache", ".yarn", ".pnp",
    "bower_components", "jspm_packages", "vendor",
    ".venv", "venv", "__pycache__",
})

# The extensions this skill claims to analyse. `.d.ts` files come along —
# a hand-written declaration file is real API surface — but generated ones
# almost always live under an excluded build directory.
TS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts")

# Filename markers that make a file a test. Used by several detectors to
# apply a different standard (a `console.log` in a test is not a leftover)
# and by find_untested_modules to tell the two halves of a repo apart.
TEST_DIR_NAMES = frozenset({"__tests__", "__test__", "test", "tests", "spec", "e2e", "cypress"})
TEST_NAME_MARKERS = (".test.", ".spec.", "-test.", "_test.")


def configure_output() -> None:
    """Keep emoji output from crashing narrow console encodings.

    On Windows, stdout often defaults to cp1252 with strict error handling,
    so the first severity icon raises UnicodeEncodeError. Downgrade
    unencodable characters instead of dying.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # A detached or closed stream has nothing to configure.
            with contextlib.suppress(ValueError, OSError):
                reconfigure(errors="replace")


def find_ts_files(path: Path) -> Iterator[Path]:
    """Yield the TypeScript files under ``path``, skipping vendored/built dirs."""
    if path.is_file():
        if path.suffix in TS_EXTENSIONS:
            yield path
        return
    if not path.is_dir():
        return
    for candidate in sorted(path.rglob("*")):
        if candidate.suffix not in TS_EXTENSIONS or not candidate.is_file():
            continue
        if EXCLUDE_DIRS.isdisjoint(candidate.relative_to(path).parts):
            yield candidate


def is_test_file(filepath: Path) -> bool:
    """True when the path names a test file by directory or by suffix."""
    name = filepath.name
    if any(marker in name for marker in TEST_NAME_MARKERS):
        return True
    return not TEST_DIR_NAMES.isdisjoint(p.lower() for p in filepath.parts)


def is_declaration_file(filepath: Path) -> bool:
    """True for `.d.ts` — types only, so most code detectors do not apply."""
    return filepath.name.endswith(".d.ts")


def warn_unparseable(filepath: Path, exc: Exception) -> None:
    """Note a file the tokenizer could not make sense of.

    The parser is a hand-written scanner, not the TypeScript compiler, so this
    means "this skill cannot analyse the file", which is a different claim from
    "this file is broken". Either way it is named rather than silently dropped.
    """
    print(f"⚠️  {filepath}: skipped, does not tokenize cleanly ({exc})", file=sys.stderr)


def warn_detector_error(filepath: Path, exc: Exception) -> None:
    """Surface a detector crash instead of silently reporting the file clean."""
    print(
        f"⚠️  {filepath}: detector failed ({type(exc).__name__}: {exc}); "
        "findings for this file are incomplete, not clean",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------- #
# One finding shape, one CLI, one report — shared by every detector
# --------------------------------------------------------------------------- #

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Finding:
    """The single output record. `smell_type` is what `--ignore` matches."""

    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str = "medium"
    code_snippet: str = ""
    # Other lines that participate in the same finding. analyze_diff.py uses
    # these so a cross-declaration smell surfaces when any participant changed.
    related_lines: list[int] = field(default_factory=list)


class Reporter:
    """Collects findings for one file, honouring the detector's --ignore set."""

    def __init__(self, tsfile, ignore: set[str]):
        self.tsfile = tsfile
        self.ignore = ignore
        self.findings: list[Finding] = []

    def add(self, line: int, smell_type: str, description: str, suggestion: str,
            severity: str = "medium", related: list[int] | None = None) -> None:
        if smell_type in self.ignore:
            return
        self.findings.append(Finding(
            file=str(self.tsfile.path), line=line, smell_type=smell_type,
            description=description, suggestion=suggestion, severity=severity,
            code_snippet=self.tsfile.snippet(line), related_lines=related or [],
        ))


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="",
                        help="Comma-separated finding types to suppress")
    return parser


def sort_findings(findings: list[Finding]) -> list[Finding]:
    findings.sort(key=lambda f: (SEVERITY_RANK.get(f.severity, 1), f.file, f.line))
    return findings


def print_findings(findings: list[Finding], clean_message: str) -> None:
    if not findings:
        print(f"✅ {clean_message}")
        return
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.smell_type] = counts.get(finding.smell_type, 0) + 1
    print(f"Found {len(findings)} issue(s):\n")
    print("Summary:")
    for smell, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {smell}: {count}")
    print()
    for finding in findings:
        icon = SEVERITY_ICONS.get(finding.severity, "")
        print(f"{icon} [{finding.severity.upper()}] {finding.file}:{finding.line}")
        print(f"   {finding.smell_type}: {finding.description}")
        if finding.code_snippet:
            print(f"   Code: {finding.code_snippet}")
        print(f"   → {finding.suggestion}\n")


def emit(findings: list[Finding], output_format: str, clean_message: str) -> None:
    sort_findings(findings)
    if output_format == "json":
        print(json.dumps([asdict(f) for f in findings], indent=2))
    else:
        print_findings(findings, clean_message)


def run_file_detector(
    description: str,
    clean_message: str,
    analyze: "Callable[..., list[Finding]]",
    *,
    skip_declaration_files: bool = True,
    argv: list[str] | None = None,
) -> None:
    """Standard main() for a detector that reasons about one file at a time.

    ``analyze`` is called as ``analyze(tsfile, ignore)`` and returns findings;
    a file that will not tokenize is named on stderr rather than counted clean.
    """
    configure_output()
    args = build_parser(description).parse_args(argv)
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    findings: list[Finding] = []
    for filepath in find_ts_files(Path(args.path)):
        if skip_declaration_files and is_declaration_file(filepath):
            continue
        try:
            findings.extend(analyze(parse_file(filepath), ignore))
        except TsSyntaxError as exc:
            warn_unparseable(filepath, exc)
        except OSError as exc:
            warn_unparseable(filepath, exc)
        except Exception as exc:  # a detector bug must not read as a clean file
            warn_detector_error(filepath, exc)
    emit(findings, args.format, clean_message)


def run_tree_detector(
    description: str,
    clean_message: str,
    analyze: "Callable[..., list[Finding]]",
    *,
    extra_arguments: "Callable[[argparse.ArgumentParser], None] | None" = None,
    argv: list[str] | None = None,
) -> None:
    """Standard main() for a detector that needs the whole tree at once."""
    configure_output()
    parser = build_parser(description)
    if extra_arguments is not None:
        extra_arguments(parser)
    args = parser.parse_args(argv)
    ignore = set(args.ignore.split(",")) if args.ignore else set()
    emit(analyze(Path(args.path), ignore, args), args.format, clean_message)
