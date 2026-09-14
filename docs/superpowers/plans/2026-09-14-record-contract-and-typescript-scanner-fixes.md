# Record Contract Port and TypeScript Scanner Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce the finding/candidate record contract in every doctor (TypeScript, Rust, Python, Django) with a cross-doctor conformance suite, reclassify nine TypeScript heuristics as candidates, and fix the six verified TypeScript scanner/config bugs test-first.

**Architecture:** `skills/code-doctor/scripts/common.py` already holds the target record: a frozen dataclass whose `__post_init__` raises `SchemaError` when a finding lacks a suggestion or a candidate lacks benign explanations. Phase 1 pastes that exact class into the TypeScript and Rust `common.py`, adds `Reporter.candidate()` beside the existing `Reporter.add()` (152 and 187 call sites stay untouched), and teaches each skill's `analyze_all.py`, `format_findings.py`, and text printer to keep candidates out of the defect list. Python keeps its 25 per-detector record classes; the contract is enforced at the report hop through `validate_record()`, and the three detectors that emit candidates are brought into conformance. A single parametrized test module discovers every `*-code-doctor/scripts/common.py` and proves the invariants hold identically. Phase 2 replaces `rglob` with a pruned `os.walk` in one shared walker, scopes test-directory classification below the project root, recognizes all three declaration-file suffixes, removes four silent caps, resolves the package manager's own executable for audits, and skips files carrying a `@generated` / `DO NOT EDIT` header.

**Tech Stack:** Python 3.11+, stdlib only (`dataclasses`, `json`, `os`, `pathlib`, `subprocess`). pytest with the existing `load_module` fixture from `tests/conftest.py`; detector tests drive scripts as subprocesses over `tmp_path` fixtures.

**Spec:** `docs/superpowers/specs/2026-09-14-coding-skills-assurance-typescript-hardening-design.md`, sections 2 (preserved invariants), 6 (Phase 1), 7 (Phase 2), 16 (testing rules). This plan covers spec phases 1 and 2 only.

## Global Constraints

- **Python 3.11+, stdlib only, no PEP 701 f-strings** (`pyproject.toml:4-9`). No nested same-quote f-strings; no backslashes inside f-string expressions.
- **Each skill directory installs and runs alone** (`tests/test_standalone_install.py`). No skill imports another skill's module. `skills/django-code-doctor/scripts/common.py` and `format_findings.py` must stay byte-identical to `skills/python-code-doctor/scripts/` (`.github/workflows/ci.yml:79-86`): every edit to the Python copy is copied with `cp` in the same commit.
- **JSON output shape does not change in this plan.** Every TypeScript, Rust, and Python detector keeps printing a bare JSON list; `analyze_all.py` keeps its `{meta, summary, categories}` envelope. The CI ratchet (`ci.yml:100-127`) reads a top-level list and filters `r.get('kind') != 'candidate'`; `tests/typescript_code_doctor/test_detectors.py:21-30` does `json.loads(stdout)` and expects a list.
- **Preserved invariants (spec §2.4):** failed doctor ≠ zero findings; bare list ≠ known coverage; a skipped or failed analyzer ungrades its categories; candidates never score; an unmeasured category is ungraded, not 0 or 100.
- **A candidate carries no `suggestion` and at least one non-blank `also_caused_by`; a finding carries a non-blank `suggestion` and no `also_caused_by`.** `kind` is always present on an emitted record after this plan (`"finding"` or `"candidate"`).
- **Every bug fix begins with a test that fails on current code** (spec §16.1). Run the failing step and paste its failure before implementing.
- **The "quiet on good code" rule holds for candidates too:** `test_detector_fires_on_bad_and_is_quiet_on_good` (`tests/typescript_code_doctor/test_detectors.py:461-471`) forbids any record, candidate included, on the `GOOD_*` fixtures.
- **CI gates that must stay green after every task:** `ruff check .`, `python tools/validate_skills.py`, `python tools/validate_styles.py`, `pytest -q` (about 4 minutes), and the ratchet. Run `ruff check .` and the touched skill's test directory before every commit; run the full `pytest -q` before the final commit of each phase.
- **Commit messages** describe the behavior change in the imperative, no model identifiers.
- Tests use the `load_module` fixture (`tests/conftest.py:151-168`) to import a skill's module; it evicts shared names from `sys.modules` so the right skill's `common.py` loads. Detector tests use each test directory's existing `run_detector`, `smells`, and `write` helpers.

---

## Phase 1 — Record contract

### Task 1: TypeScript `Finding` gains `kind`, invariants, and `Reporter.candidate()`

**Files:**
- Modify: `skills/typescript-code-doctor/scripts/common.py:109-190` (the `Finding` dataclass, `Reporter`, `sort_findings`, `print_findings`)
- Create: `tests/typescript_code_doctor/test_common.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (in `typescript-code-doctor/scripts/common.py`):
  - `class SchemaError(ValueError)`
  - `VALID_KINDS = frozenset({"finding", "candidate"})`
  - `@dataclass(frozen=True) class Finding` with fields `file, line, smell_type, description, suggestion="", also_caused_by=(), severity="medium", kind="finding", code_snippet="", related_lines=()`; `__post_init__` coerces the two tuple fields and raises `SchemaError` on contract violations.
  - `Reporter.add(line, smell_type, description, suggestion, severity="medium", related=None)` — unchanged signature, constructs `kind="finding"`.
  - `Reporter.candidate(line, smell_type, description, also_caused_by, severity="low", related=None)` — constructs `kind="candidate"`, `suggestion=""`.
  - `sort_findings` orders findings before candidates, then severity, file, line.
  - `print_findings` prints `N finding(s), M candidate(s):` and a `Candidates — unverified leads, check before acting:` section.

- [ ] **Step 1: Write the failing tests**

Create `tests/typescript_code_doctor/test_common.py`:

```python
"""The record contract in typescript-code-doctor's common.py.

A finding asserts a defect and carries a fix; a candidate reports a lead and
carries the benign explanations instead. The dataclass raises when a detector
confuses them, so the distinction cannot depend on a detector author's memory.
"""

import dataclasses
import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS_DIR, "common")


class _File:
    """The two attributes Reporter reads from a parsed file."""

    def __init__(self, path):
        self.path = path

    def snippet(self, line):
        return f"line {line}"


def test_finding_requires_a_suggestion(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a.ts", line=1, smell_type="x", description="d", suggestion="")


def test_finding_may_not_carry_benign_explanations(common):
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a.ts", line=1, smell_type="x", description="d",
                       suggestion="fix it", also_caused_by=["something benign"])


def test_candidate_requires_benign_explanations(common):
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a.ts", line=1, smell_type="x", description="d",
                       kind="candidate", also_caused_by=[])


def test_candidate_may_not_carry_a_fix(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a.ts", line=1, smell_type="x", description="d",
                       kind="candidate", suggestion="delete it",
                       also_caused_by=["it is an entry point"])


def test_unknown_kind_is_rejected(common):
    with pytest.raises(common.SchemaError, match="kind"):
        common.Finding(file="a.ts", line=1, smell_type="x", description="d",
                       suggestion="fix", kind="probably")


def test_finding_is_frozen_and_collections_are_tuples(common):
    candidate = common.Finding(file="a.ts", line=1, smell_type="x", description="d",
                               kind="candidate", also_caused_by=["it is an entry point"],
                               related_lines=[2, 3])
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.suggestion = "delete it"
    assert isinstance(candidate.also_caused_by, tuple)
    assert isinstance(candidate.related_lines, tuple)


def test_finding_survives_json_round_trip(common):
    original = common.Finding(file="a.ts", line=2, smell_type="y", description="d",
                              kind="candidate", also_caused_by=["it is an entry point"],
                              related_lines=[3, 4])
    payload = json.loads(json.dumps(dataclasses.asdict(original)))
    assert payload["kind"] == "candidate"
    assert common.Finding(**payload) == original


def test_reporter_add_makes_a_finding_with_the_snippet(common):
    reporter = common.Reporter(_File(Path("a.ts")), set())
    reporter.add(4, "smell", "d", "fix it", "high", related=[7])
    record = reporter.findings[0]
    assert record.kind == "finding"
    assert record.code_snippet == "line 4"
    assert record.related_lines == (7,)


def test_reporter_candidate_makes_a_candidate_without_a_fix(common):
    reporter = common.Reporter(_File(Path("a.ts")), set())
    reporter.candidate(4, "lead", "d", ["it may be loaded by convention"])
    record = reporter.findings[0]
    assert record.kind == "candidate"
    assert record.suggestion == ""
    assert record.severity == "low"
    assert record.code_snippet == "line 4"


def test_reporter_candidate_honours_ignore(common):
    reporter = common.Reporter(_File(Path("a.ts")), {"lead"})
    reporter.candidate(1, "lead", "d", ["benign"])
    reporter.candidate(2, "kept", "d", ["benign"])
    assert [f.smell_type for f in reporter.findings] == ["kept"]


def test_sort_puts_findings_before_candidates(common):
    low_finding = common.Finding(file="a.ts", line=9, smell_type="x", description="d",
                                 suggestion="fix", severity="low")
    high_candidate = common.Finding(file="a.ts", line=1, smell_type="y", description="d",
                                    kind="candidate", also_caused_by=["benign"], severity="high")
    ordered = common.sort_findings([high_candidate, low_finding])
    assert [f.kind for f in ordered] == ["finding", "candidate"]


def test_text_report_separates_candidates(common, capsys):
    finding = common.Finding(file="a.ts", line=1, smell_type="x", description="a defect",
                             suggestion="fix it")
    candidate = common.Finding(file="a.ts", line=2, smell_type="y", description="a lead",
                               kind="candidate", also_caused_by=["it is a test double"])
    common.print_findings([finding, candidate], "clean")
    out = capsys.readouterr().out
    assert "1 finding(s), 1 candidate(s)" in out
    assert "Candidates — unverified leads" in out
    assert out.index("a defect") < out.index("Candidates — unverified leads") < out.index("a lead")
    assert "Also caused by:" in out and "it is a test double" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/typescript_code_doctor/test_common.py -q`
Expected: failures such as `AttributeError: module 'common' has no attribute 'SchemaError'` and `TypeError: __init__() got an unexpected keyword argument 'kind'`.

- [ ] **Step 3: Replace the record and reporter**

In `skills/typescript-code-doctor/scripts/common.py`, replace lines 114–146 (from `SEVERITY_RANK = ...` through the end of `Reporter.add`) with:

```python
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


class SchemaError(ValueError):
    """A detector tried to emit a record its evidence does not support."""


VALID_KINDS = frozenset({"finding", "candidate"})


@dataclass(frozen=True)
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

    Frozen to ensure the schema enforcement holds across the lifetime of the
    object, not just at construction time.
    """

    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str = ""
    # Tuples, not lists. `frozen=True` blocks reassignment but not in-place
    # mutation, so a list here would let `candidate.also_caused_by.clear()`
    # walk a validated record into a schema-invalid state that
    # __post_init__ never re-checks — and it would then serialise and emit
    # like any other record. __post_init__ below coerces any list handed to
    # the constructor (including one that came back out of json.loads,
    # which knows nothing about tuples) into a tuple, so the type is
    # actually enforced, not just annotated.
    also_caused_by: tuple[str, ...] = ()
    severity: str = "medium"
    kind: str = "finding"
    code_snippet: str = ""
    # Other lines that participate in the same finding. analyze_diff.py uses
    # these so a cross-declaration smell surfaces when any participant changed.
    related_lines: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "also_caused_by", tuple(self.also_caused_by))
        object.__setattr__(self, "related_lines", tuple(self.related_lines))
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
            if not self.also_caused_by or not any(s.strip() for s in self.also_caused_by):
                raise SchemaError(
                    f"{self.smell_type}: a candidate must name the ways a healthy codebase produces "
                    "this observation in also_caused_by, so the reader can rule them out"
                )


class Reporter:
    """Collects records for one file, honouring the detector's --ignore set."""

    def __init__(self, tsfile, ignore: set[str]):
        self.tsfile = tsfile
        self.ignore = ignore
        self.findings: list[Finding] = []

    def add(self, line: int, smell_type: str, description: str, suggestion: str,
            severity: str = "medium", related: list[int] | None = None) -> None:
        """A finding: the evidence proves a defect and names its fix."""
        self._add(Finding(
            file=str(self.tsfile.path), line=line, smell_type=smell_type,
            description=description, suggestion=suggestion, severity=severity,
            kind="finding", code_snippet=self.tsfile.snippet(line),
            related_lines=tuple(related or ()),
        ))

    def candidate(self, line: int, smell_type: str, description: str,
                  also_caused_by: list[str] | tuple[str, ...], severity: str = "low",
                  related: list[int] | None = None) -> None:
        """A candidate: a lead the syntax alone cannot prove, with the benign readings."""
        self._add(Finding(
            file=str(self.tsfile.path), line=line, smell_type=smell_type,
            description=description, also_caused_by=tuple(also_caused_by),
            severity=severity, kind="candidate", code_snippet=self.tsfile.snippet(line),
            related_lines=tuple(related or ()),
        ))

    def _add(self, record: Finding) -> None:
        if record.smell_type not in self.ignore:
            self.findings.append(record)
