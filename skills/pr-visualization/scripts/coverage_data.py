"""Discover and parse test-coverage artifacts in a repo. Stdlib only.

Shared by both visualization skills: code-visualization renders an atlas
Coverage tab from it; pr-visualization annotates changed files with their
current coverage. Supported formats:

  - Cobertura XML (coverage.py's `coverage xml`, pytest-cov, many CI tools)
  - LCOV (lcov.info / coverage.lcov — JS/TS via nyc/c8/jest, C/C++)
  - Go cover profiles (coverage.out from `go test -coverprofile`)

Artifacts that exist but need a conversion step (.coverage sqlite, htmlcov/,
.nyc_output/) are reported as hints instead of parsed — the caller can tell
the user exactly which command produces a parseable file.
"""
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# name -> parser kind, in preference order when several artifacts exist
PARSEABLE = [
    ("coverage.xml", "cobertura"),
    ("cobertura.xml", "cobertura"),
    ("cobertura-coverage.xml", "cobertura"),
    ("lcov.info", "lcov"),
    ("coverage.lcov", "lcov"),
    ("coverage.out", "go"),
]
# artifact name -> the command that turns it into something parseable
HINTS = {
    ".coverage": "run `coverage xml` where .coverage lives to produce coverage.xml",
    "htmlcov": "htmlcov/ is rendered output; re-run `coverage xml` for a parseable coverage.xml",
    ".nyc_output": "run `npx nyc report --reporter=lcov` to produce coverage/lcov.info",
}

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist", "build"}


def discover(repo: Path, max_depth: int = 4):
    """Return (parseable: [(Path, kind)], hints: [str]) found under repo."""
    found, hints = [], []
    root_depth = len(repo.parts)
    for p in repo.rglob("*"):
        if len(p.parts) - root_depth > max_depth:
            continue
        if any(part in _SKIP_DIRS for part in p.relative_to(repo).parts[:-1]):
            continue
        name = p.name.lower()
        for known, kind in PARSEABLE:
            if name == known and p.is_file():
                found.append((p, kind))
        if p.name in HINTS:
            hints.append(f"found {p.relative_to(repo)} — {HINTS[p.name]}")
    order = {name: i for i, (name, _) in enumerate(PARSEABLE)}
    found.sort(key=lambda fk: order.get(fk[0].name.lower(), 99))
    return found, hints


def parse(path: Path, kind: str) -> dict:
    """Parse one artifact into {raw_path: (covered_lines, total_lines)}."""
    if kind == "cobertura":
        return _parse_cobertura(path)
    if kind == "lcov":
        return _parse_lcov(path)
    if kind == "go":
        return _parse_go(path)
    return {}


def _parse_cobertura(path: Path) -> dict:
    out = {}
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return out
    for cls in root.iter("class"):
        fname = cls.get("filename")
        if not fname:
            continue
        covered = total = 0
        for line in cls.iter("line"):
            total += 1
            if int(line.get("hits", "0") or 0) > 0:
                covered += 1
        if total:
            prev = out.get(fname, (0, 0))
            out[fname] = (prev[0] + covered, prev[1] + total)
    return out


def _parse_lcov(path: Path) -> dict:
    out = {}
    cur, covered, total = None, 0, 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if line.startswith("SF:"):
            cur, covered, total = line[3:].strip(), 0, 0
        elif line.startswith("DA:") and cur is not None:
            total += 1
            parts = line[3:].split(",")
            if len(parts) >= 2 and parts[1].strip() not in ("0", "-"):
                covered += 1
        elif line.startswith("end_of_record") and cur is not None:
            if total:
                out[cur] = (covered, total)
            cur = None
    return out


_GO_LINE = re.compile(r"^(?P<file>[^:]+):\d+\.\d+,\d+\.\d+ (?P<stmts>\d+) (?P<count>\d+)$")


def _parse_go(path: Path) -> dict:
    counts = defaultdict(lambda: [0, 0])  # file -> [covered_stmts, total_stmts]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    for line in text.splitlines():
        m = _GO_LINE.match(line.strip())
        if not m:
            continue
        stmts = int(m.group("stmts"))
        counts[m.group("file")][1] += stmts
        if int(m.group("count")) > 0:
            counts[m.group("file")][0] += stmts
    return {f: (c, t) for f, (c, t) in counts.items() if t}


def resolve_paths(cov: dict, repo_files: list) -> dict:
    """Map artifact paths (often absolute, or relative to some other root)
    onto repo-relative paths by exact then unique-suffix match."""
    by_suffix = defaultdict(list)
    repo_set = set(repo_files)
    for rel in repo_files:
        parts = rel.split("/")
        for i in range(len(parts)):
            by_suffix["/".join(parts[i:])].append(rel)
    out = {}
    for raw, val in cov.items():
        norm = raw.replace("\\", "/").lstrip("./")
        if norm in repo_set:
            out[norm] = val
            continue
        parts = norm.split("/")
        for i in range(len(parts)):
            cands = by_suffix.get("/".join(parts[i:]), [])
            if len(cands) == 1:
                out[cands[0]] = val
                break
    return out


def artifact_age_days(path: Path) -> float:
    """How old the artifact is — stale coverage silently misleads."""
    try:
        return (time.time() - path.stat().st_mtime) / 86400.0
    except OSError:
        return -1.0
