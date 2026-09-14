#!/usr/bin/env python3
"""Shared plumbing for the detector scripts.

Every detector needs the same four things: a way to enumerate TypeScript
files, the severity icons, a console that can print them, and one policy for
what to do when a file will not parse or a detector crashes. These were
copy-pasted per script; this module is the single copy.
"""

import argparse
import contextlib
import functools
import json
import os
import sys
from dataclasses import asdict, dataclass
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

# Declaration-file suffixes across all three module flavours.
DECLARATION_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")

# Filename markers that make a file a test. Used by several detectors to
# apply a different standard (a `console.log` in a test is not a leftover)
# and by find_untested_modules to tell the two halves of a repo apart.
TEST_DIR_NAMES = frozenset({"__tests__", "__test__", "test", "tests", "spec", "e2e", "cypress"})
TEST_NAME_MARKERS = (".test.", ".spec.", "-test.", "_test.")

# Files that mark the top of a project. Test-directory classification is
# scoped below the nearest one, so where a checkout lives cannot change what
# it reports.
ROOT_MARKERS = ("package.json", "tsconfig.json", ".git")


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


def walk_tree(path: Path, *, keep: frozenset[str] | set[str] = frozenset()) -> Iterator[Path]:
    """Yield every regular file under ``path``, pruning vendored/built directories.

    The excluded directories are pruned during the walk rather than filtered
    after it. `node_modules` holds tens of thousands of files, and `rglob`
    would traverse and materialize every one of them before the first was
    discarded — which is the difference between a fast scan and one that
    appears to hang on an installed checkout. Every other enumeration in this
    skill goes through here so the pruning rule has one home. ``keep`` names
    a directory that is otherwise in `EXCLUDE_DIRS` but should still be
    descended into here — e.g. a caller looking for coverage reports nested
    under a package's own `coverage/` directory in a monorepo.
    """
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        return
    for directory, subdirectories, names in os.walk(path):
        subdirectories[:] = sorted(d for d in subdirectories if d not in EXCLUDE_DIRS or d in keep)
        base = Path(directory)
        for name in sorted(names):
            yield base / name


def find_ts_files(path: Path) -> Iterator[Path]:
    """Yield the TypeScript files under ``path``, skipping vendored/built dirs."""
    for candidate in walk_tree(path):
        if candidate.suffix in TS_EXTENSIONS and candidate.is_file():
            yield candidate


@functools.lru_cache(maxsize=None)
def _root_of_dir(directory: Path) -> Path | None:
    """The nearest ancestor of (or equal to) ``directory`` holding a root marker.

    Cached on the resolved directory: the answer is a property of the directory,
    not of the file inside it, and every detector asks it of every file it
    touches. Without the cache a whole-tree run re-walks and re-stats the same
    ancestors once per file.

    A directory the process cannot stat or list (a `700` home directory on a
    shared host, a restrictive bind-mount, locked-down CI) raises `OSError`
    from `.exists()`/`.glob()` rather than reporting "not found"; such a
    candidate is treated as having no marker so the walk keeps going instead
    of crashing the whole detector run.
    """
    for candidate in (directory, *directory.parents):
        try:
            if any((candidate / marker).exists() for marker in ROOT_MARKERS):
                return candidate
            if any(candidate.glob("tsconfig*.json")):
                return candidate
        except OSError:
            continue
    return None


def project_root_of(filepath: Path) -> Path | None:
    """The nearest ancestor holding a package.json, a tsconfig, or .git; None if none."""
    return _root_of_dir(filepath.resolve().parent)


def is_test_file(filepath: Path) -> bool:
    """True when the path names a test file by directory or by suffix.

    Directory markers are matched *below the project root only*. Every
    component of an absolute path is the wrong scope: a checkout that happens
    to live under `/tmp/tests/` would have every one of its files classified
    as test code, silently suppressing the security and error-handling
    findings — so the same repo would report differently depending on where
    it was cloned. Without a root marker every component still counts, which
    is the old behaviour and the right one for a loose file.
    """
    name = filepath.name
    if any(marker in name for marker in TEST_NAME_MARKERS):
        return True
    root = project_root_of(filepath)
    if root is not None:
        try:
            parts = filepath.resolve().relative_to(root).parts
        except ValueError:
            parts = filepath.parts
    else:
        parts = filepath.parts
    return not TEST_DIR_NAMES.isdisjoint(p.lower() for p in parts)


def is_declaration_file(filepath: Path) -> bool:
    """True for `.d.ts`, `.d.mts`, `.d.cts` — types only, so most code detectors do not apply."""
    return filepath.name.endswith(DECLARATION_SUFFIXES)


