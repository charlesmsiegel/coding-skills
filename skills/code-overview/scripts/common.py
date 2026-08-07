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

# Markup the doctors analyze but that is not "source" by default. Django's
# detectors report `missing_csrf_token` and `query_in_template` from .html, so
# when such findings are present those lines are part of what was analyzed and
# have to be in the denominator — otherwise a template-heavy package divides
# template findings by Python lines alone and scores far worse than it is.
# Counted only when the findings show they were analyzed; see sizing_extensions.
TEMPLATE_EXTENSIONS = frozenset({".html", ".htm", ".jinja", ".jinja2", ".j2", ".twig"})

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

# What a findings file's JSON shape tells us about how much was examined.
#
#   full     analyze_all.py's report — an envelope naming the analyzers that
#            ran, so coverage is evidence rather than assumption
#   partial  everything else: a bare JSON list, or a single detector's
#            {"issues": [...]}. Neither says what was examined.
#
# There is deliberately no "flat means the whole doctor ran" shape. A bare list
# is what `analyze_django.py` emits *and* what every bundled single detector
# emits — `find_duplicates.py --format json` prints `[]` — so the shape cannot
# tell a full Django run apart from one detector that found nothing. Inferring
# the doctor's whole profile from it graded an empty one-detector run as an A+
# in all seven categories. When a bare list really does cover the rubric, say so
# with --covers; nothing else can know.
SHAPE_FULL, SHAPE_PARTIAL = "full", "partial"


def normalize_findings(data) -> dict:
    """Flatten any doctor's JSON into one report record.

    Returns `{findings, errors, ran, skipped, shape}`. The non-finding fields
    matter as much as the findings and for the same reason: an analyzer that
    crashed, one that was skipped, and one that was never part of the run all
    report zero, and zero from an analyzer that did not look means *unknown*,
    not *clean*.
    """
    findings: list[dict] = []
    errors: dict[str, str] = {}
    skipped: set[str] = set()
    ran: set[str] = set()
    shape = SHAPE_PARTIAL

    if isinstance(data, list):
        findings = [item for item in data if isinstance(item, dict)]
    elif isinstance(data, dict):
        meta = data.get("meta", {}) if isinstance(data.get("meta"), dict) else {}
        errors = {str(k): str(v) for k, v in (meta.get("analyzer_errors") or {}).items()}
        skipped = {str(name) for name in (meta.get("analyzers_skipped") or [])}
        categories = data.get("categories")
        if isinstance(categories, dict):
            shape = SHAPE_FULL
            for name, payload in categories.items():
                issues = payload.get("issues", []) if isinstance(payload, dict) else []
                for issue in issues:
                    if isinstance(issue, dict):
                        issue.setdefault("category", name)
                        findings.append(issue)
            listed = meta.get("analyzers_run")
            ran = ({str(name) for name in listed} if isinstance(listed, list) and listed
                   else set(categories))
            # An analyzer that appears as a category but is absent from
            # analyzers_run never produced its section.
            skipped |= set(categories) - ran
            ran -= set(errors) | skipped
        elif isinstance(data.get("issues"), list):
            findings = [item for item in data["issues"] if isinstance(item, dict)]

    for finding in findings:
        finding.setdefault("severity", "medium")
    return {"findings": findings, "errors": errors, "ran": ran,
            "skipped": skipped, "shape": shape}


def finding_identity(finding: dict) -> tuple:
    """What makes two findings the same defect: same place, same kind.

    The *whole* path, not the basename. Merging on basename would collapse
    `src/a/models.py:3` and `src/b/models.py:3` — two different defects that
    monorepos produce constantly — into one. Different path spellings for the
    same file (absolute vs relative) instead fail to merge, which only leaves a
    duplicate counted twice; that is the safe direction to be wrong in.
    """
    for key in ("smell_type", "issue_type", "pattern_type", "type"):
        if finding.get(key):
            kind = str(finding[key])
            break
    else:
        kind = "issue"
    path = str(finding.get("file", ""))
    normalized = Path(path).as_posix().removeprefix("./") if path else ""
    return (normalized, finding.get("line"), kind)


