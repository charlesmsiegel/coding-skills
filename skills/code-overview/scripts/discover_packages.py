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
import os
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

# Manifests identified by extension rather than exact name. A .NET solution
# names each project `<Name>.csproj`, so there is no fixed filename to look for
# and a monorepo of them would otherwise collapse into one `src` candidate.
MANIFEST_SUFFIXES = {".csproj": "dotnet", ".fsproj": "dotnet", ".vbproj": "dotnet"}

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


# Filenames discovery cares about. One pruned walk collects all of them, because
# rglob descends into node_modules before anything gets to filter the result —
# and rglob-per-filename would repeat that walk once per name, which on a
# monorepo with a populated dependency tree is the slowest thing this script does.
INTERESTING = frozenset(MANIFESTS) | {"manage.py", "apps.py", "__init__.py"}


def scan(repo: Path, excluded: set[str]) -> dict[str, list[Path]]:
    """Every file discovery cares about, found in a single pruned traversal."""
    found: dict[str, list[Path]] = {name: [] for name in INTERESTING}
    found["settings"] = []
    for suffix in MANIFEST_SUFFIXES:
        found[suffix] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and d not in excluded and not d.startswith(".")
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if name in INTERESTING:
                found[name].append(path)
            elif path.suffix in MANIFEST_SUFFIXES:
                found[path.suffix].append(path)
            elif name.startswith("settings") and name.endswith(".py"):
                found["settings"].append(path)
    return found


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


def dominant_language(roots) -> tuple[str, Counter, Counter]:
    """The main language, plus per-language file and line counts.

    Lines matter as much as files because the *line* count is what a grade is
    divided by. Nine tiny Python modules beside one enormous TypeScript file is
    a 90/10 split by file and closer to the reverse by line, and it is the lines
    that end up in the denominator — so both are measured and the judgment below
    is made on lines.
    """
    files: Counter = Counter()
    lines: Counter = Counter()
    for path in iter_code_files(roots):
        language = LANGUAGE_BY_EXT.get(path.suffix)
        if not language:
            continue
        files[language] += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines[language] += sum(1 for line in text.splitlines() if line.strip())
    if not files:
        return "", files, lines
    # TypeScript beats JavaScript whenever both are present: the TS doctor reads
    # both, and a project with any .ts is a TS project with JS left in it.
    if files.get("typescript") and files.get("javascript"):
        files["typescript"] += files.pop("javascript")
        lines["typescript"] += lines.pop("javascript", 0)
    ranked = lines if sum(lines.values()) else files
    return ranked.most_common(1)[0][0], files, lines


def find_django_root(found: dict[str, list[Path]]) -> Path | None:
    """The directory holding manage.py, if this is a Django project."""
    for candidate in found["manage.py"]:
        return candidate.parent
    for settings in found["settings"]:
        if "INSTALLED_APPS" in settings.read_text(encoding="utf-8", errors="replace"):
            return settings.parent.parent
    return None


def django_apps(repo: Path, found: dict[str, list[Path]]) -> list[Path]:
    """Local app directories named by INSTALLED_APPS, resolved against the tree."""
    apps: list[Path] = []
    seen: set[Path] = set()
    for settings in found["settings"]:
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
    for apps_py in found["apps.py"]:
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


def top_level_python_packages(found: dict[str, list[Path]]) -> list[Path]:
    """Directories with __init__.py whose parent has none — importable roots."""
    return [init.parent for init in found["__init__.py"]
            if not (init.parent.parent / "__init__.py").is_file()]


# A second language below this share of the package's *lines* is incidental — a
# build script, a couple of shims. At or above it the package is genuinely
# mixed and one doctor cannot speak for it. Lines, not files, because the harm
# is that the unread language's lines land in the grade's denominator.
MIXED_LANGUAGE_SHARE = 0.2