```

Then replace `sort_findings` and `print_findings` (lines 157–180 in the original numbering) with:

```python
def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Findings before candidates, then by severity, then by location."""
    findings.sort(key=lambda f: (f.kind != "finding",
                                 SEVERITY_RANK.get(f.severity, 1), f.file, f.line))
    return findings


def print_findings(findings: list[Finding], clean_message: str) -> None:
    if not findings:
        print(f"✅ {clean_message}")
        return
    confirmed = [f for f in findings if f.kind == "finding"]
    leads = [f for f in findings if f.kind == "candidate"]
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.smell_type] = counts.get(finding.smell_type, 0) + 1
    print(f"{len(confirmed)} finding(s), {len(leads)} candidate(s):\n")
    print("Summary:")
    for smell, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {smell}: {count}")
    print()
    for finding in confirmed:
        icon = SEVERITY_ICONS.get(finding.severity, "")
        print(f"{icon} [{finding.severity.upper()}] {finding.file}:{finding.line}")
        print(f"   {finding.smell_type}: {finding.description}")
        if finding.code_snippet:
            print(f"   Code: {finding.code_snippet}")
        print(f"   → {finding.suggestion}\n")
    if leads:
        print("Candidates — unverified leads, check before acting:\n")
    for lead in leads:
        icon = SEVERITY_ICONS.get(lead.severity, "")
        print(f"{icon} [candidate] {lead.file}:{lead.line}")
        print(f"   {lead.smell_type}: {lead.description}")
        if lead.code_snippet:
            print(f"   Code: {lead.code_snippet}")
        print("   Also caused by:")
        for reason in lead.also_caused_by:
            print(f"     - {reason}")
        print()
```

`field` is no longer used by this module: remove it from the `from dataclasses import asdict, dataclass, field` line, or ruff will fail on the unused import.

- [ ] **Step 4: Run the new tests and the existing TypeScript suite**

Run: `pytest tests/typescript_code_doctor -q`
Expected: all pass. The seven direct `Finding(...)` constructions in tree detectors (`find_dead_code.py:118`, `find_dependency_issues.py:66`, `find_duplicates.py:166`, `find_module_issues.py:66`, `find_overengineering.py:30`, `find_tsconfig_issues.py:133`, `find_untested_modules.py:27`) use keyword arguments and pass `related_lines=related or []`, which `__post_init__` coerces; they need no change. If any test fails with `SchemaError ... must carry a suggestion`, a detector emitted an empty suggestion; fix that detector's text rather than the contract.

- [ ] **Step 5: Lint and commit**

```bash
ruff check skills/typescript-code-doctor tests/typescript_code_doctor
git add skills/typescript-code-doctor/scripts/common.py tests/typescript_code_doctor/test_common.py
git commit -m "typescript-code-doctor: enforce the finding/candidate contract in Finding"
```

---

### Task 2: TypeScript `analyze_all.py` and `format_findings.py` keep candidates out of the defect list

**Files:**
- Modify: `skills/typescript-code-doctor/scripts/analyze_all.py:83-138` (`generate_report`), `:142-200` (`print_text_report`)
- Modify: `skills/typescript-code-doctor/scripts/format_findings.py:33-113`
- Test: `tests/typescript_code_doctor/test_runner.py` (append), `tests/typescript_code_doctor/test_format_findings.py` (create)

**Interfaces:**
- Consumes: `Finding`, `SchemaError` from Task 1.
- Produces:
  - `generate_report(...)["summary"]["total_candidates"]: int`
  - `generate_report(...)["meta"]["records_rejected"]: dict[str, str]` present only when a record failed re-validation.
  - `format_findings.py` renders candidates with a `❓ candidate` rank, `[Investigate]` cards, and a `kind: "candidate"` ticket field.

- [ ] **Step 1: Write the failing tests**

Append to `tests/typescript_code_doctor/test_runner.py`:

```python
def test_candidates_are_counted_separately_and_kept_out_of_the_high_list(tmp_path, load_module, capsys):
    """A candidate is a lead, not a defect: it has its own count and its own
    heading, and never appears under HIGH SEVERITY ISSUES."""
    module = load_module(SCRIPTS_DIR, "analyze_all")
    report = {
        "meta": {"analyzed_path": str(tmp_path), "timestamp": "t",
                 "analyzers_run": ["types"], "analyzers_skipped": [], "analyzer_errors": {}},
        "summary": {"total_issues": 2, "total_candidates": 1,
                    "by_severity": {"high": 2, "medium": 0, "low": 0},
                    "by_category": {"types": 2}},
        "categories": {"types": {"count": 2, "issues": [
            {"file": "a.ts", "line": 1, "smell_type": "as_any", "description": "a defect",
             "suggestion": "fix it", "severity": "high", "kind": "finding", "category": "types"},
            {"file": "a.ts", "line": 2, "smell_type": "type_assertion", "description": "a lead",
             "suggestion": "", "severity": "high", "kind": "candidate", "category": "types",
             "also_caused_by": ["the value was narrowed by a runtime check"]},
        ]}},
    }
    module.print_text_report(report)
    out = capsys.readouterr().out
    high_block = out[out.index("HIGH SEVERITY ISSUES"):out.index("CANDIDATES")]
    assert "a defect" in high_block and "a lead" not in high_block
    assert "a lead" in out[out.index("CANDIDATES"):]
    assert "Candidates: 1" in out


def test_generate_report_counts_candidates_and_rejects_invalid_records(project, load_module, monkeypatch):
    """The report hop re-validates every record. A candidate with no benign
    explanation is dropped and named, never silently counted as a finding."""
    module = load_module(SCRIPTS_DIR, "analyze_all")

    def fake_run_detectors(path, file_specs, tree_specs, jobs=None):
        return {"types": [
            {"file": "a.ts", "line": 1, "smell_type": "as_any", "description": "d",
             "suggestion": "fix", "severity": "high", "kind": "finding",
             "also_caused_by": [], "code_snippet": "", "related_lines": []},
            {"file": "a.ts", "line": 2, "smell_type": "type_assertion", "description": "d",
             "suggestion": "", "severity": "low", "kind": "candidate",
             "also_caused_by": ["benign"], "code_snippet": "", "related_lines": []},
            {"file": "a.ts", "line": 3, "smell_type": "bogus", "description": "d",
             "suggestion": "", "severity": "low", "kind": "candidate",
             "also_caused_by": [], "code_snippet": "", "related_lines": []},
        ]}

    monkeypatch.setattr(module, "run_detectors", fake_run_detectors)
    monkeypatch.setattr(module, "ANALYZERS", [("types", "find_type_gaps", "Types", module.FILE)])
    report = module.generate_report(str(project))
    assert report["summary"]["total_issues"] == 2
    assert report["summary"]["total_candidates"] == 1
    assert "bogus" in report["meta"]["records_rejected"]["types"]
```

Create `tests/typescript_code_doctor/test_format_findings.py`:

```python
"""format_findings.py must not turn an unverified lead into a refactoring ticket."""

import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"

RECORDS = [
    {"file": "src/a.ts", "line": 1, "smell_type": "as_any", "description": "a defect",
     "suggestion": "fix it", "severity": "high", "kind": "finding"},
    {"file": "src/a.ts", "line": 2, "smell_type": "type_assertion", "description": "a lead",
     "suggestion": "", "severity": "low", "kind": "candidate",
     "also_caused_by": ["the value was narrowed by a runtime check"]},
]


@pytest.fixture
def fmt(load_module):
    return load_module(SCRIPTS_DIR, "format_findings")


def test_list_marks_candidates_in_the_severity_column(fmt):
    out = fmt._render_list(RECORDS)
    assert "1 finding(s), 1 candidate(s)" in out
    assert "| ❓ candidate | type_assertion |" in out
    assert "Unverified lead" in out


def test_cards_file_candidates_as_investigations_not_refactors(fmt):
    out = fmt._render_cards(RECORDS)
    assert "### [Refactor] as_any" in out
    assert "### [Investigate] type_assertion" in out
    assert "Also caused by: the value was narrowed by a runtime check" in out
    assert "closed as not a defect" in out


def test_json_tickets_carry_kind_and_reasons(fmt):
    tickets = json.loads(fmt._render_json(RECORDS))
    lead = next(t for t in tickets if t["smell"] == "type_assertion")
    assert lead["kind"] == "candidate"
    assert lead["also_caused_by"] == ["the value was narrowed by a runtime check"]
    assert "kind:candidate" in lead["labels"]
    assert "kind" not in next(t for t in tickets if t["smell"] == "as_any")
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/typescript_code_doctor/test_runner.py tests/typescript_code_doctor/test_format_findings.py -q`
Expected: `KeyError: 'total_candidates'`, `KeyError: 'records_rejected'`, and assertion failures on `❓ candidate` / `[Investigate]`.

- [ ] **Step 3: Update `generate_report`**

In `skills/typescript-code-doctor/scripts/analyze_all.py`, add `from common import Finding, SchemaError` to the imports (keep the existing `SEVERITY_ICONS` import). In `generate_report`, add `"total_candidates": 0,` after `"total_issues": 0,` in the `summary` dict, and replace the normalization loop (the `for category, data in results.items():` block) with:

```python
    rejected: dict[str, list[str]] = {}
    for category, data in results.items():
        issues = []
        if isinstance(data, list):
            issues = data
        elif isinstance(data, dict):
            issues = data.get("issues", [])
            if data.get("error"):
                report["meta"]["analyzer_errors"][category] = str(data["error"])

        normalized = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            # Re-validate on the hop. One malformed record from a buggy detector
            # is dropped and named here, instead of reaching a grader as a
            # finding it never proved.
            try:
                Finding(**{k: v for k, v in issue.items() if k != "category"})
            except (TypeError, ValueError) as exc:
                rejected.setdefault(category, []).append(str(exc))
                continue
            issue.setdefault("severity", "medium")
            issue.setdefault("kind", "finding")
            issue["category"] = category
            normalized.append(issue)

        report["categories"][category] = {"issues": normalized, "count": len(normalized)}
        report["summary"]["total_issues"] += len(normalized)
        report["summary"]["total_candidates"] += sum(
            1 for issue in normalized if issue.get("kind") == "candidate")
        report["summary"]["by_category"][category] = len(normalized)
        for issue in normalized:
            severity = issue.get("severity", "medium")
            if severity in report["summary"]["by_severity"]:
                report["summary"]["by_severity"][severity] += 1

    if rejected:
        report["meta"]["records_rejected"] = {
            category: f"{len(errors)} record(s) did not satisfy the findings schema "
                      f"and were dropped: {errors[0]}"
            for category, errors in sorted(rejected.items())
        }

    return report
```

`SchemaError` is a `ValueError`, so the `except` clause catches it; keep the import so the relationship is visible to a reader, or drop it if ruff reports it unused.

- [ ] **Step 4: Update `print_text_report`**

In the same file, after the `print(f"Total issues found: {summary['total_issues']}")` line add:

```python
    if summary.get("total_candidates"):
        print(f"Candidates: {summary['total_candidates']} (unverified leads, not counted as defects)")
```

Replace the block from `print("🔴 HIGH SEVERITY ISSUES")` through the `print()` that follows the `... more high severity issues` line with:

```python
    print("=" * 70)
    print("🔴 HIGH SEVERITY ISSUES")
    print("=" * 70)
    # A candidate is kept out of this list on purpose. Listing a lead among the
    # high-severity defects is how a reader acts on one without confirming it.
    high, candidates = [], []
    for data in report["categories"].values():
        for issue in data["issues"]:
            if issue.get("kind") == "candidate":
                candidates.append(issue)
            elif issue.get("severity") == "high":
                high.append(issue)

    def _describe(issue):
        print(f"\n📍 {issue.get('file', '?')}:{issue.get('line', '?')}")
        print(f"   [{issue['category']}] {issue.get('smell_type', '?')}")
        if issue.get("description"):
            print(f"   {issue['description']}")
        if issue.get("suggestion"):
            print(f"   → {issue['suggestion']}")
        for reason in issue.get("also_caused_by") or []:
            print(f"   ? also caused by: {reason}")

    if not high:
        print("None found!")
    else:
        for issue in high[:25]:
            _describe(issue)
        if len(high) > 25:
            print(f"\n... and {len(high) - 25} more high severity issues")
    print()

    if candidates:
        print("=" * 70)
        print("❓ CANDIDATES — leads to confirm, not defects")
        print("=" * 70)
        print("Each names something the syntax alone cannot prove. Rule out the")
        print("benign explanation before changing anything; graders exclude these.")
        for issue in candidates[:20]:
            _describe(issue)
        if len(candidates) > 20:
            print(f"\n... and {len(candidates) - 20} more candidates")
        print()
```

- [ ] **Step 5: Update `format_findings.py`**

In `skills/typescript-code-doctor/scripts/format_findings.py`, after `_size_for` add:

```python
# A record carrying `kind: "candidate"` is an unverified lead, and every renderer
# here would otherwise turn it into a refactoring ticket with a proposed fix
# attached. That is how someone deletes live code on a tool's say-so.
_CANDIDATE_NOTE = ("Unverified lead, not a confirmed defect — confirm it before acting. "
                   "Graders exclude candidates from the score.")


def _is_candidate(issue):
    return issue.get("kind") == "candidate"
```

Replace `_render_list` with:

```python
def _render_list(issues):
    confirmed = sum(1 for i in issues if not _is_candidate(i))
    leads = len(issues) - confirmed
    heading = f"# Findings — {confirmed} finding(s)"
    if leads:
        heading += f", {leads} candidate(s)"
    lines = [heading, "",
             "| Severity | Type | Location | Description |",
             "|---|---|---|---|"]
    for issue in issues:
        severity = issue.get("severity", "medium")
        location = f"{Path(str(issue.get('file', '?'))).name}:{issue.get('line', '?')}"
        description = (issue.get("description", "") or "").replace("|", "\\|")
        if len(description) > 100:
            description = description[:97] + "..."
        rank = "❓ candidate" if _is_candidate(issue) else f"{_ICON.get(severity, '')} {severity}"
        lines.append(f"| {rank} | {_type_of(issue)} | `{location}` | {description} |")
    if leads:
        lines += ["", f"❓ **candidate** — {_CANDIDATE_NOTE}"]
    return "\n".join(lines)
```

Replace `_render_cards` with:

```python
def _render_cards(issues):
    out = [f"# Findings — {len(issues)} card(s)", ""]
    for issue in issues:
        severity = issue.get("severity", "medium")
        smell = _type_of(issue)
        category = issue.get("category", "")
        lead = _is_candidate(issue)
        labels = ["lang:typescript", f"smell:{smell}", f"size:{_size_for(severity)}",
                  "priority:candidate" if lead else f"priority:{severity}"]
        if category:
            labels.append(f"area:{category}")
        out.append(f"### [{'Investigate' if lead else 'Refactor'}] {smell} — "
                   f"{Path(str(issue.get('file', '?'))).name}:{issue.get('line', '?')}")
        out.append("")
        out.append(f"**Labels:** {'  '.join(labels)}")
        out.append("")
        out.append(f"**Location:** `{issue.get('file', '?')}:{issue.get('line', '?')}`")
        out.append("")
        out.append(f"**Smell:** {issue.get('description', '')}")
        if issue.get("related_lines"):
            out.append("")
            out.append(f"**Also at lines:** {', '.join(str(n) for n in issue['related_lines'])}")
        if lead:
            out.append("")
            out.append(f"**Candidate:** {_CANDIDATE_NOTE}")
            for reason in issue.get("also_caused_by") or []:
                out.append(f"- Also caused by: {reason}")
        if _suggestion(issue):
            out.append("")
            out.append(f"**Proposed fix:** {_suggestion(issue)}")
        out.append("")
        out.append("**Standard:** (link the relevant coding-standard or rule)")
        out.append("")
        out.append("**Definition of Done:**")
        if lead:
            out.append("- [ ] Confirmed against the benign explanations above, "
                       "or closed as not a defect")
            out.append("- [ ] If confirmed, refiled as a finding with a fix")
        else:
            out.append("- [ ] Behavior unchanged (existing + new tests green)")
            out.append("- [ ] `tsc --noEmit` and the linter clean")
            out.append("- [ ] No new `any`, assertion, or `@ts-ignore`")
            out.append("- [ ] Enforcement rule added if this closes a smell class")
        out.append("")
    return "\n".join(out)
```

Replace `_render_json` (the function that builds `tickets` and returns `json.dumps(tickets, indent=2)`) with:

```python
def _render_json(issues):
    tickets = []
    for issue in issues:
        lead = _is_candidate(issue)
        ticket = {
            "title": f"[{'Investigate' if lead else 'Refactor'}] {_type_of(issue)} in "
                     f"{Path(str(issue.get('file', '?'))).name}:{issue.get('line', '?')}",
            "severity": issue.get("severity", "medium"),
            "smell": _type_of(issue),
            "location": f"{issue.get('file', '?')}:{issue.get('line', '?')}",
            "description": issue.get("description", ""),
            "proposed_fix": _suggestion(issue),
            "labels": ["lang:typescript", f"smell:{_type_of(issue)}"],
        }
        if lead:
            ticket["kind"] = "candidate"
            ticket["also_caused_by"] = list(issue.get("also_caused_by") or [])
            ticket["labels"].append("kind:candidate")
        tickets.append(ticket)
    return json.dumps(tickets, indent=2)
```

- [ ] **Step 6: Run the tests**

Run: `pytest tests/typescript_code_doctor -q`
Expected: all pass, including `test_one_worker_and_many_report_exactly_the_same` (the new keys are deterministic).

- [ ] **Step 7: Lint and commit**

```bash
ruff check skills/typescript-code-doctor tests/typescript_code_doctor
git add skills/typescript-code-doctor/scripts/analyze_all.py skills/typescript-code-doctor/scripts/format_findings.py tests/typescript_code_doctor/test_runner.py tests/typescript_code_doctor/test_format_findings.py
git commit -m "typescript-code-doctor: count and render candidates apart from findings"
```

---

### Task 3: Rust `Finding` gains `kind`, invariants, and `Reporter.candidate()`

**Files:**
- Modify: `skills/rust-code-doctor/scripts/common.py:150-186` (`SEVERITY_RANK`, `Finding`, `Reporter`) and its `sort_findings` / `print_findings` (same shapes as the TypeScript originals; locate with `grep -n "def sort_findings\|def print_findings" skills/rust-code-doctor/scripts/common.py`)
- Create: `tests/rust_code_doctor/test_common.py`

**Interfaces:**
- Produces the same names as Task 1 in `rust-code-doctor/scripts/common.py`: `SchemaError`, `VALID_KINDS`, frozen `Finding`, `Reporter.add`, `Reporter.candidate`, kind-aware `sort_findings`, `print_findings`.

- [ ] **Step 1: Write the failing tests**

Create `tests/rust_code_doctor/test_common.py` with the same content as `tests/typescript_code_doctor/test_common.py` from Task 1, with these substitutions: the docstring names `rust-code-doctor`; `SCRIPTS_DIR` points at `"skills" / "rust-code-doctor" / "scripts"`; every `"a.ts"` becomes `"a.rs"`. Everything else, including the `_File` helper and all twelve tests, is identical.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/rust_code_doctor/test_common.py -q`
Expected: `AttributeError: module 'common' has no attribute 'SchemaError'` and `TypeError ... 'kind'`.

- [ ] **Step 3: Replace the record and reporter**

In `skills/rust-code-doctor/scripts/common.py`, replace from `SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}` (line 150) through the end of `Reporter.add` (line 186) with the exact block from Task 1 Step 3 — `SEVERITY_RANK`, `SchemaError`, `VALID_KINDS`, `Finding`, `Reporter` — with two substitutions in `Reporter`: the constructor parameter and attribute are `rsfile` (`self.rsfile = rsfile`), and both `add` and `candidate` read `str(self.rsfile.path)` and `self.rsfile.snippet(line)`. The `Finding` class body is byte-identical to the TypeScript one.

Replace `sort_findings` and `print_findings` with the exact functions from Task 1 Step 3. Remove `field` from the `dataclasses` import if it becomes unused.

- [ ] **Step 4: Run the Rust suite**

Run: `pytest tests/rust_code_doctor -q`
Expected: all pass. The six `_finding()` helpers (`find_cargo_issues.py:96`, `find_dead_code.py:44`, `find_duplicates.py:30`, `find_module_issues.py:26`, `find_overengineering.py:36`, `find_untested_modules.py:26`) construct with keywords and `related_lines=related or []`, which is coerced. A `SchemaError ... must carry a suggestion` failure means a detector emitted a blank suggestion; fix the detector text.

- [ ] **Step 5: Lint and commit**

```bash
ruff check skills/rust-code-doctor tests/rust_code_doctor
git add skills/rust-code-doctor/scripts/common.py tests/rust_code_doctor/test_common.py
git commit -m "rust-code-doctor: enforce the finding/candidate contract in Finding"
```

---

### Task 4: Rust `analyze_all.py` and `format_findings.py` keep candidates apart

**Files:**
- Modify: `skills/rust-code-doctor/scripts/analyze_all.py` (`generate_report` at ~`:81-136`, `print_text_report` at ~`:140-198`)
- Modify: `skills/rust-code-doctor/scripts/format_findings.py:33-113`
- Test: `tests/rust_code_doctor/test_runner.py` (append), `tests/rust_code_doctor/test_format_findings.py` (create)

**Interfaces:** identical to Task 2, in the Rust skill: `summary.total_candidates`, `meta.records_rejected`, candidate-aware renderers.

- [ ] **Step 1: Write the failing tests**

Append to `tests/rust_code_doctor/test_runner.py` the two tests from Task 2 Step 1 (`test_candidates_are_counted_separately_and_kept_out_of_the_high_list`, `test_generate_report_counts_candidates_and_rejects_invalid_records`) with `"a.ts"` → `"a.rs"`, `"as_any"` → `"unwrap_in_fallible_fn"`, `"type_assertion"` → `"narrowing_cast"`, and the `ANALYZERS` monkeypatch row `("types", "find_type_issues", "Types", module.FILE)`. Check that `tests/rust_code_doctor/test_runner.py` defines a `project` fixture; if its fixture is named differently, use that name.

Create `tests/rust_code_doctor/test_format_findings.py` from Task 2's `test_format_findings.py` with `SCRIPTS_DIR` pointing at `rust-code-doctor`, `"src/a.ts"` → `"src/a.rs"`, `"as_any"` → `"unwrap_in_fallible_fn"`, `"type_assertion"` → `"narrowing_cast"`.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/rust_code_doctor/test_runner.py tests/rust_code_doctor/test_format_findings.py -q`
Expected: `KeyError: 'total_candidates'` and renderer assertion failures.

- [ ] **Step 3: Apply the same edits as Task 2 Steps 3–5 to the Rust files**

`generate_report`: add the `Finding` import, `"total_candidates": 0`, the re-validating normalization loop, and `meta.records_rejected` — exactly as in Task 2 Step 3.

`print_text_report`: the `Candidates:` line after `Total issues found`, and the HIGH / CANDIDATES block from Task 2 Step 4, unchanged.

`format_findings.py`: `_CANDIDATE_NOTE`, `_is_candidate`, and the three renderers from Task 2 Step 5, with `"lang:rust"` in place of `"lang:typescript"` and the Rust card's Definition of Done for findings:

```python
            out.append("- [ ] Behavior unchanged (existing + new tests green)")
            out.append("- [ ] `cargo check --all-targets` and `cargo clippy` clean")
            out.append("- [ ] `cargo fmt --check` clean")
            out.append("- [ ] No new `unwrap`, `unsafe`, or `#[allow]` introduced")
            out.append("- [ ] Enforcement added if this closes a smell class (a `[lints]` entry, a CI step)")
```

- [ ] **Step 4: Run the Rust suite**

Run: `pytest tests/rust_code_doctor -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
ruff check skills/rust-code-doctor tests/rust_code_doctor
git add skills/rust-code-doctor/scripts/analyze_all.py skills/rust-code-doctor/scripts/format_findings.py tests/rust_code_doctor/test_runner.py tests/rust_code_doctor/test_format_findings.py
git commit -m "rust-code-doctor: count and render candidates apart from findings"
```

---

### Task 5: Python `common.py` gains the record class and a boundary validator

**Files:**
- Modify: `skills/python-code-doctor/scripts/common.py` (append after `sort_findings`, ~line 140)
- Copy: `skills/django-code-doctor/scripts/common.py` (byte-identical)
- Modify: `skills/python-code-doctor/scripts/analyze_all.py:96-135` (the normalization loop)
- Create: `tests/python_code_doctor/test_common.py`
- Modify: `tests/python_code_doctor/test_runner.py` (append)

**Interfaces:**
- Produces (in `python-code-doctor/scripts/common.py`, and therefore Django's copy):
  - `SchemaError`, `VALID_KINDS`, frozen `Finding` — byte-identical to Task 1's class.
  - `validate_record(record: dict) -> dict`: maps the Python detectors' spellings (`issue_type`/`pattern_type` for the smell, `after` for the fix, `confidence` for the severity) onto `Finding`, raises `SchemaError` on violation, and returns a copy of the record with `kind` made explicit.
  - `analyze_all.generate_report` gains `meta.records_rejected` and every emitted issue carries `kind`.

Python detectors keep their own record classes. There is no `Reporter` in the Python skill to port to; each detector's local `add()` closure plays that role, and 25 detectors define their own `CodeSmell`. The contract is enforced where every record passes: the report hop.

- [ ] **Step 1: Write the failing tests**

Create `tests/python_code_doctor/test_common.py`:

```python
"""The record contract in python-code-doctor's common.py.