_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def dedupe(reports: list[dict]) -> tuple[list[dict], int]:
    """Merge companion doctors' reports without double-charging one defect.

    Companion doctors overlap on purpose — django-code-doctor and
    python-code-doctor both flag a hardcoded `SECRET_KEY` at the same file and
    line, at different severities. Counting it twice charges the grade ~13
    weighted points for one defect instead of 10, and the recommended workflow
    runs both, so this is the normal case rather than an edge one.

    Deduplication is strictly **across** reports, never within one. A single
    detector can legitimately emit two findings that share file, line and type —
    Django's template detector reports one `hardcoded_url_in_template` per link,
    so a line with two links yields two findings that differ only in their
    description. Collapsing those would understate both the count and the
    penalty. So an identity's multiplicity in the merged set is the *maximum*
    any one report gave it, not the sum: two-from-django plus one-from-python
    stays two, and one-plus-one becomes one.
    """
    order: list[tuple] = []
    seen: set[tuple] = set()
    per_identity: dict[tuple, list[dict]] = {}
    total = 0

    for report in reports:
        grouped: dict[tuple, list[dict]] = {}
        for finding in report["findings"]:
            total += 1
            identity = finding_identity(finding)
            # `order` records first appearance once per identity — appending per
            # occurrence would emit that identity's whole group again for each
            # duplicate, multiplying rather than merging.
            if identity not in seen:
                seen.add(identity)
                order.append(identity)
            grouped.setdefault(identity, []).append(finding)
        for identity, group in grouped.items():
            kept = per_identity.get(identity, [])
            if len(group) > len(kept):
                per_identity[identity] = group
            elif len(group) == len(kept):
                # Same multiplicity from both: keep whichever call is harsher,
                # so a medium and a high for one defect scores as a high.
                worst = min(_SEVERITY_RANK.get(str(f.get("severity")), 1) for f in group)
                current = min(_SEVERITY_RANK.get(str(f.get("severity")), 1) for f in kept)
                if worst < current:
                    per_identity[identity] = group

    merged = [finding for identity in order for finding in per_identity[identity]]
    return merged, total - len(merged)


def load_reports(paths) -> tuple[list[dict], dict[str, str], set[str]]:
    """Read several findings files into per-report records. `-` reads stdin.

    Deliberately does **not** merge or deduplicate. The caller scopes each
    report to the unit being documented first, because deduplicating before
    scoping lets a package page report duplicates that were merged in a
    different package entirely.
    """
    reports: list[dict] = []
    errors: dict[str, str] = {}
    for raw in paths:
        try:
            text = sys.stdin.read() if str(raw) == "-" else Path(raw).read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"error: cannot read findings file {raw}: {exc}") from exc
        if not text.strip():
            # A zero-byte file is what a doctor leaves behind when it fails
            # *after* the shell created the redirect target. Dropping it would
            # leave no report at all, and coverage resolution would then fall
            # back to the doctor's profile and grade the whole rubric A+ off an
            # artifact that contains nothing. Record it as an unknown-coverage
            # report instead, so it refuses to grade rather than grading clean.
            warn(f"{raw} is empty — no findings and no evidence of what was examined, so "
                 "nothing from it can be graded. Did the doctor fail after the shell "
                 "created the file?")
            reports.append({"findings": [], "errors": {}, "ran": set(),
                            "skipped": set(), "shape": SHAPE_PARTIAL})
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"error: {raw} is not valid JSON: {exc}") from exc
        report = normalize_findings(data)
        reports.append(report)
        errors.update(report["errors"])

    # Skipped means skipped *everywhere*: a detector one report omitted and
    # another ran is covered. Coverage itself is computed per report, so this
    # is only for reporting.
    ran_anywhere = {name for report in reports for name in report["ran"]}
    skipped = {name for report in reports for name in report["skipped"]} - ran_anywhere
    return reports, errors, skipped


