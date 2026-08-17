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
from urllib.parse import quote

import rubric

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
#
# `bin` is deliberately NOT here, though `obj` is. It was, as a pair with `obj`
# for .NET build output, but `bin/` is maintained source in Python, Ruby and
# shell projects — python-code-doctor treats it as an entrypoint directory
# (`_ENTRYPOINT_SEGMENTS = {"scripts", "bin"}`) and reports findings from it.
# Skipping it here kept those findings in the numerator while dropping their
# lines from the denominator, which can only push a grade down, and a repo of
# nothing but `bin/*.sh` produced no package at all. .NET build output is
# excluded anyway: `bin/` holds .dll/.pdb/.exe, none of which are in
# CODE_EXTENSIONS.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "vendor", "venv", ".venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox",
    "dist", "build", "target", "out", ".next", ".nuxt", ".svelte-kit", "coverage",
    "htmlcov", ".idea", ".vscode", "site-packages", ".gradle", "obj",
    ".terraform", "Pods", ".dart_tool", ".cache", "docs",
})


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def grouped_by_doctor(mapping: dict) -> str:
    """`{doctor: names}` (a list, or a dict keyed by name) as an escaped,
    doctor-attributed inline list: `doctor: name, name; doctor: name`.

    Shared by build_health.py's own Coverage tab and build_summary.py's portal
    caveat, which have to say the same thing about the same gap — a detector
    named with no doctor beside it tells a reader *something* was not run, not
    who to go re-run.
    """
    return "; ".join(
        f"<strong>{esc(doctor)}</strong>: " +
        ", ".join(f"<code>{esc(name)}</code>" for name in sorted(mapping[doctor]))
        for doctor in sorted(mapping)
    )


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


SEVERITIES = ("high", "medium", "low")


def normalize_severity(value) -> str:
    """Fold a severity to one of the three tokens the rest of the code compares.

    Normalized once, at the door, because the two consumers disagreed about
    case. `rubric.severity_weight` lowercases, so `"High"` was charged the full
    ten-point weight; every count, icon, sort and metadata field matches the
    lowercase token exactly, so the same finding was reported as zero highs and
    ranked among the mediums. A finding that moves the grade like a high and
    reads like a medium is the worst of both. `--covers` invites producers this
    skill has never seen, so this cannot be left to convention.
    """
    token = str(value).strip().lower() if value is not None else ""
    return token if token in SEVERITIES else "medium"


def normalize_findings(data, source: str = "a findings file") -> dict:
    """Flatten any doctor's JSON into one report record.

    Returns `{findings, candidates, completeness, errors, ran, skipped, shape}`.
    The non-finding fields matter as much as the findings and for the same
    reason: an analyzer that crashed, one that was skipped, and one that was
    never part of the run all report zero, and zero from an analyzer that did
    not look means *unknown*, not *clean*.

    Four shapes are recognised — a bare list, a `categories` envelope, a bare
    detector's `{"issues": [...]}`, and code-doctor's
    `{"completeness": {...}, "findings": [...]}`. Anything else **warns**. A
    zero-byte file is loud, but an unreadable *shape* was silent: a real
    code-doctor report handed to `--findings` parsed fine, matched no branch,
    and returned zero findings, which graded A+/100 with no caveat anywhere on
    the page. An unrecognised shape is exactly as much evidence as an empty
    file — none — and has to say so.
    """
    findings: list[dict] = []
    candidates: list[dict] = []
    completeness: dict = {}
    errors: dict[str, str] = {}
    skipped: set[str] = set()
    ran: set[str] = set()
    shape = SHAPE_PARTIAL

    if isinstance(data, list):
        findings, candidates = _split_kinds(data)
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
                # The split belongs here too. A language specialist emits this
                # shape and nothing else, so reading the section wholesale put
                # every lead it raised — a PRAGMA that can only be written by
                # interpolation, a public function one file cannot prove dead —
                # into the score as an asserted defect.
                section, section_candidates = _split_kinds(issues)
                findings.extend(section)
                candidates.extend(section_candidates)
            # `analyzers_run` present but *empty* is evidence, not a missing
            # field: it says nothing ran. Falling back to the category keys on
            # a falsy value credited a report that had initialized empty
            # sections and completed no analyzers with having measured all of
            # them — an A+ from a run that did nothing. Only an absent key
            # (an older or foreign report shape) falls back.
            listed = meta.get("analyzers_run")
            ran = ({str(name) for name in listed} if isinstance(listed, list)
                   else set(categories))
            # An analyzer that appears as a category but is absent from
            # analyzers_run never produced its section.
            skipped |= set(categories) - ran
            ran -= set(errors) | skipped
        elif isinstance(data.get("findings"), list):
            # code-doctor's own report, and the one shape `merge_reports.py`
            # already knew how to read while this function did not.
            records, candidates = _split_kinds(data["findings"])
            findings = records
            block = data.get("completeness")
            completeness = block if isinstance(block, dict) else {}
            errors, skipped, ran = _code_doctor_inventory(completeness)
            # Coverage evidence is the presence of `categories_run`, not the
            # presence of findings. Without the key there is nothing to say
            # what looked, which is the bare-list case in different clothing.
            if isinstance(completeness.get("categories_run"), list):
                shape = SHAPE_FULL
        elif isinstance(data.get("issues"), list):
            findings, candidates = _split_kinds(data["issues"])
        else:
            warn(f"{source} matched no known report shape (no `categories`, `findings` or "
                 "`issues` key), so nothing in it could be read. It contributes no findings "
                 "and no coverage — the same as an empty file. Check that it is a doctor's "
                 "report and not, say, a merged envelope (pass that with --merged).")
    else:
        warn(f"{source} holds a bare {type(data).__name__}, which is not a report of any "
             "shape this skill knows. Nothing in it could be read.")

    for finding in findings:
        finding["severity"] = normalize_severity(finding.get("severity"))
    for candidate in candidates:
        candidate["severity"] = normalize_severity(candidate.get("severity"))
    return {"findings": findings, "candidates": candidates, "completeness": completeness,
            "errors": errors, "ran": ran, "skipped": skipped,
            "shape": shape, "empty_artifact": False, "doctor": ""}


