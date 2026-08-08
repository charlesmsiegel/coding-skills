# code-doctor Routing and Report Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `code-doctor` the two pieces that let one doctor call cover a whole repository — a router that names the language specialists the repo's own manifests justify, and a merge that unions every doctor's report into one envelope with each record attributed to its author.

**Architecture:** Two standalone stdlib scripts in `skills/code-doctor/scripts/`. `route.py` reads manifests and answers "which specialist skills does this repository justify running"; it never counts file extensions. `merge_reports.py` takes each doctor's JSON and emits one envelope carrying `doctors_run`, per-doctor coverage evidence, findings, and candidates — attributing every record without deduplicating any. A third task documents the two-stage protocol in `SKILL.md`, because the agent is half of it: the router names a skill, the agent loads and runs it, the merge combines the results.

**Tech Stack:** Python 3.11+, stdlib only (`tomllib`, `json`, `argparse`, `pathlib`, `dataclasses`). pytest for tests, driving each script as a subprocess over a throwaway git repo.

## Global Constraints

- **Python 3.11+, stdlib only, no network calls.** Every script in this skill.
- **Source of truth:** `docs/superpowers/specs/2026-08-07-code-overview-companions-design.md`, sections *code-doctor as the router* and *`merge_reports.py`*.
- **Prerequisite:** the code-doctor foundation plan (`docs/superpowers/plans/2026-08-07-code-doctor-foundation.md`) through **Task 8**. Tasks 1 and 2 below need only `common.py`, which exists. **Task 3 additionally requires foundation Task 9**, which writes the real `SKILL.md`; do not run Task 3 against the stub.
- **Routing is the one place language names may appear in this skill.** Naming a specialist cannot be done without naming a language. Everything else stays language-blind.
- **A route is justified by a manifest the repository wrote about itself, never by a filename census.** A Go service with three vendored Python scripts is not a Python project.
- **`merge_reports.py` attributes; it never deduplicates.** Collapsing a defect is a grading decision and belongs where the grade is computed.
- **Degrade audibly.** A report that could not be read is a named error in the envelope, never zero findings.
- **CI gates that must stay green:** `ruff check .`, `python tools/validate_skills.py`, `pytest -q`, and the bug-class detector ratchet (`skills/python-code-doctor/scripts/find_*.py` must report zero on `skills/code-doctor/scripts`).
- Tests use the existing `repo`, `run_script`, and `load_module` fixtures from `tests/conftest.py`.

---

### Task 1: `route.py` — which specialists the manifests justify

**Files:**
- Create: `skills/code-doctor/scripts/route.py`
- Test: `tests/code_doctor/test_route.py`

**Interfaces:**
- Consumes, from `skills/code-doctor/scripts/common.py`: `EXCLUDE_DIRS`, `ScanPathError`, `configure_output`, `fail_on_bad_path`, `read_text`, `walk_files`.
- Produces:
  - `PYTHON_DOCTOR = "python-code-doctor"`, `DJANGO_DOCTOR = "django-code-doctor"`, `TYPESCRIPT_DOCTOR = "typescript-code-doctor"`
  - `requirement_name(spec: str) -> str` — the distribution name at the head of a PEP 508 requirement, lowercased; `""` for a flag line.
  - `detect_routes(root: Path) -> dict` — `{"routes": [{"skill": str, "reason": str, "evidence": [str, ...]}], "raw_only": bool, "notes": [str, ...]}`. Evidence paths are repo-relative POSIX strings, sorted.
  - `main(argv: list[str] | None = None) -> int` — CLI: `route.py <path> [--format text|json]`.

- [ ] **Step 1: Write the failing test**

Create `tests/code_doctor/test_route.py`:

