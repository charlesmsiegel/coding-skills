#!/usr/bin/env python3
"""Shared plumbing for the code-doctor detectors.

This skill is language-blind: it has no parsers, no comment-syntax tables, and
no framework knowledge. That buys it the ability to review a repo written in
anything, and it costs it the ability to prove most of what it observes — so
the finding/candidate split below is the load-bearing part of this module, not
a formality.
"""

import contextlib
import os
import sys
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
