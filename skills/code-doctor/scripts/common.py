#!/usr/bin/env python3
"""Shared plumbing for the code-doctor detectors.

This skill is language-blind: it has no parsers, no comment-syntax tables, and
no framework knowledge. That buys it the ability to review a repo written in
anything, and it costs it the ability to prove most of what it observes — so
the finding/candidate split below is the load-bearing part of this module, not
a formality.
"""

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

SEVERITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

# Never the user's own code. Matched against path segments below the scanned
# root, so a repo living inside a directory with one of these names is fine.
EXCLUDE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components", "vendor", "third_party",
    ".venv", "venv", "__pycache__", ".tox", ".nox", ".eggs", "site-packages",
    "build", "dist", "out", "target", "obj",
    ".next", ".nuxt", ".svelte-kit", ".astro", ".angular",
    "coverage", ".nyc_output", ".turbo", ".cache", ".parcel-cache",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".gradle", ".idea",
})
# `bin` is deliberately NOT excluded. Ruby gems, shell CLIs, and plenty of
# other projects keep executable *source* there, and excluding it would hide
# those entry points from even the secrets and merge-marker checks. The build
# outputs that share the name (node_modules/.bin, target/) are already covered
# by their parents.

# The inverse of a language table. Enumerating what is NOT code means an
# unknown extension is still treated as code, which is what keeps this skill
# language-blind — a table of known languages would silently skip the next one.
NON_CODE_SUFFIXES = frozenset({
    ".md", ".markdown", ".rst", ".txt", ".adoc", ".org",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".properties",
    ".lock", ".sum", ".csv", ".tsv", ".xml", ".svg",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz",
    ".po", ".pot", ".snap", ".map",
})

# Directories whose contents are documentation regardless of extension. A .py
# under docs/ is an example, not the product.
DOC_DIR_NAMES = frozenset({"docs", "doc", "documentation", "examples", "example", "samples"})

# Minified or generated bundles: real code, but nobody reviews them and their
# line lengths would dominate every size metric.
GENERATED_MARKERS = (".min.js", ".min.css", ".bundle.js", "_pb2.py", ".g.dart", ".generated.")

_BINARY_SNIFF_BYTES = 8192


class ScanPathError(ValueError):
    """The path handed to a detector does not exist.

    Ending the iterator instead would let a typo in an audit path produce an
    authoritative-looking "No problems found" over nothing at all.
    """