```python
"""Routing: which language specialists this repository's manifests justify.

The rule under test is the evidence rule. A route must come from a manifest the
repository wrote about itself, because the alternative — counting file
extensions — routes a Go service with one Python script to the Python doctor,
which then reports the missing `pyproject.toml` it was never supposed to have.
"""

import json
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"
SCRIPT = SKILL / "scripts" / "route.py"


def routes_for(repo, run_script) -> dict:
    result = run_script(SCRIPT, repo.path, "--format", "json")
    return json.loads(result.stdout)


def skills_in(payload) -> list[str]:
    return [route["skill"] for route in payload["routes"]]


def test_a_python_manifest_routes_to_the_python_doctor(repo, run_script):
    repo.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["httpx"]\n')
    repo.commit()

    payload = routes_for(repo, run_script)

    assert skills_in(payload) == ["python-code-doctor"]
    assert payload["routes"][0]["evidence"] == ["pyproject.toml"]
    assert payload["raw_only"] is False


def test_a_declared_django_dependency_adds_the_django_doctor(repo, run_script):
    repo.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["Django>=5.0"]\n')
    repo.commit()

    payload = routes_for(repo, run_script)

    assert skills_in(payload) == ["python-code-doctor", "django-code-doctor"]


def test_manage_py_beside_installed_apps_routes_to_both_without_any_manifest(repo, run_script):
    repo.write("manage.py", "import os\n")
    repo.write("site/settings.py", "INSTALLED_APPS = ['django.contrib.auth']\n")
    repo.commit()

    payload = routes_for(repo, run_script)

    assert skills_in(payload) == ["python-code-doctor", "django-code-doctor"]
    assert "site/settings.py" in payload["routes"][1]["evidence"]


def test_python_files_with_no_manifest_route_nowhere_and_say_so(repo, run_script):
    repo.write("tool.py", "x = 1\n")
    repo.commit()

    payload = routes_for(repo, run_script)

    assert payload["routes"] == []
    assert payload["raw_only"] is True
    assert any("no manifest" in note for note in payload["notes"])


def test_a_go_repo_with_a_stray_python_script_is_not_a_python_project(repo, run_script):
    repo.write("go.mod", "module example.com/m\n")
    repo.write("scripts/gen.py", "print(1)\n")
    repo.commit()

    payload = routes_for(repo, run_script)

    assert payload["raw_only"] is True, "a filename census would have routed this to Python"


def test_a_tsconfig_routes_to_the_typescript_doctor(repo, run_script):
    repo.write("tsconfig.json", '{"compilerOptions": {"strict": true}}')
    repo.commit()

    payload = routes_for(repo, run_script)

    assert skills_in(payload) == ["typescript-code-doctor"]


def test_a_typescript_dev_dependency_routes_without_a_tsconfig(repo, run_script):
    repo.write("package.json", '{"name": "x", "devDependencies": {"typescript": "^5.4.0"}}')
    repo.commit()

    payload = routes_for(repo, run_script)

    assert skills_in(payload) == ["typescript-code-doctor"]
    assert payload["routes"][0]["evidence"] == ["package.json"]


def test_manifests_inside_excluded_directories_are_not_evidence(repo, run_script):
    repo.write("go.mod", "module example.com/m\n")
    repo.write("node_modules/left-pad/package.json",
               '{"name": "left-pad", "devDependencies": {"typescript": "^5.0.0"}}')
    repo.commit()

    payload = routes_for(repo, run_script)

    assert payload["raw_only"] is True, "a dependency's own manifest is not this repo's declaration"


def test_two_ecosystems_both_route_and_the_overlap_is_noted(repo, run_script):
    repo.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["httpx"]\n')
    repo.write("tsconfig.json", '{"compilerOptions": {}}')
    repo.commit()

    payload = routes_for(repo, run_script)

    assert set(skills_in(payload)) == {"python-code-doctor", "typescript-code-doctor"}
    assert any("more than one ecosystem" in note.lower() for note in payload["notes"])


def test_no_declaration_at_all_says_correctness_will_be_ungraded(repo, run_script):
    repo.write("main.rs", "fn main() {}\n")
    repo.commit()

    payload = routes_for(repo, run_script)

    assert payload["raw_only"] is True
    assert any("ungraded" in note for note in payload["notes"])


def test_text_output_names_each_route_and_its_evidence(repo, run_script):
    repo.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["Django"]\n')
    repo.commit()

    result = run_script(SCRIPT, repo.path)

    assert "django-code-doctor" in result.stdout
    assert "pyproject.toml" in result.stdout


def test_a_missing_path_exits_two(repo, run_script):
    result = run_script(SCRIPT, repo.path / "nope", expect_rc=2)

    assert "no such file" in result.stderr.lower()


@pytest.fixture
def route(load_module):
    return load_module(SKILL / "scripts", "route")


@pytest.mark.parametrize("spec, expected", [
    ("Django>=5.0", "django"),
    ("django", "django"),
    ("Django[argon2]==5.0.1", "django"),
    ("django ; python_version >= '3.11'", "django"),
    ("  DJANGO  ", "django"),
    ("-e .", ""),
    ("-r other.txt", ""),
    ("", ""),
])
def test_requirement_name_takes_the_head_of_the_spec(route, spec, expected):
    assert route.requirement_name(spec) == expected
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/code_doctor/test_route.py -q`
Expected: every test FAILS — the script does not exist, so `run_script` cannot start it and `load_module` raises `ModuleNotFoundError`.

- [ ] **Step 3: Write the router**

Create `skills/code-doctor/scripts/route.py`:

```python
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


def _declared_by(found: dict[str, list[Path]]) -> dict[str, list[Path]]:
    """Dependency name → the manifests that declare it."""
    table: dict[str, list[Path]] = {}
    readers = (
        (found["pyproject"], _pyproject_dependencies),
        (found["requirements"], _requirements_dependencies),
        (found["package_json"], _package_json_dependencies),
    )
    for paths, parse in readers:
        for path in paths:
            for name in parse(_safe_read(path)):
                table.setdefault(name, []).append(path)
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
    django_manifests = declared.get("django", [])
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

    typescript_evidence = found["tsconfig"] + declared.get("typescript", [])
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/code_doctor/test_route.py -q`
Expected: PASS — 20 test cases (12 test functions plus 8 parametrized `requirement_name` cases).

