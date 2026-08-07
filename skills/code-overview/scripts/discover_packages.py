#!/usr/bin/env python3
"""Propose the units a code overview should be built per.

Emits a *proposal*, not a decision. Every candidate carries the evidence it was
found by, overlaps are reported rather than resolved, and anything the heuristics
cannot settle comes back under `questions` for the agent to put to the user. The
answer is then saved as docs/code-overview.json and every later script reads it
from there.

Why not just decide: the right unit is a design judgment. `src/` is a candidate
by layout and never the answer; a Django app and its templates directory are one
unit while looking like two; a repo with one pyproject.toml may still be four
subsystems its author thinks of separately. Guessing silently produces a document
set organized around the wrong thing, which is worse than asking.

Usage:
  python discover_packages.py <repo>                     # JSON proposal on stdout
  python discover_packages.py <repo> --format text       # readable summary
  python discover_packages.py <repo> --exclude legacy,third_party
  python discover_packages.py <repo> --min-files 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from common import MAP_SCHEMA, SKIP_DIRS, iter_code_files, measure, warn

# Extension → language, for deciding which doctor fits a candidate.
LANGUAGE_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".cs": "csharp",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".swift": "swift", ".dart": "dart", ".vue": "vue", ".svelte": "svelte",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
    ".ex": "elixir", ".exs": "elixir", ".m": "objc", ".mm": "objc",
    ".sql": "sql", ".sh": "shell", ".bash": "shell",
}

# Language → the doctor skill that reviews it. Absent means "no doctor ships for
# this language" — the caller has to ask what to do, not quietly pick the
# closest one.
DOCTOR_BY_LANGUAGE = {
    "python": "python-code-doctor",
    "typescript": "typescript-code-doctor",
}

MANIFESTS = {
    "pyproject.toml": "python", "setup.py": "python", "setup.cfg": "python",
    "package.json": "node", "go.mod": "go", "Cargo.toml": "rust",
    "build.gradle": "jvm", "build.gradle.kts": "jvm", "pom.xml": "jvm",
    "composer.json": "php", "Gemfile": "ruby",
}

_NAME_PATTERNS = (
    re.compile(r'^\s*name\s*=\s*["\']([^"\']+)["\']', re.MULTILINE),   # toml / setup.cfg
    re.compile(r'"name"\s*:\s*"([^"]+)"'),                              # json
    re.compile(r"^module\s+(\S+)", re.MULTILINE),                       # go.mod
)

_INSTALLED_APPS_RE = re.compile(r"INSTALLED_APPS\s*(?::[^=]+)?=\s*[\[(](.*?)[\])]", re.DOTALL)
_STRING_RE = re.compile(r"""["']([A-Za-z0-9_.]+)["']""")


def is_skipped(path: Path, repo: Path, excluded: set[str]) -> bool:
    parts = path.relative_to(repo).parts
    return any(p in SKIP_DIRS or p in excluded or p.startswith(".") for p in parts)


def manifest_name(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if path.name == "go.mod":
        match = _NAME_PATTERNS[2].search(text)
        return match.group(1).rsplit("/", 1)[-1] if match else ""
    if path.suffix == ".json":
        match = _NAME_PATTERNS[1].search(text)
        return match.group(1).lstrip("@").replace("/", "-") if match else ""
    match = _NAME_PATTERNS[0].search(text)
    return match.group(1) if match else ""


def dominant_language(roots) -> tuple[str, Counter]:
    counts: Counter = Counter()
    for path in iter_code_files(roots):
        language = LANGUAGE_BY_EXT.get(path.suffix)
        if language:
            counts[language] += 1
    if not counts:
        return "", counts
    # TypeScript beats JavaScript whenever both are present: the TS doctor reads
    # both, and a project with any .ts is a TS project with JS left in it.
    if counts.get("typescript") and counts.get("javascript"):
        counts["typescript"] += counts.pop("javascript")
    return counts.most_common(1)[0][0], counts


def find_django_root(repo: Path, excluded: set[str]) -> Path | None:
    """The directory holding manage.py, if this is a Django project."""
    for candidate in sorted(repo.rglob("manage.py")):
        if not is_skipped(candidate, repo, excluded):
            return candidate.parent
    for settings in sorted(repo.rglob("settings*.py")):
        if is_skipped(settings, repo, excluded):
            continue
        if "INSTALLED_APPS" in settings.read_text(encoding="utf-8", errors="replace"):
            return settings.parent.parent
    return None


def django_apps(repo: Path, excluded: set[str]) -> list[Path]:
    """Local app directories named by INSTALLED_APPS, resolved against the tree."""
    apps: list[Path] = []
    seen: set[Path] = set()
    for settings in sorted(repo.rglob("settings*.py")):
        if is_skipped(settings, repo, excluded):
            continue
        text = settings.read_text(encoding="utf-8", errors="replace")
        match = _INSTALLED_APPS_RE.search(text)
        if not match:
            continue
        for dotted in _STRING_RE.findall(match.group(1)):
            if dotted.startswith("django.") or dotted.startswith("rest_framework"):
                continue
            # "shop.apps.ShopConfig" names the app at "shop".
            parts = dotted.split(".")
            if len(parts) >= 3 and parts[-2] == "apps":
                parts = parts[:-2]
            for base in (repo, settings.parent.parent):
                candidate = base.joinpath(*parts)
                if candidate.is_dir() and candidate.resolve() not in seen:
                    seen.add(candidate.resolve())
                    apps.append(candidate)
                    break
    # An app declared nowhere but structurally obvious still counts.
    for apps_py in sorted(repo.rglob("apps.py")):
        if is_skipped(apps_py, repo, excluded):
            continue
        if "AppConfig" in apps_py.read_text(encoding="utf-8", errors="replace"):
            if apps_py.parent.resolve() not in seen:
                seen.add(apps_py.parent.resolve())
                apps.append(apps_py.parent)
    return apps


def npm_workspaces(repo: Path, manifest: Path) -> list[Path]:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return []
    patterns = data.get("workspaces")
    if isinstance(patterns, dict):
        patterns = patterns.get("packages", [])
    if not isinstance(patterns, list):
        return []
    found: list[Path] = []
    for pattern in patterns:
        for match in sorted(manifest.parent.glob(str(pattern))):
            if match.is_dir() and (match / "package.json").is_file():
                found.append(match)
    return found


def cargo_members(repo: Path, manifest: Path) -> list[Path]:
    text = manifest.read_text(encoding="utf-8", errors="replace")
    if "[workspace]" not in text:
        return []
    block = text.split("[workspace]", 1)[1]
    members = re.search(r"members\s*=\s*\[(.*?)\]", block, re.DOTALL)
    if not members:
        return []
    found: list[Path] = []
    for pattern in _STRING_RE.findall(members.group(1)) + re.findall(r'"([^"]+)"', members.group(1)):
        for match in sorted(manifest.parent.glob(pattern)):
            if match.is_dir() and (match / "Cargo.toml").is_file():
                found.append(match)
    return found


def top_level_python_packages(repo: Path, excluded: set[str]) -> list[Path]:
    """Directories with __init__.py whose parent has none — importable roots."""
    found: list[Path] = []
    for init in sorted(repo.rglob("__init__.py")):
        if is_skipped(init, repo, excluded):
            continue
        directory = init.parent
        if not (directory.parent / "__init__.py").is_file():
            found.append(directory)
    return found


def candidate(repo: Path, roots, name: str, evidence: str, kind: str) -> dict:
    roots = [Path(r) for r in roots]
    language, counts = dominant_language(roots)
    size = measure(roots)
    return {
        "name": name,
        "roots": [r.relative_to(repo).as_posix() for r in roots],
        "docs": (Path(roots[0].relative_to(repo)) / "docs").as_posix(),
        "language": language,
        "doctor": DOCTOR_BY_LANGUAGE.get(language, ""),
        "kind": kind,
        "evidence": evidence,
        "size": size,
        "languages": dict(counts.most_common()),
    }


def discover(repo: Path, excluded: set[str], min_files: int) -> dict:
    repo = repo.resolve()
    candidates: list[dict] = []
    too_small: list[dict] = []
    seen_roots: set[str] = set()

    def add(roots, name, evidence, kind) -> dict | None:
        roots = [Path(r) for r in roots]
        key = "|".join(sorted(r.resolve().as_posix() for r in roots))
        if key in seen_roots:
            return None
        seen_roots.add(key)
        entry = candidate(repo, roots, name, evidence, kind)
        # Reported rather than dropped: a two-file package that vanishes from the
        # proposal is a hole nobody can see, and --min-files is a noise filter,
        # not a judgment that the code does not exist.
        if entry["size"]["files"] < min_files:
            too_small.append(entry)
            return None
        candidates.append(entry)
        return entry

    # --- Django apps first: they are the finest-grained real unit in a Django
    # project, and the pyproject.toml above them describes packaging, not design.
    django_root = find_django_root(repo, excluded)
    if django_root is not None:
        for app in django_apps(repo, excluded):
            if not is_skipped(app, repo, excluded):
                entry = add([app], app.name, f"Django app ({app.relative_to(repo).as_posix()})", "django-app")
                if entry:
                    entry["doctor"] = "django-code-doctor"

    # --- manifests
    for manifest_name_, family in MANIFESTS.items():
        for manifest in sorted(repo.rglob(manifest_name_)):
            if is_skipped(manifest, repo, excluded):
                continue
            directory = manifest.parent
            name = manifest_name(manifest) or directory.name
            if directory.resolve() == repo:
                name = name or repo.name
            add([directory], name, f"{manifest_name_} at {directory.relative_to(repo).as_posix() or '.'}",
                f"{family}-manifest")
            if manifest_name_ == "package.json":
                for workspace in npm_workspaces(repo, manifest):
                    add([workspace], manifest_name(workspace / "package.json") or workspace.name,
                        f"npm workspace member ({workspace.relative_to(repo).as_posix()})", "node-workspace")
            if manifest_name_ == "Cargo.toml":
                for member in cargo_members(repo, manifest):
                    add([member], member.name,
                        f"cargo workspace member ({member.relative_to(repo).as_posix()})", "rust-workspace")

    # --- importable Python roots the manifests did not already name
    for package in top_level_python_packages(repo, excluded):
        add([package], package.name,
            f"importable package ({package.relative_to(repo).as_posix()})", "python-package")

    # --- last resort: top-level source directories
    for child in sorted(repo.iterdir()):
        if not child.is_dir() or is_skipped(child, repo, excluded):
            continue
        add([child], child.name, f"top-level source directory ({child.name})", "directory")

    total = measure([repo])
    covered = {root for entry in candidates for root in entry["roots"]}
    unassigned = unassigned_dirs(repo, excluded, covered, min_files)
    return {
        "schema": MAP_SCHEMA,
        "repo": str(repo),
        "size": total,
        "django": django_root is not None,
        "packages": candidates,
        "too_small": too_small,
        "unassigned": unassigned,
        "questions": build_questions(candidates, too_small, unassigned, total),
    }


def unassigned_dirs(repo: Path, excluded: set[str], covered: set[str], min_files: int) -> list[dict]:
    """Top-level directories with real code that no candidate claims."""
    out = []
    for child in sorted(repo.iterdir()):
        if not child.is_dir() or is_skipped(child, repo, excluded):
            continue
        rel = child.relative_to(repo).as_posix()
        if rel in covered or any(c.startswith(rel + "/") for c in covered):
            continue
        size = measure([child])
        if size["files"] >= min_files:
            out.append({"path": rel, "size": size})
    return out


def build_questions(candidates: list[dict], too_small: list[dict],
                    unassigned: list[dict], total: dict) -> list[dict]:
    """What the heuristics could not settle. The agent puts these to the user."""
    questions: list[dict] = []

    overlapping = []
    for outer in candidates:
        for inner in candidates:
            if outer is inner:
                continue
            for outer_root in outer["roots"]:
                for inner_root in inner["roots"]:
                    if inner_root != outer_root and inner_root.startswith(outer_root.rstrip("/") + "/"):
                        overlapping.append((outer["name"], inner["name"]))
    if overlapping:
        pairs = sorted({f"{a} contains {b}" for a, b in overlapping})
        questions.append({
            "id": "nesting",
            "question": "Some candidates contain others. Which level is the unit you think in?",
            "detail": "; ".join(pairs[:12]),
            "options": ["the outer ones (fewer, larger documents)",
                        "the inner ones (one document per subsystem)",
                        "a specific mix — name it"],
        })

    structural = [c["name"] for c in candidates
                  if c["kind"] == "directory" and c["name"] in {"src", "app", "lib", "source", "code", "pkg"}]
    if structural:
        questions.append({
            "id": "structural-names",
            "question": f"{', '.join(structural)} looks like a layout convention rather than a subsystem. Split it?",
            "detail": "A package named for packaging produces a document that describes the whole repo twice.",
            "options": ["split it into its subdirectories", "keep it as one package"],
        })

    no_doctor = sorted({c["language"] or "unknown" for c in candidates if not c["doctor"]})
    if no_doctor:
        questions.append({
            "id": "no-doctor",
            "question": f"No code doctor ships for: {', '.join(no_doctor)}. What should those packages get?",
            "detail": "Options are an ungraded health page built from language-agnostic signals, "
                      "no health page at all, or the closest doctor run anyway.",
            "options": ["codemap + ungraded health page", "codemap only, no health page",
                        "run the closest doctor anyway"],
        })

    if too_small:
        listed = ", ".join(f"{c['name']} ({c['size']['files']} files)" for c in too_small[:8])
        questions.append({
            "id": "too-small",
            "question": f"Below the size filter, so not proposed: {listed}. Promote any of them?",
            "detail": "They are real units; --min-files only decided they were probably not worth "
                      "their own document set.",
            "options": ["leave them out", "promote — name which", "fold into a parent package"],
        })

    if unassigned:
        listed = ", ".join(f"{u['path']} ({u['size']['files']} files)" for u in unassigned[:8])
        questions.append({
            "id": "unassigned",
            "question": f"Code no candidate claims: {listed}. Include it?",
            "detail": "Unclaimed code is invisible in the roll-up, and the root grade is computed "
                      "from what the packages covered.",
            "options": ["add each as its own package", "fold into an existing package — say which",
                        "leave it out"],
        })

    if len(candidates) == 1 and candidates[0]["roots"] == ["."]:
        questions.append({
            "id": "single-package",
            "question": "The whole repo came back as one package. Is that right, or are there subsystems to split out?",
            "detail": f"{total['files']} files, {total['loc']} lines. A single-package repo produces "
                      "the three root documents only, with no package layer.",
            "options": ["one package is right", "split — name the subsystems"],
        })

    return questions


def render_text(proposal: dict) -> str:
    lines = [f"📦 {len(proposal['packages'])} candidate package(s) in {proposal['repo']}",
             f"   repo total: {proposal['size']['files']} files, {proposal['size']['loc']} lines", ""]
    for entry in proposal["packages"]:
        doctor = entry["doctor"] or "(no doctor for this language)"
        lines.append(f"  {entry['name']}  [{entry['language'] or 'unknown'} → {doctor}]")
        lines.append(f"      roots: {', '.join(entry['roots'])}")
        lines.append(f"      {entry['size']['files']} files, {entry['size']['loc']} lines · {entry['evidence']}")
    if proposal["too_small"]:
        lines += ["", "Below --min-files, not proposed:"]
        lines += [f"  {c['name']} ({c['size']['files']} files, {', '.join(c['roots'])})"
                  for c in proposal["too_small"]]
    if proposal["unassigned"]:
        lines += ["", "Unclaimed code:"]
        lines += [f"  {u['path']} ({u['size']['files']} files)" for u in proposal["unassigned"]]
    if proposal["questions"]:
        lines += ["", "❓ Ask the user:"]
        for question in proposal["questions"]:
            lines.append(f"  [{question['id']}] {question['question']}")
            lines.append(f"      {question['detail']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("repo", nargs="?", default=".", help="repository root (default: .)")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument("--exclude", default="", help="comma-separated directory names to ignore")
    parser.add_argument("--min-files", type=int, default=3,
                        help="drop candidates with fewer source files (default: 3)")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 1

    excluded = {part.strip() for part in args.exclude.split(",") if part.strip()}
    proposal = discover(repo, excluded, args.min_files)
    if not proposal["packages"]:
        warn("no candidate packages found — the repo may be smaller than --min-files, "
             "or entirely under excluded directories")
    print(render_text(proposal) if args.format == "text" else json.dumps(proposal, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
