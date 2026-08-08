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
    ".rs", ".scala", ".r", ".R", ".sql", ".sh", ".c", ".h", ".cc", ".cpp", ".cxx",
    ".hpp", ".cs", ".m", ".swift", ".php",
})
CONFIG_SUFFIXES = frozenset({".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".env"})
DOC_SUFFIXES = frozenset({".md", ".rst", ".txt"})

# `.env` and `.env.production` have suffix "" and ".production" respectively, so a
# suffix test never sees them — and thresholds, model ids, and feature flags live
# in them constantly. Matched by name instead.
CONFIG_NAME_PREFIXES = (".env",)


def is_config(path) -> bool:
    """Config by suffix or by name.

    One predicate, used by both the file walk and the site classifier: when only
    the walk knew about `.env`, a threshold in one was scanned and then reported
    as a code definition.
    """
    path = Path(path)
    return path.suffix in CONFIG_SUFFIXES or path.name.startswith(CONFIG_NAME_PREFIXES)


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


def wanted(path: Path, suffixes) -> bool:
    if path.suffix in suffixes:
        return True
    return CONFIG_SUFFIXES <= suffixes and is_config(path)


def iter_files(root: Path, suffixes) -> list:
    """Every file under `root` with one of `suffixes`, sorted, vendored trees skipped."""
    root = Path(root)
    if root.is_file():
        return [root] if wanted(root, suffixes) else []
    found = []
    for path in root.rglob("*"):
        if not path.is_file() or not wanted(path, suffixes):
            continue
        if EXCLUDE_DIRS.isdisjoint(path.relative_to(root).parts):
            found.append(path)
    return sorted(found)


def read_source(path: Path) -> tuple:
    """(lines, skip_reason). A skipped file is never silently reported as clean.

    Returning the reason rather than a bare [] is the point: a 3 MB generated
    scoring module read as zero lines would otherwise show up inside
    `files_scanned` and produce a "no candidates found" headline over code nobody
    looked at — which is the exact silent-cap failure this skill audits for.
    """
    try:
        size = path.stat().st_size
        if size > MAX_SOURCE_BYTES:
            return [], "larger than " + str(MAX_SOURCE_BYTES // 1_000_000) + "MB (" + str(size) + " bytes)"
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return [], "unreadable (" + type(exc).__name__ + ")"
    if path.suffix == ".ipynb":
        return notebook_source(text)
    return text.splitlines(), None


def notebook_source(text: str) -> tuple:
    """A notebook's code-cell source, as lines, plus per-line cell locations.

    `.ipynb` is JSON: read as raw text its code sits inside quoted strings, where
    string masking hides it and a metrics notebook reports no metrics at all,
    while markdown prose and stored outputs read as source to every scanner.

    The locations matter as much as the lines. A row pointing at `nb.ipynb:7`
    cannot be opened — the file may be one physical line — so each extracted line
    carries the cell it came from, and scanners report `nb.ipynb#cell2:3`.
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return [], "unreadable notebook (" + type(exc).__name__ + ")"
    lines, locations, index = [], [], 0
    for cell in payload.get("cells", []) if isinstance(payload, dict) else []:
        if not isinstance(cell, dict):
            continue
        if cell.get("cell_type") != "code":
            index += 1
            continue
        source = cell.get("source") or []
        if isinstance(source, str):
            source = source.splitlines()
        if not isinstance(source, list):
            # `{"source": 42}` is a valid JSON notebook and an invalid cell. Iterating
            # it raised TypeError out of read_source and killed the entire scan, so a
            # single corrupt cell took the whole audit down with it.
            return [], "unreadable notebook (cell source is not text)"
        for offset, line in enumerate(source, 1):
            lines.append(str(line).rstrip("\n"))
            locations.append(("cell" + str(index), offset))
        index += 1
    return lines, None, locations


def locate(display: str, lineno: int, locations) -> tuple:
    """(file, line) for a row, expanded to a cell reference inside a notebook."""
    if not locations or lineno > len(locations):
        return display, lineno
    cell, offset = locations[lineno - 1]
    return display + "#" + cell, offset


def relocate(rows: list, locations_by_file: dict) -> list:
    """Rewrite notebook rows to cell references, once, at the end of a scan.

    Purely presentational, and deliberately last: the scanners index lines the way
    they read them, and only the reported location needs to be something an
    auditor can actually open.
    """
    if not locations_by_file:
        return rows
    for row in rows:
        locations = locations_by_file.get(row["file"])
        if locations:
            row["file"], row["line"] = locate(row["file"], row["line"], locations)
    return rows


def unpack_source(result: tuple) -> tuple:
    """(lines, reason, locations) from read_source, whatever arity it returned."""
    if len(result) == 3:
        return result
    lines, reason = result
    return lines, reason, None


def skipped_note(skipped: list) -> str:
    """A sentence naming the files no scan actually looked at, or ''."""
    if not skipped:
        return ""
    shown = ", ".join(name + " — " + reason for name, reason in skipped[:3])
    return (" " + str(len(skipped)) + " file(s) were NOT read (" + shown
            + "), so this result does not cover them.")


def split_comment(line: str) -> tuple:
    """(code, comment), split at the first `#` or `//` that is outside a string.

    A regex cannot do this: `log("https://errors.example"); return 0;` has a `//`
    inside a string literal, and cutting there discarded the `return 0` and
    produced a clean report on a handler that scores its failures.
    """
    quote, index = None, 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" or (char == "/" and line[index + 1:index + 2] == "/"):
            return line[:index], line[index:]
        index += 1
    return line, ""


def mask_strings(line: str, filler: str = "\u00b7") -> str:
    """String *bodies* replaced by a filler, quotes and length preserved.

    Lets a pattern run over code without matching text that only appears inside a
    message: `message = "quality_score > 0.8"` is not a threshold.
    """
    out, quote = [], None
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\" and index + 1 < len(line):
                out.append(filler * 2)
                index += 2
                continue
            if char == quote:
                quote = None
                out.append(char)
            else:
                out.append(filler)
        elif char in "\"'":
            quote = char
            out.append(char)
        else:
            out.append(char)
        index += 1
    return "".join(out)


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
