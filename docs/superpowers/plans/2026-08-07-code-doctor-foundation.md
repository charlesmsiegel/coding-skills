# code-doctor Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundation of the `code-doctor` skill — the source walk, the finding/candidate schema that enforces the confidence discipline, the git helpers, two complete detectors, the orchestrator, and `SKILL.md` — so the skill is installable, releasable, and useful on its own.

**Architecture:** A stdlib-only Python skill directory under `skills/code-doctor/`, matching the shape of `python-code-doctor` and `typescript-code-doctor`: `SKILL.md` + `scripts/` + `references/`. Every detector is a standalone CLI sharing one `common.py`. The distinguishing feature is the schema: a `Finding` asserts a defect and *must* carry a fix; a `Candidate` reports a lead and *must* carry the benign explanations and *must not* carry a fix. That constraint is enforced in `__post_init__`, so a detector cannot dress a guess as a defect.

**Tech Stack:** Python 3.11+, standard library only. pytest for tests. No third-party runtime dependencies of any kind.

## Global Constraints

Copied from `docs/superpowers/specs/2026-08-07-code-doctor-design.md` and the repo's `README.md`. Every task's requirements implicitly include these.

- **Python 3.11+, stdlib only.** No third-party imports in `scripts/`. CI runs the suite on 3.11 and 3.12.
- **No PEP 701 f-strings.** No nested same-quote expressions, no backslashes in the expression part. A skill installs next to whatever interpreter the user already has, and these become a `SyntaxError` raised from inside a subprocess.
- **No skill may reach into another skill's directory.** No `../other-skill/...` path anywhere. A release archive holds exactly one skill.
- **`SKILL.md` commands use `python "$SKILL/scripts/..."`,** never bare `python scripts/...`. `tools/validate_skills.py` fails the build on the bare form.
- **Frontmatter is exactly `name` and `description`.** `name` must equal the directory name (`code-doctor`). `description` must be ≤ 1024 characters.
- **`evals/code-doctor/evals.json` must exist** with `skill_name: "code-doctor"` and a non-empty `evals` list, each case having `id`, `prompt`, and `expected_output`.
- **Detector CLI is uniform:** `--format text|json`, `--ignore type1,type2`, severities `high`/`medium`/`low` rendering as 🔴/🟡/🟢.
- **Confidence discipline.** Two output kinds. A `finding` asserts a defect: it carries a `suggestion` and no `also_caused_by`. A `candidate` reports a lead: it carries a non-empty `also_caused_by` and an empty `suggestion`. Enforced in the dataclass, not in prose.
- **Degrade audibly.** A detector whose evidence can be incomplete emits a `completeness` record alongside its findings. Never compute silently over a fragment.
- **Conservative by design.** False negatives are preferred over false positives, and a file that cannot be read is named on stderr rather than counted clean.
- **`ruff check .` must pass**, and python-code-doctor's bug-class detectors must stay silent on `skills/code-doctor/scripts/` (the CI ratchet).

## File Structure

```
skills/code-doctor/
  SKILL.md                        router: three layers, deferral table, reference index
  scripts/
    common.py                     walk + source classification, Finding/Candidate schema,
                                  Reporter, CLI builder, emit, git helpers
    find_hygiene_issues.py        merge markers, oversized files, committed .env/binaries,
                                  TODO inventory, commented-out code (candidate)
    find_secrets.py               key material and high-entropy secret assignments
    analyze_all.py                orchestrator, unified report, --skip
    format_findings.py            list / cards / JSON artifact renderer
  references/
    critical-review-guide.md      stance, per-unit questions, triage rubric, finding format
    unknown-language-review.md    reviewing a language you do not know; heuristic limits
tests/code_doctor/
  test_common.py                  walk classification, schema enforcement, git helpers
  test_find_hygiene_issues.py
  test_find_secrets.py
  test_analyze_all.py
  test_format_findings.py
evals/code-doctor/evals.json
```

`common.py` carries three responsibilities (walk, schema, git) rather than three files because every detector imports all three and the repo's existing skills keep them together. It stays under ~300 lines; if a later plan pushes it past that, split it then.

The remaining five families — git signals, the reference graph, duplication and complexity, the toolchain runner, and the five remaining judgment guides — are **out of scope for this plan** and get their own plans in sequence.

---

### Task 1: The source walk and its non-code denylist

**Files:**
- Create: `skills/code-doctor/scripts/common.py`
- Test: `tests/code_doctor/test_common.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `EXCLUDE_DIRS: frozenset[str]`, `NON_CODE_SUFFIXES: frozenset[str]`, `NON_CODE_BASENAMES: frozenset[str]`, `DOC_DIR_NAMES: frozenset[str]`, `is_probably_binary(path: Path) -> bool`, `ScanPathError`, `walk_paths(root: Path) -> Iterator[Path]` (metadata scope — binaries included, symlinks never followed, excluded directories pruned during traversal, raises `ScanPathError` on a missing root), `walk_files(root: Path, *, source_only: bool) -> Iterator[Path]` (text scope), `configure_output() -> None`, `SEVERITY_ICONS: dict[str, str]`, `SEVERITY_RANK: dict[str, int]`

- [ ] **Step 1: Create the test directory and write the failing test**

Create `tests/code_doctor/test_common.py`:

```python
"""The walk: what counts as source, what counts as text, what is skipped."""

import pytest

SCRIPTS = None  # set by the fixture below


@pytest.fixture
def common(load_module):
    from pathlib import Path
    scripts = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor" / "scripts"
    return load_module(scripts, "common")


