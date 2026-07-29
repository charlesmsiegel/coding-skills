#!/usr/bin/env python3
"""Shared plumbing for the detector scripts.

Every detector needs the same four things: a way to enumerate Python files,
the severity icons, a console that can print them, and one policy for what to
do when a file will not parse or a detector crashes. These were copy-pasted
per script; this module is the single copy.
"""

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