def sizing_extensions(findings: list[dict], extra=()) -> frozenset:
    """Which extensions the denominator should cover.

    Code always, plus any template extension the findings themselves show was
    analyzed. Deriving it from the findings keeps the invariant self-correcting:
    lines enter the denominator exactly when the analysis reached them, so a
    Django package with template findings is sized over its templates and a
    Python package that happens to ship an HTML fixture is not.
    """
    used = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extra}
    for finding in findings:
        suffix = Path(str(finding.get("file", ""))).suffix.lower()
        if suffix in TEMPLATE_EXTENSIONS:
            used.add(suffix)
    return frozenset(CODE_EXTENSIONS | used)


def within(path: str, roots: list[Path], base: Path) -> bool:
    """Is `path` inside any of `roots`? Used to partition repo-wide findings.

    A relative path is resolved against `base`, not the working directory: the
    doctors echo back whatever path they were invoked with, so a findings file
    can hold either form, and resolving a repo-relative path against wherever
    this script happens to be running would put every finding out of scope.
    """
    if not path:
        return False
    try:
        candidate = Path(path)
        resolved = (candidate if candidate.is_absolute() else Path(base) / candidate).resolve()
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


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
    seen: set[str] = set()
    for package in packages:
        if not package.get("name") or not package.get("roots"):
            raise SystemExit(f"error: {path} has a package with no name or no roots: {package!r}")
        # The name is both the identity used to match packages across scripts and
        # the label a reader clicks in the nav. Two packages called `api` — easy
        # to get from two manifests — would silently lose every link between
        # them and could point a grade row at the wrong package, so this is
        # rejected rather than worked around.
        if package["name"] in seen:
            raise SystemExit(
                f"error: {path} has two packages named {package['name']!r} — names are the "
                "identity used to link documents together and the label a reader sees, so "
                "they have to be unique; rename one (e.g. to its path)"
            )
        seen.add(package["name"])
        package.setdefault("docs", str(Path(package["roots"][0]) / "docs"))

    # Two packages writing to one docs directory is silent data loss: the second
    # build overwrites the first's health and summary pages, and navigation then
    # walks the same three files twice, so only the last package survives.
    by_docs: dict[str, str] = {}
    for package in packages:
        # normpath so `src/a/docs` and `src/a/../a/docs` are recognized as one
        # directory. Symlinked aliases still slip through — resolving those
        # needs the repo root and a filesystem that already has the tree.
        resolved = Path(os.path.normpath(package["docs"])).as_posix().rstrip("/")
        # `docs` is the repository's own document directory. A package may only
        # claim it by *being* the repository; otherwise the root build would
        # overwrite that package's pages and every roll-up would drop it.
        if resolved == "docs" and not all(is_repo_root(r) for r in package["roots"]):
            raise SystemExit(
                f"error: {path} points {package['name']!r} at the repository's own docs "
                f"directory while it is rooted at {', '.join(package['roots'])}. Only a "
                'package covering the whole repo (roots: ["."]) may do that; give this one '
                "its own docs directory."
            )
        if resolved in by_docs:
            raise SystemExit(
                f"error: {path} points {package['name']!r} and {by_docs[resolved]!r} at the same "
                f"docs directory ({resolved}) — the second build would overwrite the first; "
                "give each package its own"
            )
        by_docs[resolved] = package["name"]
    return data


def is_repo_root(path: str) -> bool:
    """Does this repo-relative path denote the repository root itself?"""
    return os.path.normpath(path or ".") in {".", "", os.sep}


def is_root_collapsed(repo: Path, package: dict) -> bool:
    """Is this package's document set the repository's own?

    True only for the documented single-package map — `roots: ["."]` *and*
    `docs: "docs"`. Both halves matter: a package rooted at `src/api` that
    merely points `docs` at the repo's directory is not collapsed, it is
    misconfigured, and treating it as collapsed would drop it from navigation
    and every roll-up while its pages were silently overwritten by the root
    build. `load_map` rejects that combination outright.
    """
    docs_is_root = (Path(repo) / package.get("docs", "")).resolve() == (Path(repo) / "docs").resolve()
    roots_are_root = all(is_repo_root(root) for root in package.get("roots", []))
    return docs_is_root and roots_are_root


def listed_packages(repo: Path, packages: list[dict]) -> list[dict]:
    """Packages that genuinely have their own document set."""
    return [p for p in packages if not is_root_collapsed(repo, p)]


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