def configure_output() -> None:
    """Keep emoji output from crashing narrow console encodings."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(errors="replace")


def is_probably_binary(path: Path) -> bool:
    """A NUL byte in the first block means this is not text. Cheap and reliable."""
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True


def is_source(rel_parts: tuple[str, ...], path: Path) -> bool:
    """Whether the code-only detectors should read this file.

    Not-binary is not the same as is-source: branch counting and duplication
    shingling over a README manufactures findings out of prose.
    """
    if any(part in DOC_DIR_NAMES for part in rel_parts[:-1]):
        return False
    if path.suffix.lower() in NON_CODE_SUFFIXES:
        return False
    name = path.name.lower()
    return not any(marker in name for marker in GENERATED_MARKERS)


def walk_paths(root: Path) -> Iterator[Path]:
    """Yield every non-excluded file path under ``root``, binaries included.

    Metadata-only checks (file size, tracking state) need the binary paths that
    ``walk_files`` filters out — a committed multi-gigabyte archive is exactly
    the thing a hygiene check should see, and it is never going to be text.

    **Symlinks are never followed.** A symlink passes ``is_file()`` and its
    target would then be read, so a link pointing outside the tree would let
    this skill inspect host data and report a credential found there as
    committed to this repository. Git stores only the link target string, so
    there is nothing of the target's content to review in any case.
    """
    if root.is_symlink():
        return
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        raise ScanPathError(f"{root}: no such file or directory")

    # os.walk with in-place dirnames pruning, NOT rglob-then-filter: rglob
    # descends into node_modules, vendor and target in full and stats every
    # file inside before anything is discarded. On a real project that tree
    # dwarfs the source and dominates both wall-clock and memory.
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in EXCLUDE_DIRS
                             and not Path(dirpath, d).is_symlink())
        for name in sorted(filenames):
            path = Path(dirpath, name)
            if not path.is_symlink() and path.is_file():
                yield path


def walk_files(root: Path, *, source_only: bool) -> Iterator[Path]:
    """Yield the files under ``root`` a detector should *read as text*.

    ``source_only=True`` restricts to files classified as source. Detectors
    whose findings are real in any text file (secrets, merge markers) pass
    False and take the wider set. Binaries are excluded from both.
    """
    for path in walk_paths(root):
        if source_only and not is_source(path.relative_to(root).parts
                                         if root.is_dir() else (path.name,), path):
            continue
        if is_probably_binary(path):
            continue
        yield path


# --------------------------------------------------------------------------- #
# The confidence discipline, as a type
# --------------------------------------------------------------------------- #

class SchemaError(ValueError):
    """A detector tried to emit a record its evidence does not support."""


VALID_KINDS = frozenset({"finding", "candidate"})


@dataclass(frozen=True)
class Finding:
    """One output record, in one of two kinds.

    A **finding** asserts a defect. It carries a concrete fix, because a claim
    you cannot act on is not worth making.

    A **candidate** reports a lead that needs verification. It carries the
    specific ways a healthy codebase produces the same observation, and it
    carries no fix — recommending an edit on heuristic evidence is how a tool
    like this talks someone into deleting live code.

    The constructor enforces the difference. Prose in a reference file does not
    survive contact with a detector author in a hurry; a raised exception does.

    Frozen to ensure the schema enforcement holds across the lifetime of the
    object, not just at construction time.
    """

    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str = ""
    also_caused_by: list[str] = field(default_factory=list)
    severity: str = "medium"
    kind: str = "finding"
    code_snippet: str = ""
    related_lines: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise SchemaError(
                f"{self.smell_type}: kind must be one of {sorted(VALID_KINDS)}, got {self.kind!r}"
            )
        if self.kind == "finding":
            if not self.suggestion.strip():
                raise SchemaError(
                    f"{self.smell_type}: a finding asserts a defect and must carry a suggestion; "
                    "if you cannot name the fix, emit a candidate instead"
                )
            if self.also_caused_by:
                raise SchemaError(
                    f"{self.smell_type}: also_caused_by belongs to candidates; a finding that has "
                    "benign explanations is a candidate"
                )
        else:
            if self.suggestion.strip():
                raise SchemaError(
                    f"{self.smell_type}: a candidate must not carry a suggestion — it is an "
                    "unverified lead, and a fix on unverified evidence is how live code gets deleted"
                )
            if not self.also_caused_by or not any(s.strip() for s in self.also_caused_by):
                raise SchemaError(
                    f"{self.smell_type}: a candidate must name the ways a healthy codebase produces "
                    "this observation in also_caused_by, so the reader can rule them out"
                )


class Reporter:
    """Collects records for one file, honouring the detector's --ignore set."""

    def __init__(self, path: Path, ignore: set[str]):
        self.path = path
        self.ignore = ignore
        self.findings: list[Finding] = []

    def finding(self, line: int, smell_type: str, description: str, suggestion: str,
                severity: str = "medium", snippet: str = "",
                related: list[int] | None = None) -> None:
        self._add(Finding(
            file=str(self.path), line=line, smell_type=smell_type,
            description=description, suggestion=suggestion, severity=severity,
            kind="finding", code_snippet=snippet, related_lines=related or [],
        ))

    def candidate(self, line: int, smell_type: str, description: str,
                  also_caused_by: list[str], severity: str = "low",
                  snippet: str = "", related: list[int] | None = None) -> None:
        self._add(Finding(
            file=str(self.path), line=line, smell_type=smell_type,
            description=description, also_caused_by=also_caused_by,
            severity=severity, kind="candidate", code_snippet=snippet,
            related_lines=related or [],
        ))

    def _add(self, record: Finding) -> None:
        if record.smell_type not in self.ignore:
            self.findings.append(record)


