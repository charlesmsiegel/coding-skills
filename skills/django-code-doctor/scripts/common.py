#!/usr/bin/env python3
"""Shared plumbing for the detector scripts.

Every detector needs the same four things: a way to enumerate Python files,
the severity icons, a console that can print them, and one policy for what to
do when a file will not parse or a detector crashes. These were copy-pasted
per script; this module is the single copy.
"""

import ast
import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# Severity → icon, in report order.
SEVERITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}

# Directory names that are never the user's own code. Matched against path
# segments *below* the scanned root, so a repo that happens to live inside a
# directory with one of these names is still scanned.
EXCLUDE_DIRS = frozenset({
    ".venv", "venv", "node_modules", "__pycache__", ".git", ".hg",
    ".tox", ".nox", "build", "dist", ".eggs", "site-packages",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
})


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


def find_python_files(path: Path) -> Iterator[Path]:
    """Yield the .py files under ``path``, skipping vendored/generated dirs."""
    if path.is_file() and path.suffix == ".py":
        yield path
    elif path.is_dir():
        for p in path.rglob("*.py"):
            if EXCLUDE_DIRS.isdisjoint(p.relative_to(path).parts):
                yield p


class _ParseCache:
    """The last file read, held only until a different one is asked for.

    Every detector used to read and parse every file for itself, so running the
    whole suite parsed each file once per detector — on a large repository, most
    of the runtime. The runner instead asks every detector about one file before
    moving to the next, so the second and later callers for a file get the
    first one's work.

    Holding **one** file is the whole design, not a limitation. Keeping every
    tree would cost gigabytes on a large repository, and file-major order never
    wants an older one back. A detector run on its own — one file at a time,
    all files — is therefore no worse than it was, just no better.

    A read or parse failure is remembered alongside the text so it can be
    re-raised rather than retried, and so `source()` can still answer for a file
    that will not parse.
    """

    def __init__(self) -> None:
        self._path: str | None = None
        self._source: str | None = None
        self._tree = None
        self._error: Exception | None = None

    def _load(self, filepath: Path) -> None:
        key = str(filepath)
        if key == self._path:
            return
        self._path, self._source, self._tree, self._error = key, None, None, None
        try:
            self._source = filepath.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # re-raised by whichever accessor was asked
            self._error = exc
            return
        try:
            self._tree = ast.parse(self._source, filename=key)
        except Exception as exc:
            self._error = exc

    def parse(self, filepath: Path) -> tuple[str, "ast.AST"]:
        """``(source, tree)``, re-raising exactly what a direct parse would raise."""
        self._load(filepath)
        if self._error is not None:
            raise self._error
        return self._source, self._tree

    def source(self, filepath: Path) -> str:
        """The text alone. Raises only when the *read* failed."""
        self._load(filepath)
        if self._source is None:
            raise self._error
        return self._source


_PARSE_CACHE = _ParseCache()


def cached_parse(filepath: Path) -> tuple[str, "ast.AST"]:
    """``(source, tree)`` for ``filepath``, sharing the runner's parse of it.

    A read or parse failure is re-raised exactly as a direct ``read_text`` or
    ``ast.parse`` would have raised it, so a caller's own ``except SyntaxError``
    still classifies the file the same way.
    """
    return _PARSE_CACHE.parse(filepath)


def cached_source(filepath: Path) -> str:
    """The text of ``filepath``, for detectors that read rather than parse.

    A file that will not parse still has text, and a detector that only needs
    the text must still see it.
    """
    return _PARSE_CACHE.source(filepath)


def sort_findings(findings: list) -> list:
    """The order a report is read in: severity first, then file, then line.

    Every detector but one sorts this way, and each used to spell the key out in
    its own main(). One copy, so the runner and a detector's own CLI cannot
    drift into ordering the same findings differently.
    """
    findings.sort(key=lambda f: (f.severity != "high", f.severity != "medium",
                                 f.file, f.line))
    return findings


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


# The keys Python detectors use for the same three ideas. `format_findings.py`
# and `analyze_all.py` already read these alternatives; the validator accepts
# the same spellings so a detector need not be rewritten to be checked.
_TYPE_KEYS = ("smell_type", "issue_type", "pattern_type", "type")


def _severity_of(record: dict) -> str:
    if record.get("severity"):
        return str(record["severity"])
    confidence = record.get("confidence")
    if isinstance(confidence, (int, float)):
        return "high" if confidence >= 90 else ("medium" if confidence >= 70 else "low")
    return "medium"


def validate_record(record: dict) -> dict:
    """Check one detector record against the finding/candidate contract.

    Raises SchemaError when a finding has no fix or a candidate has no benign
    explanation. Returns a copy of the record with `kind` made explicit, so a
    consumer never has to guess what an absent key meant. The argument is not
    mutated.
    """
    smell = next((str(record[key]) for key in _TYPE_KEYS if record.get(key)), "issue")
    suggestion = str(record.get("suggestion") or record.get("after") or "")
    kind = str(record.get("kind") or "finding")
    Finding(
        file=str(record.get("file", "")), line=int(record.get("line") or 1),
        smell_type=smell, description=str(record.get("description", "")),
        suggestion=suggestion, severity=_severity_of(record), kind=kind,
        also_caused_by=tuple(record.get("also_caused_by") or ()),
    )
    return {**record, "kind": kind}


class _SaidOnce:
    """Remembers what has already been said, so it is not said again.

    The runner asks every detector about one file, and without this each of them
    announces the same unreadable file: one broken file produced 28 identical
    lines and buried the report under them. A detector run on its own sees each
    file once, so its output is unchanged either way.
    """

    def __init__(self) -> None:
        self._said: set[str] = set()

    def first_time(self, key: str) -> bool:
        if key in self._said:
            return False
        self._said.add(key)
        return True


_UNPARSEABLE_ANNOUNCED = _SaidOnce()


def warn_unparseable(filepath: Path, exc: Exception) -> None:
    """Note a file that will not parse — expected for broken or non-Python files.

    Said once per file per process, however many detectors trip over it. Which
    detector noticed first is not information; that the file was skipped is.
    """
    if _UNPARSEABLE_ANNOUNCED.first_time(str(filepath)):
        print(f"⚠️  {filepath}: skipped, does not parse ({exc})", file=sys.stderr)


def warn_detector_error(filepath: Path, exc: Exception) -> None:
    """Surface a detector crash instead of silently reporting the file clean."""
    print(
        f"⚠️  {filepath}: detector failed ({type(exc).__name__}: {exc}); "
        "findings for this file are incomplete, not clean",
        file=sys.stderr,
    )