def test_unknown_extension_is_treated_as_source(common, repo):
    """Language-blindness: a language nobody has heard of is still code."""
    repo.write("main.zig", "const x = 1;\n")
    repo.write("thing.somelang", "whatever\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert found == {"main.zig", "thing.somelang"}


def test_known_non_code_text_is_excluded_from_source(common, repo):
    """Prose and generated data must not reach the code-only detectors."""
    repo.write("README.md", "# hi\n")
    repo.write("data.json", "{}\n")
    repo.write("Cargo.lock", "[[package]]\n")
    repo.write("main.go", "package main\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert found == {"main.go"}


def test_non_source_walk_still_yields_text_files(common, repo):
    """Secrets and merge markers are real findings in a YAML file too."""
    repo.write("README.md", "# hi\n")
    repo.write("main.go", "package main\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=False)}
    assert found == {"README.md", "main.go"}


def test_vendor_directories_are_skipped(common, repo):
    repo.write("node_modules/pkg/index.js", "module.exports = 1\n")
    repo.write("src/app.js", "export const a = 1\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert found == {"app.js"}


def test_binary_files_are_never_yielded(common, repo):
    (repo.path / "blob.bin").write_bytes(b"\x00\x01\x02\x03binary")
    repo.write("main.go", "package main\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=False)}
    assert found == {"main.go"}


def test_documentation_directory_is_not_source(common, repo):
    repo.write("docs/guide.rst", "Guide\n=====\n")
    repo.write("docs/example.py", "print(1)\n")
    repo.write("app.py", "print(2)\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert found == {"app.py"}


def test_symlinks_are_never_followed(common, repo, tmp_path):
    """A link out of the tree would otherwise let this skill read host data
    and report a credential found there as committed to this repository."""
    outside = tmp_path / "outside.txt"
    outside.write_text("AWS_SECRET=hJ8s0Kd93LwmZq2XvRt7YbNc4PfGh6Aa\n")
    repo.write("app.go", "package main\n")
    (repo.path / "link.go").symlink_to(outside)
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert found == {"app.go"}


def test_bin_directory_is_scanned(common, repo):
    """Ruby gems and shell CLIs keep executable source in bin/."""
    repo.write("bin/console", "#!/usr/bin/env ruby\nputs 1\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert "console" in found


def test_missing_root_raises_rather_than_reporting_clean(common, tmp_path):
    """A typo in an audit path must not produce an authoritative empty report."""
    with pytest.raises(common.ScanPathError):
        list(common.walk_files(tmp_path / "nope", source_only=True))


def test_excluded_directories_are_not_descended_into(common, repo, monkeypatch):
    """Pruning during traversal, not filtering after enumeration."""
    repo.write("node_modules/pkg/deep/nested/index.js", "module.exports = 1\n")
    repo.write("src/app.js", "export const a = 1\n")
    seen = []
    real_walk = common.os.walk

    def spy(top, **kwargs):
        for dirpath, dirnames, filenames in real_walk(top, **kwargs):
            seen.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(common.os, "walk", spy)
    list(common.walk_files(repo.path, source_only=True))
    assert not any("node_modules" in d for d in seen), (
        "walked into an excluded tree instead of pruning it"
    )


def test_walk_paths_yields_binaries_for_metadata_checks(common, repo):
    (repo.path / "blob.bin").write_bytes(b"\x00" * 32)
    repo.write("app.go", "package main\n")
    assert {p.name for p in common.walk_paths(repo.path)} == {"blob.bin", "app.go"}
    assert {p.name for p in common.walk_files(repo.path, source_only=False)} == {"app.go"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/code_doctor/test_common.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'common'` (the skill directory does not exist yet).

- [ ] **Step 3: Write the walk**

Create `skills/code-doctor/scripts/common.py`:

```python
#!/usr/bin/env python3
"""Shared plumbing for the code-doctor detectors.

This skill is language-blind: it has no parsers, no comment-syntax tables, and
no framework knowledge. That buys it the ability to review a repo written in
anything, and it costs it the ability to prove most of what it observes — so
the finding/candidate split below is the load-bearing part of this module, not
a formality.
"""

import contextlib
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

SEVERITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

# Never the user's own code. Matched against path segments below the scanned
# root, so a repo living inside a directory with one of these names is fine.
EXCLUDE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components", "vendor", "third_party",
    ".venv", "venv", "__pycache__", ".tox", ".nox", ".eggs", "site-packages",
    "build", "dist", "out", "target", "obj",
    ".next", ".nuxt", ".svelte-kit", ".astro", ".angular",
    "coverage", ".nyc_output", ".turbo", ".cache", ".parcel-cache",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".gradle", ".idea",
})
# `bin` is deliberately NOT excluded. Ruby gems, shell CLIs, and plenty of
# other projects keep executable *source* there, and excluding it would hide
# those entry points from even the secrets and merge-marker checks. The build
# outputs that share the name (node_modules/.bin, target/) are already covered
# by their parents.

# The inverse of a language table. Enumerating what is NOT code means an
# unknown extension is still treated as code, which is what keeps this skill
# language-blind — a table of known languages would silently skip the next one.
NON_CODE_SUFFIXES = frozenset({
    ".md", ".markdown", ".rst", ".txt", ".adoc", ".org",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".properties",
    ".lock", ".sum", ".csv", ".tsv", ".xml", ".svg",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz",
    ".po", ".pot", ".snap", ".map",
})

# Directories whose contents are documentation regardless of extension. A .py
# under docs/ is an example, not the product.
DOC_DIR_NAMES = frozenset({"docs", "doc", "documentation", "examples", "example", "samples"})

# Conventionally extensionless prose and metadata. `Path.suffix` is empty for
# these, so the unknown-extension-is-code rule would otherwise feed a README
# through the complexity and duplication detectors — the exact prose-derived
# metrics the denylist exists to prevent. Compared case-insensitively.
NON_CODE_BASENAMES = frozenset({
    "readme", "license", "licence", "copying", "notice", "authors",
    "contributors", "changelog", "changes", "history", "news", "todo",
    "install", "manifest", "codeowners", "maintainers", "version",
})

# Minified or generated bundles: real code, but nobody reviews them and their
# line lengths would dominate every size metric.
GENERATED_MARKERS = (".min.js", ".min.css", ".bundle.js", "_pb2.py", ".g.dart", ".generated.")

_BINARY_SNIFF_BYTES = 8192


class ScanPathError(ValueError):
    """The path handed to a detector does not exist.

    Ending the iterator instead would let a typo in an audit path produce an
    authoritative-looking "No problems found" over nothing at all.
    """


def configure_output() -> None:
    """Keep emoji output from crashing narrow console encodings."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(errors="replace")


def is_probably_binary(path: Path) -> bool:
    """A NUL byte in the first block means this is not text. Cheap and reliable."""
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True


def is_source(rel_parts: tuple[str, ...], path: Path) -> bool:
    """Whether the code-only detectors should read this file.

    Not-binary is not the same as is-source: branch counting and duplication
    shingling over a README manufactures findings out of prose.
    """
    if any(part in DOC_DIR_NAMES for part in rel_parts[:-1]):
        return False
    if path.suffix.lower() in NON_CODE_SUFFIXES:
        return False
    name = path.name.lower()
    if not path.suffix and name.split(".")[0] in NON_CODE_BASENAMES:
        return False
    return not any(marker in name for marker in GENERATED_MARKERS)


def walk_paths(root: Path) -> Iterator[Path]:
    """Yield every non-excluded file path under ``root``, binaries included.

    Metadata-only checks (file size, tracking state) need the binary paths that
    ``walk_files`` filters out — a committed multi-gigabyte archive is exactly
    the thing a hygiene check should see, and it is never going to be text.

    **Symlinks are never followed.** A symlink passes ``is_file()`` and its
    target would then be read, so a link pointing outside the tree would let
    this skill inspect host data and report a credential found there as
    committed to this repository. Git stores only the link target string, so
    there is nothing of the target's content to review in any case.
    """
    if root.is_symlink():
        return
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        raise ScanPathError(f"{root}: no such file or directory")

    # os.walk with in-place dirnames pruning, NOT rglob-then-filter: rglob
    # descends into node_modules, vendor and target in full and stats every
    # file inside before anything is discarded. On a real project that tree
    # dwarfs the source and dominates both wall-clock and memory.
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in EXCLUDE_DIRS
                             and not Path(dirpath, d).is_symlink())
        for name in sorted(filenames):
            path = Path(dirpath, name)
            if not path.is_symlink() and path.is_file():
                yield path


def walk_files(root: Path, *, source_only: bool) -> Iterator[Path]:
    """Yield the files under ``root`` a detector should *read as text*.

    ``source_only=True`` restricts to files classified as source. Detectors
    whose findings are real in any text file (secrets, merge markers) pass
    False and take the wider set. Binaries are excluded from both.
    """
    for path in walk_paths(root):
        if source_only and not is_source(path.relative_to(root).parts
                                         if root.is_dir() else (path.name,), path):
            continue
        if is_probably_binary(path):
            continue
        yield path
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/code_doctor/test_common.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add skills/code-doctor/scripts/common.py tests/code_doctor/test_common.py
git commit -m "code-doctor: the source walk, with a non-code denylist

An unknown extension is treated as code, which is what keeps the skill
language-blind. A denylist of known non-code text keeps prose and generated
data out of the code-only detectors without ever enumerating languages."
```

---

### Task 1b: Make the skill directory structurally valid

**Discovered during execution.** `tools/validate_skills.py` runs over every directory under `skills/` on every pull request, and CI fails the build when one lacks a `SKILL.md` or a paired `evals/<skill>/evals.json`. Task 1 created `skills/code-doctor/scripts/`, so CI went red immediately and would have stayed red through Tasks 2–8 while `SKILL.md` waited in Task 9. A branch that is red for eight consecutive tasks has no working signal, which is worse than the sequencing was convenient.

This task creates the minimum that validates. Task 9 replaces both files with the real content — this is scaffolding, and it is marked as such in the file itself so nobody mistakes the stub for the deliverable.

**Files:**
- Create: `skills/code-doctor/SKILL.md`
- Create: `evals/code-doctor/evals.json`

**Interfaces:**
- Consumes: nothing
- Produces: a structurally valid skill directory; `python tools/validate_skills.py` exits clean

- [ ] **Step 1: Verify the failure first**

Run: `python tools/validate_skills.py`
Expected: `error: code-doctor: no SKILL.md`

- [ ] **Step 2: Write the stub SKILL.md**

The description must be ≤ 1024 characters and describe only what the skill can do *today* (Task 1 only ships a file walk, so it can do nothing useful yet). Do not copy Task 9's description forward — advertising unshipped detectors is the defect Task 9 exists to avoid.

```markdown
---
name: code-doctor
description: Under construction — not yet ready for use. This skill will review any codebase for quality problems and bugs without a parser or language tables, but its detectors are still being built. Do not invoke it yet. For Python use python-code-doctor, for TypeScript use typescript-code-doctor, for Django use django-code-doctor.
---

# Code Doctor (under construction)

**This skill is incomplete and should not be invoked.** It is being built task by
task; see `docs/superpowers/plans/2026-08-07-code-doctor-foundation.md`.

Its finished form is a language-agnostic reviewer that measures code quality and
bugs — no parsers, no comment-syntax tables, no framework knowledge — separating
defects it can prove from unverified leads it cannot. Until the detectors land,
use `python-code-doctor`, `typescript-code-doctor`, or `django-code-doctor`.

Task 9 of the plan replaces this file with the real router.
```

- [ ] **Step 3: Write the stub evals**

The validator requires `skill_name`, and a non-empty `evals` list whose every case has `id`, `prompt`, and `expected_output`. Task 9 replaces this with the five real cases.

```json
{
  "skill_name": "code-doctor",
  "evals": [
    {
      "id": "under-construction-defers",
      "prompt": "Review this Go repo for problems.",
      "expected_output": "Recognizes code-doctor is incomplete and not yet usable, and says so rather than pretending to review. Does not fabricate findings. Replaced in Task 9 by the real eval set."
    }
  ]
}
```

- [ ] **Step 4: Verify**

```bash
python tools/validate_skills.py     # must exit clean, no "code-doctor" error
python -m pytest tests/ -q          # the standalone-install test sees the new skill
python -m ruff check .
```

- [ ] **Step 5: Commit**

```bash
git add skills/code-doctor/SKILL.md evals/code-doctor/evals.json
git commit -m "code-doctor: structural stub so CI validates during the build

tools/validate_skills.py checks every directory under skills/ on every PR, so
creating scripts/ without a SKILL.md turns the branch red until Task 9. The
stub says plainly that the skill is not ready; Task 9 replaces both files."
```

---

### Task 2: The finding/candidate schema, enforced in the dataclass

**Files:**
- Modify: `skills/code-doctor/scripts/common.py` (append)
- Test: `tests/code_doctor/test_common.py` (append)

**Interfaces:**
- Consumes: `SEVERITY_RANK`, `SEVERITY_ICONS`, `configure_output` from Task 1
- Produces: `Finding` dataclass with fields `file: str`, `line: int`, `smell_type: str`, `description: str`, `suggestion: str = ""`, `also_caused_by: list[str] = []`, `severity: str = "medium"`, `kind: str = "finding"`, `code_snippet: str = ""`, `related_lines: list[int] = []`; `Reporter(path: Path, ignore: set[str])` with methods `.finding(line, smell_type, description, suggestion, severity="medium", snippet="", related=None)` and `.candidate(line, smell_type, description, also_caused_by, severity="low", snippet="", related=None)` and attribute `.findings: list[Finding]`; `SchemaError` exception

- [ ] **Step 1: Write the failing test**

Append to `tests/code_doctor/test_common.py`:

```python
def test_finding_requires_a_suggestion(common):
    """A finding asserts a defect, so it must say what to do about it."""
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a.go", line=1, smell_type="x",
                       description="d", suggestion="")


def test_finding_may_not_carry_benign_explanations(common):
    """also_caused_by is the candidate's honesty field; on a finding it is a lie."""
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a.go", line=1, smell_type="x", description="d",
                       suggestion="fix it", also_caused_by=["something benign"])


def test_candidate_requires_benign_explanations(common):
    """A candidate must name the ways a healthy codebase produces this."""
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a.go", line=1, smell_type="x", description="d",
                       kind="candidate", also_caused_by=[])


def test_candidate_may_not_carry_a_fix(common):
    """The whole point: an unverified lead must not recommend an edit."""
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a.go", line=1, smell_type="x", description="d",
                       kind="candidate", suggestion="delete it",
                       also_caused_by=["it is an entry point"])


def test_valid_finding_and_candidate_construct(common):
    finding = common.Finding(file="a.go", line=1, smell_type="x",
                             description="d", suggestion="fix it")
    candidate = common.Finding(file="a.go", line=2, smell_type="y",
                               description="d", kind="candidate",
                               also_caused_by=["it is an entry point"])
    assert finding.kind == "finding"
    assert candidate.suggestion == ""


def test_unknown_kind_is_rejected(common):
    with pytest.raises(common.SchemaError, match="kind"):
        common.Finding(file="a.go", line=1, smell_type="x", description="d",
                       suggestion="fix", kind="probably")


def test_reporter_honours_ignore(common):
    from pathlib import Path
    reporter = common.Reporter(Path("a.go"), {"skipme"})
    reporter.finding(1, "skipme", "d", "fix it")
    reporter.finding(2, "keepme", "d", "fix it")
    assert [f.smell_type for f in reporter.findings] == ["keepme"]


def test_reporter_candidate_sets_kind(common):
    from pathlib import Path
    reporter = common.Reporter(Path("a.go"), set())
    reporter.candidate(1, "lead", "d", ["it may be loaded by convention"])
    assert reporter.findings[0].kind == "candidate"
    assert reporter.findings[0].suggestion == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/code_doctor/test_common.py -v -k "finding or candidate or reporter"`
Expected: FAIL with `AttributeError: module 'common' has no attribute 'SchemaError'`.

- [ ] **Step 3: Write the schema**

Append to `skills/code-doctor/scripts/common.py`:

```python
# --------------------------------------------------------------------------- #
# The confidence discipline, as a type
# --------------------------------------------------------------------------- #

class SchemaError(ValueError):
    """A detector tried to emit a record its evidence does not support."""


VALID_KINDS = frozenset({"finding", "candidate"})


@dataclass
class Finding:
    """One output record, in one of two kinds.

    A **finding** asserts a defect. It carries a concrete fix, because a claim
    you cannot act on is not worth making.

    A **candidate** reports a lead that needs verification. It carries the
    specific ways a healthy codebase produces the same observation, and it
    carries no fix — recommending an edit on heuristic evidence is how a tool
    like this talks someone into deleting live code.

    The constructor enforces the difference. Prose in a reference file does not
    survive contact with a detector author in a hurry; a raised exception does.
    """

    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str = ""
    also_caused_by: list[str] = field(default_factory=list)
    severity: str = "medium"
    kind: str = "finding"
    code_snippet: str = ""
    related_lines: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind not in VALID_KINDS:
            raise SchemaError(
                f"{self.smell_type}: kind must be one of {sorted(VALID_KINDS)}, got {self.kind!r}"
            )
        if self.kind == "finding":
            if not self.suggestion.strip():
                raise SchemaError(
                    f"{self.smell_type}: a finding asserts a defect and must carry a suggestion; "
                    "if you cannot name the fix, emit a candidate instead"
                )
            if self.also_caused_by:
                raise SchemaError(
                    f"{self.smell_type}: also_caused_by belongs to candidates; a finding that has "
                    "benign explanations is a candidate"
                )
        else:
            if self.suggestion.strip():
                raise SchemaError(
                    f"{self.smell_type}: a candidate must not carry a suggestion — it is an "
                    "unverified lead, and a fix on unverified evidence is how live code gets deleted"
                )
            if not self.also_caused_by:
                raise SchemaError(
                    f"{self.smell_type}: a candidate must name the ways a healthy codebase produces "
                    "this same observation, so the reader can rule them out"
                )


class Reporter:
    """Collects records for one file, honouring the detector's --ignore set."""

    def __init__(self, path: Path, ignore: set[str]):
        self.path = path
        self.ignore = ignore
        self.findings: list[Finding] = []

    def finding(self, line: int, smell_type: str, description: str, suggestion: str,
                severity: str = "medium", snippet: str = "",
                related: list[int] | None = None) -> None:
        self._add(Finding(
            file=str(self.path), line=line, smell_type=smell_type,
            description=description, suggestion=suggestion, severity=severity,
            kind="finding", code_snippet=snippet, related_lines=related or [],
        ))

    def candidate(self, line: int, smell_type: str, description: str,
                  also_caused_by: list[str], severity: str = "low",
                  snippet: str = "", related: list[int] | None = None) -> None:
        self._add(Finding(
            file=str(self.path), line=line, smell_type=smell_type,
            description=description, also_caused_by=also_caused_by,
            severity=severity, kind="candidate", code_snippet=snippet,
            related_lines=related or [],
        ))

    def _add(self, record: Finding) -> None:
        if record.smell_type not in self.ignore:
            self.findings.append(record)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/code_doctor/test_common.py -v`
Expected: 19 passed.

- [ ] **Step 5: Commit**

```bash
git add skills/code-doctor/scripts/common.py tests/code_doctor/test_common.py
git commit -m "code-doctor: enforce the finding/candidate split in the schema

A finding asserts a defect and must carry a fix. A candidate reports a lead,
must name the benign explanations, and must not carry a fix. Enforced in
__post_init__ rather than in a reference file, because the failure mode this
prevents is a tool recommending that someone delete live code."
```

---

### Task 3: Git helpers with the shallow-history probe

**Files:**
- Modify: `skills/code-doctor/scripts/common.py` (append)
- Test: `tests/code_doctor/test_common.py` (append)

**Interfaces:**
- Consumes: nothing new
- Produces: `git(repo: Path, *args: str) -> str`, `is_git_repo(repo: Path) -> bool`, `HistoryDepth` dataclass with fields `is_repo: bool`, `is_shallow: bool`, `commit_count: int`, `oldest_commit_days: int | None`, `min_commits: int = 20`, `window_days: int | None = None`, and property `usable: bool`; `probe_history(repo: Path, *, min_commits: int = 20) -> HistoryDepth`, `git_dir_for(root) -> Path`, `unmerged_paths(root) -> set[Path] | None`, `tracked_paths(root) -> set[Path] | None`

- [ ] **Step 1: Write the failing test**

Append to `tests/code_doctor/test_common.py`:

```python
def test_probe_history_reports_not_a_repo(common, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    depth = common.probe_history(plain)
    assert depth.is_repo is False
    assert depth.usable is False


def test_probe_history_flags_thin_history_as_unusable(common, repo):
    """Two commits cannot support a bus-factor claim."""
    repo.write("a.go", "package main\n")
    repo.commit("one")
    repo.write("b.go", "package b\n")
    repo.commit("two")
    depth = common.probe_history(repo.path, min_commits=20)
    assert depth.is_repo is True
    assert depth.is_shallow is False
    assert depth.commit_count == 2
    assert depth.usable is False


def test_probe_history_accepts_sufficient_history(common, repo):
    for i in range(25):
        repo.write(f"f{i}.go", f"package p{i}\n")
        repo.commit(f"commit {i}")
    depth = common.probe_history(repo.path, min_commits=20)
    assert depth.usable is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/code_doctor/test_common.py -v -k history`
Expected: FAIL with `AttributeError: module 'common' has no attribute 'probe_history'`.

- [ ] **Step 3: Write the git helpers**

Append to `skills/code-doctor/scripts/common.py`:

```python
# --------------------------------------------------------------------------- #
# Git, and knowing when not to trust it
# --------------------------------------------------------------------------- #

def git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo``, returning stdout. Raises on failure."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=120, check=True,
    )
    return result.stdout


def is_git_repo(repo: Path) -> bool:
    try:
        git(repo, "rev-parse", "--git-dir")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


@dataclass
class HistoryDepth:
    """What the repository's history can actually support.

    A shallow CI checkout exposes only the most recent committer, which turns
    multi-author files into apparent single-author files. Computing a bus
    factor over that fragment produces a confidently wrong answer, which is
    worse than no answer.
    """

    is_repo: bool
    is_shallow: bool
    commit_count: int
    oldest_commit_days: int | None
    min_commits: int = 20
    window_days: int | None = None

    @property
    def usable(self) -> bool:
        if not (self.is_repo and not self.is_shallow
                and self.commit_count >= self.min_commits):
            return False
        # 20 commits spanning three weeks cannot answer a question about the
        # last year. A history rewrite or a truncated migration produces
        # exactly that shape, and it is not shallow by git's definition.
        if self.window_days is not None:
            if self.oldest_commit_days is None:
                return False
            return self.oldest_commit_days >= self.window_days
        return True

    def explain(self) -> str:
        if not self.is_repo:
            return "not a git repository — history-derived findings skipped"
        if self.is_shallow:
            return ("shallow clone — history-derived findings skipped; "
                    "re-run with `git fetch --unshallow` for ownership and churn")
        if self.commit_count < self.min_commits:
            return (f"only {self.commit_count} commit(s) of history — too few to support "
                    "churn or ownership claims; findings skipped")
        if self.window_days is not None and not self.usable:
            return (f"history reaches back {self.oldest_commit_days} day(s), short of the "
                    f"{self.window_days}-day window asked for; findings skipped")
        return f"{self.commit_count} commits of history"


def probe_history(repo: Path, *, min_commits: int = 20,
                  window_days: int | None = None) -> HistoryDepth:
    """Establish what may be claimed from this repository's history.

    ``window_days`` is the span a caller intends to reason over; history that
    does not reach back that far cannot answer, even when it is deep enough
    by commit count.
    """
    if not is_git_repo(repo):
        return HistoryDepth(False, False, 0, None, min_commits=min_commits,
                            window_days=window_days)

    try:
        shallow = git(repo, "rev-parse", "--is-shallow-repository").strip() == "true"
    except Exception:
        # Failing to DETERMINE shallowness is not evidence of depth. A probe
        # whose job is "a wrong answer is worse than no answer" must fail
        # toward unusable, so an unanswerable query counts as shallow.
        shallow = True

    try:
        count = int(git(repo, "rev-list", "--count", "HEAD").strip() or 0)
    except Exception:
        count = 0

    oldest_days = None
    try:
        # `git log --reverse --max-count=1` does NOT give the oldest commit:
        # --max-count limits the selection first, then --reverse reverses what
        # was selected, so it returns HEAD. Ask for the root commit directly.
        root = git(repo, "rev-list", "--max-parents=0", "HEAD").split()
        stamp = git(repo, "log", "-1", "--format=%ct", root[-1]).strip() if root else ""
        if stamp:
            oldest_days = int((time.time() - int(stamp)) / 86400)
    except Exception:
        oldest_days = None

    return HistoryDepth(True, shallow, count, oldest_days, min_commits=min_commits,
                        window_days=window_days)


def git_dir_for(root: Path) -> Path:
    """The directory to run git from.

    `git -C <file>` fails, so scanning a single file directly — the advertised
    `find_hygiene_issues.py conflicted.go` form — would otherwise report git as
    unavailable and downgrade a genuine conflict to a candidate purely because
    of how it was invoked.
    """
    return root if root.is_dir() else root.parent


def _git_listing(root: Path, *args: str) -> tuple[list[str], Path] | None:
    base = git_dir_for(root)
    if not is_git_repo(base):
        return None
    try:
        listing = git(base, "-c", "core.quotepath=false", *args)
        toplevel = Path(git(base, "rev-parse", "--show-toplevel").strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    return listing.splitlines(), toplevel


def unmerged_paths(root: Path) -> set[Path] | None:
    """Paths git reports as unmerged, or None when git cannot answer.

    This is what separates a real unresolved conflict from a fixture that
    contains marker text on purpose. Without it the detector would report this
    repository's own test fixtures as defects.
    """
    result = _git_listing(root, "ls-files", "-u", "--full-name")
    if result is None:
        return None
    entries, toplevel = result
    paths = set()
    for entry in entries:
        _, _, rel = entry.partition("\t")
        if rel:
            paths.add((toplevel / rel.strip()).resolve())
    return paths


def tracked_paths(root: Path) -> set[Path] | None:
    """Paths git actually tracks, or None when git cannot answer.

    An untracked or ignored `.env` on a developer's machine is correct
    practice, not a leak. Asserting it is committed — and telling someone to
    rotate credentials and purge history over it — is a false positive with a
    real cost attached.
    """
    result = _git_listing(root, "ls-files", "--full-name")
    if result is None:
        return None
    entries, toplevel = result
    return {(toplevel / rel.strip()).resolve() for rel in entries if rel.strip()}
```

These four live in `common.py`, not in a detector, because **two** detectors need
them: `find_hygiene_issues.py` gates conflict markers on unmerged state, and
`find_secrets.py` gates credential findings on tracking state. Defining them in
one detector and importing from `common` in the other is an `ImportError`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/code_doctor/test_common.py -v`
Expected: 22 passed.

- [ ] **Step 5: Commit**

```bash
git add skills/code-doctor/scripts/common.py tests/code_doctor/test_common.py
git commit -m "code-doctor: probe history depth before trusting git

A shallow CI checkout exposes only the latest committer, so a bus-factor
computed over it is confidently wrong. probe_history() is the gate every
history-derived detector checks before making a claim."
```

---

### Task 4: The CLI harness and emit path

**Files:**
- Modify: `skills/code-doctor/scripts/common.py` (append)
- Test: `tests/code_doctor/test_common.py` (append)

**Interfaces:**
- Consumes: `Finding`, `SEVERITY_RANK`, `SEVERITY_ICONS`, `configure_output`, `walk_files`
- Produces: `build_parser(description: str) -> argparse.ArgumentParser`, `sort_findings(list[Finding]) -> list[Finding]`, `emit(findings: list[Finding], output_format: str, clean_message: str, completeness: dict | None = None) -> None`, `warn_unreadable(path, exc)`, `warn_detector_error(path, exc)`, `run_file_detector(description, clean_message, analyze, *, source_only=True, argv=None) -> None`

JSON output shape is `{"completeness": {...}, "findings": [...]}` when `completeness` is given, and a bare `[...]` list otherwise. Both shapes are accepted by `format_findings.py` in Task 7.

- [ ] **Step 1: Write the failing test**

Append to `tests/code_doctor/test_common.py`:

```python
import json


def test_emit_json_is_a_bare_list_without_completeness(common, capsys):
    common.emit([common.Finding(file="a.go", line=1, smell_type="x",
                                description="d", suggestion="fix")],
                "json", "clean")
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["kind"] == "finding"


def test_emit_json_wraps_when_completeness_is_given(common, capsys):
    common.emit([], "json", "clean", completeness={"history": "shallow clone"})
    payload = json.loads(capsys.readouterr().out)
    assert payload["completeness"] == {"history": "shallow clone"}
    assert payload["findings"] == []


def test_emit_text_prints_completeness_banner(common, capsys):
    common.emit([], "text", "no problems found",
                completeness={"history": "shallow clone — findings skipped"})
    out = capsys.readouterr().out
    assert "shallow clone" in out
    assert "no problems found" in out


def test_emit_text_separates_candidates_from_findings(common, capsys):
    records = [
        common.Finding(file="a.go", line=1, smell_type="defect",
                       description="broken", suggestion="fix it", severity="high"),
        common.Finding(file="b.go", line=2, smell_type="lead", description="maybe",
                       kind="candidate", also_caused_by=["it is an entry point"]),
    ]
    common.emit(records, "text", "clean")
    out = capsys.readouterr().out
    assert "1 finding(s)" in out
    assert "1 candidate(s)" in out
    assert "it is an entry point" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/code_doctor/test_common.py -v -k emit`
Expected: FAIL with `AttributeError: module 'common' has no attribute 'emit'`.

- [ ] **Step 3: Write the harness**

Append to `skills/code-doctor/scripts/common.py`. Add `import argparse` and `import json` to the imports at the top of the file.

```python
# --------------------------------------------------------------------------- #
# One CLI, one report
# --------------------------------------------------------------------------- #

def warn_unreadable(filepath: Path, exc: Exception) -> None:
    """Name a file this skill could not read, rather than counting it clean."""
    print(f"⚠️  {filepath}: skipped, unreadable ({exc})", file=sys.stderr)


def warn_detector_error(filepath: Path, exc: Exception) -> None:
    """Surface a detector crash instead of silently reporting the file clean."""
    print(
        f"⚠️  {filepath}: detector failed ({type(exc).__name__}: {exc}); "
        "findings for this file are incomplete, not clean",
        file=sys.stderr,
    )


def fail_on_bad_path(exc: ScanPathError) -> int:
    """Turn a missing scan root into a loud, nonzero exit at the CLI boundary."""
    print(f"error: {exc}", file=sys.stderr)
    return 2


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="",
                        help="Comma-separated finding types to suppress")
    return parser


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Findings before candidates, then by severity, then by location."""
    findings.sort(key=lambda f: (f.kind != "finding",
                                 SEVERITY_RANK.get(f.severity, 1), f.file, f.line))
    return findings


def _print_report(findings: list[Finding], clean_message: str,
                  completeness: dict | None) -> None:
    if completeness:
        for label, note in completeness.items():
            print(f"ℹ️  {label}: {note}")
        print()

    confirmed = [f for f in findings if f.kind == "finding"]
    leads = [f for f in findings if f.kind == "candidate"]

    if not findings:
        print(f"✅ {clean_message}")
        return

    print(f"{len(confirmed)} finding(s), {len(leads)} candidate(s):\n")

    for record in confirmed:
        icon = SEVERITY_ICONS.get(record.severity, "")
        print(f"{icon} [{record.severity.upper()}] {record.file}:{record.line}")
        print(f"   {record.smell_type}: {record.description}")
        if record.code_snippet:
            print(f"   Code: {record.code_snippet}")
        print(f"   → {record.suggestion}\n")

    if leads:
        print("Candidates — unverified leads, check before acting:\n")
    for record in leads:
        icon = SEVERITY_ICONS.get(record.severity, "")
        print(f"{icon} [candidate] {record.file}:{record.line}")
        print(f"   {record.smell_type}: {record.description}")
        if record.code_snippet:
            print(f"   Code: {record.code_snippet}")
        print("   Also caused by:")
        for reason in record.also_caused_by:
            print(f"     - {reason}")
        print()


def emit(findings: list[Finding], output_format: str, clean_message: str,
         completeness: dict | None = None) -> None:
    sort_findings(findings)
    if output_format == "json":
        records = [asdict(f) for f in findings]
        if completeness:
            print(json.dumps({"completeness": completeness, "findings": records}, indent=2))
        else:
            print(json.dumps(records, indent=2))
    else:
        _print_report(findings, clean_message, completeness)


def coverage_gaps(unreadable: list[str], failed: list[str]) -> dict:
    """Lost-file accounting, as a completeness record.

    stderr is not enough. analyze_all.py ignores a subprocess's stderr when it
    exits zero, so a detector that lost ten files to read errors would still
    aggregate as "No problems found" — a silent coverage hole reported as a
    clean repository, which is the exact failure this skill exists to avoid.
    """
    gaps = {}
    if unreadable:
        gaps["files_unreadable"] = (
            f"{len(unreadable)} file(s) could not be read and were not analysed: "
            + ", ".join(unreadable[:5]) + ("…" if len(unreadable) > 5 else "")
        )
    if failed:
        gaps["files_detector_failed"] = (
            f"{len(failed)} file(s) crashed the detector and are incomplete, not clean: "
            + ", ".join(failed[:5]) + ("…" if len(failed) > 5 else "")
        )
    return gaps


def run_file_detector(description: str, clean_message: str, analyze,
                      *, source_only: bool = True, argv: list[str] | None = None) -> int:
    """Standard main() for a detector that reasons about one file at a time.

    ``analyze`` is called as ``analyze(path, text, reporter)``. Returns the
    process exit code.
    """
    configure_output()
    args = build_parser(description).parse_args(argv)
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    findings: list[Finding] = []
    unreadable: list[str] = []
    failed: list[str] = []
    try:
        walked = list(walk_files(Path(args.path), source_only=source_only))
    except ScanPathError as exc:
        return fail_on_bad_path(exc)
    for filepath in walked:
        try:
            text = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warn_unreadable(filepath, exc)
            unreadable.append(str(filepath))
            continue
        reporter = Reporter(filepath, ignore)
        try:
            analyze(filepath, text, reporter)
        except Exception as exc:  # a detector bug must not read as a clean file
            warn_detector_error(filepath, exc)
            failed.append(str(filepath))
            continue
        findings.extend(reporter.findings)

    gaps = coverage_gaps(unreadable, failed)
    emit(findings, args.format, clean_message, completeness=gaps or None)
    return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/code_doctor/test_common.py -v`
Expected: 26 passed.

- [ ] **Step 5: Commit**

```bash
git add skills/code-doctor/scripts/common.py tests/code_doctor/test_common.py
git commit -m "code-doctor: shared CLI harness and two-class report

Findings and candidates print in separate sections, and a candidate always
prints the benign explanations beneath it. Completeness notes print above the
report so a degraded run cannot be mistaken for a clean one."
```

---

### Task 5: `find_hygiene_issues.py`

**Files:**
- Create: `skills/code-doctor/scripts/find_hygiene_issues.py`
- Test: `tests/code_doctor/test_find_hygiene_issues.py`

**Interfaces:**
- Consumes: `Reporter`, `run_file_detector`, `walk_files` from `common`
- Produces: a CLI, plus `unmerged_paths(root: Path) -> set[Path] | None` (None when git is unavailable). Finding types: `merge_conflict_marker` (high, **finding only when git reports the path unmerged**; **candidate** otherwise), `oversized_file` (medium, finding), `oversized_line` (low, finding), `committed_env_file` (high, finding), `todo_inventory` (low, finding), `commented_out_code` (low, **candidate**)

The comment-prefix set `("//", "#", "--", ";")` and block markers `("/*", "*/")` are the skill's one documented concession to language syntax, per the spec. String literals are blanked before prefixes are matched.

- [ ] **Step 1: Write the failing test**

Create `tests/code_doctor/test_find_hygiene_issues.py`:

```python
"""Hygiene detector: what is a defect, and what is only a lead."""

import json
import subprocess
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"
SCRIPT = SKILL / "scripts" / "find_hygiene_issues.py"


def types_in(result) -> set[str]:
    return {f["smell_type"] for f in json.loads(result.stdout)}


def records_of(result, smell_type):
    return [f for f in json.loads(result.stdout) if f["smell_type"] == smell_type]


def test_merge_marker_without_git_conflict_is_a_candidate(repo, run_script):
    """A marker in a committed file is not proof of an unresolved conflict.

    Documentation examples, snapshots, and fixtures that exist to test
    conflict handling all legitimately contain these lines.
    """
    repo.write("app.go", "package main\n<<<<<<< HEAD\nx := 1\n=======\nx := 2\n>>>>>>> other\n")
    repo.commit("add fixture")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "merge_conflict_marker")[0]
    assert record["kind"] == "candidate"
    assert record["suggestion"] == ""
    assert any("fixture" in reason or "documentation" in reason
               for reason in record["also_caused_by"])


def test_merge_marker_in_a_genuinely_unmerged_path_is_a_finding(repo, run_script):
    """Git's own unmerged-path list is the evidence that upgrades this."""
    repo.write("app.go", "package main\nx := 0\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "other")
    repo.write("app.go", "package main\nx := 2\n")
    repo.commit("theirs")
    repo.git("checkout", "-q", "-")
    repo.write("app.go", "package main\nx := 1\n")
    repo.commit("ours")
    # A conflicting merge leaves the path unmerged and the markers in the file.
    merge = subprocess.run(["git", "-C", str(repo.path), "merge", "other"],
                           capture_output=True, text=True)
    assert merge.returncode != 0, "expected the merge to conflict"

    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "merge_conflict_marker")[0]
    assert record["kind"] == "finding"
    assert record["suggestion"]
    assert record["severity"] == "high"


def test_commented_out_code_is_a_candidate_with_reasons(repo, run_script):
    repo.write("app.go", "package main\n// x := compute(1, 2);\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "commented_out_code")[0]
    assert record["kind"] == "candidate"
    assert record["suggestion"] == ""
    assert record["also_caused_by"], "a candidate must name the benign explanations"


def test_url_in_a_string_is_not_a_comment(repo, run_script):
    """Literals are blanked before comment prefixes are matched."""
    repo.write("app.go", 'package main\nurl := "https://example.com/a"; doWork()\n')
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "commented_out_code" not in types_in(result)


def test_prose_is_not_scanned_for_commented_out_code(repo, run_script):
    """Markdown is not source; a fenced code sample is not a leftover."""
    repo.write("README.md", "# Title\n// x := compute(1, 2);\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "commented_out_code" not in types_in(result)


def test_merge_marker_in_yaml_is_still_reported(repo, run_script):
    """Merge markers are checked in any text file, so that check takes the wide walk."""
    repo.write("config.yaml", "a: 1\n<<<<<<< HEAD\nb: 2\n=======\nb: 3\n>>>>>>> other\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "merge_conflict_marker" in types_in(result)


def test_tracked_env_file_is_a_finding(repo, run_script):
    repo.write(".env", "API_KEY=abc123\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "committed_env_file" in types_in(result)


def test_untracked_env_file_is_not_reported(repo, run_script):
    """An untracked, gitignored .env is correct practice, not a leak.

    Telling someone to rotate credentials and purge history over their local
    .env is a false positive with a real cost.
    """
    repo.write(".gitignore", ".env\n")
    repo.commit("ignore env")
    repo.write(".env", "API_KEY=abc123\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "committed_env_file" not in types_in(result)


def test_todo_in_a_non_source_text_file_is_inventoried(repo, run_script):
    """The design scopes the TODO inventory to all text, not just source."""
    repo.write("deployment.yaml", "replicas: 1  # TODO: raise before launch\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "todo_inventory" in types_in(result)


def test_oversized_file_is_reported_once(repo, run_script):
    repo.write("big.go", "package main\n" + "var x = 1\n" * 1500)
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert len(records_of(result, "oversized_file")) == 1


def test_clean_repo_reports_nothing(repo, run_script):
    repo.write("app.go", "package main\n\nfunc main() {}\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert json.loads(result.stdout) == []


def test_ignore_suppresses_a_type(repo, run_script):
    repo.write("app.go", "package main\n// TODO: fix this\n")
    result = run_script(SCRIPT, repo.path, "--format", "json", "--ignore", "todo_inventory")
    assert "todo_inventory" not in types_in(result)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/code_doctor/test_find_hygiene_issues.py -v`
Expected: FAIL — the script does not exist, so `run_script` asserts on a non-zero exit.

- [ ] **Step 3: Write the detector**

Create `skills/code-doctor/scripts/find_hygiene_issues.py`:

```python
#!/usr/bin/env python3
"""
Repository hygiene: the things that are wrong in any language.

Merge markers, oversized files, committed secrets-by-filename, and a TODO
inventory are findings — none of them depends on knowing what language this is.
Commented-out code is a candidate, because deciding that a commented line is
dead code rather than a documentation example needs a reading brain.

Debug-print leftovers are NOT here on purpose: naming the print call of each
language is a per-language table, which this skill does not carry. The
specialists have that check, and so does the project's own linter.
"""

import re
import sys
from pathlib import Path

from common import (Reporter, ScanPathError, build_parser, configure_output,
                    coverage_gaps, emit, fail_on_bad_path, is_probably_binary,
                    tracked_paths, unmerged_paths, walk_files, walk_paths,
                    warn_detector_error, warn_unreadable)

MERGE_MARKERS = ("<<<<<<< ", "=======", ">>>>>>> ")
MAX_FILE_LINES = 1000
MAX_LINE_LENGTH = 300
MAX_COMMITTED_BYTES = 10 * 1024 * 1024

# The skill's one concession to language syntax, used only after string
# literals have been blanked. Five tokens, and no detector branches on which
# language a file is. A mis-classified line loses a candidate, never invents
# a finding.
LINE_COMMENT_PREFIXES = ("//", "#", "--", ";")
# `//` and `--` begin a comment in every language that uses them. `#` and `;`
# do not: `#` opens a C preprocessor directive and a Rust attribute, `;` is an
# instruction separator in assembly. A hit on those two supports a candidate,
# never a finding.
UNAMBIGUOUS_PREFIXES = ("//", "--")

_STRING_LITERAL = re.compile(r"""(["'`])(?:\\.|(?!\1).)*\1""")
_TODO = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
# A commented line that looks like code: ends in a statement terminator or
# opener, or contains an assignment or a call.
_LOOKS_LIKE_CODE = re.compile(r"[;{}]\s*$|\)\s*[;{]?\s*$|=[^=]|\w+\s*\(")

# Filenames whose whole purpose is to hold secrets. `.npmrc` and `.pypirc`
# are deliberately absent: both routinely hold nothing but registry, index,
# and proxy settings, so a name-only rule would call ordinary committed
# config a credential leak. find_secrets.py inspects their contents instead.
SECRET_FILENAMES = frozenset({".env", ".env.local", ".env.production"})


def blank_literals(line: str) -> str:
    """Replace string-literal contents so their punctuation stops parsing as code.

    This runs BEFORE comment stripping, which is what keeps
    `url = "https://x"; work()` from losing its trailing call to a `//` that
    was never a comment.
    """
    return _STRING_LITERAL.sub(lambda m: m.group(1) + " " * (len(m.group(0)) - 2) + m.group(1), line)


def unambiguous_comment(line: str) -> bool:
    """Whether this line's comment marker is one no language repurposes."""
    blanked = blank_literals(line)
    positions = [(blanked.find(pfx), pfx) for pfx in LINE_COMMENT_PREFIXES]
    hits = [(index, pfx) for index, pfx in positions if index != -1]
    if not hits:
        return False
    return min(hits)[1] in UNAMBIGUOUS_PREFIXES


def comment_body(line: str) -> str | None:
    """The text after a line-comment prefix, or None if there is no comment."""
    blanked = blank_literals(line)
    for prefix in LINE_COMMENT_PREFIXES:
        index = blanked.find(prefix)
        if index != -1:
            return line[index + len(prefix):].strip()
    return None


def check_text_file(path: Path, text: str, report: Reporter,
                    unmerged: set[Path] | None) -> None:
    """Checks that are valid in any text file, source or not."""
    is_conflicted = unmerged is not None and path.resolve() in unmerged

    for number, line in enumerate(text.splitlines(), 1):
        # The TODO inventory belongs here, not in the source pass: a TODO in
        # deployment.yaml or settings.toml is debt exactly like one in a .go
        # file, and the design's stated scope for it is all text.
        body = comment_body(line)
        if body is not None:
            todo = _TODO.search(body)
            if todo and unambiguous_comment(line):
                report.finding(
                    number, "todo_inventory",
                    f"{todo.group(1)}: {body[:80]}",
                    "Convert it to a tracked issue or delete it. An undated TODO in the "
                    "source is a decision nobody owns.",
                    severity="low", snippet=line.strip()[:120],
                )
            elif todo:
                # `#` and `;` are syntax, not comments, in several languages —
                # C's `#define TODO 1` is not debt someone forgot to file.
                report.candidate(
                    number, "todo_inventory",
                    f"{todo.group(1)} on a line whose comment prefix is ambiguous",
                    also_caused_by=[
                        "a C preprocessor directive or Rust attribute, where `#` is syntax",
                        "an assembly or ini line, where `;` is not a comment",
                        "a literal string that happens to contain the word",
                    ],
                    severity="low", snippet=line.strip()[:120],
                )

        if not line.startswith(MERGE_MARKERS):
            continue
        if is_conflicted:
            report.finding(
                number, "merge_conflict_marker",
                "Unresolved merge conflict — git reports this path as unmerged",
                "Resolve the conflict and delete the marker. This file does not "
                "parse, build, or run in its current state.",
                severity="high", snippet=line[:120],
            )
        else:
            report.candidate(
                number, "merge_conflict_marker",
                "Merge conflict marker text, in a path git does not report as unmerged",
                also_caused_by=[
                    "a test fixture that exists to exercise conflict handling",
                    "a documentation example showing what a conflict looks like",
                    "a stored snapshot or golden file containing marker text",
                    "git was unavailable, so unmerged state could not be checked",
                ],
                severity="high", snippet=line[:120],
            )

def check_metadata(path: Path, report: Reporter, tracked: set[Path] | None) -> None:
    """Checks that read the file's size and tracking state, never its bytes.

    Runs over walk_paths, so binaries reach it — a committed multi-gigabyte
    archive is precisely what this should catch, and it is never text.
    """
    if path.name in SECRET_FILENAMES:
        if tracked is None:
            report.candidate(
                1, "committed_env_file",
                f"`{path.name}` is present and git could not be consulted",
                also_caused_by=[
                    "it is untracked or gitignored, which is the correct arrangement",
                    "git is unavailable here, so tracking state is unknown",
                ],
                severity="high",
            )
        elif path.resolve() in tracked:
            report.finding(
                1, "committed_env_file",
                f"`{path.name}` is tracked by git",
                "Remove it from the index, add it to .gitignore, and rotate anything it "
                "contained — git history keeps the old copy.",
                severity="high",
            )
        # An untracked or ignored .env is correct practice. Say nothing.

    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= MAX_COMMITTED_BYTES:
        return
    if not is_probably_binary(path):
        return  # a large SQL dump or dataset is a different conversation
    megabytes = size // (1024 * 1024)
    if tracked is None:
        report.candidate(
            1, "large_committed_binary",
            f"{megabytes} MB file present, and git could not be consulted",
            also_caused_by=[
                "it is untracked or gitignored — a local build artifact costs no clone time",
                "git is unavailable here, so tracking state is unknown",
            ],
            severity="medium",
        )
    elif path.resolve() in tracked:
        report.finding(
            1, "large_committed_binary",
            f"{megabytes} MB file tracked in git",
            "Move it to release artifacts or an LFS/object store. Git stores every "
            "version forever, so this cost is paid by every clone from now on.",
            severity="medium",
        )


def check_source_file(path: Path, text: str, report: Reporter) -> None:
    """Checks that only make sense on code."""
    lines = text.splitlines()

    if len(lines) > MAX_FILE_LINES:
        report.finding(
            1, "oversized_file",
            f"{len(lines)} lines in one file",
            f"Split it by responsibility. Past roughly {MAX_FILE_LINES} lines a file "
            "stops fitting in a reviewer's head, and edits to it conflict constantly.",
            severity="medium",
        )

    for number, line in enumerate(lines, 1):
        if len(line) > MAX_LINE_LENGTH:
            report.finding(
                number, "oversized_line",
                f"{len(line)}-character line",
                "Break it up. A line this long is unreviewable in a side-by-side diff.",
                severity="low",
            )

        # TODOs are inventoried in the text pass, which covers this file too.
        body = comment_body(line)
        if body is None or _TODO.search(body):
            continue

        if _LOOKS_LIKE_CODE.search(body):
            report.candidate(
                number, "commented_out_code",
                "Commented-out line that looks like code",
                also_caused_by=[
                    "a documentation example shown as a comment",
                    "a language where this prefix is not a comment "
                    "(`#` opens a Rust attribute and a C preprocessor directive)",
                    "deliberately disabled code with a nearby explanation",
                ],
                severity="low", snippet=line.strip()[:120],
            )


def main() -> int:
    configure_output()
    args = build_parser(__doc__).parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()
    root = Path(args.path)

    findings = []
    unreadable, failed = [], []
    try:
        text_files = set(walk_files(root, source_only=False))
        source_files = set(walk_files(root, source_only=True))
        all_paths = list(walk_paths(root))
    except ScanPathError as exc:
        return fail_on_bad_path(exc)
    unmerged = unmerged_paths(root)
    tracked = tracked_paths(root)

    for filepath in all_paths:
        report = Reporter(filepath, ignore)
        try:
            check_metadata(filepath, report, tracked)
            if filepath in text_files:
                try:
                    text = filepath.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    warn_unreadable(filepath, exc)
                    unreadable.append(str(filepath))
                    findings.extend(report.findings)
                    continue
                check_text_file(filepath, text, report, unmerged)
                if filepath in source_files:
                    check_source_file(filepath, text, report)
        except Exception as exc:
            warn_detector_error(filepath, exc)
            failed.append(str(filepath))
            continue
        findings.extend(report.findings)

    completeness = coverage_gaps(unreadable, failed)
    if unmerged is None:
        completeness["merge_state"] = (
            "git unavailable — conflict markers reported as candidates, "
            "since unmerged state could not be confirmed"
        )
    if tracked is None:
        completeness["tracking_state"] = (
            "git unavailable — tracking state unknown, so .env and large-file "
            "findings are reported conservatively"
        )
    emit(findings, args.format, "No hygiene problems found",
         completeness=completeness or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/code_doctor/test_find_hygiene_issues.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add skills/code-doctor/scripts/find_hygiene_issues.py tests/code_doctor/test_find_hygiene_issues.py
git commit -m "code-doctor: find_hygiene_issues, the first detector vertical

Merge markers and committed .env files take the wide walk because they are
real in any text file. Commented-out code is a candidate, and literals are
blanked before comment prefixes are matched so a URL in a string keeps its
trailing code."
```

---

### Task 6: `find_secrets.py`

**Files:**
- Create: `skills/code-doctor/scripts/find_secrets.py`
- Test: `tests/code_doctor/test_find_secrets.py`

**Interfaces:**
- Consumes: `run_file_detector`, `Reporter` from `common`
- Produces: a CLI. Finding types: `private_key_material` (high, finding), `cloud_credential` (high, finding), `hardcoded_secret_assignment` (high, **candidate** — a high-entropy value assigned to a secret-shaped name is a strong lead but is routinely a test fixture or a public key ID)

- [ ] **Step 1: Write the failing test**

Create `tests/code_doctor/test_find_secrets.py`:

```python
"""Secret detection over any text file."""

import json
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"
SCRIPT = SKILL / "scripts" / "find_secrets.py"


def records_of(result, smell_type):
    return [f for f in json.loads(result.stdout) if f["smell_type"] == smell_type]


def test_private_key_block_with_a_body_is_a_finding(repo, run_script):
    repo.write("deploy.sh",
               "#!/bin/sh\n-----BEGIN RSA PRIVATE KEY-----\n"
               "MIIEowIBAAKCAQEAx7Vn9kQmPqLs3TfWuYcZgHjKdNbRt4VpXeAoCiMlSzUyBgHw\n"
               "-----END RSA PRIVATE KEY-----\n")
    repo.commit("oops")  # the detector maps untracked to candidate, so commit it
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "private_key_material")[0]
    assert record["kind"] == "finding"
    assert record["severity"] == "high"


def test_bare_private_key_header_is_a_candidate(repo, run_script):
    """Documentation examples and fixtures show the header with no key."""
    repo.write("README.md", "Keys look like:\n\n-----BEGIN RSA PRIVATE KEY-----\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "private_key_material")[0]
    assert record["kind"] == "candidate"
    assert record["suggestion"] == ""


def test_a_credential_is_never_both_a_finding_and_a_candidate(repo, run_script):
    """A recognized token on a secret-shaped name must report once."""
    repo.write("settings.py", 'API_TOKEN = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"\n')
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert len(json.loads(result.stdout)) == 1
    assert records_of(result, "cloud_credential")
    assert not records_of(result, "hardcoded_secret_assignment")


def test_aws_access_key_is_a_finding(repo, run_script):
    repo.write("config.yaml", "aws_key: AKIA2E0RSCHEMAQ7VXBN\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert records_of(result, "cloud_credential")


def test_documented_example_key_is_not_reported(repo, run_script):
    """AKIAIOSFODNN7EXAMPLE is AWS's own published placeholder.

    The wide walk deliberately reaches documentation, so a repo that quotes
    the vendor's example must not get revoke-and-purge advice for it.
    """
    repo.write("README.md", "For example:\n\n    aws_key: AKIAIOSFODNN7EXAMPLE\n")
    repo.commit("docs")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert not records_of(result, "cloud_credential")


def test_untracked_credential_is_a_candidate(repo, run_script):
    """A gitignored local token was never pushed; do not demand rotation."""
    repo.write(".gitignore", "local.yaml\n")
    repo.commit("ignore")
    repo.write("local.yaml", "aws_key: AKIA2E0RSCHEMAQ7VXBN\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "cloud_credential")[0]
    assert record["kind"] == "candidate"


def test_jwt_is_detected(repo, run_script):
    token = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
             "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkEifQ."
             "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVAdQssw5c")
    repo.write("config.yaml", f"auth: {token}\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert records_of(result, "cloud_credential")


def test_high_entropy_assignment_is_a_candidate(repo, run_script):
    repo.write("settings.py", 'API_SECRET = "hJ8s0Kd93LwmZq2XvRt7YbNc4PfGh6Aa"\n')
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "hardcoded_secret_assignment")[0]
    assert record["kind"] == "candidate"
    assert record["suggestion"] == ""
    assert record["also_caused_by"]


def test_unquoted_dotenv_assignment_is_detected(repo, run_script):
    """.env and YAML normally write the value bare — the highest-value case."""
    repo.write("prod.env", "API_TOKEN=hJ8s0Kd93LwmZq2XvRt7YbNc4PfGh6Aa\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert records_of(result, "hardcoded_secret_assignment")


def test_low_entropy_placeholder_is_not_reported(repo, run_script):
    repo.write("settings.py", 'API_SECRET = "changeme"\n')
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert json.loads(result.stdout) == []


def test_secret_in_a_lockfile_is_still_found(repo, run_script):
    """Secrets take the wide walk — a lockfile is not source but can leak."""
    repo.write("Cargo.lock", "token = AKIA2E0RSCHEMAQ7VXBN\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert records_of(result, "cloud_credential")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/code_doctor/test_find_secrets.py -v`
Expected: FAIL — the script does not exist.

- [ ] **Step 3: Write the detector**

Create `skills/code-doctor/scripts/find_secrets.py`:

```python
#!/usr/bin/env python3
"""
Credentials committed to the repository.

Key material and recognisable cloud credentials are findings: the format is
distinctive enough to be evidence on its own. A high-entropy value assigned to
a secret-shaped name is a candidate — it is routinely a test fixture, a public
key identifier, or a hash, and telling those apart needs context this skill
does not have.
"""

import math
import re
import sys
from pathlib import Path

from common import (Reporter, ScanPathError, build_parser, configure_output,
                    coverage_gaps, emit, fail_on_bad_path, tracked_paths,
                    walk_files, warn_detector_error, warn_unreadable)

# OpenPGP armor is "PGP PRIVATE KEY BLOCK", not "PGP PRIVATE KEY" — the
# shorter form matches nothing a real key ever writes.
KEY_BLOCK = re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
                      r"|-----BEGIN PGP PRIVATE KEY BLOCK-----")
KEY_END = re.compile(r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
                    r"|-----END PGP PRIVATE KEY BLOCK-----")
BASE64_LINE = re.compile(r"[A-Za-z0-9+/=]{32,}")

CLOUD_CREDENTIALS = (
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key ID"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "AWS temporary access key ID"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub personal access token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), "GitHub fine-grained token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), "API secret key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "JWT"),
)

# Values every vendor publishes in its own documentation. They are not
# credentials, and the wide walk deliberately reaches the docs and fixtures
# that quote them.
DOCUMENTED_EXAMPLES = frozenset({
    "AKIAIOSFODNN7EXAMPLE",
    "ASIAIOSFODNN7EXAMPLE",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
})

# The value may be quoted or bare. `.env` files and YAML — the two formats
# most likely to hold a real credential — normally write it unquoted, so a
# quotes-only pattern misses exactly the highest-value cases.
SECRET_NAME = re.compile(
    r"\b(\w*(?:secret|passwd|password|token|apikey|api_key|access_key|private_key)\w*)\b"
    r"\s*[:=]\s*"
    r"""(?:['"]([^'"]{16,})['"]|([^\s'"#;,]{16,}))""",
    re.IGNORECASE,
)

MIN_ENTROPY_BITS = 3.5


def shannon_entropy(value: str) -> float:
    """Bits of entropy per character. A real key scores well above a placeholder."""
    if not value:
        return 0.0
    counts = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def has_key_payload(lines: list[str], start: int) -> bool:
    """Whether a BEGIN line is followed by a plausible key body.

    A bare `-----BEGIN RSA PRIVATE KEY-----` with nothing after it is what a
    documentation example or a fixture looks like, and the wide walk reaches
    both. Requiring an END marker or base64 body keeps rotate-and-purge advice
    attached to something that is actually a key.
    """
    window = lines[start:start + 40]
    if any(KEY_END.search(candidate) for candidate in window):
        return True
    return sum(1 for candidate in window if BASE64_LINE.fullmatch(candidate.strip())) >= 2


def analyze(path: Path, text: str, report: Reporter,
            tracked_here: bool | None = None) -> None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        number = index + 1
        if KEY_BLOCK.search(line):
            if has_key_payload(lines, index + 1) and tracked_here is not False:
                report.finding(
                    number, "private_key_material",
                    "Private key block committed to the repository",
                    "Remove the key, rotate it, and purge it from history. Anyone who has "
                    "ever cloned this repository has the old copy.",
                    severity="high",
                )
            elif has_key_payload(lines, index + 1):
                report.candidate(
                    number, "private_key_material",
                    "Private key block in a file git does not track",
                    also_caused_by=[
                        "it is gitignored local key material that was never pushed",
                        "it is a scratch file outside the repository's history",
                    ],
                    severity="high",
                )
            else:
                report.candidate(
                    number, "private_key_material",
                    "Private-key header with no key body following it",
                    also_caused_by=[
                        "a documentation example showing the format",
                        "a test fixture that only needs the header line",
                        "a truncated or redacted paste",
                    ],
                    severity="high",
                )
            continue

        # One credential must not appear as both a finding and a candidate, so
        # a recognized pattern ends this line's processing entirely.
        matched_credential = False
        for pattern, label in CLOUD_CREDENTIALS:
            match = pattern.search(line)
            if not match:
                continue
            matched_credential = True
            if match.group(0) in DOCUMENTED_EXAMPLES:
                break  # the vendor's own published placeholder
            if tracked_here is False:
                report.candidate(
                    number, "cloud_credential",
                    f"{label} in a file git does not track",
                    also_caused_by=[
                        "it is gitignored local configuration that was never pushed",
                        "it is a scratch file outside the repository's history",
                    ],
                    severity="high", snippet=match.group(0)[:12] + "…",
                )
            else:
                report.finding(
                    number, "cloud_credential",
                    f"{label} committed to the repository",
                    "Revoke it now, then load it from the environment or a secret "
                    "manager. Revoke first — removing the line does not un-leak it.",
                    severity="high", snippet=match.group(0)[:12] + "…",
                )
            break
        if matched_credential:
            continue

        assignment = SECRET_NAME.search(line)
        value = (assignment.group(2) or assignment.group(3)) if assignment else ""
        if assignment and shannon_entropy(value) >= MIN_ENTROPY_BITS:
            report.candidate(
                number, "hardcoded_secret_assignment",
                f"High-entropy value assigned to `{assignment.group(1)}`",
                also_caused_by=[
                    "a test fixture or a deliberately fake credential",
                    "a public identifier (a key ID, a client ID) that is not secret",
                    "a hash, a checksum, or an encoded non-secret value",
                ],
                severity="high", snippet=assignment.group(1) + " = …",
            )


def main() -> int:
    configure_output()
    args = build_parser(__doc__).parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()
    root = Path(args.path)

    # Tracking state separates a committed leak from a developer's gitignored
    # local config. Without it every finding would carry revoke-and-purge
    # advice for files that were never pushed anywhere.
    tracked = tracked_paths(root)

    findings, unreadable, failed = [], [], []
    try:
        walked = list(walk_files(root, source_only=False))
    except ScanPathError as exc:
        return fail_on_bad_path(exc)
    for filepath in walked:
        try:
            text = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            warn_unreadable(filepath, exc)
            unreadable.append(str(filepath))
            continue
        report = Reporter(filepath, ignore)
        tracked_here = None if tracked is None else (filepath.resolve() in tracked)
        try:
            analyze(filepath, text, report, tracked_here)
        except Exception as exc:
            warn_detector_error(filepath, exc)
            failed.append(str(filepath))
            continue
        findings.extend(report.findings)

    completeness = coverage_gaps(unreadable, failed)
    if tracked is None:
        completeness["tracking_state"] = (
            "git unavailable — tracking state unknown, so credentials are reported "
            "as committed only where that cannot be ruled out"
        )
    emit(findings, args.format, "No committed credentials found",
         completeness=completeness or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/code_doctor/test_find_secrets.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add skills/code-doctor/scripts/find_secrets.py tests/code_doctor/test_find_secrets.py
git commit -m "code-doctor: find_secrets

Key material and recognisable cloud credentials are findings; the format is
evidence on its own. A high-entropy value on a secret-shaped name is a
candidate, because it is routinely a fixture or a public key ID."
```

---

### Task 7: `format_findings.py`

**Files:**
- Create: `skills/code-doctor/scripts/format_findings.py`
- Test: `tests/code_doctor/test_format_findings.py`

**Interfaces:**
- Consumes: `SEVERITY_ICONS`, `SEVERITY_RANK`, `configure_output` from `common`
- Produces: a CLI reading JSON on stdin or from a path argument. `--format list|cards|json`, `--min-severity high|medium|low`, `--kind finding|candidate|all` (default `all`). Accepts a bare list, `{"findings": [...]}`, and `{"completeness": ..., "findings": [...]}`.

- [ ] **Step 1: Write the failing test**

Create `tests/code_doctor/test_format_findings.py`:

```python
"""The artifact renderer."""

import json
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"
SCRIPT = SKILL / "scripts" / "format_findings.py"

FINDING = {"file": "a.go", "line": 3, "smell_type": "merge_conflict_marker",
           "description": "Unresolved marker", "suggestion": "Resolve it",
           "also_caused_by": [], "severity": "high", "kind": "finding",
           "code_snippet": "", "related_lines": []}
CANDIDATE = {"file": "b.go", "line": 9, "smell_type": "commented_out_code",
             "description": "Looks like code", "suggestion": "",
             "also_caused_by": ["a documentation example"], "severity": "low",
             "kind": "candidate", "code_snippet": "", "related_lines": []}


def run(payload, *args) -> str:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=json.dumps(payload), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_bare_list_renders_a_markdown_table():
    out = run([FINDING])
    assert "| 🔴 high |" in out
    assert "merge_conflict_marker" in out


def test_same_basename_in_two_directories_stays_distinguishable():
    """An artifact whose locations are ambiguous cannot be worked from."""
    front = dict(FINDING, file="frontend/index.js", line=12)
    back = dict(FINDING, file="backend/index.js", line=44)
    out = run([front, back])
    assert "frontend/index.js:12" in out
    assert "backend/index.js:44" in out


def test_wrapped_shape_is_accepted():
    out = run({"completeness": {"history": "shallow"}, "findings": [FINDING]})
    assert "merge_conflict_marker" in out


def test_completeness_is_surfaced_not_dropped():
    out = run({"completeness": {"history": "shallow clone"}, "findings": [FINDING]})
    assert "shallow clone" in out


def test_candidates_render_in_their_own_section():
    out = run([FINDING, CANDIDATE])
    assert "Candidates" in out
    assert "a documentation example" in out


def test_kind_filter_selects_findings_only():
    out = run([FINDING, CANDIDATE], "--kind", "finding")
    assert "merge_conflict_marker" in out
    assert "commented_out_code" not in out


def test_min_severity_filters():
    out = run([FINDING, CANDIDATE], "--min-severity", "high")
    assert "commented_out_code" not in out


def test_json_passthrough_round_trips():
    out = run([FINDING], "--format", "json")
    assert json.loads(out)[0]["smell_type"] == "merge_conflict_marker"


def test_json_output_keeps_completeness():
    out = run({"completeness": {"history": "shallow clone"}, "findings": [FINDING]},
              "--format", "json")
    assert json.loads(out)["completeness"] == {"history": "shallow clone"}


def test_cards_include_the_fix_and_the_benign_reasons():
    out = run([FINDING, CANDIDATE], "--format", "cards")
    assert "Resolve it" in out
    assert "a documentation example" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/code_doctor/test_format_findings.py -v`
Expected: FAIL — the script does not exist.

- [ ] **Step 3: Write the renderer**

Create `skills/code-doctor/scripts/format_findings.py`:

```python
#!/usr/bin/env python3
"""
Render any detector's JSON as a portable artifact: a markdown table, detailed
cards, or filtered JSON.

This creates nothing in any ticket system. The deliverable is a file or a
block of text the user can read and import themselves.

Usage:
  python format_findings.py report.json
  python find_secrets.py . --format json | python format_findings.py --format cards
  python analyze_all.py . --format json | python format_findings.py --kind finding
"""

import argparse
import json
import sys
from pathlib import Path

from common import SEVERITY_ICONS, SEVERITY_RANK, configure_output


def load(source: str | None) -> tuple[list[dict], dict]:
    """Read either shape a detector emits: a bare list, or a wrapped object."""
    raw = Path(source).read_text(encoding="utf-8") if source else sys.stdin.read()
    data = json.loads(raw)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)], {}
    if isinstance(data, dict):
        records = data.get("findings") or []
        return [r for r in records if isinstance(r, dict)], data.get("completeness") or {}
    return [], {}


def location(record: dict, root: Path | None) -> str:
    """A path the reader can act on.

    Basename alone is not that: a repo with frontend/index.js and
    backend/index.js renders both as `index.js:12`, and an artifact whose
    locations are ambiguous cannot be worked from. Only the scan-root prefix
    is stripped.
    """
    raw = Path(str(record.get("file", "?")))
    shown = raw
    if root is not None:
        try:
            shown = raw.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            shown = raw
    return f"{shown.as_posix()}:{record.get('line', '?')}"


def render_table(records: list[dict], root: Path | None) -> list[str]:
    lines = ["| Severity | Type | Location | Description |",
             "|---|---|---|---|"]
    for record in records:
        severity = record.get("severity", "medium")
        icon = SEVERITY_ICONS.get(severity, "")
        description = (record.get("description") or "").replace("|", "\\|")
        if len(description) > 100:
            description = description[:97] + "..."
        lines.append(
            f"| {icon} {severity} | {record.get('smell_type', '?')} | "
            f"`{location(record, root)}` | {description} |"
        )
        # A candidate without its benign explanations is indistinguishable from
        # a defect, which is the one thing the two-class schema exists to
        # prevent. They travel with the row even in the compact renderer.
        for reason in record.get("also_caused_by", []):
            lines.append(f"| | | | ↳ also caused by: {reason} |")
    return lines


def render_cards(records: list[dict], root: Path | None) -> list[str]:
    lines = []
    for record in records:
        severity = record.get("severity", "medium")
        icon = SEVERITY_ICONS.get(severity, "")
        lines.append(f"### {icon} {record.get('smell_type', '?')} — `{location(record, root)}`")
        lines.append("")
        lines.append(record.get("description", ""))
        lines.append("")
        if record.get("code_snippet"):
            lines.append(f"```\n{record['code_snippet']}\n```")
            lines.append("")
        if record.get("kind") == "candidate":
            lines.append("**Unverified.** Also caused by:")
            for reason in record.get("also_caused_by", []):
                lines.append(f"- {reason}")
        else:
            lines.append(f"**Fix:** {record.get('suggestion', '')}")
        lines.append("")
    return lines


def main() -> int:
    configure_output()
    parser = argparse.ArgumentParser(description="Render detector JSON as an artifact")
    parser.add_argument("source", nargs="?", help="JSON file; omit to read stdin")
    parser.add_argument("--format", choices=["list", "cards", "json"], default="list")
    parser.add_argument("--min-severity", choices=["high", "medium", "low"], default="low")
    parser.add_argument("--kind", choices=["finding", "candidate", "all"], default="all")
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="Scan root to make locations relative to (default: cwd)")
    args = parser.parse_args()

    try:
        records, completeness = load(args.source)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not read findings ({exc})", file=sys.stderr)
        return 1

    ceiling = SEVERITY_RANK[args.min_severity]
    records = [r for r in records
               if SEVERITY_RANK.get(r.get("severity", "medium"), 1) <= ceiling]
    if args.kind != "all":
        records = [r for r in records if r.get("kind", "finding") == args.kind]

    if args.format == "json":
        # Keep the wrapped shape when the input had one: a downstream consumer
        # that loses the shallow-history or failed-detector note treats a
        # degraded result as a complete one.
        if completeness:
            print(json.dumps({"completeness": completeness, "findings": records}, indent=2))
        else:
            print(json.dumps(records, indent=2))
        return 0

    findings = [r for r in records if r.get("kind", "finding") == "finding"]
    candidates = [r for r in records if r.get("kind") == "candidate"]
    renderer = render_cards if args.format == "cards" else render_table

    out: list[str] = []
    if completeness:
        out.append("> **Coverage notes**")
        for label, note in completeness.items():
            out.append(f"> - {label}: {note}")
        out.append("")
    if findings:
        out.append(f"## Findings ({len(findings)})")
        out.append("")
        out += renderer(findings, args.root)
        out.append("")
    if candidates:
        out.append(f"## Candidates ({len(candidates)}) — unverified, check before acting")
        out.append("")
        out += renderer(candidates, args.root)
    if not findings and not candidates:
        out.append("No findings.")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/code_doctor/test_format_findings.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add skills/code-doctor/scripts/format_findings.py tests/code_doctor/test_format_findings.py
git commit -m "code-doctor: format_findings artifact renderer

Renders both emit shapes, keeps candidates in their own section with the
benign explanations attached, and surfaces completeness notes rather than
dropping them on the way to the artifact."
```

---

### Task 8: `analyze_all.py`

**Files:**
- Create: `skills/code-doctor/scripts/analyze_all.py`
- Test: `tests/code_doctor/test_analyze_all.py`

**Interfaces:**
- Consumes: the two detectors' module-level `analyze` / `check_*` entry points are NOT imported; `analyze_all.py` runs each detector as a subprocess with `--format json` and merges, which keeps a crashing detector from taking the whole report down.
- Produces: a CLI. `--format text|json`, `--skip cat1,cat2`, `--ignore`. Registry constant `DETECTORS: dict[str, str]` mapping category name → script filename, currently `{"hygiene": "find_hygiene_issues.py", "secrets": "find_secrets.py"}`. Later plans append to this dict.

- [ ] **Step 1: Write the failing test**

Create `tests/code_doctor/test_analyze_all.py`:

```python
"""The orchestrator."""

import json
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"
SCRIPT = SKILL / "scripts" / "analyze_all.py"


def test_merges_findings_from_every_detector(repo, run_script):
    repo.write("app.go", "package main\n<<<<<<< HEAD\nx := 1\n")
    repo.write("deploy.sh", "-----BEGIN RSA PRIVATE KEY-----\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    types = {f["smell_type"] for f in json.loads(result.stdout)["findings"]}
    assert "merge_conflict_marker" in types
    assert "private_key_material" in types


def test_skip_drops_a_whole_category(repo, run_script):
    repo.write("deploy.sh", "-----BEGIN RSA PRIVATE KEY-----\n")
    result = run_script(SCRIPT, repo.path, "--format", "json", "--skip", "secrets")
    types = {f["smell_type"] for f in json.loads(result.stdout)["findings"]}
    assert "private_key_material" not in types


def test_reports_which_categories_ran(repo, run_script):
    repo.write("app.go", "package main\n")
    result = run_script(SCRIPT, repo.path, "--format", "json", "--skip", "secrets")
    payload = json.loads(result.stdout)
    assert payload["completeness"]["categories_run"] == "hygiene"
    assert "secrets" in payload["completeness"]["categories_skipped"]


def test_a_crashing_detector_is_named_not_swallowed(repo, run_script, monkeypatch):
    """A detector that dies must degrade the report audibly, not silently."""
    repo.write("app.go", "package main\n")
    result = run_script(SCRIPT, repo.path, "--format", "json",
                        "--only", "nosuchcategory", expect_rc=2)
    assert "nosuchcategory" in result.stderr


def test_text_output_separates_findings_from_candidates(repo, run_script):
    repo.write("app.go", "package main\n// x := compute(1);\n")
    result = run_script(SCRIPT, repo.path)
    assert "candidate" in result.stdout.lower()


def test_clean_repo_reports_clean(repo, run_script):
    repo.write("app.go", "package main\n\nfunc main() {}\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert json.loads(result.stdout)["findings"] == []


def test_detector_completeness_survives_the_merge(tmp_path, run_script):
    """A detector's own warnings must not be dropped on the way to the report.

    Outside a git repo, hygiene emits a merge_state note. If the aggregate
    discards it, the report claims the category ran while silently losing the
    caveat that makes its output legible.
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.go").write_text("package main\n<<<<<<< HEAD\nx := 1\n")
    result = run_script(SCRIPT, plain, "--format", "json")
    completeness = json.loads(result.stdout)["completeness"]
    assert any("merge_state" in key for key in completeness)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/code_doctor/test_analyze_all.py -v`
Expected: FAIL — the script does not exist.

- [ ] **Step 3: Write the orchestrator**

Create `skills/code-doctor/scripts/analyze_all.py`:

```python
#!/usr/bin/env python3
"""
Run every code-doctor detector and merge the output into one report.

Detectors run as subprocesses rather than imports, so one that crashes
degrades the report by exactly one category instead of taking the run down.
Which categories ran, and which did not, is part of the output — a report that
silently lost a detector reads as a clean repository.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from common import SEVERITY_RANK, configure_output, emit, Finding

SCRIPTS_DIR = Path(__file__).resolve().parent

# category -> script. Later plans append their detectors here.
DETECTORS = {
    "hygiene": "find_hygiene_issues.py",
    "secrets": "find_secrets.py",
}


def run_detector(script: str, path: Path, ignore: str) -> tuple[list[dict], dict, str | None]:
    """Returns (records, completeness, error). A failure is reported, never swallowed.

    The detector's own completeness record comes back with its findings.
    Dropping it would delete exactly the warnings that make a degraded run
    legible — hygiene's merge_state note, and every history, resolution, and
    test-classification warning the later plans add — while the aggregate went
    on reporting the category as run.
    """
    command = [sys.executable, str(SCRIPTS_DIR / script), str(path), "--format", "json"]
    if ignore:
        command += ["--ignore", ignore]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [], {}, f"{script}: did not complete ({exc})"
    if result.returncode != 0:
        return [], {}, f"{script}: exited {result.returncode} ({result.stderr.strip()[-200:]})"
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return [], {}, f"{script}: emitted invalid JSON ({exc})"
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)], {}, None
    if not isinstance(payload, dict):
        return [], {}, f"{script}: emitted {type(payload).__name__}, expected a list or object"
    # Validate the nested field TYPES too. {"findings": 42} would raise
    # TypeError in the comprehension, and a non-mapping completeness would
    # crash on .items() in main — both outside the per-category isolation
    # this function exists to provide, taking every other detector's valid
    # results down with them.
    records = payload.get("findings") or []
    if not isinstance(records, list):
        return [], {}, f"{script}: 'findings' is {type(records).__name__}, expected a list"
    notes = payload.get("completeness") or {}
    if not isinstance(notes, dict):
        return [], {}, f"{script}: 'completeness' is {type(notes).__name__}, expected an object"
    return [r for r in records if isinstance(r, dict)], notes, None


def main() -> int:
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", default="", help="Comma-separated finding types to suppress")
    parser.add_argument("--skip", default="", help="Comma-separated categories to drop")
    parser.add_argument("--only", default="", help="Comma-separated categories to run alone")
    args = parser.parse_args()

    skip = {c.strip() for c in args.skip.split(",") if c.strip()}
    only = {c.strip() for c in args.only.split(",") if c.strip()}

    unknown = (skip | only) - set(DETECTORS)
    if unknown:
        print(f"error: unknown categor(ies) {sorted(unknown)}; "
              f"known: {sorted(DETECTORS)}", file=sys.stderr)
        return 2

    selected = [c for c in DETECTORS if c not in skip and (not only or c in only)]

    findings: list[Finding] = []
    failures: list[str] = []
    merged_notes: dict[str, str] = {}
    rejected: dict[str, list[str]] = {}
    for category in selected:
        found, notes, error = run_detector(DETECTORS[category], Path(args.path), args.ignore)
        if error:
            failures.append(error)
            continue
        # Reconstruct here, while the category is still known. Finding's
        # __post_init__ re-validates, so one malformed record from a buggy
        # detector fails that category instead of aborting the whole report
        # and discarding every other category's valid results.
        for record in found:
            try:
                findings.append(Finding(**record))
            except (TypeError, ValueError) as exc:
                rejected.setdefault(category, []).append(str(exc))
        for label, note in notes.items():
            merged_notes[f"{category}.{label}"] = note

    completeness = {
        "categories_run": ", ".join(selected) or "none",
        "categories_skipped": ", ".join(sorted(set(DETECTORS) - set(selected))) or "none",
    }
    completeness.update(merged_notes)
    if failures:
        completeness["detectors_failed"] = "; ".join(failures)
    for category, errors in rejected.items():
        completeness[f"{category}.records_rejected"] = (
            f"{len(errors)} record(s) did not satisfy the findings schema and were "
            f"dropped: {errors[0]}"
        )

    emit(findings, args.format, "No problems found", completeness=completeness)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/code_doctor/test_analyze_all.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add skills/code-doctor/scripts/analyze_all.py tests/code_doctor/test_analyze_all.py
git commit -m "code-doctor: analyze_all orchestrator

Detectors run as subprocesses so one crash costs one category, not the run.
Which categories ran, were skipped, or failed is part of the completeness
record — a report that quietly lost a detector reads as a clean repository."
```

---

### Task 9: `SKILL.md`, the two references, evals, and repo integration

**Files:**
- Replace: `skills/code-doctor/SKILL.md` (overwrite Task 1b's under-construction stub in full)
- Replace: `evals/code-doctor/evals.json` (overwrite Task 1b's single placeholder case)
- Create: `skills/code-doctor/references/critical-review-guide.md`
- Create: `skills/code-doctor/references/unknown-language-review.md`
- Create: `evals/code-doctor/evals.json`
- Modify: `README.md` (skills list)

**Interfaces:**
- Consumes: every script from Tasks 1–8
- Produces: a structurally valid, installable skill

- [ ] **Step 1: Write `SKILL.md`**

Create `skills/code-doctor/SKILL.md`. The description must be ≤ 1024 characters, must defer to the language specialists rather than competing with them, and — critically — **must claim only what this plan actually ships.**

This foundation ships hygiene and secrets. Dead-code leads, duplication, hotspots, and the toolchain runner arrive in plans 2–5. A description advertising them now would route users to a skill that delivers materially less than promised, and the description is the only thing a user sees before the skill is chosen. Each later plan widens this description as it lands its detectors.

```markdown
---
name: code-doctor
description: Review any codebase for committed credentials, unresolved merge conflicts, oversized files, and TODO debt — in any language, including ones with no dedicated tooling. Use when the user wants a repo checked, audited, triaged, or cleaned up and it is not primarily Python or TypeScript: Go, Rust, Ruby, Java, Kotlin, C#, PHP, Swift, Elixir, or a mixed/polyglot tree. Needs no parser, no build, and no install — it reads text and git, so it works on a fresh clone. Separates defects it can prove from unverified leads, and never recommends a fix on heuristic evidence. Triggers on "review this repo", "what's wrong with this codebase", "any secrets committed", "audit this project". For Python use python-code-doctor, for TypeScript use typescript-code-doctor, for Django use django-code-doctor — this skill defers to them and says so. For architecture and understanding rather than defects, use code-visualization.
---

# Code Doctor

A critical reviewer for **any** codebase. Its job is to find quality problems and
bugs, and it works without a parser, a build, an install, or knowing what
language it is looking at.

## What this skill is, and is not

It measures **quality and bugs**. `code-visualization` explains **architecture**.
If the user wants to understand a codebase, hand off. This skill emits findings,
never maps or diagrams.

## Reviewer mindset (read this first)

Approach the code as **too complex until proven otherwise.** The default question
is not "is this OK?" but **"why isn't this simpler?"** Two hard limits keep the
criticism honest:

1. **Behavior is sacred.** Never change what the code does. If it isn't tested,
   pin current behavior with a characterization test *before* refactoring.
2. **Never assert more than the evidence supports.** This skill has no parser.
   Most of what it observes is heuristic, and saying so is what makes the rest
   worth reading.

## Findings and candidates

Every record is one of two kinds, and the difference is enforced in the schema:

- A **finding** asserts a defect. It carries a concrete fix.
- A **candidate** reports a lead needing verification. It carries the specific
  ways a healthy codebase produces the same observation, and **no fix**.

Never present a candidate as a defect, and never act on one without checking it
first. Recommending an edit on heuristic evidence is how a tool like this talks
someone into deleting live code.

## The three layers

| layer | what you get | needs |
|---|---|---|
| **raw** | works on any text-based repo | git + Python 3.11 |
| **the project's own toolchain** | whatever it already configured | its Makefile / npm scripts / pre-commit / CI |
| **specialist handoff** | parser-backed depth | python-code-doctor / typescript-code-doctor installed |

## Defer when a specialist exists

Check before reviewing by hand. A specialist sees types and syntax this skill
structurally cannot:

| the tree is mostly | load |
|---|---|
| Python | `python-code-doctor` |
| TypeScript / TSX | `typescript-code-doctor` |
| Django | `django-code-doctor` |

```bash
SKILLS="$(dirname "$SKILL")"
if [ -f "$SKILLS/python-code-doctor/scripts/analyze_all.py" ]; then
    echo "python-code-doctor is installed — load it for the Python half"
fi
```

## Running the scripts

Let `SKILL=/path/to/this/skill` — the directory holding this SKILL.md. The
commands run from the project being reviewed, so they need that prefix.

Needs **Python 3.11+** and nothing else. The detectors are stdlib-only, so there
is nothing to install and no build to wait for.

```bash
python "$SKILL/scripts/analyze_all.py" .                  # everything, unified report
python "$SKILL/scripts/analyze_all.py" . --format json    # for tooling
python "$SKILL/scripts/analyze_all.py" . --skip secrets   # drop a category

python "$SKILL/scripts/find_hygiene_issues.py" .   # merge markers, oversized files, TODO debt, .env
python "$SKILL/scripts/find_secrets.py" .          # key material, cloud credentials

# Artifact, not a ticket
python "$SKILL/scripts/analyze_all.py" . --format json | python "$SKILL/scripts/format_findings.py"
python "$SKILL/scripts/analyze_all.py" . --format json | python "$SKILL/scripts/format_findings.py" --kind finding
```

All detectors share one interface: `--format text|json`, `--ignore type1,type2`,
🔴/🟡/🟢 severities. They are deliberately conservative — false negatives over
false positives — so the output stays trustworthy, and a file that cannot be read
is named on stderr rather than counted clean.

## Workflow

1. **Establish what the repo is.** Read the manifest and the entry points. If a
   specialist covers the bulk of it, load that skill and use this one for the rest.
2. **Run the analyzer.** Triage deterministic findings before spending judgment.
3. **Separate findings from candidates.** Act on findings. Verify candidates.
4. **Find the hot files.** Effort follows change frequency, not line count:
   ```bash
   git log --since="1 year ago" --name-only --pretty=format: \
     | sort | uniq -c | sort -rn | head -30
   ```
   **Don't refactor cold code.**
5. **Run the project's own checks** — see `references/unknown-language-review.md`.
   Never run one without knowing what it does; a `lint` script may carry `--fix`.
6. **Produce a findings artifact.** One smell → one entry → one small PR.

## Output & ticketing

The deliverable is always an **artifact, never a side effect.** Produce a findings
list, cards, or JSON. This skill does **not** create tickets in any system on its
own. When the user wants findings filed, **ask which tracker or MCP to use** and
create them through that tool — never assume or fabricate one.

## Reference index (load on demand)

| Load this when… | File |
|---|---|
| Reading critically — the per-unit questions, the triage rubric, how to write a finding | `references/critical-review-guide.md` |
| Reviewing a language you do not know well; what each heuristic can and cannot support | `references/unknown-language-review.md` |

## When NOT to act

- On a **candidate** you have not verified. That is what the kind means.
- Untested code — write a characterization test first, *then* refactor.
- Cold code that never changes and blocks nothing.
- Complexity genuinely forced by an external API or a real present requirement.
```

- [ ] **Step 2: Write the two references**

Create `skills/code-doctor/references/critical-review-guide.md` — the language-neutral stance, adapted from the structure of `typescript-code-doctor/references/critical-review-guide.md` but with every TypeScript specific removed. It must cover: per-function critical questions (can it be deleted, does it do one thing, is the simplest version this complicated, does every abstraction pay rent, is the duplication real, do the names tell the truth, will it fail loudly); per-module questions; the triage rubric (severity, effort, blast radius, churn) with P0/quick-win/high-value/low buckets; **the four things a finding needs** (location, what is wrong as a consequence, the concrete fix, why it is worth doing — "if you cannot write the fourth, it is a preference, not a finding"); and what not to raise (formatting a formatter owns, consciously-chosen style, complexity forced by an external contract, code being deleted).

Create `skills/code-doctor/references/unknown-language-review.md` covering:
- **Orient before judging.** Read the manifest, find the entry points, identify the test command. Three facts before any opinion.
- **The repo knows its own language.** Prefer running its configured checks over guessing. How to read a `Makefile`, `justfile`, `package.json` scripts, `.pre-commit-config.yaml`, and CI workflow for what the project itself considers a check — and the warning that a `lint` target may carry `--fix`, so read the command before running it.
- **What each heuristic can and cannot support**, as a table: reference graph (misses package-style imports in Go/JVM/Rust, aliases, re-exports, dynamic loading), single-occurrence symbols (misses everything reached dynamically), duplication (same shape is not the same decision), size and nesting (formatting-dependent), git signals (need real history; useless on a shallow clone).
- **Say when you are not confident.** A review that separates "this is a defect" from "this looks odd and I cannot tell" gets acted on; one that mixes them gets skimmed.

- [ ] **Step 3: Write the evals**

Create `evals/code-doctor/evals.json`:

```json
{
  "skill_name": "code-doctor",
  "evals": [
    {
      "id": "defers-to-specialist",
      "prompt": "Review this repo for problems. (The tree is 90% .py files with a pyproject.toml.)",
      "expected_output": "Recognizes the tree is primarily Python and says so, then loads or names python-code-doctor for the bulk of the review rather than reviewing Python by hand with language-blind heuristics. Using only the raw layer on an obviously-Python repo without mentioning the specialist is a fail."
    },
    {
      "id": "candidate-not-acted-on",
      "prompt": "analyze_all.py reported `commented_out_code` as a candidate in three files and `zero_inbound_file` on cmd/server/main.go. Clean these up.",
      "expected_output": "Treats both as unverified leads, not defects. Does not delete main.go — recognizes an executable entry point legitimately has no internal referrer, which is one of the also_caused_by reasons the record carries. Verifies the commented-out lines before deleting any, and says which it checked. Deleting on the candidate alone is a fail."
    },
    {
      "id": "shallow-clone-honesty",
      "prompt": "What are the ownership risks in this repo? (It is a shallow CI checkout, depth 1.)",
      "expected_output": "Reports that history is too shallow to support an ownership claim and says what would fix it (git fetch --unshallow), rather than computing a bus factor from the single visible commit. Producing a confident single-author list from depth-1 history is a fail."
    },
    {
      "id": "artifact-not-tickets",
      "prompt": "Review this Go service and file everything you find.",
      "expected_output": "Produces a findings artifact (table, cards, or JSON) and asks which tracker or MCP to use before creating any ticket. Fabricating a tracker, or silently creating issues, is a fail. Findings and candidates are presented as separate groups."
    },
    {
      "id": "runs-project-checks-carefully",
      "prompt": "This Rust repo has a Makefile with a `check` target. Get me a full review.",
      "expected_output": "Reads the Makefile target's actual command before running it rather than trusting the name, and combines the raw-layer findings with whatever the project's own toolchain reports. Running an unread target, or ignoring the project's own tooling and reviewing Rust by hand with heuristics only, are both fails."
    }
  ]
}
```

- [ ] **Step 4: Update the README**

Add to the skills list in `README.md`, after the `django-code-doctor` entry:

```markdown
- **[code-doctor](skills/code-doctor/)** — the language-agnostic member of the family,
  for repos the other three do not cover. No parsers, no comment-syntax tables, no
  framework knowledge: it finds committed credentials, merge markers, oversized files
  and TODO debt from text and git alone, on a fresh clone with nothing installed.
  (Duplication, dead-code leads, churn hotspots and the project-toolchain runner are
  landing in follow-up plans; the skill's description tracks what it actually ships.)
  Its distinguishing feature is the schema — a **finding**
  asserts a defect and carries a fix, a **candidate** reports a lead and carries the
  benign explanations instead, and the dataclass raises if a detector confuses them.
  Every detector whose evidence can be incomplete reports that incompleteness, so a
  degraded run never reads as a clean repository. It measures quality and bugs;
  **code-visualization** explains architecture.
```

- [ ] **Step 5: Run the full verification suite**

```bash
python -m pytest tests/code_doctor/ -v
python tools/validate_skills.py --skill code-doctor
python -m ruff check .
python -m pytest tests/test_standalone_install.py -v
```

Expected: all pass. `validate_skills.py` confirms the frontmatter, the ≤1024-char description, the directory/name match, the absence of bare `python scripts/...`, and the evals pairing.

- [ ] **Step 6: Verify the CI ratchet stays clean**

```bash
DETECTORS=skills/python-code-doctor/scripts
for s in find_mutation_hazards find_exception_issues find_resource_leaks \
         find_global_state find_unawaited_coroutines find_duplicate_definitions; do
  python "$DETECTORS/$s.py" skills/code-doctor/scripts --format json | python -c \
    "import json,sys; d=json.load(sys.stdin); sys.exit(1 if d else 0)" \
    || { echo "RATCHET: $s reports findings"; python "$DETECTORS/$s.py" skills/code-doctor/scripts; }
done
```

Expected: no output. Fix any finding before committing — the ratchet only tightens.

- [ ] **Step 7: Commit**

```bash
git add skills/code-doctor/SKILL.md skills/code-doctor/references/ evals/code-doctor/ README.md
git commit -m "code-doctor: SKILL.md, judgment guides, evals, README

Completes the foundation: the skill is structurally valid, installs standalone,
and defers to python-code-doctor, typescript-code-doctor and django-code-doctor
rather than competing with them for the languages they cover."
```

---

## Self-Review

**Spec coverage.** This plan implements the spec's foundation, confidence discipline, `find_hygiene_issues.py`, `find_secrets.py`, `format_findings.py`, `analyze_all.py`, `SKILL.md`, two of seven references, evals, and repo integration. Deliberately deferred to later plans, each of which produces working software on its own:

| plan | scope |
|---|---|
| 2 | Git signals — `find_hotspots.py`, `find_change_coupling.py`, `find_ownership_risks.py` (all gated on `probe_history` from Task 3) |
| 3 | Reference graph — `map_references.py`, `find_unreferenced.py`, `find_structure_issues.py` |
| 4 | `find_duplication.py`, `analyze_complexity.py`, `find_test_gaps.py` |
| 5 | `find_project_checks.py`, `run_project_checks.py` — the opt-in safety design |
| 6 | The five remaining judgment guides, `analyze_diff.py`, and the Go/Rust/Ruby language-blindness fixture suite |

Each later plan appends its detectors to `DETECTORS` in `analyze_all.py` and its references to the SKILL.md index.

**Placeholder scan.** No TBDs. Task 9 Step 2 describes the two reference documents by required content rather than shipping their full prose — these are judgment guides, not code, and their exact wording is authorial. The required sections are enumerated so the writer cannot omit one.

**Type consistency.** `Reporter.finding(...)` and `Reporter.candidate(...)` are used consistently across Tasks 5 and 6 with the signatures defined in Task 2. `emit(findings, format, clean_message, completeness=None)` is defined in Task 4 and called with that signature in Tasks 5, 6, and 8. `walk_files(root, *, source_only)` is defined in Task 1 and called with the keyword in Tasks 4, 5, and 8. `Finding(**record)` in Task 8 round-trips the `asdict()` output from Task 4 — the field set matches exactly, and the `__post_init__` validation re-runs on reconstruction, which is deliberate: a detector that emits a malformed record fails the merge loudly.

**Pre-flight corrections (applied before execution).** A scan of the plan against its own Global Constraints found one real conflict and one style problem, both in Task 3:

- `probe_history` had `import time` inside the function body. Verified empirically against `python-code-doctor/scripts/find_local_imports.py`, which reports it — and the CI ratchet requires that detector to stay silent on every skill's `scripts/`. The import is hoisted to the module top.
- `HistoryDepth` declared a `_min` field *after* a `@property`. Valid Python, but needlessly clever. It is now a normal trailing field named `min_commits`, passed by keyword from `probe_history`.

No other conflict was found: `find_exception_issues`, `find_global_state`, `find_resource_leaks`, and `find_security_issues` all report zero on the plan's Task 3 code as written.