# --------------------------------------------------------------------------- #
# Git, and knowing when not to trust it
# --------------------------------------------------------------------------- #

def git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo``, returning stdout. Raises on failure."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=120, check=True,
    )
    return result.stdout


def is_git_repo(repo: Path) -> bool:
    try:
        git(repo, "rev-parse", "--git-dir")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


@dataclass
class HistoryDepth:
    """What the repository's history can actually support.

    A shallow CI checkout exposes only the most recent committer, which turns
    multi-author files into apparent single-author files. Computing a bus
    factor over that fragment produces a confidently wrong answer, which is
    worse than no answer.
    """

    is_repo: bool
    is_shallow: bool
    commit_count: int
    oldest_commit_days: int | None
    min_commits: int = 20

    @property
    def usable(self) -> bool:
        return self.is_repo and not self.is_shallow and self.commit_count >= self.min_commits

    def explain(self) -> str:
        if not self.is_repo:
            return "not a git repository — history-derived findings skipped"
        if self.is_shallow:
            return ("shallow clone — history-derived findings skipped; "
                    "re-run with `git fetch --unshallow` for ownership and churn")
        if self.commit_count < self.min_commits:
            return (f"only {self.commit_count} commit(s) of history — too few to support "
                    "churn or ownership claims; findings skipped")
        return f"{self.commit_count} commits of history"


def probe_history(repo: Path, *, min_commits: int = 20) -> HistoryDepth:
    """Establish what may be claimed from this repository's history."""
    if not is_git_repo(repo):
        return HistoryDepth(False, False, 0, None, min_commits=min_commits)

    try:
        shallow = git(repo, "rev-parse", "--is-shallow-repository").strip() == "true"
    except Exception:
        shallow = False

    try:
        count = int(git(repo, "rev-list", "--count", "HEAD").strip() or 0)
    except Exception:
        count = 0

    oldest_days = None
    try:
        # `git log --reverse --max-count=1` does NOT give the oldest commit:
        # --max-count limits the selection first, then --reverse reverses what
        # was selected, so it returns HEAD. Ask for the root commit directly.
        root = git(repo, "rev-list", "--max-parents=0", "HEAD").split()
        stamp = git(repo, "log", "-1", "--format=%ct", root[-1]).strip() if root else ""
        if stamp:
            oldest_days = int((time.time() - int(stamp)) / 86400)
    except Exception:
        oldest_days = None

    return HistoryDepth(True, shallow, count, oldest_days, min_commits=min_commits)


# --------------------------------------------------------------------------- #
# One CLI, one report
# --------------------------------------------------------------------------- #

def warn_unreadable(filepath: Path, exc: Exception) -> None:
    """Name a file this skill could not read, rather than counting it clean."""
    print(f"⚠️  {filepath}: skipped, unreadable ({exc})", file=sys.stderr)


def warn_detector_error(filepath: Path, exc: Exception) -> None:
    """Surface a detector crash instead of silently reporting the file clean."""
    print(
        f"⚠️  {filepath}: detector failed ({type(exc).__name__}: {exc}); "
        "findings for this file are incomplete, not clean",
        file=sys.stderr,
    )


