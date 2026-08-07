#!/usr/bin/env python3
"""Shared helpers for the code-overview scripts.

Everything here is about the *document set* — reading findings out of whatever
shape a doctor emitted, sizing a package, rendering the bundled templates, and
computing the relative links that hold the set together. The judgment about what
those findings are worth lives in rubric.py.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"

MAP_SCHEMA = "code-overview/1"
HEALTH_SCHEMA = "code-health/1"
META_BLOCK_ID = "code-health-meta"

# Extensions that count as source when sizing a package. Deliberately narrow:
# a LOC number that includes vendored JSON fixtures makes every density look
# better than it is, and density is what the grade is computed from.
CODE_EXTENSIONS = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".cs", ".rb", ".php", ".swift",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".ex", ".exs", ".dart", ".vue",
    ".svelte", ".sql", ".sh", ".bash",
})

# Directories never worth walking into. Vendored or generated, both of which
# would distort size and finding counts alike.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "vendor", "venv", ".venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox",
    "dist", "build", "target", "out", ".next", ".nuxt", ".svelte-kit", "coverage",
    "htmlcov", ".idea", ".vscode", "site-packages", ".gradle", "bin", "obj",
    ".terraform", "Pods", ".dart_tool", ".cache", "docs",
})


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def json_block(data) -> str:
    """Serialize for embedding in a <script> block.

    `</` is escaped because a `</script>` inside a JSON string would end the
    block early and spill the rest of the payload into the document body.
    """
    return json.dumps(data, separators=(",", ":"), sort_keys=False).replace("</", "<\\/")


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------

def normalize_findings(data) -> tuple[list[dict], dict[str, str]]:
    """Flatten any doctor's JSON into (findings, analyzer_errors).

    Three shapes in the wild: analyze_all.py's report (`categories` mapping),
    a single detector's `{"issues": [...]}`, and django-code-doctor's flat list.
    `analyzer_errors` matters as much as the findings — a category that crashed
    reports zero, and zero from a crashed detector means *unknown*, not *clean*.
    """
    findings: list[dict] = []
    errors: dict[str, str] = {}

    if isinstance(data, list):
        findings = [item for item in data if isinstance(item, dict)]
    elif isinstance(data, dict):
        errors = {
            str(k): str(v)
            for k, v in (data.get("meta", {}).get("analyzer_errors") or {}).items()
        }
        categories = data.get("categories")
        if isinstance(categories, dict):
            for name, payload in categories.items():
                issues = payload.get("issues", []) if isinstance(payload, dict) else []
                for issue in issues:
                    if isinstance(issue, dict):
                        issue.setdefault("category", name)
                        findings.append(issue)
        elif isinstance(data.get("issues"), list):
            findings = [item for item in data["issues"] if isinstance(item, dict)]

    for finding in findings:
        finding.setdefault("severity", "medium")
    return findings, errors


def load_findings(paths) -> tuple[list[dict], dict[str, str]]:
    """Read and merge several findings files. `-` reads stdin."""
    findings: list[dict] = []
    errors: dict[str, str] = {}
    for raw in paths:
        text = sys.stdin.read() if str(raw) == "-" else Path(raw).read_text(encoding="utf-8")
        if not text.strip():
            warn(f"{raw} is empty — treated as no findings")
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"error: {raw} is not valid JSON: {exc}") from exc
        part, part_errors = normalize_findings(data)
        findings.extend(part)
        errors.update(part_errors)
    return findings, errors


# --------------------------------------------------------------------------
# sizing
# --------------------------------------------------------------------------

def iter_code_files(roots, extensions=CODE_EXTENSIONS):
    """Yield source files under the given roots, skipping vendor/generated trees."""
    seen: set[Path] = set()
    for root in roots:
        root = Path(root)
        if root.is_file():
            if root.suffix in extensions:
                seen.add(root.resolve())
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix in extensions:
                    seen.add(path.resolve())
    yield from sorted(seen)


def measure(roots, extensions=CODE_EXTENSIONS) -> dict:
    """Files and non-blank lines under `roots`.

    Blank lines are excluded and comments are not, on purpose: stripping
    comments per language is a parser's job, and the number only has to be
    stable enough to divide by.
    """
    files = 0
    loc = 0
    for path in iter_code_files(roots, extensions):
        files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warn(f"could not read {path}: {exc}")
            continue
        loc += sum(1 for line in text.splitlines() if line.strip())
    return {"files": files, "loc": loc}


def git_sha(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"<!--([A-Z0-9_]+)-->")


def read_asset(name: str) -> str:
    path = ASSETS / name
    if not path.is_file():
        raise SystemExit(f"error: bundled asset {name} is missing from {ASSETS}")
    return path.read_text(encoding="utf-8")


def render(template: str, values: dict[str, str]) -> str:
    """Fill `<!--KEY-->` placeholders. Unfilled placeholders become empty.

    Values are inserted raw — callers escape anything derived from user or
    codebase text before it gets here. Leaving substitution dumb keeps the
    templates readable as HTML.
    """
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), template)


# --------------------------------------------------------------------------
# the embedded metadata block
# --------------------------------------------------------------------------

_META_RE = re.compile(
    r'<script[^>]*id="' + META_BLOCK_ID + r'"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def read_meta(path: Path) -> dict | None:
    """Pull the code-health metadata back out of a generated document."""
    if not Path(path).is_file():
        return None
    match = _META_RE.search(Path(path).read_text(encoding="utf-8"))
    if not match:
        return None
    try:
        return json.loads(match.group(1).replace("<\\/", "</"))
    except json.JSONDecodeError as exc:
        warn(f"{path} has a code-health block that is not valid JSON: {exc}")
        return None


# --------------------------------------------------------------------------
# the package map
# --------------------------------------------------------------------------

DOC_KINDS = ("summary", "codemap", "health")
DOC_TITLES = {"summary": "Summary", "codemap": "Code Map", "health": "Health"}


def load_map(path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"error: no package map at {path} — run discover_packages.py first")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: {path} is not valid JSON: {exc}") from exc
    packages = data.get("packages")
    if not isinstance(packages, list):
        raise SystemExit(f"error: {path} has no `packages` list")
    for package in packages:
        if not package.get("name") or not package.get("roots"):
            raise SystemExit(f"error: {path} has a package with no name or no roots: {package!r}")
        package.setdefault("docs", str(Path(package["roots"][0]) / "docs"))
    return data


def save_map(path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def doc_path(repo: Path, package: dict | None, kind: str) -> Path:
    """Where a document lives. `package=None` means the repo-level one."""
    base = Path(repo) / ("docs" if package is None else package["docs"])
    return base / f"{kind}.html"


def rel_href(from_doc: Path, to_doc: Path) -> str:
    """A relative href from one document to another, POSIX-separated.

    relpath rather than a URL join because the whole set has to work off
    `file://` — an absolute path would break the moment someone opens it from
    a checkout rather than a server.
    """
    relative = os.path.relpath(Path(to_doc).resolve(), Path(from_doc).resolve().parent)
    return Path(relative).as_posix()