# Markers that tools write into files they own. `@generated` is the convention
# Prettier, Jest and code-review tools honour; `DO NOT EDIT` is the
# protoc/openapi/go-style banner. Both are checked in the first five lines
# only, and matched exactly: "please do not edit this without asking" in a
# hand-written header must not silence a file's findings.
_GENERATED_MARKERS = ("@generated", "DO NOT EDIT")


def is_generated_file(filepath: Path) -> bool:
    """True when the file's header says a tool owns it."""
    try:
        with open(filepath, encoding="utf-8", errors="replace") as handle:
            head = [next(handle, "") for _ in range(5)]
    except OSError:
        return False
    return any(marker in line for line in head for marker in _GENERATED_MARKERS)


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
    # Tuples, not lists. `frozen=True` blocks reassignment but not in-place
    # mutation, so a list here would let `candidate.also_caused_by.clear()`
    # walk a validated record into a schema-invalid state that
    # __post_init__ never re-checks — and it would then serialise and emit
    # like any other record. __post_init__ below coerces any list handed to
    # the constructor (including one that came back out of json.loads,
    # which knows nothing about tuples) into a tuple, so the type is
    # actually enforced, not just annotated.
    also_caused_by: tuple[str, ...] = ()
    severity: str = "medium"
    kind: str = "finding"
    code_snippet: str = ""
    # Other lines that participate in the same finding. analyze_diff.py uses
    # these so a cross-declaration smell surfaces when any participant changed.
    related_lines: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "also_caused_by", tuple(self.also_caused_by))
        object.__setattr__(self, "related_lines", tuple(self.related_lines))
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

    def __init__(self, tsfile, ignore: set[str]):
        self.tsfile = tsfile
        self.ignore = ignore
        self.findings: list[Finding] = []

    def add(self, line: int, smell_type: str, description: str, suggestion: str,
            severity: str = "medium", related: list[int] | None = None) -> None:
        """A finding: the evidence proves a defect and names its fix."""
        self._add(Finding(
            file=str(self.tsfile.path), line=line, smell_type=smell_type,
            description=description, suggestion=suggestion, severity=severity,
            kind="finding", code_snippet=self.tsfile.snippet(line),
            related_lines=tuple(related or ()),
        ))

    def candidate(self, line: int, smell_type: str, description: str,
                  also_caused_by: list[str] | tuple[str, ...], severity: str = "low",
                  related: list[int] | None = None) -> None:
        """A candidate: a lead the syntax alone cannot prove, with the benign readings."""
        self._add(Finding(
            file=str(self.tsfile.path), line=line, smell_type=smell_type,
            description=description, also_caused_by=tuple(also_caused_by),
            severity=severity, kind="candidate", code_snippet=self.tsfile.snippet(line),
            related_lines=tuple(related or ()),
        ))

    def _add(self, record: Finding) -> None:
        if record.smell_type not in self.ignore:
            self.findings.append(record)


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


def print_findings(findings: list[Finding], clean_message: str) -> None:
    if not findings:
        print(f"✅ {clean_message}")
        return
    confirmed = [f for f in findings if f.kind == "finding"]
    leads = [f for f in findings if f.kind == "candidate"]
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.smell_type] = counts.get(finding.smell_type, 0) + 1
    print(f"{len(confirmed)} finding(s), {len(leads)} candidate(s):\n")
    print("Summary:")
    for smell, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {smell}: {count}")
    print()
    for finding in confirmed:
        icon = SEVERITY_ICONS.get(finding.severity, "")
        print(f"{icon} [{finding.severity.upper()}] {finding.file}:{finding.line}")
        print(f"   {finding.smell_type}: {finding.description}")
        if finding.code_snippet:
            print(f"   Code: {finding.code_snippet}")
        print(f"   → {finding.suggestion}\n")
    if leads:
        print("Candidates — unverified leads, check before acting:\n")
    for lead in leads:
        icon = SEVERITY_ICONS.get(lead.severity, "")
        print(f"{icon} [candidate] {lead.file}:{lead.line}")
        print(f"   {lead.smell_type}: {lead.description}")
        if lead.code_snippet:
            print(f"   Code: {lead.code_snippet}")
        print("   Also caused by:")
        for reason in lead.also_caused_by:
            print(f"     - {reason}")
        print()


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
    generated = 0
    for filepath in find_ts_files(Path(args.path)):
        if skip_declaration_files and is_declaration_file(filepath):
            continue
        if is_generated_file(filepath):
            generated += 1
            continue
        try:
            findings.extend(analyze(parse_file(filepath), ignore))
        except TsSyntaxError as exc:
            warn_unparseable(filepath, exc)
        except OSError as exc:
            warn_unparseable(filepath, exc)
        except Exception as exc:  # a detector bug must not read as a clean file
            warn_detector_error(filepath, exc)
    if generated:
        print(f"ℹ️  {generated} generated file(s) skipped (header says a tool owns them)",
              file=sys.stderr)
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