Python detectors spell their records several ways — `issue_type` for the
smell, `before`/`after` for the fix, `confidence` for the severity — so the
contract is enforced through validate_record at the report hop rather than at
each of 25 constructors. The Finding class itself is the same one every other
doctor ships.
"""

import dataclasses
import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS_DIR, "common")


def test_finding_requires_a_suggestion(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a.py", line=1, smell_type="x", description="d", suggestion="")


def test_candidate_requires_benign_explanations(common):
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a.py", line=1, smell_type="x", description="d",
                       kind="candidate", also_caused_by=[])


def test_candidate_may_not_carry_a_fix(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a.py", line=1, smell_type="x", description="d",
                       kind="candidate", suggestion="delete it", also_caused_by=["benign"])


def test_finding_is_frozen_and_round_trips(common):
    original = common.Finding(file="a.py", line=2, smell_type="y", description="d",
                              kind="candidate", also_caused_by=["benign"], related_lines=[3])
    with pytest.raises(dataclasses.FrozenInstanceError):
        original.suggestion = "x"
    assert common.Finding(**json.loads(json.dumps(dataclasses.asdict(original)))) == original


def test_validate_record_accepts_the_detectors_spellings(common):
    dead_code = {"file": "a.py", "line": 1, "issue_type": "unused_import", "name": "os",
                 "description": "d", "confidence": 95, "suggestion": "Remove it."}
    unpythonic = {"file": "a.py", "line": 1, "pattern_type": "range_len", "description": "d",
                  "before": "for i in range(len(x))", "after": "for item in x", "severity": "low"}
    plain = {"file": "a.py", "line": 1, "smell_type": "x", "description": "d",
             "suggestion": "fix", "severity": "high"}
    for record in (dead_code, unpythonic, plain):
        assert common.validate_record(record)["kind"] == "finding"


def test_validate_record_rejects_a_finding_without_a_fix(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.validate_record({"file": "a.py", "line": 1, "issue_type": "unused_function",
                                "description": "d", "confidence": 60})


def test_validate_record_rejects_a_candidate_without_reasons(common):
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.validate_record({"file": "a.py", "line": 1, "smell_type": "sql_injection",
                                "description": "d", "suggestion": "", "severity": "high",
                                "kind": "candidate"})


def test_validate_record_does_not_mutate_its_argument(common):
    record = {"file": "a.py", "line": 1, "smell_type": "x", "description": "d", "suggestion": "fix"}
    out = common.validate_record(record)
    assert "kind" not in record and out["kind"] == "finding"
```

Append to `tests/python_code_doctor/test_runner.py` (its `SCRIPTS_DIR` already points at the Python scripts; confirm the fixture that builds a sample tree — if it is not named `project`, use its name):

```python
def test_generate_report_rejects_records_that_break_the_contract(tmp_path, load_module, monkeypatch):
    """The report hop is where every Python detector's records pass, so it is
    where the contract is enforced: a candidate with no benign explanation is
    dropped and named, and every surviving record carries an explicit kind."""
    module = load_module(SCRIPTS_DIR, "analyze_all")

    def fake_run_detectors(path, file_specs, tree_specs, jobs=None):
        return {"security": [
            {"file": "a.py", "line": 1, "smell_type": "eval_call", "description": "d",
             "suggestion": "fix", "severity": "high"},
            {"file": "a.py", "line": 2, "smell_type": "sql_injection", "description": "d",
             "suggestion": "", "severity": "high", "kind": "candidate"},
        ]}

    monkeypatch.setattr(module, "run_detectors", fake_run_detectors)
    monkeypatch.setattr(module, "ANALYZERS", [("security", "find_security_issues", "Security", module.FILE)])
    report = module.generate_report(str(tmp_path))
    issues = report["categories"]["security"]["issues"]
    assert [i["smell_type"] for i in issues] == ["eval_call"]
    assert issues[0]["kind"] == "finding"
    assert "sql_injection" in report["meta"]["records_rejected"]["security"]
```

Check the names `run_detectors`, `ANALYZERS`, and `FILE` exist in `skills/python-code-doctor/scripts/analyze_all.py` with `grep -n "^ANALYZERS\|^FILE\|def run_detectors\|^from runner import"`; if the Python skill imports `run_detectors` from `runner`, the monkeypatch target is still `module.run_detectors` because `analyze_all` binds the name at import.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/python_code_doctor/test_common.py tests/python_code_doctor/test_runner.py -q`
Expected: `AttributeError: module 'common' has no attribute 'Finding'` / `validate_record`, and `KeyError: 'records_rejected'`.

- [ ] **Step 3: Add the record class and validator**

Append to `skills/python-code-doctor/scripts/common.py`, after `sort_findings`:

```python
# --------------------------------------------------------------------------- #
# The confidence discipline, as a type
# --------------------------------------------------------------------------- #

class SchemaError(ValueError):
    """A detector tried to emit a record its evidence does not support."""


VALID_KINDS = frozenset({"finding", "candidate"})
```

followed by the exact `@dataclass(frozen=True) class Finding` block from Task 1 Step 3 (add `from dataclasses import dataclass` to the imports if the module does not already import it), and then:

```python
# The keys Python detectors use for the same three ideas. `format_findings.py`
# and `analyze_all.py` already read these alternatives; the validator accepts
# the same spellings so a detector need not be rewritten to be checked.
_TYPE_KEYS = ("smell_type", "issue_type", "pattern_type", "type")


def _severity_of(record: dict) -> str:
    if record.get("severity"):
        return str(record["severity"])
    confidence = record.get("confidence")
    if isinstance(confidence, (int, float)):
        return "high" if confidence >= 90 else ("medium" if confidence >= 70 else "low")
    return "medium"


def validate_record(record: dict) -> dict:
    """Check one detector record against the finding/candidate contract.

    Raises SchemaError when a finding has no fix or a candidate has no benign
    explanation. Returns a copy of the record with `kind` made explicit, so a
    consumer never has to guess what an absent key meant. The argument is not
    mutated.
    """
    smell = next((str(record[key]) for key in _TYPE_KEYS if record.get(key)), "issue")
    suggestion = str(record.get("suggestion") or record.get("after") or "")
    kind = str(record.get("kind") or "finding")
    Finding(
        file=str(record.get("file", "")), line=int(record.get("line") or 1),
        smell_type=smell, description=str(record.get("description", "")),
        suggestion=suggestion, severity=_severity_of(record), kind=kind,
        also_caused_by=tuple(record.get("also_caused_by") or ()),
    )
    return {**record, "kind": kind}
```

Then copy: `cp skills/python-code-doctor/scripts/common.py skills/django-code-doctor/scripts/common.py`.

- [ ] **Step 4: Validate at the report hop**

In `skills/python-code-doctor/scripts/analyze_all.py`, add `from common import SchemaError, validate_record` to the imports. In `generate_report`, replace the inner normalization loop:

```python
        normalized = []
        for issue in issues:
            if isinstance(issue, dict):
                if 'severity' not in issue:
                    if 'confidence' in issue:
                        conf = issue['confidence']
                        issue['severity'] = 'high' if conf >= 90 else ('medium' if conf >= 70 else 'low')
                    else:
                        issue['severity'] = 'medium'
                issue['category'] = category
                normalized.append(issue)
```

with:

```python
        normalized = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            # Every Python detector's records pass through here, so this is
            # where the contract is enforced. A record that breaks it is
            # dropped and named, never counted as a finding it did not prove.
            try:
                issue = validate_record(issue)
            except SchemaError as exc:
                rejected.setdefault(category, []).append(str(exc))
                continue
            if 'severity' not in issue:
                if 'confidence' in issue:
                    conf = issue['confidence']
                    issue['severity'] = 'high' if conf >= 90 else ('medium' if conf >= 70 else 'low')
                else:
                    issue['severity'] = 'medium'
            issue['category'] = category
            normalized.append(issue)
```

Add `rejected: dict[str, list[str]] = {}` before the `for category, data in results.items():` loop, and before `return report` add:

```python
    if rejected:
        report['meta']['records_rejected'] = {
            category: f"{len(errors)} record(s) did not satisfy the findings schema "
                      f"and were dropped: {errors[0]}"
            for category, errors in sorted(rejected.items())
        }
```

- [ ] **Step 5: Run the new tests, then the whole Python and Django suites**

Run: `pytest tests/python_code_doctor/test_common.py tests/python_code_doctor/test_runner.py -q`
Expected: pass.

Run: `pytest tests/python_code_doctor tests/django_code_doctor -q`
Expected: failures in tests that drive `analyze_all.py` end to end over fixtures containing dead-code or PRAGMA candidates, because those records do not yet satisfy the contract (they are fixed in Task 6). Record which tests fail; they must pass after Task 6. If a failure names a category other than `dead_code`, `security`, or `duplication`, that detector emits a finding with no fix: add a suggestion to its record and re-run.

- [ ] **Step 6: Lint and commit**

```bash
ruff check skills/python-code-doctor skills/django-code-doctor tests/python_code_doctor
cmp skills/python-code-doctor/scripts/common.py skills/django-code-doctor/scripts/common.py
git add skills/python-code-doctor/scripts/common.py skills/django-code-doctor/scripts/common.py skills/python-code-doctor/scripts/analyze_all.py tests/python_code_doctor/test_common.py tests/python_code_doctor/test_runner.py
git commit -m "python-code-doctor: validate every record against the contract at the report hop"
```

---

### Task 6: Python and Django candidate emitters conform

**Files:**
- Modify: `skills/python-code-doctor/scripts/find_security_issues.py:32-46, 181-224, 420-430`
- Modify: `skills/python-code-doctor/scripts/find_dead_code.py:41-53, 380-452, 540-556`
- Modify: `skills/python-code-doctor/scripts/find_duplicates.py:262-292`
- Modify: `skills/django-code-doctor/scripts/django_report.py:31-46`, `skills/django-code-doctor/scripts/find_template_issues.py:131-142`
- Modify: `tests/python_code_doctor/test_detectors.py:605-639`
- Test: `tests/python_code_doctor/test_detectors.py` (append), `tests/django_code_doctor/test_template_and_overengineering.py`

**Interfaces:**
- `find_security_issues.CodeSmell` gains `also_caused_by: tuple[str, ...] = ()`; `add(..., kind=None, also_caused_by=())`.
- `find_dead_code.DeadCodeIssue` gains `suggestion: str = ""` and `also_caused_by: tuple[str, ...] = ()`; `to_record` fills a per-type default suggestion for findings and always emits `kind`.
- `find_duplicates.to_findings` emits `kind: "finding"` for exact matches and, for inexact ones, `kind: "candidate"` with `also_caused_by` and no suggestion.
- `django_report.finding(...)` validates through `Finding`; `django_report.candidate(file, line, smell_type, description, also_caused_by, severity)` no longer takes a suggestion.

- [ ] **Step 1: Write the failing tests**

In `tests/python_code_doctor/test_detectors.py`, change the last assertion of `test_a_dynamic_query_is_still_a_scored_finding` (line 639) from `assert "kind" not in findings[0], ...` to:

```python
    assert findings[0]["kind"] == "finding"
    assert not findings[0].get("also_caused_by")
```

and extend `test_a_dynamic_pragma_is_a_candidate_not_an_injection_finding` (after line 621) with:

```python
    assert findings[0]["also_caused_by"], "a candidate names how healthy code produces it"
    assert not findings[0]["suggestion"], "a candidate carries what to confirm, not a fix"
```

Append:

```python
def test_dead_code_candidates_carry_reasons_and_findings_carry_fixes(tmp_path):
    (tmp_path / "sample.py").write_text(
        "import os\n\n"
        "def helper():\n    return 1\n"
    )
    records = run_detector("find_dead_code.py", tmp_path)
    by_type = {r["issue_type"]: r for r in records}
    assert by_type["unused_import"]["kind"] == "finding"
    assert by_type["unused_import"]["suggestion"]
    assert by_type["unused_function"]["kind"] == "candidate"
    assert by_type["unused_function"]["also_caused_by"]
    assert not by_type["unused_function"].get("suggestion")


def test_inexact_duplicates_are_candidates_with_reasons(tmp_path):
    body = "def {name}(items):\n    out = []\n    for item in items:\n        if item.kind == '{tag}':\n            out.append(item.value * 2)\n        else:\n            out.append(item.value)\n    return out\n"
    (tmp_path / "a.py").write_text(body.format(name="first", tag="alpha"))
    (tmp_path / "b.py").write_text(body.format(name="second", tag="beta"))
    records = [r for r in run_detector("find_duplicates.py", tmp_path)
               if r["smell_type"] == "duplicate_code"]
    assert records, "two blocks with one shape and different literals"
    assert all(r["kind"] == "candidate" for r in records)
    assert all(r["also_caused_by"] and not r["suggestion"] for r in records)
```

If the duplicate fixture is below the detector's minimum block size (`DEFAULT_MIN_LINES` in `find_duplicates.py`), lengthen the body with more `elif` branches until the detector fires, keeping one literal different between the two files.

In `tests/django_code_doctor/test_template_and_overengineering.py`, find the test that asserts on `relation_walk_in_loop` records (the helper at line 7 filters them) and add to it:

```python
    assert all(r["kind"] == "candidate" and r["also_caused_by"] and not r["suggestion"]
               for r in walks)
```

where `walks` is the filtered list that test already builds (adapt the variable name).

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/python_code_doctor/test_detectors.py -k "pragma or scored_finding or dead_code_candidates or inexact_duplicates" tests/django_code_doctor/test_template_and_overengineering.py -q`
Expected: failures on `kind`, `also_caused_by`, and `suggestion` assertions.

- [ ] **Step 3: `find_security_issues.py`**

Change the `CodeSmell` dataclass (lines 32–46) so `kind` defaults to `"finding"` and add a reasons field:

```python
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()
```

Change the `add` closure (line 183) to:

```python
    def add(line, st, desc, sug, sev, kind="finding", also_caused_by=()):
        if st in ignore:
            return
        issues.append(CodeSmell(filename, line, st, desc, sug, sev, _get_line(lines, line),
                                kind, tuple(also_caused_by)))
```

Change the PRAGMA emission (lines 211–219) to:

```python
                    add(node.lineno, "sql_injection",
                        f"Call to .{func.attr}() interpolates a value into a PRAGMA statement",
                        "", "high", kind="candidate",
                        also_caused_by=(
                            "PRAGMA accepts no bound parameters, so a dynamic identifier can "
                            "only be interpolated — this is the only spelling",
                            "the interpolated value is validated against a known set of table "
                            "names before it reaches the statement",
                        ))
```

In the serialization helper (lines 420–430), replace the `if record.get("kind") is None: record.pop("kind", None)` lines with `record["also_caused_by"] = list(record["also_caused_by"])` so `kind` is always emitted and the tuple serializes as a list. Update the comment at lines 422 and 446 to say `kind` is always present.

- [ ] **Step 4: `find_dead_code.py`**

Extend `DeadCodeIssue` (lines 41–53):

```python
    kind: str = "finding"
    suggestion: str = ""
    also_caused_by: tuple[str, ...] = ()
```

Add after the class:

```python
# What to do about a proven dead-code finding, by issue type. A finding must
# carry a fix; these are the fixes the description already implies.
_SUGGESTIONS = {
    "unused_import": "Remove the import, or re-export it through __all__ if it is public API.",
    "unused_variable": "Remove the assignment, or use the value.",
    "unused_parameter": "Remove the parameter, or prefix it with an underscore if the "
                        "signature is dictated by a caller.",
    "unreachable_code": "Delete the code after the return/raise, or fix the control flow "
                        "so it can run.",
}
_DEFAULT_SUGGESTION = "Remove it, or reference it where it is meant to be used."

_CANDIDATE_REASONS = {
    "unused_function": ("another module imports and calls it — cross-module use is "
                        "invisible to a single-file scan",
                        "it is looked up by name: a plugin registry, getattr, or a template"),
    "unused_class": ("another module imports and instantiates it",
                     "it is registered by name: an ORM model, a plugin, a serializer"),
    "unused_parameter": ("the signature is dictated by the receiver — a callback, a "
                         "registry entry, a dispatch-table row",),
}
```

Run `grep -n 'issue_type="' skills/python-code-doctor/scripts/find_dead_code.py` and add an entry to `_SUGGESTIONS` for any issue type not listed above; the default covers the rest.

In `finalize()` (line 395), replace `issue.kind = "candidate"` with a rebuilt record, because the class stays a plain mutable dataclass here but the reasons must be attached at the same time:

```python
            if issue.kind == "finding" and node.name in self.value_references:
                issue.kind = "candidate"
                issue.also_caused_by = _CANDIDATE_REASONS["unused_parameter"]
```

At the two `kind="candidate"` constructions for `unused_function` and `unused_class` (lines ~430–452), add `also_caused_by=_CANDIDATE_REASONS["unused_function"]` and `also_caused_by=_CANDIDATE_REASONS["unused_class"]` respectively.

Replace `to_record` (lines 540–556) with:

```python
def to_record(issue: "DeadCodeIssue") -> dict:
    """The JSON shape this detector emits. Shared with the runner so a pooled
    run and a `find_dead_code.py <path>` run produce the same records.

    A finding carries a fix and a candidate carries its benign explanations;
    `kind` is always present. Severity is derived from confidence, which is
    what this detector ranks by.
    """
    record = asdict(issue)
    record['also_caused_by'] = list(issue.also_caused_by)
    if issue.kind == "finding" and not issue.suggestion:
        record['suggestion'] = _SUGGESTIONS.get(issue.issue_type, _DEFAULT_SUGGESTION)
    if issue.kind == "candidate":
        record['suggestion'] = ""
    record['severity'] = (
        'high' if issue.confidence >= 90
        else ('medium' if issue.confidence >= 70 else 'low')
    )
    return record
```

- [ ] **Step 5: `find_duplicates.py`**

In `to_findings` (lines 262–292), the inexact branch currently sets `suggestion = ('Read them side by side before extracting: ...')`. Change that branch to `suggestion = ''` and, in the record dict, replace:

```python
        if not dup.exact:
            finding['kind'] = 'candidate'
```

with:

```python
        finding['kind'] = 'finding' if dup.exact else 'candidate'
        if not dup.exact:
            finding['also_caused_by'] = [
                'a literal that differs is the whole meaning — a tag, a message, a mode — '
                'and one function with a flag parameter would be worse than the copy',
                'the blocks implement one protocol for two backends and are expected to '
                'diverge',
            ]
```

- [ ] **Step 6: Django factories**

Replace `skills/django-code-doctor/scripts/django_report.py:31-46` with:

```python
from dataclasses import asdict

from common import Finding


def finding(file, line, smell_type, description, suggestion, severity):
    record = Finding(file=str(file), line=line or 1, smell_type=smell_type,
                     description=description, suggestion=suggestion, severity=severity)
    return _emit(record)


def candidate(file, line, smell_type, description, also_caused_by, severity):
    record = Finding(file=str(file), line=line or 1, smell_type=smell_type,
                     description=description, also_caused_by=tuple(also_caused_by),
                     severity=severity, kind="candidate")
    return _emit(record)


def _emit(record):
    """The dict shape every django detector has always returned, with the
    tuple fields serialised as lists and `kind` always present."""
    out = asdict(record)
    out["also_caused_by"] = list(record.also_caused_by)
    out["related_lines"] = list(record.related_lines)
    return out
```

Place the imports at the top of the module with the others. In `skills/django-code-doctor/scripts/find_template_issues.py:131-142`, remove the `suggestion` argument (the `"select_related/prefetch_related '...' on the queryset the view passes in."` string) and pass `also_caused_by` positionally before `"high"`:

```python
                findings.append(candidate(
                    path, number, "relation_walk_in_loop",
                    "`{{ " + expression + " }}` walks " + str(len(relation_parts))
                    + " attributes inside "
                    "the loop opened on line " + str(loop_line) + " — one query per row unless it "
                    "is prefetched",
                    (
                        "the relation is already loaded by select_related() or prefetch_related()",
                        "the queryset the view passes in is small enough that N+1 is not measurable",
                    ),
                    "high"))
```

- [ ] **Step 7: Run both suites**

Run: `pytest tests/python_code_doctor tests/django_code_doctor -q`
Expected: all pass, including the tests recorded as failing in Task 5 Step 5. If `tests/django_code_doctor/test_analyze_django.py:166` (`"1 candidate(s)" in output`) still passes, the count path is intact. If a Django test asserted the removed suggestion text, change it to assert on `also_caused_by`.

- [ ] **Step 8: Run the ratchet locally**

```bash
for s in find_mutation_hazards find_exception_issues find_global_state find_resource_leaks \
         find_security_issues find_debug_leftovers find_duplicate_definitions \
         find_unawaited_coroutines find_local_imports find_import_cycles find_dependency_issues; do
  for target in skills/*/scripts; do
    n=$(python skills/python-code-doctor/scripts/$s.py "$target" --format json \
        | python -c "import sys,json;print(sum(1 for r in json.load(sys.stdin) if r.get('kind') != 'candidate'))")
    [ "$n" = "0" ] || echo "$s reports $n on $target"
  done
done
```

Expected: no output.

- [ ] **Step 9: Lint and commit**

```bash
ruff check skills/python-code-doctor skills/django-code-doctor tests/python_code_doctor tests/django_code_doctor
git add skills/python-code-doctor/scripts/find_security_issues.py skills/python-code-doctor/scripts/find_dead_code.py skills/python-code-doctor/scripts/find_duplicates.py skills/django-code-doctor/scripts/django_report.py skills/django-code-doctor/scripts/find_template_issues.py tests/python_code_doctor/test_detectors.py tests/django_code_doctor/test_template_and_overengineering.py
git commit -m "python and django doctors: candidates carry reasons, findings carry fixes"
```

---

### Task 7: Cross-doctor conformance suite

**Files:**
- Create: `tests/test_record_contract.py`

**Interfaces:**
- Consumes: `SchemaError`, `VALID_KINDS`, `Finding` from every `skills/*code-doctor/scripts/common.py`.
- Produces: a parametrized suite discovered from the tree, so a new `*-code-doctor` is tested the moment it exists.

- [ ] **Step 1: Write the suite**

Create `tests/test_record_contract.py`:

```python
"""Every doctor enforces the same finding/candidate contract, at construction.