- [ ] **Step 5: Verify the repo gates**

Run each and expect clean output:

```bash
ruff check skills/code-doctor/scripts/route.py
python skills/python-code-doctor/scripts/find_exception_issues.py skills/code-doctor/scripts --format json
python skills/python-code-doctor/scripts/find_resource_leaks.py skills/code-doctor/scripts --format json
python skills/python-code-doctor/scripts/find_mutation_hazards.py skills/code-doctor/scripts --format json
```

Expected: ruff silent; each detector prints `[]`. A non-empty list means the ratchet caught a real defect class — fix the script, do not add an ignore.

- [ ] **Step 6: Commit**

```bash
git add skills/code-doctor/scripts/route.py tests/code_doctor/test_route.py
git commit -m "code-doctor: route to language specialists on manifest evidence

Routing is the one place this skill may name a language, because naming a
specialist requires it. The evidence rule survives the exception: a route
comes from a manifest the repository wrote about itself, never from a
filename census, so a Go service with a stray Python script does not get
graded on a pyproject.toml it never had.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `merge_reports.py` — one envelope, every record attributed

**Files:**
- Create: `skills/code-doctor/scripts/merge_reports.py`
- Test: `tests/code_doctor/test_merge_reports.py`

**Interfaces:**
- Consumes: `configure_output` from `common.py`. Nothing from Task 1.
- Consumes, as input data, three report shapes it must all accept:
  - code-doctor's own: `{"completeness": {...}, "findings": [...]}` or a bare `[...]` list (from `common.emit`)
  - a specialist's `analyze_all.py`: `{"meta": {"analyzers_run": [...], "analyzer_errors": {...}, "analyzers_skipped": [...]}, "categories": {name: {"issues": [...]}}}`
  - a bare `[...]` list (`analyze_django.py`, a single detector)
- Produces:
  - `MERGE_SCHEMA = "code-doctor-merge/1"`
  - `read_report(path: Path) -> dict` — `{"records": [...], "analyzers_run": [...] | None, "analyzers_skipped": [...], "analyzer_errors": {...}, "completeness": {...}, "error": str}`. `error` non-empty means nothing else in the dict is trustworthy.
  - `merge(reports: list[tuple[str, Path]]) -> dict` — the envelope below.
  - `main(argv: list[str] | None = None) -> int` — CLI: `merge_reports.py --report <doctor>:<path> [--report ...] [--format text|json] [--out FILE]`. Exit 1 when no report contributed.

  The envelope:

  ```json
  {"schema": "code-doctor-merge/1",
   "doctors_run": ["code-doctor", "python-code-doctor"],
   "analyzers_run": {"python-code-doctor": ["find_security_issues"]},
   "analyzers_skipped": {"python-code-doctor": []},
   "analyzer_errors": {"python-code-doctor": {"find_duplicates": "timed out"}},
   "doctor_errors": {"django-code-doctor": "empty report — ..."},
   "completeness": {"code-doctor": {"reference_graph": {"resolution_rate": 0.41}}},
   "coverage_unknown": ["django-code-doctor"],
   "findings": [{"doctor": "...", "file": "...", "severity": "high"}],
   "candidates": [{"doctor": "code-doctor", "kind": "candidate", "also_caused_by": ["..."]}]}
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/code_doctor/test_merge_reports.py`:

```python
"""The merge: one envelope, every record attributed, nothing collapsed.

Two properties carry the design. Attribution, because nothing inside a report
says who wrote it and a consumer that credits one doctor's coverage to another
grades a language nobody analysed. And *no deduplication*, because collapsing
two reports of one defect changes a count someone will divide by — that is a
grading decision, and it belongs where the grade is computed.
"""

import json
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"
SCRIPT = SKILL / "scripts" / "merge_reports.py"


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def merge(run_script, *reports, expect_rc=0) -> dict:
    args = []
    for label, path in reports:
        args += ["--report", f"{label}:{path}"]
    result = run_script(SCRIPT, *args, "--format", "json", expect_rc=expect_rc)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def test_every_record_carries_the_doctor_that_produced_it(run_script, tmp_path):
    a = write_json(tmp_path / "a.json", [{"file": "x.py", "line": 1, "smell_type": "secret"}])
    b = write_json(tmp_path / "b.json", [{"file": "y.ts", "line": 2, "smell_type": "any_type"}])

    envelope = merge(run_script,
                     ("code-doctor", a), ("typescript-code-doctor", b))

    doctors = {record["doctor"] for record in envelope["findings"]}
    assert doctors == {"code-doctor", "typescript-code-doctor"}
    assert envelope["doctors_run"] == ["code-doctor", "typescript-code-doctor"]


