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


def warn_unparseable(filepath: Path, exc: Exception) -> None:
    """Note a file that will not parse — expected for broken or non-Python files."""
    print(f"⚠️  {filepath}: skipped, does not parse ({exc})", file=sys.stderr)


def warn_detector_error(filepath: Path, exc: Exception) -> None:
    """Surface a detector crash instead of silently reporting the file clean."""
    print(
        f"⚠️  {filepath}: detector failed ({type(exc).__name__}: {exc}); "
        "findings for this file are incomplete, not clean",
        file=sys.stderr,
    )