The parametrization is discovered from the tree, not listed: a new
`*-code-doctor` directory is held to the contract the moment it has a
`scripts/common.py`, and a doctor that drifts fails here rather than in a
consumer that graded a lead as a defect.
"""

import dataclasses
import json
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parent.parent / "skills"
DOCTORS = sorted(p.parent.parent.name for p in SKILLS.glob("*code-doctor/scripts/common.py"))


@pytest.fixture(params=DOCTORS)
def common(request, load_module):
    return load_module(SKILLS / request.param / "scripts", "common")


def test_every_doctor_is_discovered():
    assert {"code-doctor", "python-code-doctor", "typescript-code-doctor",
            "rust-code-doctor", "django-code-doctor"} <= set(DOCTORS)


def test_kinds_are_exactly_finding_and_candidate(common):
    assert common.VALID_KINDS == frozenset({"finding", "candidate"})
    assert issubclass(common.SchemaError, ValueError)


def test_valid_finding_and_candidate_construct(common):
    finding = common.Finding(file="a", line=1, smell_type="x", description="d", suggestion="fix")
    candidate = common.Finding(file="a", line=2, smell_type="y", description="d",
                               kind="candidate", also_caused_by=["benign"])
    assert finding.kind == "finding" and candidate.suggestion == ""


def test_finding_without_a_suggestion_is_rejected(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a", line=1, smell_type="x", description="d", suggestion="  ")


def test_finding_with_benign_explanations_is_rejected(common):
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a", line=1, smell_type="x", description="d",
                       suggestion="fix", also_caused_by=["benign"])


def test_candidate_with_a_suggestion_is_rejected(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a", line=1, smell_type="x", description="d",
                       kind="candidate", suggestion="fix", also_caused_by=["benign"])


def test_candidate_without_benign_explanations_is_rejected(common):
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a", line=1, smell_type="x", description="d",
                       kind="candidate", also_caused_by=[" "])


def test_unknown_kind_is_rejected(common):
    with pytest.raises(common.SchemaError, match="kind"):
        common.Finding(file="a", line=1, smell_type="x", description="d",
                       suggestion="fix", kind="maybe")


def test_record_is_frozen_with_tuple_collections(common):
    record = common.Finding(file="a", line=1, smell_type="x", description="d",
                            kind="candidate", also_caused_by=["benign"], related_lines=[2])
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.kind = "finding"
    assert isinstance(record.also_caused_by, tuple)
    assert isinstance(record.related_lines, tuple)


def test_serialized_shape_carries_kind_and_round_trips(common):
    record = common.Finding(file="a", line=1, smell_type="x", description="d",
                            kind="candidate", also_caused_by=["benign"], related_lines=[2])
    payload = json.loads(json.dumps(dataclasses.asdict(record)))
    assert payload["kind"] == "candidate"
    assert common.Finding(**payload) == record
```

- [ ] **Step 2: Run it**

Run: `pytest tests/test_record_contract.py -q`
Expected: all pass for all five doctors.

- [ ] **Step 3: Mutation check**

For each of `skills/code-doctor`, `skills/typescript-code-doctor`, `skills/rust-code-doctor`, `skills/python-code-doctor`, delete the four-line `if not self.suggestion.strip(): raise SchemaError(...)` block from `scripts/common.py`, run `pytest tests/test_record_contract.py -q`, and confirm `test_finding_without_a_suggestion_is_rejected` fails for that doctor. Restore with `git checkout -- skills/<doctor>/scripts/common.py`. Do the same for the `if not self.also_caused_by` block and `test_candidate_without_benign_explanations_is_rejected`. Record the two commands in the commit message body.

- [ ] **Step 4: Commit**

```bash
ruff check tests/test_record_contract.py
git add tests/test_record_contract.py
git commit -m "Hold every doctor to one record contract, discovered from the tree"
```

---

### Task 8: Reclassify nine TypeScript heuristics as candidates

**Files:**
- Modify: `skills/typescript-code-doctor/scripts/find_type_gaps.py:92-100, 191-194, 220-226`
- Modify: `skills/typescript-code-doctor/scripts/find_async_issues.py:187-192`
- Modify: `skills/typescript-code-doctor/scripts/find_design_smells.py:104-108, 120-126`
- Modify: `skills/typescript-code-doctor/scripts/find_encapsulation_issues.py:48-53`
- Modify: `skills/typescript-code-doctor/scripts/find_module_issues.py:64-68, 106-112`
- Modify: `skills/typescript-code-doctor/scripts/find_overengineering.py:28-32, 57-72`
- Modify: `skills/typescript-code-doctor/references/critical-review-guide.md:74`, `references/refactoring-catalog.md:18,24,74,117`, `references/type-system.md:215,218` (one sentence each noting the smell is reported as a candidate)
- Test: `tests/typescript_code_doctor/test_detectors.py` (append)

**Interfaces:**
- Consumes: `Reporter.candidate(...)` from Task 1.
- Produces: the nine smell types `type_assertion`, `missing_return_type`, `all_optional_type`, `await_in_loop`, `single_implementation_interface`, `barrel_file`, `data_clump`, `primitive_obsession`, `public_mutable_field` emit `kind: "candidate"` with non-empty `also_caused_by`. Tree detectors gain a local `lead(path, line, smell, description, also_caused_by, severity)` closure beside `add`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/typescript_code_doctor/test_detectors.py`:

```python
# --------------------------------------------------------------------------- #
# Candidates: leads the syntax cannot prove, with the benign readings attached
# --------------------------------------------------------------------------- #

def _candidates(records, smell):
    return [r for r in records if r["smell_type"] == smell]


@pytest.mark.parametrize("script, source, smell", [
    ("find_type_gaps.py",
     "export function port(raw: string): number { return (JSON.parse(raw) as { port: number }).port; }\n",
     "type_assertion"),
    ("find_type_gaps.py",
     "export function total(xs: number[]) { return xs.reduce((a, b) => a + b, 0); }\n",
     "missing_return_type"),
    ("find_type_gaps.py",
     "export interface Loose { a?: string; b?: number; c?: boolean; d?: Date }\n",
     "all_optional_type"),
    ("find_async_issues.py",
     "export async function run(ids: string[]): Promise<void> {\n  for (const id of ids) { await fetch(id); }\n}\n",
     "await_in_loop"),
    ("find_encapsulation_issues.py",
     "export class Counter { count = 0; }\n",
     "public_mutable_field"),
])
def test_file_level_heuristics_are_candidates(tmp_path, script, source, smell):
    root = write(tmp_path / smell, {"sample.ts": source})
    found = _candidates(run_detector(script, root), smell)
    assert found, f"{script} did not report {smell}"
    for record in found:
        assert record["kind"] == "candidate"
        assert record["also_caused_by"], "a candidate names how healthy code produces it"
        assert not record["suggestion"], "a candidate carries no fix"


def test_design_smells_are_candidates(tmp_path):
    root = write(tmp_path, {"sample.ts": BAD_DESIGN})
    records = run_detector("find_design_smells.py", root)
    for smell in ("data_clump", "primitive_obsession"):
        found = _candidates(records, smell)
        assert found and all(r["kind"] == "candidate" and r["also_caused_by"]
                             and not r["suggestion"] for r in found), smell


def test_tree_level_heuristics_are_candidates(tmp_path):
    root = write(tmp_path, {
        "package.json": '{"name": "p"}',
        "src/index.ts": "export * from './a';\nexport * from './b';\nexport { c } from './c';\n",
        "src/a.ts": "export const a = 1;\n",
        "src/b.ts": "export const b = 2;\n",
        "src/c.ts": "export const c = 3;\n",
        "src/svc.ts": "export interface Service { run(): void }\nexport class Impl implements Service { run(): void {} }\n",
    })
    barrels = _candidates(run_detector("find_module_issues.py", root), "barrel_file")
    singles = _candidates(run_detector("find_overengineering.py", root), "single_implementation_interface")
    for found in (barrels, singles):
        assert found
        assert all(r["kind"] == "candidate" and r["also_caused_by"] and not r["suggestion"]
                   for r in found)
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/typescript_code_doctor/test_detectors.py -k "candidates" -q`
Expected: `KeyError: 'also_caused_by'` or `assert record["kind"] == "candidate"` failures for every case. If a case reports nothing at all (`did not report`), adjust the fixture until the smell fires on current code before continuing — the fixture must exercise the detector.

- [ ] **Step 3: File-level detectors**

`find_type_gaps.py:97-100` (`_check_assertions`), replace the `report.add(...)` for `type_assertion` with:

```python
        report.candidate(token.line, "type_assertion",
                         f"Type assertion `as {nxt.value}` — asserted, not checked",
                         ("the value was just narrowed by a runtime check the compiler cannot follow",
                          "the assertion sits at a serialization boundary — JSON.parse, a DOM "
                          "lookup — where the shape is known by contract",
                          "the file is a test installing a double"),
                         "low")
```

`find_type_gaps.py:191-194` (`_check_signatures`), replace the `missing_return_type` emission with:

```python
            report.candidate(func.line, "missing_return_type",
                             f"Exported {func.kind} `{func.qualname}` has no declared return type",
                             ("the body returns one literal or one constructor call, so the "
                              "inferred type is stable",
                              "the project relies on inference by convention and reviews "
                              "signature changes in diffs"),
                             "low")
```

`find_type_gaps.py:220-226` (`_check_optional_soup`), replace the `all_optional_type` emission with:

```python
            report.candidate(decl.line, "all_optional_type",
                             f"`{decl.name}` has {len(optional)} of {len(members)} members optional — "
                             "the type permits an empty object",
                             ("it is a partial-update or patch payload where every field is "
                              "legitimately optional",
                              "it is an options bag with a meaningful default for every member",
                              "it mirrors an external schema that is genuinely all-optional"),
                             "medium")
```

`find_async_issues.py:187-192` (`_check_await_in_loop`), replace the `report.add(...)` with:

```python
            report.candidate(token.line, "await_in_loop",
                             f"`await` inside a {token.value} loop — {len(awaits)} sequential round trip(s) per iteration",
                             ("each iteration depends on the previous iteration's result",
                              "the target rate-limits and the loop is deliberately serial",
                              "side-effect order is part of the contract"),
                             "medium")
```

`find_design_smells.py:104-108` (`_check_data_clumps`), replace the `report.add(...)` with:

```python
        report.candidate(sites[0][1], "data_clump",
                         f"`{', '.join(clump)}` are passed together to {len(sites)} functions ({where})",
                         ("the parameters are a documented positional API that callers depend on",
                          "the functions are overloads or adapters of one external signature",
                          "the group is passed through unchanged to a third-party call"),
                         "medium", related=[line for _, line in sites[1:5]])
```

`find_design_smells.py:120-126` (`_check_primitive_obsession`), replace the `report.add(...)` with:

```python
            report.candidate(func.line, "primitive_obsession",
                             f"`{func.qualname}` takes {longest} adjacent parameters of primitive type",
                             ("the values are validated at the boundary and the function is internal",
                              "the signature mirrors a third-party API the code cannot change",
                              "the parameters are of distinct semantic types the compiler could only "
                              "express with branding the project has chosen not to adopt"),
                             "medium" if longest >= PRIMITIVE_RUN + 1 else "low")
```

`find_encapsulation_issues.py:48-53` (`_check_class_fields`), replace the `report.add(...)` for `public_mutable_field` with:

```python
            report.candidate(prop.line, "public_mutable_field",
                             f"`{klass.name}.{prop.name}` is public and mutable — any caller can set it",
                             ("the class is a plain data holder — a DTO, a config — whose fields "
                              "are meant to be set by callers",
                              "the field is mutated only through a framework: an ORM entity, a form model",
                              "the field is assigned once during builder-style initialization"),
                             "medium")
```

- [ ] **Step 4: Tree-level detectors**

In `find_module_issues.py`, after the `add` closure (lines 64–68) add:

```python
    def lead(path, line, smell, description, also_caused_by, severity):
        if smell not in ignore:
            findings.append(Finding(file=str(path), line=line, smell_type=smell,
                                    description=description, also_caused_by=tuple(also_caused_by),
                                    severity=severity, kind="candidate"))
```

and replace the `add(path, 1, "barrel_file", ...)` call (lines 106–112) with:

```python
                lead(path, 1, "barrel_file",
                     f"{relative_name} is a barrel re-exporting {len(re_exports)} symbol(s)"
                     + (f", {star} of them with `export *`" if star else ""),
                     ("the barrel is the package's declared public entry point — `main` or "
                      "`exports` in package.json",
                      "the bundler tree-shakes and package.json declares `sideEffects: false`",
                      "the folder is a library boundary and the barrel is its only import path by policy"),
                     "medium" if star else "low")
```

In `find_overengineering.py`, after the `add` closure (lines 28–32) add the same `lead` closure, change the call `_check_interfaces(tsfile, path, implementers, add)` to `_check_interfaces(tsfile, path, implementers, lead)`, change the signature at line 57 to `def _check_interfaces(tsfile, path: Path, implementers, lead) -> None:`, and replace the `add(path, decl.line, "single_implementation_interface", ...)` call (lines 67–72) with:

```python
        lead(path, decl.line, "single_implementation_interface",
             f"`{decl.name}` declares behaviour and is implemented only by `{class_name}` "
             f"({where.name})",
             ("a second implementation lives in test code or in a package this scan did not load",
              "the interface is a library's public contract and the class is one vendor of it",
              "the interface exists so a test double can be written without importing the class"),
             "medium")
```

- [ ] **Step 5: Run the whole TypeScript suite**

Run: `pytest tests/typescript_code_doctor -q`
Expected: all pass. `test_detector_fires_on_bad_and_is_quiet_on_good` still passes because `smells()` collapses both kinds and the `GOOD_*` fixtures stay silent. `evals/typescript-code-doctor/evals.json:19` mentions `await_in_loop` in an expected output; read it and, if it describes the record as a defect with a fix, reword it to describe a candidate with benign explanations.

- [ ] **Step 6: Reference notes**

Add one sentence at each of `references/critical-review-guide.md:74`, `references/refactoring-catalog.md:18, 24, 74, 117`, and `references/type-system.md:215, 218`: "Reported as a candidate: the record lists the benign readings to rule out before acting."

- [ ] **Step 7: Lint and commit**

```bash
ruff check skills/typescript-code-doctor tests/typescript_code_doctor
git add skills/typescript-code-doctor tests/typescript_code_doctor/test_detectors.py evals/typescript-code-doctor/evals.json
git commit -m "typescript-code-doctor: report nine contextual heuristics as candidates"
```

- [ ] **Step 8: Phase 1 gate**

Run: `ruff check . && python tools/validate_skills.py && pytest -q`
Expected: clean, all pass.

---

## Phase 2 — TypeScript scanner and config correctness

### Task 9: One pruned walker, and TypeScript-presence checks that see every extension

**Files:**
- Modify: `skills/typescript-code-doctor/scripts/common.py:10-16` (imports), `:63-75` (`find_ts_files`)
- Modify: `skills/typescript-code-doctor/scripts/find_tsconfig_issues.py:123-129` (`_has_typescript`)
- Test: `tests/typescript_code_doctor/test_walk.py` (create), `tests/typescript_code_doctor/test_detectors.py` (append)

**Interfaces:**
- Produces (in `common.py`): `walk_tree(root: Path) -> Iterator[Path]` — every regular file under `root` in sorted order, with `EXCLUDE_DIRS` pruned before descent; `find_ts_files` is `walk_tree` filtered by `TS_EXTENSIONS`.
- `find_tsconfig_issues._has_typescript(root)` is `any(find_ts_files(root))`.

- [ ] **Step 1: Write the failing tests**

Create `tests/typescript_code_doctor/test_walk.py`:

```python
"""The walker prunes excluded directories before descending into them.