def _split_kinds(records) -> tuple[list[dict], list[dict]]:
    """Separate asserted defects from unverified leads.

    code-doctor emits both under one `findings` array, distinguished only by
    `kind`. Reading the array wholesale would put candidates into the score,
    which is the one thing this skill promises never to do: a candidate is a
    lead that a healthy codebase produces too, and charging the grade for it
    penalises code for being hard to analyse rather than for being wrong.
    """
    findings: list[dict] = []
    candidates: list[dict] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        (candidates if item.get("kind") == "candidate" else findings).append(item)
    return findings, candidates


def _code_doctor_inventory(completeness: dict) -> tuple[dict[str, str], set[str], set[str]]:
    """`(errors, skipped, ran)` out of code-doctor's completeness block.

    `categories_run` means ran *and completed*, so nothing is subtracted from
    it here; `categories_failed` and `categories_skipped` are the categories it
    already excludes, carried across so a reader can see which run lost them.
    """
    failed = completeness.get("categories_failed")
    errors = ({str(k): str(v) for k, v in failed.items()} if isinstance(failed, dict) else {})
    skipped_raw = completeness.get("categories_skipped")
    skipped = {str(name) for name in skipped_raw} if isinstance(skipped_raw, list) else set()
    ran_raw = completeness.get("categories_run")
    ran = {str(name) for name in ran_raw} if isinstance(ran_raw, list) else set()
    return errors, skipped - ran, ran - set(errors)


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


def split_doctor_label(raw: str) -> tuple[str, str]:
    """Split `<doctor>:<path>` into its parts. A bare path keeps an empty label.

    Only a prefix that names a doctor this rubric knows is treated as a label,
    so an ordinary path is never mangled — `C:/reports/x.json` has no doctor
    called `C`, and stays a path.
    """
    text = str(raw)
    head, sep, tail = text.partition(":")
    if sep and head in rubric.DOCTOR_COVERAGE:
        return head, tail
    return "", text