def test_identical_findings_from_two_doctors_both_survive(run_script, tmp_path):
    same = {"file": "settings.py", "line": 7, "smell_type": "hardcoded_secret",
            "severity": "high", "description": "SECRET_KEY", "suggestion": "move it"}
    a = write_json(tmp_path / "a.json", [same])
    b = write_json(tmp_path / "b.json", [dict(same)])

    envelope = merge(run_script,
                     ("code-doctor", a), ("django-code-doctor", b))

    assert len(envelope["findings"]) == 2, "the merge attributes; it must not deduplicate"


def test_candidates_are_separated_from_findings_by_kind(run_script, tmp_path):
    report = write_json(tmp_path / "cd.json", {
        "completeness": {"reference_graph": "resolution 0.41"},
        "findings": [
            {"file": "a.go", "line": 1, "smell_type": "merge_marker", "kind": "finding",
             "suggestion": "resolve the conflict"},
            {"file": "b.go", "line": 9, "smell_type": "zero_inbound_file", "kind": "candidate",
             "also_caused_by": ["an executable entry point has no internal referrer"]},
        ],
    })

    envelope = merge(run_script, ("code-doctor", report))

    assert [f["smell_type"] for f in envelope["findings"]] == ["merge_marker"]
    assert [c["smell_type"] for c in envelope["candidates"]] == ["zero_inbound_file"]
    assert envelope["completeness"]["code-doctor"] == {"reference_graph": "resolution 0.41"}


def test_a_specialist_envelope_keeps_its_coverage_evidence(run_script, tmp_path):
    report = write_json(tmp_path / "py.json", {
        "meta": {"analyzers_run": ["find_security_issues", "find_duplicates"],
                 "analyzer_errors": {"find_complexity": "timed out"},
                 "analyzers_skipped": ["find_type_gaps"]},
        "categories": {"security": {"issues": [{"file": "s.py", "line": 3,
                                                "issue_type": "hardcoded_secret"}]}},
    })

    envelope = merge(run_script, ("python-code-doctor", report))

    assert envelope["analyzers_run"]["python-code-doctor"] == [
        "find_security_issues", "find_duplicates"]
    assert envelope["analyzer_errors"]["python-code-doctor"] == {"find_complexity": "timed out"}
    assert envelope["analyzers_skipped"]["python-code-doctor"] == ["find_type_gaps"]
    assert envelope["coverage_unknown"] == []


def test_a_category_shaped_report_stamps_its_section_name_on_each_issue(run_script, tmp_path):
    report = write_json(tmp_path / "py.json", {
        "meta": {"analyzers_run": ["find_security_issues"]},
        "categories": {"security": {"issues": [{"file": "s.py", "line": 3,
                                                "issue_type": "hardcoded_secret"}]}},
    })

    envelope = merge(run_script, ("python-code-doctor", report))

    assert envelope["findings"][0]["category"] == "security"


def test_a_bare_list_evidences_no_coverage(run_script, tmp_path):
    report = write_json(tmp_path / "dj.json", [{"file": "m.py", "line": 4,
                                                "smell_type": "n_plus_one_query"}])

    envelope = merge(run_script, ("django-code-doctor", report))

    assert envelope["coverage_unknown"] == ["django-code-doctor"]
    assert envelope["analyzers_run"]["django-code-doctor"] == []


def test_an_empty_file_is_a_failed_doctor_not_a_clean_one(run_script, tmp_path):
    empty = tmp_path / "dj.json"
    empty.write_text("", encoding="utf-8")
    good = write_json(tmp_path / "py.json", [{"file": "a.py", "line": 1, "smell_type": "x"}])

    envelope = merge(run_script, ("python-code-doctor", good),
                     ("django-code-doctor", empty))

    assert "django-code-doctor" in envelope["doctor_errors"]
    assert "django-code-doctor" not in envelope["doctors_run"]
    assert len(envelope["findings"]) == 1


def test_invalid_json_is_a_named_doctor_error(run_script, tmp_path):
    broken = tmp_path / "py.json"
    broken.write_text("{not json", encoding="utf-8")

    envelope = merge(run_script, ("python-code-doctor", broken), expect_rc=1)

    assert "python-code-doctor" in envelope["doctor_errors"]


def test_a_missing_file_is_a_named_doctor_error(run_script, tmp_path):
    envelope = merge(run_script,
                     ("python-code-doctor", tmp_path / "absent.json"), expect_rc=1)

    assert "python-code-doctor" in envelope["doctor_errors"]