`sorted(path.rglob("*"))` materialised every file under node_modules before the
first one was discarded — on a built checkout that is the difference between a
scan and a hang.
"""

import os
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS_DIR, "common")


def _tree(root: Path) -> Path:
    for rel in ("src/a.ts", "src/b.tsx", "src/c.mts", "src/d.cts", "src/e.js",
                "node_modules/pkg/index.ts", "node_modules/pkg/deep/er/x.ts",
                "dist/out.ts", "docs/readme.md"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export {};\n", encoding="utf-8")
    return root


def test_find_ts_files_yields_every_supported_extension_and_nothing_excluded(common, tmp_path):
    found = [p.relative_to(tmp_path).as_posix() for p in common.find_ts_files(_tree(tmp_path))]
    assert found == ["src/a.ts", "src/b.tsx", "src/c.mts", "src/d.cts"]


def test_excluded_directories_are_never_entered(common, tmp_path, monkeypatch):
    _tree(tmp_path)
    entered = []
    real_walk = os.walk

    def spy(top, *args, **kwargs):
        for directory, subdirectories, names in real_walk(top, *args, **kwargs):
            entered.append(Path(directory).name)
            yield directory, subdirectories, names

    monkeypatch.setattr(common.os, "walk", spy)
    list(common.walk_tree(tmp_path))
    assert "node_modules" not in entered and "dist" not in entered
    assert "src" in entered


def test_walk_tree_on_a_single_file_and_a_missing_path(common, tmp_path):
    single = tmp_path / "only.ts"
    single.write_text("export {};\n", encoding="utf-8")
    assert list(common.find_ts_files(single)) == [single]
    assert list(common.find_ts_files(tmp_path / "missing")) == []
```

Append to `tests/typescript_code_doctor/test_detectors.py`:

```python
@pytest.mark.parametrize("name", ["App.tsx", "worker.mts", "legacy.cts"])
def test_no_tsconfig_is_reported_for_any_typescript_extension(tmp_path, name):
    """The `no_tsconfig` gate used to glob `*.ts` only, so a TSX-only repo with
    no tsconfig was reported clean on the check the guide says to answer first."""
    root = write(tmp_path, {f"src/{name}": "export const a = 1;\n"})
    assert "no_tsconfig" in smells(run_detector("find_tsconfig_issues.py", root))
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/typescript_code_doctor/test_walk.py tests/typescript_code_doctor/test_detectors.py -k "no_tsconfig_is_reported or walk" -q`
Expected: `AttributeError: module 'common' has no attribute 'walk_tree'`; the spy test fails with `node_modules` entered; the three `no_tsconfig` cases fail.

- [ ] **Step 3: Implement the walker**

In `skills/typescript-code-doctor/scripts/common.py`, add `import os` to the imports and replace `find_ts_files` (lines 63–75) with:

```python
def walk_tree(path: Path) -> Iterator[Path]:
    """Yield every regular file under ``path``, pruning vendored/built directories.

    The excluded directories are pruned during the walk rather than filtered
    after it. `node_modules` holds tens of thousands of files, and `rglob`
    would traverse and materialize every one of them before the first was
    discarded — which is the difference between a fast scan and one that
    appears to hang on an installed checkout. Every other enumeration in this
    skill goes through here so the pruning rule has one home.
    """
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        return
    for directory, subdirectories, names in os.walk(path):
        subdirectories[:] = sorted(d for d in subdirectories if d not in EXCLUDE_DIRS)
        base = Path(directory)
        for name in sorted(names):
            yield base / name


def find_ts_files(path: Path) -> Iterator[Path]:
    """Yield the TypeScript files under ``path``, skipping vendored/built dirs."""
    for candidate in walk_tree(path):
        if candidate.suffix in TS_EXTENSIONS and candidate.is_file():
            yield candidate
```

- [ ] **Step 4: Fix `_has_typescript`**

In `find_tsconfig_issues.py`, change the import to `from common import EXCLUDE_DIRS, Finding, find_ts_files, run_tree_detector` and replace `_has_typescript` (lines 123–129) with:

```python
def _has_typescript(root: Path) -> bool:
    return any(True for _ in find_ts_files(root))
```

- [ ] **Step 5: Run the suite**

Run: `pytest tests/typescript_code_doctor -q`
Expected: all pass.

- [ ] **Step 6: Lint and commit**

```bash
ruff check skills/typescript-code-doctor tests/typescript_code_doctor
git add skills/typescript-code-doctor/scripts/common.py skills/typescript-code-doctor/scripts/find_tsconfig_issues.py tests/typescript_code_doctor/test_walk.py tests/typescript_code_doctor/test_detectors.py
git commit -m "typescript-code-doctor: prune excluded directories during the walk; detect TSX-only trees"
```

---

### Task 10: No silent caps on config, alias, or manifest discovery

**Files:**
- Modify: `skills/typescript-code-doctor/scripts/find_tsconfig_issues.py:114-120` (`_configs`)
- Modify: `skills/typescript-code-doctor/scripts/tsproject.py:19, 90-95` (`_load_aliases`), `:176-190` (`read_package_json`)
- Modify: `skills/typescript-code-doctor/scripts/find_dependency_issues.py:51-57` (`_manifests`)
- Modify: `skills/typescript-code-doctor/scripts/run_external_tools.py:43, 282-290` (`_coverage_files`)
- Test: `tests/typescript_code_doctor/test_detectors.py` (append), `tests/typescript_code_doctor/test_tsproject.py` (create)

**Interfaces:**
- Consumes: `walk_tree` from Task 9.
- Produces: `_configs`, `_load_aliases`, `_manifests` return every match, shallowest first; `read_package_json` returns the nearest manifest with no candidate cap; `_coverage_files` uses the walker. No function in the skill slices a discovery list.

- [ ] **Step 1: Write the failing tests**

Append to `tests/typescript_code_doctor/test_detectors.py`:

```python
def test_the_eleventh_tsconfig_is_audited(tmp_path):
    """Discovery used to keep the first ten configs and drop the rest silently."""
    files = {"src/a.ts": "export const a = 1;\n",
             "tsconfig.json": json.dumps({"compilerOptions": {"strict": True, "target": "es2022"}})}
    for n in range(10):
        files[f"packages/p{n:02d}/tsconfig.json"] = json.dumps(
            {"compilerOptions": {"strict": True, "target": "es2022"}})
        files[f"packages/p{n:02d}/index.ts"] = "export const x = 1;\n"
    files["packages/zz/tsconfig.json"] = json.dumps({"compilerOptions": {"strict": False}})
    files["packages/zz/index.ts"] = "export const z = 1;\n"
    records = run_detector("find_tsconfig_issues.py", write(tmp_path, files))
    assert any(r["smell_type"] == "strict_mode_off" and r["file"].endswith("zz/tsconfig.json")
               for r in records)


def test_the_sixth_manifest_is_reconciled(tmp_path):
    files = {}
    for n in range(5):
        files[f"packages/p{n}/package.json"] = json.dumps({"name": f"p{n}", "dependencies": {}})
        files[f"packages/p{n}/index.ts"] = "export const x = 1;\n"
    files["packages/zz/package.json"] = json.dumps({"name": "zz", "dependencies": {"left-pad": "*"}})
    files["packages/zz/index.ts"] = "export const z = 1;\n"
    records = run_detector("find_dependency_issues.py", write(tmp_path, files))
    assert any(r["smell_type"] == "unused_dependency" and "left-pad" in r["description"]
               for r in records)
```

Create `tests/typescript_code_doctor/test_tsproject.py`:

```python
"""Whole-tree loading: every tsconfig contributes its path aliases."""

import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"


@pytest.fixture
def tsproject(load_module):
    return load_module(SCRIPTS_DIR, "tsproject")


def test_aliases_from_the_sixth_config_resolve(tsproject, tmp_path):
    """Alias loading used to stop after five configs, so imports through the
    sixth package's `paths` silently became external."""
    for n in range(5):
        pkg = tmp_path / "packages" / f"p{n}"
        pkg.mkdir(parents=True)
        (pkg / "tsconfig.json").write_text(json.dumps({"compilerOptions": {}}))
    deep = tmp_path / "packages" / "zz"
    (deep / "lib").mkdir(parents=True)
    (deep / "tsconfig.json").write_text(json.dumps(
        {"compilerOptions": {"baseUrl": ".", "paths": {"@zz/*": ["lib/*"]}}}))
    (deep / "lib" / "util.ts").write_text("export const u = 1;\n")
    (deep / "main.ts").write_text("import { u } from '@zz/util';\nexport const m = u;\n")
    project = tsproject.load_project(tmp_path)
    resolved = project.resolve((deep / "main.ts").resolve(), "@zz/util")
    assert resolved == (deep / "lib" / "util.ts").resolve()


