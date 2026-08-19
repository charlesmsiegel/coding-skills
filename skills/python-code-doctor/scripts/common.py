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


# The last file read, as {path: (source, tree, error)}. Exactly one entry;
# see cached_parse. `source` is None when the read itself failed, `tree` is None
# when the file did not parse, and `error` carries whichever failure happened.
_LAST_PARSE: dict[str, tuple] = {}


def _read_and_parse(filepath: Path) -> tuple:
    key = str(filepath)
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return (None, None, exc)
    try:
        return (source, ast.parse(source, filename=key), None)
    except Exception as exc:
        return (source, None, exc)


def _cached(filepath: Path) -> tuple:
    """The one cached entry for ``filepath``, reading it if this is the first ask.

    Every detector used to read and parse every file for itself, so running the
    whole suite parsed each file once per detector. The runner instead asks all
    of them about one file before moving to the next, which makes the second and
    later callers for a file free.

    The cache deliberately holds **one** file. Keeping every tree would cost
    gigabytes on a large repository, and file-major order means nothing older is
    ever wanted again. A detector run on its own — one file at a time, all files
    — is therefore no worse than before, just no better.
    """
    key = str(filepath)
    if key not in _LAST_PARSE:
        entry = _read_and_parse(filepath)
        _LAST_PARSE.clear()
        _LAST_PARSE[key] = entry
    return _LAST_PARSE[key]


def cached_parse(filepath: Path) -> tuple[str, "ast.AST"]:
    """``(source, tree)`` for ``filepath``, sharing the runner's parse of it.

    A read or parse failure is re-raised exactly as a direct ``read_text`` or
    ``ast.parse`` would have raised it, so a caller's own ``except SyntaxError``
    still classifies the file the same way.
    """
    source, tree, error = _cached(filepath)
    if error is not None:
        raise error
    return source, tree


def cached_source(filepath: Path) -> str:
    """The text of ``filepath``, for detectors that read rather than parse.

    Raises only when the *read* failed. A file that will not parse still has
    text, and a detector that only needs the text must still see it.
    """
    source, _, error = _cached(filepath)
    if source is None:
        raise error
    return source


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