def test_every_report_failing_exits_one(run_script, tmp_path):
    broken = tmp_path / "a.json"
    broken.write_text("{", encoding="utf-8")

    result = run_script(SCRIPT, "--report", f"code-doctor:{broken}",
                        "--format", "json", expect_rc=1)

    envelope = json.loads(result.stdout)
    assert envelope["doctors_run"] == []
    assert envelope["findings"] == []


def test_a_report_argument_without_a_label_is_rejected(run_script, tmp_path):
    good = write_json(tmp_path / "a.json", [])

    result = run_script(SCRIPT, "--report", str(good), expect_rc=2)

    assert "doctor:path" in result.stderr


def test_out_writes_the_envelope_to_a_file(run_script, tmp_path):
    report = write_json(tmp_path / "a.json", [{"file": "a.py", "line": 1, "smell_type": "x"}])
    out = tmp_path / "merged.json"

    run_script(SCRIPT, "--report", f"code-doctor:{report}", "--out", out)

    assert json.loads(out.read_text(encoding="utf-8"))["schema"] == "code-doctor-merge/1"


def test_text_output_names_the_doctors_and_the_failures(run_script, tmp_path):
    good = write_json(tmp_path / "a.json", [{"file": "a.py", "line": 1, "smell_type": "x"}])
    empty = tmp_path / "b.json"
    empty.write_text("", encoding="utf-8")

    result = run_script(SCRIPT, "--report", f"code-doctor:{good}",
                        "--report", f"django-code-doctor:{empty}")

    assert "code-doctor" in result.stdout
    assert "django-code-doctor" in result.stdout
    assert "failed" in result.stdout.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/code_doctor/test_merge_reports.py -q`
Expected: every test FAILS — `merge_reports.py` does not exist.

- [ ] **Step 3: Write the merge**

Create `skills/code-doctor/scripts/merge_reports.py`:

```python
#!/usr/bin/env python3
"""Union several doctors' reports into one envelope, attributing every record.

Two properties define this script.

**It attributes.** Nothing inside a report says who wrote it, and the same
report gets handed to several consumers. An unattributed TypeScript report
reaching a Python package's grade credits Python coverage that nothing
measured — an A+ from another language's analysis. Every record leaves here
stamped with its doctor.

**It does not deduplicate.** Two security detectors legitimately flag the same
hardcoded key, and collapsing them changes a count that something downstream
divides by. Which severity survives, and whether one identity counts once or
twice, are grading decisions — they belong wherever the grade is computed, with
the metadata field that records how many were merged. A merge that quietly
halved a finding count would make every number after it unexplainable.

A report that could not be read is a named entry in `doctor_errors`, never zero
findings: a doctor that produced no output failed, and "failed" and "found
nothing" must not arrive at a grader as the same fact.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import configure_output

MERGE_SCHEMA = "code-doctor-merge/1"


def _blank_report() -> dict:
    return {"records": [], "analyzers_run": None, "analyzers_skipped": [],
            "analyzer_errors": {}, "completeness": {}, "error": ""}


def _from_categories(data: dict, report: dict) -> None:
    """A specialist's analyze_all envelope: sections plus a meta block."""
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    listed = meta.get("analyzers_run")
    categories = data.get("categories")

    report["analyzers_run"] = ([str(name) for name in listed] if isinstance(listed, list)
                               else sorted(categories))
    report["analyzers_skipped"] = [str(name) for name in (meta.get("analyzers_skipped") or [])]
    report["analyzer_errors"] = {str(k): str(v)
                                 for k, v in (meta.get("analyzer_errors") or {}).items()}

    for name, payload in categories.items():
        issues = payload.get("issues", []) if isinstance(payload, dict) else []
        for issue in issues:
            if isinstance(issue, dict):
                issue.setdefault("category", name)
                report["records"].append(issue)


def read_report(path: Path) -> dict:
    """Parse one doctor's JSON into a common record shape.

    A non-empty ``error`` means nothing else in the returned dict is
    trustworthy, and the caller must treat the doctor as having failed rather
    than as having found nothing.
    """
    report = _blank_report()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        report["error"] = f"report could not be read ({exc})"
        return report

    if not text.strip():
        report["error"] = (
            "empty report — a doctor that produced no output failed; it did not find nothing. "
            "Re-run it, or declare what the surviving reports examined."
        )
        return report

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        report["error"] = f"report is not valid JSON ({exc})"
        return report

    if isinstance(data, list):
        # A bare list carries no evidence of what ran. analyze_django.py and a
        # single detector's --format json emit the same shape, so nothing here
        # distinguishes a full run from one detector that found nothing.
        report["records"] = [item for item in data if isinstance(item, dict)]
        return report

    if not isinstance(data, dict):
        report["error"] = f"unrecognised report shape: {type(data).__name__}"
        return report

    if isinstance(data.get("categories"), dict):
        _from_categories(data, report)
        return report

    if isinstance(data.get("findings"), list):
        report["records"] = [item for item in data["findings"] if isinstance(item, dict)]
        completeness = data.get("completeness")
        report["completeness"] = completeness if isinstance(completeness, dict) else {}
        # code-doctor's own detectors inventory what ran under
        # `categories_run`. An absent key is unknown coverage, NOT an empty
        # run: defaulting to [] here would tell a grader that nothing ran,
        # which reads as "every category is unmeasured" for a scan that
        # actually completed. Leaving it None routes the doctor to
        # coverage_unknown, which is the honest answer.
        ran = report["completeness"].get("categories_run")
        report["analyzers_run"] = sorted(ran) if isinstance(ran, list) else None
        return report

    if isinstance(data.get("issues"), list):
        report["records"] = [item for item in data["issues"] if isinstance(item, dict)]
        return report

    report["error"] = "unrecognised report shape: no findings, categories, or issues"
    return report