def load_reports(paths) -> tuple[list[dict], dict[str, str], set[str]]:
    """Read several findings files into per-report records. `-` reads stdin.

    Each path may be written `<doctor>:<path>` to say which doctor produced it.
    That label is what makes a multi-doctor run safe: without it, a report is
    attributed to whatever `--doctor` names, and a TypeScript report handed to a
    Python package's page granted Python coverage it had no evidence for.

    Deliberately does **not** merge or deduplicate. The caller scopes each
    report to the unit being documented first, because deduplicating before
    scoping lets a package page report duplicates that were merged in a
    different package entirely.
    """
    reports: list[dict] = []
    errors: dict[str, str] = {}
    for entry in paths:
        doctor, raw = split_doctor_label(entry)
        try:
            text = sys.stdin.read() if str(raw) == "-" else Path(raw).read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"error: cannot read findings file {raw}: {exc}") from exc
        if not text.strip():
            # A zero-byte file is what a doctor leaves behind when it fails
            # *after* the shell created the redirect target. It is flagged
            # rather than dropped, and flagged distinctly from a bare list: an
            # empty artifact is positive evidence that a run *failed*, and the
            # gap it leaves cannot be attributed to any category, because
            # nothing in the file says which doctor was meant to write it.
            warn(f"{raw} is empty — no findings and no evidence of what was examined, so "
                 "nothing from it can be graded. Did the doctor fail after the shell "
                 "created the file?")
            reports.append({"findings": [], "candidates": [], "completeness": {},
                            "errors": {}, "ran": set(),
                            "skipped": set(), "shape": SHAPE_PARTIAL,
                            "empty_artifact": True, "doctor": doctor})
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"error: {raw} is not valid JSON: {exc}") from exc
        report = normalize_findings(data, source=str(raw))
        report["doctor"] = doctor
        reports.append(report)
        errors.update(report["errors"])

    # Skipped means skipped *everywhere*: a detector one report omitted and
    # another ran is covered. Coverage itself is computed per report, so this
    # is only for reporting.
    ran_anywhere = {name for report in reports for name in report["ran"]}
    skipped = {name for report in reports for name in report["skipped"]} - ran_anywhere
    return reports, errors, skipped


def sizing_extensions(findings: list[dict], extra=(), doctors=()) -> frozenset:
    """Which extensions the denominator should cover.

    Code always, plus templates when the analysis reached them. Two signals say
    it did, and both are needed:

    - **Some contributing doctor parses markup.** `django-code-doctor` reads
      templates on every run, so its template lines are in the denominator
      whether or not any of them was faulty. Findings alone would leave a
      package of clean templates sized over its Python only — and then a single
      new template finding would drop every template line into the divisor and
      *raise* the grade, which is precisely backwards.

      `doctors` is every doctor that contributed a report, not just the one
      named by `--doctor`. The recommended Python+Django merge passes
      `--doctor python-code-doctor` — that flag caps *coverage*, and reading it
      as the whole story left a clean Django run's templates out of the divisor
      in the skill's own documented workflow.
    - **A finding points at one.** This still catches the case the doctor
      profile cannot know about: an unrecognized `--doctor`, or a tool passed
      through `--covers`.

    A Python package that merely ships an HTML fixture is covered by neither,
    so it is sized over its code alone, as it should be.
    """
    used = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extra}
    if any(doctor in rubric.DOCTORS_ANALYZING_TEMPLATES for doctor in doctors):
        used |= set(TEMPLATE_EXTENSIONS)
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

MEASUREMENT_BLOCK_ID = "measurement-meta"
THEORY_BLOCK_ID = "theory-meta"


def _meta_re(block_id: str) -> re.Pattern:
    return re.compile(r'<script[^>]*id="' + re.escape(block_id) + r'"[^>]*>(.*?)</script>',
                      re.DOTALL)


def read_meta(path, block_id: str = META_BLOCK_ID) -> dict | None:
    """Pull a generated document's metadata block back out of it.

    Both document types embed the same way and escape `</` the same way, so one
    reader serves both — which is what keeps the portal from having to be told a
    grade it could read.
    """
    path = Path(path)
    if not path.is_file():
        return None
    match = _meta_re(block_id).search(path.read_text(encoding="utf-8"))
    if not match:
        return None
    try:
        return json.loads(match.group(1).replace("<\\/", "</"))
    except json.JSONDecodeError as exc:
        warn(f"{path} has a {block_id} block that is not valid JSON: {exc}")
        return None


# --------------------------------------------------------------------------
# the package map
# --------------------------------------------------------------------------