def secondary_languages(lines: Counter, files: Counter, dominant: str) -> list[str]:
    """Languages that are a real part of a candidate beyond its dominant one."""
    counts = lines if sum(lines.values()) else files
    total = sum(counts.values())
    if total == 0:
        return []
    return sorted(language for language, count in counts.items()
                  if language != dominant and count / total >= MIXED_LANGUAGE_SHARE)


def candidate(repo: Path, roots, name: str, evidence: str, kind: str) -> dict:
    roots = [Path(r) for r in roots]
    language, files, lines = dominant_language(roots)
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
        "languages": dict(files.most_common()),
        "language_lines": dict(lines.most_common()),
        # The dominant language decides the doctor, which decides what the grade
        # claims to cover. When a real second language is present that choice is
        # not the script's to make: the other language's lines would swell the
        # denominator while no detector ever reads them.
        "mixed_with": secondary_languages(lines, files, language),
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

    found = scan(repo, excluded)

    # --- Django apps first: they are the finest-grained real unit in a Django
    # project, and the pyproject.toml above them describes packaging, not design.
    django_root = find_django_root(found)
    if django_root is not None:
        for app in django_apps(repo, found):
            if not is_skipped(app, repo, excluded):
                entry = add([app], app.name, f"Django app ({app.relative_to(repo).as_posix()})", "django-app")
                if entry:
                    entry["doctor"] = "django-code-doctor"

    # --- manifests
    for manifest_name_, family in MANIFESTS.items():
        for manifest in found[manifest_name_]:
            directory = manifest.parent
            name = manifest_name(manifest) or directory.name
            if directory.resolve() == repo:
                name = name or repo.name
            add([directory], name, f"{manifest_name_} at {directory.relative_to(repo).as_posix() or '.'}",
                f"{family}-manifest")
            # Workspace globs are expanded separately from the pruned walk, so
            # they have to be filtered through the same exclusions — otherwise
            # `workspaces: ["legacy"]` re-adds a tree the caller excluded, and a
            # broad glob descends into vendored directories the walk skipped.
            if manifest_name_ == "package.json":
                for workspace in npm_workspaces(repo, manifest):
                    if is_skipped(workspace, repo, excluded):
                        continue
                    add([workspace], manifest_name(workspace / "package.json") or workspace.name,
                        f"npm workspace member ({workspace.relative_to(repo).as_posix()})", "node-workspace")
            if manifest_name_ == "Cargo.toml":
                for member in cargo_members(repo, manifest):
                    if is_skipped(member, repo, excluded):
                        continue
                    add([member], member.name,
                        f"cargo workspace member ({member.relative_to(repo).as_posix()})", "rust-workspace")

    # --- extension-named manifests (.NET projects)
    for suffix, family in MANIFEST_SUFFIXES.items():
        for manifest in found[suffix]:
            directory = manifest.parent
            add([directory], manifest.stem,
                f"{manifest.name} at {directory.relative_to(repo).as_posix() or '.'}",
                f"{family}-manifest")

    # --- importable Python roots the manifests did not already name
    for package in top_level_python_packages(found):
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

    mixed = [c for c in candidates if c["mixed_with"]]
    if mixed:
        listed = "; ".join(
            f"{c['name']} is mostly {c['language'] or 'unknown'} with "
            f"{', '.join(c['mixed_with'])}" for c in mixed[:8])
        questions.append({
            "id": "mixed-languages",
            "question": f"These packages hold more than one language: {listed}. How should they be analyzed?",
            "detail": "Only the dominant language's doctor would run, so the other language's "
                      "lines swell the size the grade is divided by while no detector ever "
                      "reads them.",
            "options": ["run a doctor per language and merge the findings",
                        "split the package by language",
                        "grade the dominant language only, and say so on the page"],
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
        mixed = f"  (+ {', '.join(entry['mixed_with'])})" if entry["mixed_with"] else ""
        lines.append(f"  {entry['name']}  [{entry['language'] or 'unknown'} → {doctor}]{mixed}")
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