def merge(reports: list[tuple[str, Path]]) -> dict:
    envelope = {
        "schema": MERGE_SCHEMA,
        "doctors_run": [],
        "analyzers_run": {},
        "analyzers_skipped": {},
        "analyzer_errors": {},
        "doctor_errors": {},
        "completeness": {},
        "coverage_unknown": [],
        "findings": [],
        "candidates": [],
    }

    for doctor, path in reports:
        parsed = read_report(path)
        if parsed["error"]:
            envelope["doctor_errors"][doctor] = parsed["error"]
            continue

        envelope["doctors_run"].append(doctor)
        if parsed["analyzers_run"] is None:
            envelope["coverage_unknown"].append(doctor)
            envelope["analyzers_run"][doctor] = []
        else:
            envelope["analyzers_run"][doctor] = parsed["analyzers_run"]
        envelope["analyzers_skipped"][doctor] = parsed["analyzers_skipped"]
        if parsed["analyzer_errors"]:
            envelope["analyzer_errors"][doctor] = parsed["analyzer_errors"]
        if parsed["completeness"]:
            envelope["completeness"][doctor] = parsed["completeness"]

        for record in parsed["records"]:
            attributed = {**record, "doctor": doctor}
            bucket = "candidates" if record.get("kind") == "candidate" else "findings"
            envelope[bucket].append(attributed)

    return envelope


def _parse_report_argument(value: str) -> tuple[str, Path]:
    doctor, separator, path = value.partition(":")
    # A Windows drive letter makes the first colon ambiguous, so require a
    # non-empty label AND a non-empty remainder rather than splitting blindly.
    if not separator or not doctor.strip() or not path.strip() or len(doctor.strip()) == 1:
        raise argparse.ArgumentTypeError(
            f"--report takes doctor:path (got {value!r}); the label is how a finding "
            "keeps the name of the doctor that produced it"
        )
    return doctor.strip(), Path(path.strip())


def _print_text(envelope: dict) -> None:
    for doctor in envelope["doctors_run"]:
        analyzers = envelope["analyzers_run"].get(doctor, [])
        coverage = (f"{len(analyzers)} analyzer(s)" if analyzers
                    else "no coverage evidence — a bare list says nothing about what ran")
        print(f"✅ {doctor}: {coverage}")
    for doctor, message in envelope["doctor_errors"].items():
        print(f"⚠️  {doctor}: failed — {message}")
    print()
    print(f"{len(envelope['findings'])} finding(s), {len(envelope['candidates'])} candidate(s) "
          f"from {len(envelope['doctors_run'])} doctor(s). Not deduplicated — the same defect "
          "reported by two doctors appears twice, and collapsing it is the grader's decision.")


def main(argv: list[str] | None = None) -> int:
    configure_output()
    parser = argparse.ArgumentParser(
        description="Union several doctors' reports into one attributed envelope."
    )
    parser.add_argument("--report", action="append", required=True, type=_parse_report_argument,
                        metavar="DOCTOR:PATH", help="A doctor's JSON report, labelled")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--out", type=Path, default=None,
                        help="Write the JSON envelope here as well as reporting")
    args = parser.parse_args(argv)

    envelope = merge(args.report)
    serialized = json.dumps(envelope, indent=2)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized, encoding="utf-8")

    if args.format == "json":
        print(serialized)
    else:
        _print_text(envelope)

    # Every report failing is not a clean repository; it is a merge with no
    # input. Exiting zero here would let a caller write an A+ health page from
    # nothing at all.
    return 1 if not envelope["doctors_run"] else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/code_doctor/test_merge_reports.py -q`
Expected: PASS, 13 tests.

- [ ] **Step 5: Verify the repo gates**

```bash
ruff check skills/code-doctor/scripts/merge_reports.py
python skills/python-code-doctor/scripts/find_exception_issues.py skills/code-doctor/scripts --format json
python skills/python-code-doctor/scripts/find_mutation_hazards.py skills/code-doctor/scripts --format json
python skills/python-code-doctor/scripts/find_global_state.py skills/code-doctor/scripts --format json
pytest tests/code_doctor -q
```

Expected: ruff silent, each detector prints `[]`, the whole code-doctor suite passes.

- [ ] **Step 6: Commit**

```bash
git add skills/code-doctor/scripts/merge_reports.py tests/code_doctor/test_merge_reports.py
git commit -m "code-doctor: merge every doctor's report into one attributed envelope

