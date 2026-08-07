#!/usr/bin/env python3
"""Shared plumbing for the measurement-investigation scripts.

Every script here answers one mechanical question and emits the same envelope:

  {"tool", "root", "headline", "caveat", "counts", "candidates": [...]}

`headline` is the script's most likely finding, `caveat` is what it structurally
cannot see, and every row in `candidates` is a *candidate* — a hypothesis to
confirm by reading the code, never a finding. That distinction is the whole
discipline of the skill, so it is enforced by the data shape rather than left to
the prose: each row carries its own `confirm` instruction.

Stdlib only, no network, no imports from any sibling skill.
"""

import os
import sys
import json
import contextlib
from pathlib import Path

# Vendored, generated, and cache trees. Measurement code does not live here, and
# node_modules alone would swamp every count the scripts report.
EXCLUDE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".tox", ".nox", "build", "dist", ".eggs", "site-packages", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "htmlcov", ".idea", ".vscode", "target",
    "vendor", ".next", ".nuxt", "coverage", ".terraform",
})

# Where measurement code lives, across the languages this has to work in.
CODE_SUFFIXES = frozenset({
    ".py", ".ipynb", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".kt", ".rb",
    ".rs", ".scala", ".r", ".R", ".sql", ".sh",
})
CONFIG_SUFFIXES = frozenset({".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".env"})
DOC_SUFFIXES = frozenset({".md", ".rst", ".txt"})

# A file big enough to be data rather than source. Reading it as source produces
# thousands of candidates from one artifact, which buries everything else.
MAX_SOURCE_BYTES = 2_000_000


def configure_output():
    """UTF-8 out, and never crash on a console that cannot encode a character."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def add_common_args(parser, root_help="directory or file to scan (default: .)"):
    parser.add_argument("root", nargs="?", default=".", help=root_help)
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="text for reading, json for piping (same content)")
    parser.add_argument("--limit", type=int, default=60,
                        help="maximum candidate rows to emit (default: 60)")
    return parser


def iter_files(root: Path, suffixes) -> list:
    """Every file under `root` with one of `suffixes`, sorted, vendored trees skipped."""
    root = Path(root)
    if root.is_file():
        return [root] if root.suffix in suffixes else []
    found = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if EXCLUDE_DIRS.isdisjoint(path.relative_to(root).parts):
            found.append(path)
    return sorted(found)


def read_lines(path: Path) -> list:
    """The file's lines, or [] if it cannot be read as text.

    Unreadable is not the same as clean, so the caller is told: an unreadable file
    is counted as skipped and reported in `counts`, never silently dropped.
    """
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def rel(path: Path, root: Path) -> str:
    """Display path: relative to the scanned root where possible."""
    root = Path(root)
    base = root if root.is_dir() else root.parent
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return str(path)


def candidate(kind: str, file: str, line: int, detail: str, confirm: str, evidence: str = "") -> dict:
    """One hypothesis. `confirm` says what reading would turn it into a finding."""
    return {
        "kind": kind,
        "file": file,
        "line": line,
        "detail": detail,
        "evidence": evidence.strip()[:200],
        "confirm": confirm,
        "status": "candidate",
    }


def envelope(tool: str, root, headline: str, caveat: str, counts: dict, candidates: list) -> dict:
    return {
        "tool": tool,
        "root": str(root),
        "headline": headline,
        "caveat": caveat,
        "counts": counts,
        "candidates": candidates,
    }


def emit(payload: dict, fmt: str) -> None:
    """Print the envelope, surviving a downstream `| head` closing the pipe.

    Without this, piping into `head` prints a BrokenPipeError traceback over the
    output — which reads exactly like the script crashed, and an agent auditing
    measurement should not have to distinguish a real failure from a closed pipe.
    """
    try:
        render(payload, fmt)
        sys.stdout.flush()
    except BrokenPipeError:
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def render(payload: dict, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(payload, indent=2))
        return

    print("== " + payload["tool"] + " over " + payload["root"])
    print("")
    print("HEADLINE  " + payload["headline"])
    print("CAVEAT    " + payload["caveat"])
    print("")
    counts = payload.get("counts") or {}
    if counts:
        print("counts: " + ", ".join(str(k) + "=" + str(v) for k, v in counts.items()))
        print("")

    rows = payload.get("candidates") or []
    if not rows:
        print("No candidates. That is not a clean bill of health — read the caveat.")
        return

    print(str(len(rows)) + " candidate(s) — each is a hypothesis to confirm by reading:")
    last_kind = None
    for row in rows:
        if row["kind"] != last_kind:
            print("")
            print("-- " + row["kind"])
            last_kind = row["kind"]
        location = row["file"] + ":" + str(row["line"])
        print("  " + location + "  " + row["detail"])
        if row.get("evidence"):
            print("      | " + row["evidence"])
        print("      confirm: " + row["confirm"])