def fail_on_bad_path(exc: ScanPathError) -> int:
    """Turn a missing scan root into a loud, nonzero exit at the CLI boundary."""
    print(f"error: {exc}", file=sys.stderr)
    return 2


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="",
                        help="Comma-separated finding types to suppress")
    return parser


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Findings before candidates, then by severity, then by location."""
    findings.sort(key=lambda f: (f.kind != "finding",
                                 SEVERITY_RANK.get(f.severity, 1), f.file, f.line))
    return findings


def _print_report(findings: list[Finding], clean_message: str,
                  completeness: dict | None) -> None:
    if completeness:
        for label, note in completeness.items():
            print(f"ℹ️  {label}: {note}")
        print()

    confirmed = [f for f in findings if f.kind == "finding"]
    leads = [f for f in findings if f.kind == "candidate"]

    if not findings:
        print(f"✅ {clean_message}")
        return

    print(f"{len(confirmed)} finding(s), {len(leads)} candidate(s):\n")

    for record in confirmed:
        icon = SEVERITY_ICONS.get(record.severity, "")
        print(f"{icon} [{record.severity.upper()}] {record.file}:{record.line}")
        print(f"   {record.smell_type}: {record.description}")
        if record.code_snippet:
            print(f"   Code: {record.code_snippet}")
        print(f"   → {record.suggestion}\n")

    if leads:
        print("Candidates — unverified leads, check before acting:\n")
    for record in leads:
        icon = SEVERITY_ICONS.get(record.severity, "")
        print(f"{icon} [candidate] {record.file}:{record.line}")
        print(f"   {record.smell_type}: {record.description}")
        if record.code_snippet:
            print(f"   Code: {record.code_snippet}")
        print("   Also caused by:")
        for reason in record.also_caused_by:
            print(f"     - {reason}")
        print()


def emit(findings: list[Finding], output_format: str, clean_message: str,
         completeness: dict | None = None) -> None:
    sort_findings(findings)
    if output_format == "json":
        records = [asdict(f) for f in findings]
        if completeness:
            print(json.dumps({"completeness": completeness, "findings": records}, indent=2))
        else:
            print(json.dumps(records, indent=2))
    else:
        _print_report(findings, clean_message, completeness)


def coverage_gaps(unreadable: list[str], failed: list[str]) -> dict:
    """Lost-file accounting, as a completeness record.

    stderr is not enough. analyze_all.py ignores a subprocess's stderr when it
    exits zero, so a detector that lost ten files to read errors would still
    aggregate as "No problems found" — a silent coverage hole reported as a
    clean repository, which is the exact failure this skill exists to avoid.
    """
    gaps = {}
    if unreadable:
        gaps["files_unreadable"] = (
            f"{len(unreadable)} file(s) could not be read and were not analysed: "
            + ", ".join(unreadable[:5]) + ("…" if len(unreadable) > 5 else "")
        )
    if failed:
        gaps["files_detector_failed"] = (
            f"{len(failed)} file(s) crashed the detector and are incomplete, not clean: "
            + ", ".join(failed[:5]) + ("…" if len(failed) > 5 else "")
        )
    return gaps


def run_file_detector(description: str, clean_message: str, analyze,
                      *, source_only: bool = True, argv: list[str] | None = None) -> int:
    """Standard main() for a detector that reasons about one file at a time.

    ``analyze`` is called as ``analyze(path, text, reporter)``. Returns the
    process exit code.
    """
    configure_output()
    args = build_parser(description).parse_args(argv)
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    findings: list[Finding] = []
    unreadable: list[str] = []
    failed: list[str] = []
    try:
        walked = list(walk_files(Path(args.path), source_only=source_only))
    except ScanPathError as exc:
        return fail_on_bad_path(exc)
    for filepath in walked:
        try:
            text = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warn_unreadable(filepath, exc)
            unreadable.append(str(filepath))
            continue
        reporter = Reporter(filepath, ignore)
        try:
            analyze(filepath, text, reporter)
        except Exception as exc:  # a detector bug must not read as a clean file
            warn_detector_error(filepath, exc)
            failed.append(str(filepath))
            continue
        findings.extend(reporter.findings)

    gaps = coverage_gaps(unreadable, failed)
    emit(findings, args.format, clean_message, completeness=gaps or None)
    return 0