def test_nearest_package_json_is_found_below_excluded_dirs_only(tsproject, tmp_path):
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "package.json").write_text('{"name": "x"}')
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "package.json").write_text('{"name": "app"}')
    manifest, data = tsproject.read_package_json(tmp_path)
    assert manifest == tmp_path / "app" / "package.json" and data["name"] == "app"
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/typescript_code_doctor/test_tsproject.py tests/typescript_code_doctor/test_detectors.py -k "eleventh or sixth" -q`
Expected: the eleventh-config, sixth-manifest, and sixth-alias tests fail; the nearest-manifest test may pass already (keep it as a regression guard for the rewrite).

- [ ] **Step 3: Rewrite the four discovery sites**

`find_tsconfig_issues.py:114-120`:

```python
def _configs(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.name.startswith("tsconfig") else []
    found = [p for p in walk_tree(root)
             if p.name.startswith("tsconfig") and p.suffix == ".json"]
    found.sort(key=lambda p: (len(p.relative_to(root).parts), str(p)))
    return found
```

with the import changed to `from common import Finding, find_ts_files, run_tree_detector, walk_tree` (`EXCLUDE_DIRS` is no longer used in this module).

`tsproject.py:90-95`, replace the config discovery and loop head with:

```python
    configs = [p for p in walk_tree(root)
               if p.name.startswith("tsconfig") and p.suffix == ".json"] if root.is_dir() else []
    for config in sorted(configs, key=lambda p: (len(p.relative_to(root).parts), str(p))):
```

and change the import at line 19 to `from common import TS_EXTENSIONS, find_ts_files, is_test_file, walk_tree, warn_unparseable` (drop `EXCLUDE_DIRS` if unused after Step 4 below).

`tsproject.py:176-190`, replace `read_package_json` with:

```python
def read_package_json(root: Path) -> tuple[Path | None, dict]:
    """The nearest package.json and its parsed contents."""
    if root.is_file():
        root = root.parent
    candidates = [root / "package.json"] + sorted(
        (p for p in walk_tree(root) if p.name == "package.json"),
        key=lambda p: (len(p.relative_to(root).parts), str(p)))
    for manifest in candidates:
        if not manifest.is_file():
            continue
        try:
            return manifest, json.loads(manifest.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return manifest, {}
    return None, {}
```

(The old line 184 tested `EXCLUDE_DIRS` against the manifest's absolute parts; the walker's pruning replaces it.)

`find_dependency_issues.py:51-57`:

```python
def _manifests(root: Path) -> list[Path]:
    if root.is_file():
        root = root.parent
    found = [p for p in walk_tree(root) if p.name == "package.json"]
    found.sort(key=lambda p: (len(p.relative_to(root).parts), str(p)))
    return found
```

with `walk_tree` added to that module's `from common import ...` line.

`run_external_tools.py:282-290`:

```python
def _coverage_files(root: Path):
    for name in ("coverage/coverage-final.json", "coverage/coverage-summary.json"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    for candidate in walk_tree(root):
        if candidate.name == "coverage-final.json":
            return candidate
    return None
```

Note `EXCLUDE_DIRS` contains `"coverage"`, so the walker never enters a `coverage/` directory; the two explicit paths above are how a conventional layout is found, which is the same behavior as before. Change line 43 to `from common import SEVERITY_ICONS, configure_output, walk_tree`.

- [ ] **Step 4: Confirm no slice remains**

Run: `grep -n "\[:[0-9]\+\]" skills/typescript-code-doctor/scripts/find_tsconfig_issues.py skills/typescript-code-doctor/scripts/tsproject.py skills/typescript-code-doctor/scripts/find_dependency_issues.py`
Expected: no matches on a discovery list. (`_find_cycles(graph)[:20]` in `find_module_issues.py` bounds reported cycles, not discovery; leave it.)

- [ ] **Step 5: Run the suite**

Run: `pytest tests/typescript_code_doctor -q`
Expected: all pass.

- [ ] **Step 6: Lint and commit**

```bash
ruff check skills/typescript-code-doctor tests/typescript_code_doctor
git add skills/typescript-code-doctor/scripts tests/typescript_code_doctor
git commit -m "typescript-code-doctor: audit every tsconfig, alias, and manifest — no silent caps"
```

---

### Task 11: Test classification is relative to the project root

**Files:**
- Modify: `skills/typescript-code-doctor/scripts/common.py:78-83` (`is_test_file`)
- Test: `tests/typescript_code_doctor/test_common.py` (append), `tests/typescript_code_doctor/test_detectors.py` (append)

**Interfaces:**
- Produces (in `common.py`): `project_root_of(filepath: Path) -> Path | None` — the nearest ancestor containing `package.json`, a `tsconfig*.json`, or `.git`; `is_test_file` matches `TEST_DIR_NAMES` only against path components below that root, falling back to all components when no root is found.

- [ ] **Step 1: Write the failing tests**

Append to `tests/typescript_code_doctor/test_common.py`:

```python
def test_a_checkout_under_a_tests_directory_is_not_test_code(common, tmp_path):
    """Every component of the absolute path is the wrong scope: a repo cloned
    to /tmp/tests/repo had all of its sources classified as tests, which
    silenced the security and error-handling findings on exactly those files."""
    repo = tmp_path / "tests" / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text('{"name": "repo"}')
    source = repo / "src" / "app.ts"
    source.write_text("export const a = 1;\n")
    assert common.project_root_of(source) == repo
    assert not common.is_test_file(source)


def test_test_directories_below_the_root_still_classify(common, tmp_path):
    repo = tmp_path / "repo"
    for rel in ("tests/a.ts", "src/__tests__/b.ts", "e2e/c.ts", "src/d.test.ts", "src/e.spec.tsx"):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export {};\n")
    (repo / "tsconfig.json").write_text("{}")
    for rel in ("tests/a.ts", "src/__tests__/b.ts", "e2e/c.ts", "src/d.test.ts", "src/e.spec.tsx"):
        assert common.is_test_file(repo / rel), rel
    assert not common.is_test_file(repo / "src" / "f.ts")


def test_without_a_root_marker_every_component_counts(common, tmp_path):
    loose = tmp_path / "spec" / "loose.ts"
    loose.parent.mkdir(parents=True)
    loose.write_text("export {};\n")
    assert common.project_root_of(loose) is None
    assert common.is_test_file(loose)
```

Append to `tests/typescript_code_doctor/test_detectors.py`:

```python
def test_a_leftover_is_reported_in_a_repo_cloned_under_tests(tmp_path):
    root = write(tmp_path / "tests" / "repo", {
        "package.json": '{"name": "repo"}',
        "src/app.ts": "export function f() { console.log('debug'); return 1; }\n",
    })
    assert "console_leftover" in smells(run_detector("find_debug_leftovers.py", root))
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/typescript_code_doctor/test_common.py tests/typescript_code_doctor/test_detectors.py -k "tests_directory or below_the_root or root_marker or cloned_under_tests" -q`
Expected: `AttributeError: ... 'project_root_of'`; the cloned-under-tests detector test fails with `console_leftover` absent. (If `tmp_path` itself contains no `.git`, the fallback test passes only once `project_root_of` exists.)

- [ ] **Step 3: Implement**

In `skills/typescript-code-doctor/scripts/common.py`, add after `TEST_NAME_MARKERS`:

```python
# Files that mark the top of a project. Test-directory classification is
# scoped below the nearest one, so where a checkout lives cannot change what
# it reports.
ROOT_MARKERS = ("package.json", "tsconfig.json", ".git")
```

and replace `is_test_file` (lines 78–83) with:

```python
def project_root_of(filepath: Path) -> Path | None:
    """The nearest ancestor holding a package.json, a tsconfig, or .git; None if none."""
    for candidate in filepath.resolve().parents:
        if any((candidate / marker).exists() for marker in ROOT_MARKERS):
            return candidate
        if any(candidate.glob("tsconfig*.json")):
            return candidate
    return None


def is_test_file(filepath: Path) -> bool:
    """True when the path names a test file by directory or by suffix.

    Directory markers are matched *below the project root only*. Every
    component of an absolute path is the wrong scope: a checkout that happens
    to live under `/tmp/tests/` would have every one of its files classified
    as test code, silently suppressing the security and error-handling
    findings — so the same repo would report differently depending on where
    it was cloned. Without a root marker every component still counts, which
    is the old behaviour and the right one for a loose file.
    """
    name = filepath.name
    if any(marker in name for marker in TEST_NAME_MARKERS):
        return True
    root = project_root_of(filepath)
    if root is not None:
        try:
            parts = filepath.resolve().relative_to(root).parts
        except ValueError:
            parts = filepath.parts
    else:
        parts = filepath.parts
    return not TEST_DIR_NAMES.isdisjoint(p.lower() for p in parts)
```

`project_root_of` is called per file at 18 sites; the `parents` walk touches a handful of directories per call. If `pytest tests/typescript_code_doctor/test_runner.py` shows a slowdown above a second on the fixture tree, memoize with `functools.lru_cache` keyed on the resolved parent directory.

- [ ] **Step 4: Run the suite**

Run: `pytest tests/typescript_code_doctor -q`
Expected: all pass. `test_test_files_get_a_lighter_standard` (`test_detectors.py:671`) still passes because it classifies by the `.test.` name marker.

- [ ] **Step 5: Lint and commit**

```bash
ruff check skills/typescript-code-doctor tests/typescript_code_doctor
git add skills/typescript-code-doctor/scripts/common.py tests/typescript_code_doctor
git commit -m "typescript-code-doctor: classify test directories below the project root only"
```

---

### Task 12: All three declaration-file suffixes

**Files:**
- Modify: `skills/typescript-code-doctor/scripts/common.py:86-88` (`is_declaration_file`)
- Modify: `skills/typescript-code-doctor/scripts/tsproject.py:24-27` (`_CANDIDATE_SUFFIXES`)
- Test: `tests/typescript_code_doctor/test_common.py` (append), `tests/typescript_code_doctor/test_detectors.py` (append)

**Interfaces:**
- Produces: `DECLARATION_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")` in `common.py`; `is_declaration_file` checks all three; module resolution tries all three.

- [ ] **Step 1: Write the failing tests**

Append to `tests/typescript_code_doctor/test_common.py`:

```python
@pytest.mark.parametrize("name, expected", [
    ("types.d.ts", True), ("types.d.mts", True), ("types.d.cts", True),
    ("types.ts", False), ("d.ts", False), ("mod.mts", False),
])
def test_declaration_files_are_recognized_in_every_module_flavour(common, name, expected):
    assert common.is_declaration_file(Path("src") / name) is expected
```

Append to `tests/typescript_code_doctor/test_detectors.py`:

```python
def test_a_d_mts_declaration_is_not_scanned_as_implementation(tmp_path):
    """`.mts` is a supported source extension, so `foo.d.mts` was collected and
    then fed to every code detector as if it were a module with a body."""
    root = write(tmp_path, {
        "package.json": '{"name": "p"}',
        "src/shapes.d.mts": "export declare function shape(x: any): any;\n",
        "src/main.ts": "export const m = 1;\n",
    })
    records = run_detector("find_type_gaps.py", root)
    assert not any(r["file"].endswith("shapes.d.mts") for r in records)
    dead = run_detector("find_dead_code.py", root)
    assert not any(r["file"].endswith("shapes.d.mts") for r in dead)
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/typescript_code_doctor -k "declaration or d_mts" -q`
Expected: `.d.mts` and `.d.cts` cases fail; the detector test fails with `shapes.d.mts` reported.

- [ ] **Step 3: Implement**

In `common.py`, after `TS_EXTENSIONS`, add `DECLARATION_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")` and replace `is_declaration_file` with:

```python
def is_declaration_file(filepath: Path) -> bool:
    """True for `.d.ts`, `.d.mts`, `.d.cts` — types only, so most code detectors do not apply."""
    return filepath.name.endswith(DECLARATION_SUFFIXES)
```

In `tsproject.py`, replace `_CANDIDATE_SUFFIXES` with:

```python
_CANDIDATE_SUFFIXES = (
    ".ts", ".tsx", ".mts", ".cts", ".d.ts", ".d.mts", ".d.cts",
    "/index.ts", "/index.tsx", "/index.mts", "/index.cts",
)
```

- [ ] **Step 4: Run the suite, lint, commit**

Run: `pytest tests/typescript_code_doctor -q` — expected all pass.

```bash
ruff check skills/typescript-code-doctor
git add skills/typescript-code-doctor/scripts/common.py skills/typescript-code-doctor/scripts/tsproject.py tests/typescript_code_doctor
git commit -m "typescript-code-doctor: treat .d.mts and .d.cts as declaration files"
```

---

### Task 13: The audit runs the package manager it names

**Files:**
- Modify: `skills/typescript-code-doctor/scripts/run_external_tools.py:26-31` (usage), `:226-243` (`_package_manager`, `run_audit`), `:327-337` (`TOOLS`), `:383-394` (resolution loop in `main`)
- Modify: `skills/typescript-code-doctor/SKILL.md` (the external-tools paragraph around lines 183–212: the tool key is `audit`)
- Create: `tests/typescript_code_doctor/test_external_tools.py`

**Interfaces:**
- Produces: `TOOLS["audit"]` replaces `TOOLS["npm"]`; `_audit_argv(root: Path) -> tuple[str, list[str]] | None` returns `(manager, argv)` with the executable resolved for the manager the lockfile names, or `None` when that executable is not installed; `run_audit(inv, root)` executes exactly that argv.

- [ ] **Step 1: Write the failing tests**

Create `tests/typescript_code_doctor/test_external_tools.py`:

```python
"""run_external_tools.py resolves the package manager it reports.

The audit used to resolve `npm` and then label the output with whatever the
lockfile implied: on a yarn project it ran `npm npm audit`, which cannot
succeed, and on a pnpm project it ran npm's audit and reported it as pnpm's.
"""

from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"


@pytest.fixture
def external(load_module):
    return load_module(SCRIPTS_DIR, "run_external_tools")


@pytest.fixture
def which(monkeypatch, external):
    """Every manager is 'installed' at /bin/<name>; the test asserts which one is chosen."""
    monkeypatch.setattr(external.shutil, "which", lambda name: f"/bin/{name}")


@pytest.mark.parametrize("files, manager, tail", [
    ({"package-lock.json": "{}"}, "npm", ["audit", "--json"]),
    ({"pnpm-lock.yaml": ""}, "pnpm", ["audit", "--json"]),
    ({"yarn.lock": ""}, "yarn", ["audit", "--json"]),
    ({"yarn.lock": "", ".yarnrc.yml": "nodeLinker: node-modules\n"}, "yarn", ["npm", "audit", "--json"]),
])
def test_audit_argv_names_and_runs_the_same_manager(external, which, tmp_path, files, manager, tail):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    got_manager, argv = external._audit_argv(tmp_path)
    assert got_manager == manager
    assert argv == [f"/bin/{manager}", *tail]


def test_audit_prefers_the_projects_own_binary(external, tmp_path, monkeypatch):
    (tmp_path / "pnpm-lock.yaml").write_text("")
    local = tmp_path / "node_modules" / ".bin"
    local.mkdir(parents=True)
    (local / "pnpm").write_text("")
    monkeypatch.setattr(external.shutil, "which", lambda name: None)
    manager, argv = external._audit_argv(tmp_path)
    assert manager == "pnpm" and argv[0] == str(local / "pnpm")


def test_a_missing_manager_is_reported_not_substituted(external, tmp_path, monkeypatch):
    (tmp_path / "pnpm-lock.yaml").write_text("")
    monkeypatch.setattr(external.shutil, "which", lambda name: None)
    assert external._audit_argv(tmp_path) is None


def test_run_audit_executes_the_resolved_argv_and_labels_it(external, tmp_path, monkeypatch):
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "package.json").write_text("{}")
    monkeypatch.setattr(external.shutil, "which", lambda name: f"/bin/{name}")
    seen = []

    def fake_run(argv, cwd, timeout=900):
        seen.append(argv)
        return 0, '{"vulnerabilities": {"left-pad": {"severity": "high", "via": [{"title": "t", "url": "u"}]}}}', ""

    monkeypatch.setattr(external, "_run", fake_run)
    findings = external.run_audit(None, tmp_path)
    assert seen == [["/bin/pnpm", "audit", "--json"]]
    assert findings[0]["smell_type"] == "pnpm-audit:high"
    assert "pnpm audit fix" in findings[0]["suggestion"]


def test_the_tool_table_has_an_audit_entry_and_no_npm_entry(external):
    assert "audit" in external.TOOLS and "npm" not in external.TOOLS
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/typescript_code_doctor/test_external_tools.py -q`
Expected: `AttributeError: ... '_audit_argv'` and `"npm" not in external.TOOLS` failing.

- [ ] **Step 3: Implement**

In `run_external_tools.py`, replace `_package_manager` and the head of `run_audit` (lines 226–243) with:

```python
def _package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    return "npm"


def _audit_argv(root: Path) -> tuple[str, list[str]] | None:
    """The manager the lockfile names, and the audit command for *that* manager.

    Resolved independently of the `npm` binary: a pnpm project is audited by
    pnpm, a yarn project by yarn, and when that executable is not installed
    the audit is reported missing rather than run under a different manager
    and labelled as this one.
    """
    manager = _package_manager(root)
    inv = _invocation(root, manager)
    if inv is None:
        return None
    if manager == "yarn" and (root / ".yarnrc.yml").is_file():
        return manager, [*inv, "npm", "audit", "--json"]   # Yarn Berry
    return manager, [*inv, "audit", "--json"]


def run_audit(_inv, root):
    """Known advisories against the installed dependency tree."""
    resolved = _audit_argv(root)
    if resolved is None:
        manager = _package_manager(root)
        return [_tool_error(f"{manager}-audit", root, None, f"{manager} is not installed")]
    manager, argv = resolved
    returncode, out, err = _run(argv, root, timeout=600)
    if returncode is None or not (out or "").strip():
        return [_tool_error(f"{manager}-audit", root, returncode, err or out)]
```

The remainder of `run_audit` (from `manifest = root / "package.json"`) is unchanged.

In `TOOLS`, replace the `"npm": ("npm (ships with node)", run_audit, None),` row with `"audit": ("the package manager named by the lockfile", run_audit, None),`.

In `main`'s resolution loop (lines 384–394), add a branch before the generic `_invocation` call:

```python
        if name == "audit":
            resolved = _audit_argv(root)
            if resolved:
                available[name] = resolved[1]
            else:
                manager = _package_manager(root)
                missing.append({"name": name, "install": f"install {manager} (it is not on PATH "
                                                          f"or in node_modules/.bin)"})
            continue
```

In the module docstring (line 28) change `{tools_run, missing_tools, findings}` example if it lists `npm`, and in `SKILL.md` replace any `--tools ...npm...` spelling with `audit` and state that the audit runs the manager the lockfile names. Run `grep -n '"npm"\|tools npm\|--tools' skills/typescript-code-doctor/SKILL.md skills/typescript-code-doctor/scripts/run_external_tools.py` to find every spelling.

- [ ] **Step 4: Run the suite, lint, commit**

Run: `pytest tests/typescript_code_doctor -q` — expected all pass.

```bash
ruff check skills/typescript-code-doctor tests/typescript_code_doctor
git add skills/typescript-code-doctor/scripts/run_external_tools.py skills/typescript-code-doctor/SKILL.md tests/typescript_code_doctor/test_external_tools.py
git commit -m "typescript-code-doctor: audit with the package manager the lockfile names"
```

---

### Task 14: Skip generated files on strong evidence only

**Files:**
- Modify: `skills/typescript-code-doctor/scripts/common.py` (add `is_generated_file`; use it in `run_file_detector`)
- Modify: `skills/typescript-code-doctor/scripts/tsproject.py:155-173` (`_build_project`)
- Modify: `skills/typescript-code-doctor/scripts/runner.py:186` (the file list)
- Test: `tests/typescript_code_doctor/test_common.py` (append), `tests/typescript_code_doctor/test_detectors.py` (append)

**Interfaces:**
- Produces: `is_generated_file(filepath: Path) -> bool` — True when any of the first five lines contains `@generated` or the exact phrase `DO NOT EDIT`. Skipped files are counted on stderr as `ℹ️  N generated file(s) skipped`. `Project.files` excludes generated files; `Project.generated: list[Path]` lists them.

Path-based exclusion of `outDir`/`declarationDir` is deferred: the file detectors would need tsconfig parsing inside `common.py`, which `find_tsconfig_issues.py` already imports, and a build directory not named in `EXCLUDE_DIRS` is rare enough not to justify the cycle.

- [ ] **Step 1: Write the failing tests**

Append to `tests/typescript_code_doctor/test_common.py`:

```python
@pytest.mark.parametrize("head, expected", [
    ("/* eslint-disable */\n// @generated by protoc-gen-ts\n", True),
    ("// Code generated by openapi-typescript. DO NOT EDIT.\n", True),
    ("// Please do not edit this by hand without asking.\n", False),
    ("export const a = 1;\n" * 6 + "// @generated\n", False),
])
def test_generated_files_are_recognized_by_header_only(common, tmp_path, head, expected):
    path = tmp_path / "gen.ts"
    path.write_text(head + "export const x = 1;\n", encoding="utf-8")
    assert common.is_generated_file(path) is expected
```

Append to `tests/typescript_code_doctor/test_detectors.py`:

```python
def test_generated_files_are_skipped_and_counted(tmp_path):
    root = write(tmp_path, {
        "package.json": '{"name": "p"}',
        "src/api.ts": "// @generated by a tool\nexport function f(x: any): any { return x; }\n",
        "src/main.ts": "export function g(x: any): any { return x; }\n",
    })
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_type_gaps.py"), str(root), "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    records = json.loads(result.stdout)
    assert all(r["file"].endswith("main.ts") for r in records) and records
    assert "1 generated file(s) skipped" in result.stderr
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/typescript_code_doctor -k generated -q`
Expected: `AttributeError: ... 'is_generated_file'`; the detector test fails with `api.ts` reported.

- [ ] **Step 3: Implement**

In `common.py`, after `is_declaration_file`, add:

```python
# Markers that tools write into files they own. `@generated` is the convention
# Prettier, Jest and code-review tools honour; `DO NOT EDIT` is the
# protoc/openapi/go-style banner. Both are checked in the first five lines
# only, and matched exactly: "please do not edit this without asking" in a
# hand-written header must not silence a file's findings.
_GENERATED_MARKERS = ("@generated", "DO NOT EDIT")


def is_generated_file(filepath: Path) -> bool:
    """True when the file's header says a tool owns it."""
    try:
        with open(filepath, encoding="utf-8", errors="replace") as handle:
            head = [next(handle, "") for _ in range(5)]
    except OSError:
        return False
    return any(marker in line for line in head for marker in _GENERATED_MARKERS)
```

In `run_file_detector`, replace the loop with:

```python
    findings: list[Finding] = []
    generated = 0
    for filepath in find_ts_files(Path(args.path)):
        if skip_declaration_files and is_declaration_file(filepath):
            continue
        if is_generated_file(filepath):
            generated += 1
            continue
        try:
            findings.extend(analyze(parse_file(filepath), ignore))
        except TsSyntaxError as exc:
            warn_unparseable(filepath, exc)
        except OSError as exc:
            warn_unparseable(filepath, exc)
        except Exception as exc:  # a detector bug must not read as a clean file
            warn_detector_error(filepath, exc)
    if generated:
        print(f"ℹ️  {generated} generated file(s) skipped (header says a tool owns them)",
              file=sys.stderr)
    emit(findings, args.format, clean_message)
```

In `tsproject.py`, add `generated: list[Path] = field(default_factory=list)` to `Project`, import `is_generated_file` from `common`, and in `_build_project` insert before the `try:` that parses each file:

```python
        if is_generated_file(path):
            project.generated.append(resolved)
            continue
```

In `runner.py:186`, change the file list to `files = [p for p in find_ts_files(root) if not is_declaration_file(p) and not is_generated_file(p)]`, importing `is_generated_file` alongside `is_declaration_file`.

- [ ] **Step 4: Run the suite, lint, commit**

Run: `pytest tests/typescript_code_doctor -q` — expected all pass.

```bash
ruff check skills/typescript-code-doctor tests/typescript_code_doctor
git add skills/typescript-code-doctor/scripts/common.py skills/typescript-code-doctor/scripts/tsproject.py skills/typescript-code-doctor/scripts/runner.py tests/typescript_code_doctor
git commit -m "typescript-code-doctor: skip files whose header says a tool owns them"
```

---

### Task 15: Phase 2 gate and documentation

**Files:**
- Modify: `skills/typescript-code-doctor/SKILL.md` (the "lighter standard inside test files" paragraph around line 176: add that test directories are recognized below the project root; the external-tools paragraph: the audit runs the manager the lockfile names)
- Modify: `README.md` (the typescript-code-doctor bullet, lines 47–56: one clause noting candidates)

- [ ] **Step 1: Documentation**

In `skills/typescript-code-doctor/SKILL.md` after the sentence ending "a cast that installs a mock is not a claim about the product's types." add: "A test file is one under `tests/`, `__tests__/`, `spec/`, `e2e/` or `cypress/` *below the project root* (the nearest `package.json`, tsconfig or `.git`), or one named `*.test.*` / `*.spec.*` — where the checkout itself lives does not change the classification."

In `README.md`'s typescript-code-doctor bullet, after "It reports in the finding/candidate schema above" (or the equivalent sentence; if absent, after the first sentence), add: "Contextual heuristics — an ordinary `as` cast, an `await` in a loop, a barrel file, a data clump — are reported as candidates with their benign readings attached, never as defects."

- [ ] **Step 2: Full gate**

Run:

```bash
ruff check .
python tools/validate_skills.py
python tools/validate_styles.py
cmp skills/python-code-doctor/scripts/common.py skills/django-code-doctor/scripts/common.py
cmp skills/python-code-doctor/scripts/format_findings.py skills/django-code-doctor/scripts/format_findings.py
pytest -q
```

Expected: every command clean; `pytest -q` reports all passed, 1 skipped (the pre-existing skip).

- [ ] **Step 3: Commit**

```bash
git add skills/typescript-code-doctor/SKILL.md README.md
git commit -m "Document root-relative test classification and candidate reporting"
```

---

## Self-review notes

**Spec coverage.** §6.2 items 1–4: Tasks 1–7. §6.3 first reclassifications: Task 8. §7A: Task 11. §7B: Task 12. §7C: Task 9. §7D (three caps, plus the fourth found in `find_dependency_issues._manifests`): Task 10. §7E: Task 9 and Task 10. §7F: Task 13. §7G: Task 14, header markers only, with the `outDir` deferral stated. §16.1 failing-first: every task's Step 2. §16.4 mutation check: Task 7 Step 3. §16.5 output compatibility: Global Constraints and Task 5's `validate_record` returning a copy.

**Deliberate deviations from the spec text.** Python gets no `Reporter` (there is none to port to; 25 local `add()` closures already play that role) and keeps its per-detector record classes; the contract is enforced at the report hop through `validate_record`, and the three candidate-emitting detectors are made to conform at the source. `kind` becomes always-present on Python records, which changes the assertion at `tests/python_code_doctor/test_detectors.py:639` from absent-key to `== "finding"`. Django's `candidate()` loses its `suggestion` parameter because the contract forbids one.

**Repeated code.** Tasks 3 and 4 name the Task 1 and Task 2 blocks with exact substitutions rather than pasting 150 lines twice more; the substitutions are mechanical (`tsfile` → `rsfile`, `.ts` → `.rs`, labels and Definition-of-Done lines) and are listed in full where they apply.

**Type consistency.** `Reporter.candidate(line, smell_type, description, also_caused_by, severity="low", related=None)` in Tasks 1, 3, 8. Tree-detector `lead(path, line, smell, description, also_caused_by, severity)` in Task 8. `walk_tree(path)` in Tasks 9, 10, 14. `project_root_of(filepath)` in Task 11. `_audit_argv(root) -> tuple[str, list[str]] | None` in Task 13. `validate_record(record) -> dict` in Tasks 5, 6. `summary.total_candidates` and `meta.records_rejected` in Tasks 2, 4, 5.

**Out of scope for this plan** (later plans per spec §17): external-tool status/version metadata and policy flags (Phase 3), `assure.py` and `checks[]` (Phase 4), authoritative `tsc`/typed-ESLint (Phase 5), Rust routing and the new-specialist CI invariant (Phase 6), workflow adoption (Phase 7), the full 166-smell audit and parser corpus (Phase 8).