DOC_KINDS = ("summary", "codemap", "health", "measurement", "theory")
DOC_TITLES = {"summary": "Summary", "codemap": "Code Map", "health": "Health",
              "measurement": "Measurement", "theory": "Theory"}


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
        # `roots` must be a list. `"roots": "src/api"` is a plausible thing to
        # write by hand and every consumer iterates it, so a string is silently
        # taken apart character by character: the default docs path became
        # `s/docs`, and every letter of the path became its own scoring root.
        # The package is then ungraded and its documents land somewhere absurd,
        # with nothing anywhere saying why.
        roots = package["roots"]
        if (not isinstance(roots, list)
                or not all(isinstance(root, str) and root.strip() for root in roots)):
            raise SystemExit(
                f"error: {path} gives package {package['name']!r} roots {roots!r} — `roots` "
                "has to be a non-empty list of repo-relative path strings, even for a single "
                'root (["src/api"], not "src/api"), because every consumer iterates it'
            )
        package.setdefault("docs", str(Path(package["roots"][0]) / "docs"))
        if not isinstance(package["docs"], str) or not package["docs"].strip():
            raise SystemExit(
                f"error: {path} gives package {package['name']!r} docs {package['docs']!r} — "
                "`docs` has to be a repo-relative path string"
            )
        # Every path in the map is documented as repo-relative, and the scripts
        # treat it that way — `repo / path`. An absolute path or a `..` escape
        # silently reaches outside the checkout: sizing would measure someone
        # else's code, and inject_nav.py would rewrite summary.html, codemap.html
        # and health.html wherever `docs` pointed. `roots: ["/"]` even satisfied
        # is_repo_root. Nothing legitimate needs it, so it is rejected here
        # rather than defended against at each use.
        for field in ("roots", "docs"):
            value = package[field]
            for entry in ([value] if isinstance(value, str) else value):
                escapes = (Path(entry).is_absolute()
                           or os.path.normpath(entry).split(os.sep)[0] == "..")
                if escapes:
                    raise SystemExit(
                        f"error: {path} gives package {package['name']!r} the {field} "
                        f"{entry!r}, which points outside the repository — every path in "
                        "the map has to be repo-relative, because they are all resolved "
                        "against the repo root and written to"
                    )

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

    Percent-encoded per segment, because a path is not a URL. A directory named
    `a#b` produced `../a#b/docs/summary.html`, where a browser reads everything
    from `#` as a fragment and navigates to the wrong document — while a link
    checker resolving the literal string against the filesystem found the file
    and reported the set healthy. `/` is preserved as the separator; `..` and
    `.` contain nothing that needs encoding.
    """
    relative = os.path.relpath(Path(to_doc).resolve(), Path(from_doc).resolve().parent)
    return quote(Path(relative).as_posix(), safe="/")


# --------------------------------------------------------------------------
# code-doctor's merged envelope
# --------------------------------------------------------------------------

MERGE_SCHEMA = "code-doctor-merge/1"


def load_merged(path) -> dict:
    """Unpack code-doctor's merged envelope into this skill's report records.

    The envelope replaces three things this skill used to be told by flag: which
    doctors ran, what they covered, and which records assert a defect. Every one
    of those was a place where a wrong answer produced a *confident* grade, so
    each is read as evidence here rather than declared.
    """
    path = Path(path)
    blank = {"reports": [], "candidates": [], "completeness": {}, "doctor_errors": {},
             "doctors": []}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        warn(f"{path}: {exc} — nothing from this envelope is graded")
        return blank
    if not text.strip():
        # Identical to the zero-byte findings rule: a doctor that produced no
        # output failed, and failure is not a clean bill of health.
        warn(f"{path} is empty — a merge that produced nothing failed; nothing is graded")
        return blank
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        warn(f"{path} is not valid JSON: {exc} — nothing from this envelope is graded")
        return blank
    if not isinstance(data, dict) or data.get("schema") != MERGE_SCHEMA:
        warn(f"{path} is not a {MERGE_SCHEMA} envelope — nothing from it is graded")
        return blank

    # A malformed shape here is worse than a missing one. `"findings": {...}`
    # (a dict where a list belongs) would otherwise be silently skipped by
    # `data.get("findings") or []` and read as zero findings — a clean bill of
    # health from a producer that never actually reported zero — and a
    # `completeness` value that is not a dict raises deep inside
    # `rubric.ungraded_from_completeness` instead of being caught here. Reject
    # the whole envelope rather than guess at a partial reading of it.
    findings_field = data.get("findings")
    if findings_field is not None and not isinstance(findings_field, list):
        warn(f"{path} has a `findings` field that is not a list — nothing from this "
             "envelope is graded")
        return blank
    candidates_field = data.get("candidates")
    if candidates_field is not None and not isinstance(candidates_field, list):
        warn(f"{path} has a `candidates` field that is not a list — nothing from this "
             "envelope is graded")
        return blank
    completeness_field = data.get("completeness")
    if completeness_field is not None and (
            not isinstance(completeness_field, dict)
            or any(not isinstance(block, dict) for block in completeness_field.values())):
        warn(f"{path} has a `completeness` field that is not a dict of dicts — nothing "
             "from this envelope is graded")
        return blank

    doctors = [str(name) for name in (data.get("doctors_run") or [])]
    analyzers = data.get("analyzers_run") or {}
    skipped = data.get("analyzers_skipped") or {}
    errors = data.get("analyzer_errors") or {}
    unknown = {str(name) for name in (data.get("coverage_unknown") or [])}

    by_doctor: dict[str, dict] = {}
    for doctor in doctors:
        ran = {str(name) for name in (analyzers.get(doctor) or [])}
        doctor_skipped = {str(name) for name in (skipped.get(doctor) or [])}
        doctor_errors_here = {str(k): str(v) for k, v in (errors.get(doctor) or {}).items()}
        # A doctor listed with no analyzers_run evidence is exactly the bare
        # list case: nothing in it distinguishes a full run from one detector
        # that found nothing, so it grants no coverage profile. Decided from
        # the raw list, before the skipped/errored reduction below — a doctor
        # whose every named analyzer crashed still said *something* about what
        # ran (all of it bad), which is SHAPE_FULL, not the "said nothing"
        # SHAPE_PARTIAL.
        shape = SHAPE_PARTIAL if (doctor in unknown or not ran) else SHAPE_FULL
        # Mirrors normalize_findings: an analyzer that also appears skipped or
        # crashed never completed, so it is not "ran" for per-category credit
        # even though the envelope named it.
        ran -= set(doctor_errors_here) | doctor_skipped
        by_doctor[doctor] = {
            "findings": [],
            "candidates": [],
            "completeness": (completeness_field or {}).get(doctor) or {},
            "errors": doctor_errors_here,
            "ran": ran,
            "skipped": doctor_skipped,
            "shape": shape,
            "empty_artifact": False,
            "doctor": doctor,
        }

    # `coverage_unknown` used to be returned and read by nobody. It is consumed
    # here instead: a doctor that contributed findings but no inventory of what
    # it looked at is the caveat a reader most needs, and the page otherwise
    # says nothing about it — every category it alone could have covered comes
    # back ungraded with no explanation of why. Returning the list as well
    # would just re-create the unread field.
    stranded = sorted(name for name in unknown if name in set(doctors))
    if stranded:
        warn(f"{', '.join(stranded)} contributed findings but no record of what "
             "was examined (a bare list cannot say), so they grant no coverage and every "
             "category resting on them alone is ungraded. Re-run them with a report shape "
             "that inventories its analyzers.")

    orphaned: dict[str, int] = {}
    for record in findings_field or []:
        if isinstance(record, dict):
            record["severity"] = normalize_severity(record.get("severity"))
            name = str(record.get("doctor"))
            report = by_doctor.get(name)
            if report is not None:
                report["findings"].append(record)
            else:
                # Dropping these silently shrank the numerator against an
                # unchanged denominator: findings vanished from the grade while
                # the lines they were found in stayed in the divisor, so a
                # mis-attributed record made the code look *better*. Count and
                # say so.
                orphaned[name] = orphaned.get(name, 0) + 1
    if orphaned:
        detail = ", ".join(f"{count} from {name or '(unattributed)'}"
                           for name, count in sorted(orphaned.items()))
        warn(f"{sum(orphaned.values())} finding(s) in {path} name a doctor that is not in "
             f"`doctors_run` ({detail}), so they are not graded. The lines they point at are "
             "still in the denominator, so the grade is flattering by exactly that much. "
             "Re-merge with every doctor labelled.")

    candidates = [record for record in (candidates_field or []) if isinstance(record, dict)]
    for record in candidates:
        record["severity"] = normalize_severity(record.get("severity"))
        report = by_doctor.get(str(record.get("doctor")))
        if report is not None:
            report["candidates"].append(record)

    return {
        "reports": list(by_doctor.values()),
        "candidates": candidates,
        "completeness": completeness_field or {},
        "doctor_errors": {str(k): str(v) for k, v in (data.get("doctor_errors") or {}).items()},
        "doctors": doctors,
    }