Attribution, because nothing inside a report says who wrote it and crediting
one doctor's coverage to another grades a language nobody analysed. No
deduplication, because collapsing two reports of one defect changes a count
something downstream divides by — that is the grader's decision, made where
the merged count can be recorded beside it.

An unreadable or empty report is a named doctor_error, never zero findings:
a doctor that produced no output failed, and a grader must not read that as
a clean bill of health.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: The two-stage protocol in `SKILL.md`

**Requires foundation Task 9 to have written the real `SKILL.md`.** Run `head -5 skills/code-doctor/SKILL.md` first; if the description still says "Under construction — not yet ready for use", stop and finish the foundation plan.

**Files:**
- Modify: `skills/code-doctor/SKILL.md` (add a "Routing to the specialists" section after the workflow, before the reference index)
- Modify: `evals/code-doctor/evals.json` (append two cases)
- Modify: `README.md` (one line in the skill table's code-doctor row)
- Test: `tests/code_doctor/test_skill_contract.py`

**Interfaces:**
- Consumes: `route.py`'s CLI and JSON shape (Task 1), `merge_reports.py`'s CLI (Task 2).
- Produces: no code. The documented protocol the *agent* executes, and a test that the documentation names the scripts it depends on.

- [ ] **Step 1: Write the failing test**

Create `tests/code_doctor/test_skill_contract.py`:

```python
"""SKILL.md is half the routing protocol, so it is tested like code.

route.py names a specialist; only the agent can load a skill. If SKILL.md
stops describing that hand-off, the router still exits zero and the specialists
silently never run — a partial review that reads exactly like a complete one.
"""

import json
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"


def skill_text() -> str:
    return (SKILL / "SKILL.md").read_text(encoding="utf-8")


def test_skill_md_is_no_longer_the_under_construction_stub():
    assert "Under construction" not in skill_text(), (
        "finish the code-doctor foundation plan's Task 9 before this one"
    )


def test_the_routing_section_names_both_scripts():
    text = skill_text()
    assert "route.py" in text
    assert "merge_reports.py" in text


def test_the_routing_section_names_every_specialist_route_py_can_emit():
    text = skill_text()
    for specialist in ("python-code-doctor", "django-code-doctor", "typescript-code-doctor"):
        assert specialist in text, f"{specialist} is routable but undocumented"


def test_the_protocol_says_the_agent_loads_the_skill():
    text = skill_text().lower()
    assert "load" in text and "route.py" in text, (
        "a script cannot invoke a skill; SKILL.md must tell the agent to do it"
    )


def test_the_evidence_rule_is_stated_where_someone_would_change_it():
    assert "manifest" in skill_text().lower()


def test_evals_cover_routing_and_the_empty_report_trap():
    payload = json.loads((SKILL.parent.parent / "evals" / "code-doctor" / "evals.json")
                         .read_text(encoding="utf-8"))
    prompts = " ".join(case["prompt"] + case["expected_output"] for case in payload["evals"])
    assert "route" in prompts.lower()
    assert "empty" in prompts.lower() or "failed" in prompts.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/code_doctor/test_skill_contract.py -q`
Expected: FAIL — `route.py` and `merge_reports.py` are not mentioned in `SKILL.md`, and the evals do not cover routing.

- [ ] **Step 3: Add the routing section to `SKILL.md`**

Insert this section immediately before the reference-index table in `skills/code-doctor/SKILL.md`:

```markdown
## Routing to the specialists

The raw layer works on any repository. Where the project declares a language
this repo has a specialist for, that specialist finds things the raw layer
cannot, so run it too. **Ask the repository, not the filenames:**

```bash
python "$SKILL/scripts/route.py" <repo> --format json
```

It answers with the specialists the repo's own manifests justify, each with the
evidence: a declared dependency, a `tsconfig.json`, a `manage.py` beside a
settings module defining `INSTALLED_APPS`. A Go service containing three Python
scripts routes nowhere, on purpose — a filename census would send it to
`python-code-doctor`, which would report the missing `pyproject.toml` it was
never supposed to have as a finding.

**You are half of this protocol.** A Python subprocess cannot load a skill, so
`route.py` names one and stops. For each route, load that skill and run its
`analyze_all.py` over **the repository root** — never a package subdirectory,
where a doctor cannot see the root manifest or the test tree and reports their
absence as findings.

Keep every report, then union them:

```bash
python "$SKILL/scripts/merge_reports.py" \
  --report code-doctor:$WORK/raw.json \
  --report python-code-doctor:$WORK/py.json \
  --report django-code-doctor:$WORK/dj.json \
  --out $WORK/merged.json --format json
```

The envelope carries `doctors_run`, per-doctor `analyzers_run`, `doctor_errors`,
`completeness`, and every record stamped with the doctor that produced it —
findings and candidates in separate lists. **It does not deduplicate**: two
security detectors flagging one hardcoded key stay two records, because
collapsing them changes a count a grader divides by, and that decision belongs
to the grader.

Three results are not the same and the envelope keeps them apart:

- **A doctor that ran** appears in `doctors_run` with its `analyzers_run`.
- **A doctor that failed** appears in `doctor_errors`. An empty report file is
  a failure, not a clean result — re-run it, and if you cannot, say which
  categories now rest on nothing.
- **A doctor with no coverage evidence** — a bare JSON list, which is what a
  single detector and a whole `analyze_django.py` run both emit — appears in
  `coverage_unknown`. Nothing in the file distinguishes a full run from one
  detector that found nothing, so it grants no coverage.

`merge_reports.py` exits 1 when every report failed. That is not a clean
repository; it is a merge with no input.

**If a routed specialist is not installed**, say so and continue with the raw
layer. Correctness is the category that suffers: the only correctness-class
defect this skill can prove on its own is a merge marker in a path git reports
as unmerged, so a consumer that grades this report should leave Correctness
ungraded rather than score it from silence.
```

- [ ] **Step 4: Append the eval cases**

Add these two cases to the `evals` list in `evals/code-doctor/evals.json`:

```json
{
  "id": "routes-on-manifest-evidence-not-filenames",
  "prompt": "Review this repo. It's a Go service — there's a go.mod at the root, a few thousand lines of Go, and a scripts/gen.py that regenerates some fixtures.",
  "expected_output": "Runs route.py (or reasons the same way) and does NOT run python-code-doctor: no manifest declares a Python project, so the single .py file is not evidence. Says the raw layer is what ran, and that Correctness will be ungraded because no specialist could examine the Go. Does not report a missing pyproject.toml as a defect of the Go service."
},
{
  "id": "an-empty-report-is-a-failure-not-a-clean-result",
  "prompt": "I ran both doctors and merged them, but the django report file came out 0 bytes. The python one looks fine — can you give me the summary?",
  "expected_output": "Treats the empty report as django-code-doctor having failed, not as it having found nothing. Names the categories that now rest only on the Python report, and either re-runs the Django doctor or states explicitly what the surviving reports examined. Does not present the merged result as a clean or complete review."
}
```

- [ ] **Step 5: Add the README line**

In `README.md`'s skill table, extend the `code-doctor` row's description with:

```
Routes to the language specialists on manifest evidence and merges every doctor's report into one attributed envelope.
```

- [ ] **Step 6: Run the tests and the validator**

```bash
pytest tests/code_doctor -q
python tools/validate_skills.py
ruff check .
```

Expected: all pass. `validate_skills.py` checks the frontmatter, the ≤1024-character description, and the skill/evals pairing — the appended eval cases must not break the schema.

- [ ] **Step 7: Commit**

```bash
git add skills/code-doctor/SKILL.md evals/code-doctor/evals.json README.md tests/code_doctor/test_skill_contract.py
git commit -m "code-doctor: document the two-stage routing protocol

A script cannot load a skill, so route.py names one and stops and the agent
runs it. That makes SKILL.md half the protocol, which is why it is tested:
if the hand-off stops being documented, the router still exits zero and the
specialists silently never run — a partial review that reads exactly like a
complete one.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Self-review notes

Checked against the spec's *code-doctor as the router* and *`merge_reports.py`* sections:

| Spec requirement | Task |
|---|---|
| Manifest-evidence detection, four rows of the routing table | 1 |
| Django routes to Python *and* Django | 1 (both the declared-dependency and `manage.py` paths) |
| Handoff names the skill for the agent to load | 3 |
| Envelope with `doctors_run`, `analyzers_run`, `doctor_errors`, `completeness`, `findings`, `candidates` | 2 |
| Attributes but does not deduplicate | 2 (`test_identical_findings_from_two_doctors_both_survive`) |
| A bare list grants no coverage | 2 (`coverage_unknown`) |
| A zero-byte report means a doctor failed | 2 (`test_an_empty_file_is_a_failed_doctor_not_a_clean_one`) |
| An envelope claiming a doctor with no `analyzers_run` is an error, not full coverage | 2 (`coverage_unknown`, consumed by Plan 3's grading) |
| code-doctor alone does not claim Correctness | 3 (documented; enforced in Plan 3's rubric) |

**Not in this plan, by design:** the rubric changes that consume this envelope, and the health page's Candidates tab. Both live in Plan 3 (code-overview), which is where grading happens.
