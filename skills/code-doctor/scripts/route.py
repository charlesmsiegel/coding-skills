#!/usr/bin/env python3
"""Which language specialists this repository's own manifests justify running.

Routing is the one place in this skill where language names legitimately
appear. Every detector is language-blind by design; the router's entire job is
to name a specialist, and that cannot be done without naming a language.

What survives the exception is the evidence rule. A route is justified by a
manifest the repository wrote *about itself* — a declared dependency, a
compiler config — never by counting file extensions. A Go service with three
vendored Python scripts is not a Python project, and a filename census would
route it to python-code-doctor, which would then report the missing
dependency manifest it was never supposed to have as a finding.

Under-routing is the safe direction to be wrong in: the raw layer still runs,
and a consumer that grades the report is told which categories nobody measured.
Over-routing fabricates findings.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from common import (
    EXCLUDE_DIRS,
    ScanPathError,
    configure_output,
    fail_on_bad_path,
    read_text,
    walk_files,
)

PYTHON_DOCTOR = "python-code-doctor"
DJANGO_DOCTOR = "django-code-doctor"
TYPESCRIPT_DOCTOR = "typescript-code-doctor"

# Where a PEP 508 name ends: extras, version specifiers, environment markers,
# separators, or whitespace before a trailing comment.
_NAME_TERMINATORS = "[<>=!~;, \t"


@dataclass(frozen=True)
class Route:
    """One specialist, and the manifest evidence that justifies it."""

    skill: str
    reason: str
    evidence: tuple[str, ...]


def requirement_name(spec: str) -> str:
    """The distribution name at the head of a requirement string, lowercased.

    Returns "" for anything that is not a requirement — a pip flag line, a
    blank line — so callers can filter on falsiness rather than re-parsing.
    """
    spec = spec.strip()
    if not spec or spec.startswith("-"):
        return ""
    for index, char in enumerate(spec):
        if char in _NAME_TERMINATORS:
            return spec[:index].strip().lower()
    return spec.lower()


def _safe_read(path: Path) -> str:
    """A manifest this process cannot read declares nothing it can act on."""
    try:
        return read_text(path)
    except OSError:
        return ""


def _pyproject_dependencies(text: str) -> list[str]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []

    specs: list[str] = []
    project = data.get("project")
    if isinstance(project, dict):
        specs += [s for s in project.get("dependencies", []) if isinstance(s, str)]
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                specs += [s for s in group if isinstance(s, str)]
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        for group in groups.values():
            specs += [s for s in group if isinstance(s, str)]

    tool = data.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    if isinstance(poetry, dict):
        for table in ("dependencies", "dev-dependencies"):
            block = poetry.get(table)
            if isinstance(block, dict):
                specs += [str(key) for key in block]

    return [name for name in (requirement_name(spec) for spec in specs) if name]


def _requirements_dependencies(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        name = requirement_name(line.split("#", 1)[0])
        if name:
            names.append(name)
    return names


def _package_json_dependencies(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    names: list[str] = []
    for table in ("dependencies", "devDependencies", "peerDependencies"):
        block = data.get(table)
        if isinstance(block, dict):
            names += [str(key).lower() for key in block]
    return names


def _gather(root: Path) -> dict[str, list[Path]]:
    """One walk, classifying every file this router might use as evidence."""
    found: dict[str, list[Path]] = {
        "pyproject": [], "setup_py": [], "requirements": [],
        "package_json": [], "tsconfig": [], "manage_py": [], "python_source": [],
    }
    for path in walk_files(root, source_only=False):
        name = path.name
        if name == "pyproject.toml":
            found["pyproject"].append(path)
        elif name == "setup.py":
            found["setup_py"].append(path)
        elif name.startswith("requirements") and name.endswith(".txt"):
            found["requirements"].append(path)
        elif name == "package.json":
            found["package_json"].append(path)
        elif name == "tsconfig.json":
            found["tsconfig"].append(path)
        elif name == "manage.py":
            found["manage_py"].append(path)
        if path.suffix == ".py":
            found["python_source"].append(path)
    return found


def _declared_by(found: dict[str, list[Path]]) -> dict[str, dict[str, list[Path]]]:
    """Ecosystem → dependency name → the manifests that declare it.

    Two namespaces, kept apart. A package name means something only inside the
    registry it was published to: npm has packages called `django` and
    `typescript`, PyPI has a distribution called `typescript`, and neither says
    anything about the other ecosystem. Merged into one table, a pure-JS repo
    whose `package.json` happened to list a dependency named `django` routed to
    python-code-doctor *and* django-code-doctor, citing `package.json` as the
    evidence — a Python doctor pointed at a repo with no Python in it, which
    then reports the missing `pyproject.toml` as a finding.

    Over-routing is the direction this router must not be wrong in (see the
    module docstring): under-routing still leaves the raw layer running and
    tells a grader which categories nobody measured, while over-routing
    fabricates findings.
    """
    table: dict[str, dict[str, list[Path]]] = {"python": {}, "npm": {}}
    readers = (
        ("python", found["pyproject"], _pyproject_dependencies),
        ("python", found["requirements"], _requirements_dependencies),
        ("npm", found["package_json"], _package_json_dependencies),
    )
    for ecosystem, paths, parse in readers:
        for path in paths:
            for name in parse(_safe_read(path)):
                table[ecosystem].setdefault(name, []).append(path)
    return table


def _settings_with_installed_apps(manage_files: list[Path]) -> list[Path]:
    """The settings module beside each manage.py, when one declares apps.

    The fallback for a Django project with no dependency manifest at all. One
    hit per manage.py is enough — this establishes the route, and listing every
    settings variant would bury the evidence rather than support it.
    """
    hits: list[Path] = []
    for manage in manage_files:
        for candidate in sorted(manage.parent.rglob("*.py")):
            relative = candidate.relative_to(manage.parent)
            if any(part in EXCLUDE_DIRS for part in relative.parts):
                continue
            if "INSTALLED_APPS" in _safe_read(candidate):
                hits.append(candidate)
                break
    return hits


def detect_routes(root: Path) -> dict:
    """The specialists this repository's manifests justify, with evidence."""
    root = root.resolve()
    found = _gather(root)
    declared = _declared_by(found)

    def rel(paths) -> tuple[str, ...]:
        return tuple(sorted({path.relative_to(root).as_posix() for path in paths}))

    routes: list[Route] = []
    notes: list[str] = []

    python_manifests = found["pyproject"] + found["setup_py"] + found["requirements"]
    # Django is a PyPI distribution, so only a Python manifest can declare it.
    # npm publishes a package called `django` too; a package.json listing it is
    # evidence about a JavaScript dependency and nothing else.
    django_manifests = declared["python"].get("django", [])
    settings_hits = _settings_with_installed_apps(found["manage_py"])
    django_evidence = [*django_manifests, *settings_hits]

    if python_manifests:
        routes.append(Route(PYTHON_DOCTOR,
                            "the repository declares a Python project",
                            rel(python_manifests)))
    elif django_evidence:
        # A Django project is a Python project. Without this, a repo whose only
        # evidence is manage.py plus a settings module would get the Django
        # doctor alone — and django-code-doctor ships no general duplication or
        # dead-code detector, so those categories would come back ungraded on a
        # codebase that has plenty of both.
        routes.append(Route(PYTHON_DOCTOR,
                            "a Django project is a Python project",
                            rel(django_evidence)))

    if django_evidence:
        reason = ("django is a declared dependency" if django_manifests
                  else "manage.py sits beside a settings module defining INSTALLED_APPS")
        routes.append(Route(DJANGO_DOCTOR, reason, rel(django_evidence)))

    # Likewise the compiler is an npm package. PyPI carries a `typescript`
    # distribution of its own, and a Python project that depends on it is still
    # not a TypeScript project.
    typescript_evidence = found["tsconfig"] + declared["npm"].get("typescript", [])
    if typescript_evidence:
        routes.append(Route(TYPESCRIPT_DOCTOR,
                            "the repository declares TypeScript",
                            rel(typescript_evidence)))

    if found["python_source"] and not any(r.skill == PYTHON_DOCTOR for r in routes):
        notes.append(
            f"{len(found['python_source'])} Python source file(s) are present but no manifest "
            "declares a Python project — raw layer only. A missing dependency manifest is worth "
            "reporting in its own right; it is not evidence that the specialist should run."
        )
    if len({route.skill for route in routes}) > 1:
        notes.append(
            "More than one ecosystem is declared. Every route below runs, and each doctor's "
            "findings are attributed to it in the merged report."
        )
    if not routes:
        notes.append(
            "No manifest declares a language this skill has a specialist for — raw layer only. "
            "Findings will be language-blind, and Correctness will be ungraded by any consumer "
            "that grades this report."
        )

    return {
        "routes": [{"skill": r.skill, "reason": r.reason, "evidence": list(r.evidence)}
                   for r in routes],
        "raw_only": not routes,
        "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    configure_output()
    parser = argparse.ArgumentParser(
        description="Which language specialists this repository's manifests justify running."
    )
    parser.add_argument("path", nargs="?", default=".", help="Repository root")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    try:
        result = detect_routes(Path(args.path))
    except ScanPathError as exc:
        return fail_on_bad_path(exc)

    if args.format == "json":
        print(json.dumps(result, indent=2))
        return 0

    if result["raw_only"]:
        print("No specialist routes — raw layer only.\n")
    for route in result["routes"]:
        print(f"→ load and run {route['skill']}: {route['reason']}")
        for item in route["evidence"]:
            print(f"    evidence: {item}")
        print()
    for note in result["notes"]:
        print(f"ℹ️  {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
