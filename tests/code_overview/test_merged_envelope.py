"""Grading from code-doctor's merged envelope.

The envelope replaces three things code-overview used to be told: which doctors
ran, what they covered, and which records are defects. Each of those was a place
a wrong answer produced a confident grade, so each gets a test about the failure
rather than the success.
"""

import json
import re
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "code-overview" /
          "scripts" / "build_health.py")


def meta_of(page: Path) -> dict:
    match = re.search(r'id="code-health-meta">(.*?)</script>',
                      page.read_text(encoding="utf-8"), re.S)
    assert match
    return json.loads(match.group(1).replace("<\\/", "</"))


def envelope(tmp_path, **overrides) -> Path:
    payload = {
        "schema": "code-doctor-merge/1",
        "doctors_run": ["code-doctor", "python-code-doctor"],
        # Real detector-category tokens (the ones analyze_all.py's own
        # `meta.analyzers_run` names, e.g. FULL_RUN in test_build_health.py) —
        # not doctor names. Coverage is now resolved per detector, the same
        # way a --findings report is, so a token has to be one
        # `rubric.DETECTOR_CATEGORIES` actually recognizes to grant credit.
        "analyzers_run": {"code-doctor": ["duplicates", "naming_issues"],
                          "python-code-doctor": ["mutation_hazards", "security",
                                                 "design_smells"]},
        "analyzers_skipped": {"code-doctor": [], "python-code-doctor": []},
        "analyzer_errors": {},
        "doctor_errors": {},
        "completeness": {},
        "coverage_unknown": [],
        "findings": [
            {"doctor": "code-doctor", "file": "src/app/settings.py", "line": 4,
             "smell_type": "hardcoded_secret_assignment", "severity": "high",
             "description": "SECRET_KEY assigned a literal", "suggestion": "read it from env",
             "kind": "finding"},
        ],
        "candidates": [
            {"doctor": "code-doctor", "file": "src/app/legacy.py", "line": 12,
             "smell_type": "dead_function_candidate", "severity": "medium",
             "description": "identifier occurs once in the tree", "kind": "candidate",
             "also_caused_by": ["a library's public surface has no internal referrer"]},
        ],
    }
    payload.update(overrides)
    path = tmp_path / "merged.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def build(repo, run_script, tmp_path, merged: Path, *extra, expect_rc=0) -> Path:
    out = tmp_path / "health.html"
    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app",
               "--root-dir", "src/app", "--merged", merged, *extra, expect_rc=expect_rc)
    return out


@pytest.fixture
def repo_with_code(repo):
    for i in range(12):
        repo.write(f"src/app/mod{i}.py", "def f():\n    return 1\n" * 20)
    repo.commit()
    return repo


def test_candidates_are_counted_but_never_scored(repo_with_code, run_script, tmp_path):
    with_candidates = meta_of(build(repo_with_code, run_script, tmp_path,
                                    envelope(tmp_path)))

    without = meta_of(build(repo_with_code, run_script, tmp_path,
                            envelope(tmp_path, candidates=[])))

    assert with_candidates["candidates_total"] == 1
    assert without["candidates_total"] == 0
    assert with_candidates["score"] == pytest.approx(without["score"]), (
        "a candidate asserts no defect; charging one to the grade punishes honesty"
    )


def test_coverage_comes_from_doctors_run_not_a_flag(repo_with_code, run_script, tmp_path):
    meta = meta_of(build(repo_with_code, run_script, tmp_path, envelope(tmp_path)))

    assert meta["doctors"] == ["code-doctor", "python-code-doctor"]
    assert "correctness" not in meta["ungraded"], (
        "python-code-doctor ran, so Correctness is measured"
    )


def test_code_doctor_alone_leaves_correctness_ungraded(repo_with_code, run_script, tmp_path):
    solo = envelope(tmp_path, doctors_run=["code-doctor"],
                    analyzers_run={"code-doctor": ["duplicates", "naming_issues"]})

    meta = meta_of(build(repo_with_code, run_script, tmp_path, solo))

    assert "correctness" in meta["ungraded"]


def test_a_failed_doctor_ungrades_what_it_covered(repo_with_code, run_script, tmp_path):
    broken = envelope(tmp_path, doctors_run=["code-doctor"],
                      analyzers_run={"code-doctor": ["naming_issues"]},
                      doctor_errors={"python-code-doctor": "crashed reading settings"})

    meta = meta_of(build(repo_with_code, run_script, tmp_path, broken))

    assert meta["doctor_errors"] == {"python-code-doctor": "crashed reading settings"}
    assert "correctness" in meta["ungraded"], (
        "the surviving report must not score categories the failed doctor was measuring"
    )


def test_an_inadequate_reference_graph_ungrades_design(repo_with_code, run_script, tmp_path):
    thin = envelope(tmp_path, completeness={
        "code-doctor": {"reference_graph": {"adequate": False, "resolution_rate": 0.3}}})

    meta = meta_of(build(repo_with_code, run_script, tmp_path, thin))

    assert "design" in meta["ungraded"]
    assert meta["completeness"]["code-doctor"]["reference_graph"]["resolution_rate"] == 0.3


def test_a_doctor_with_no_coverage_evidence_grants_nothing(repo_with_code, run_script, tmp_path):
    unknown = envelope(tmp_path, doctors_run=["django-code-doctor"],
                       analyzers_run={"django-code-doctor": []},
                       coverage_unknown=["django-code-doctor"])

    meta = meta_of(build(repo_with_code, run_script, tmp_path, unknown))

    assert meta["score"] is None, "a bare list says nothing about what was examined"


def test_an_envelope_with_no_doctors_grades_nothing(repo_with_code, run_script, tmp_path):
    empty = envelope(tmp_path, doctors_run=[], analyzers_run={}, findings=[],
                     candidates=[], doctor_errors={"code-doctor": "empty report"})

    meta = meta_of(build(repo_with_code, run_script, tmp_path, empty))

    assert meta["score"] is None
    assert meta["grade"] == "—"


def test_out_of_scope_candidates_are_dropped_with_the_findings(repo_with_code, run_script,
                                                               tmp_path):
    elsewhere = envelope(tmp_path, candidates=[
        {"doctor": "code-doctor", "file": "src/other/x.py", "line": 1,
         "smell_type": "zero_inbound_file", "severity": "low", "kind": "candidate",
         "description": "no inbound edges", "also_caused_by": ["an entry point"]}])

    meta = meta_of(build(repo_with_code, run_script, tmp_path, elsewhere))

    assert meta["candidates_total"] == 0


def test_a_failed_doctor_does_not_ungrade_what_a_surviving_doctor_also_covers(
        repo_with_code, run_script, tmp_path):
    """python-code-doctor's own security detector completed; django's crash beside
    it is not this category's gap — the surviving doctor still measured it."""
    covered_by_both = envelope(tmp_path, doctors_run=["python-code-doctor"],
                               analyzers_run={"python-code-doctor": ["security"]},
                               doctor_errors={"django-code-doctor": "crashed reading settings"})

    meta = meta_of(build(repo_with_code, run_script, tmp_path, covered_by_both))

    assert "security" not in meta["ungraded"]


def test_a_merged_findings_severity_is_normalised(repo_with_code, run_script, tmp_path):
    """A finding's severity is charged at whatever weight this normalizes it to;
    the count reported beside it has to agree, or the numbers contradict."""
    shouting = envelope(tmp_path, findings=[
        {"doctor": "code-doctor", "file": "src/app/mod0.py", "line": 1,
         "smell_type": "hardcoded_secret_assignment", "severity": "High",
         "description": "an unnormalised severity token", "suggestion": "read it from env",
         "kind": "finding"},
    ])

    meta = meta_of(build(repo_with_code, run_script, tmp_path, shouting))

    assert meta["findings_by_severity"]["high"] == 1, (
        "an unnormalised 'High' is charged full high weight but would be counted nowhere"
    )


def test_assume_full_coverage_yields_to_a_doctor_that_demonstrably_crashed(
        repo_with_code, run_script, tmp_path):
    """merge_reports.py emits exactly this combination when every doctor failed:
    an envelope naming no doctors but recording why each one failed. Subtracting
    a doctor's profile from the --assume-full-coverage sentinel must not crash,
    and the demonstrated failure has to win over the blanket assertion."""
    every_doctor_failed = envelope(tmp_path, doctors_run=[], analyzers_run={},
                                   findings=[], candidates=[],
                                   doctor_errors={"code-doctor": "crashed",
                                                  "python-code-doctor": "crashed"})

    out = build(repo_with_code, run_script, tmp_path, every_doctor_failed,
               "--assume-full-coverage")
    meta = meta_of(out)

    assert meta["score"] is None
    assert len(meta["ungraded"]) == 7


def test_a_non_list_findings_field_grades_nothing(repo_with_code, run_script, tmp_path):
    """`{"findings": {...}}` must not be silently read as zero findings and A+."""
    malformed = envelope(tmp_path, findings={"oops": "should have been a list"})

    meta = meta_of(build(repo_with_code, run_script, tmp_path, malformed))

    assert meta["score"] is None
    assert meta["doctors"] == []


def test_a_non_dict_completeness_block_grades_nothing(repo_with_code, run_script, tmp_path):
    """A `completeness` value that is not itself a dict must not crash the page."""
    malformed = envelope(tmp_path, completeness={"code-doctor": "not a dict"})

    meta = meta_of(build(repo_with_code, run_script, tmp_path, malformed))

    assert meta["score"] is None
    assert meta["doctors"] == []


def test_an_unrecognised_failed_doctor_is_named_in_a_warning(repo_with_code, run_script,
                                                             tmp_path):
    """A failed doctor this rubric has no coverage profile for must not silently
    subtract nothing — the gap in the warning has to name the doctor."""
    out = tmp_path / "health.html"
    mystery = envelope(tmp_path, doctor_errors={"mystery-doctor": "boom"})

    result = run_script(SCRIPT, "--out", out, "--repo", repo_with_code.path, "--name", "app",
                        "--root-dir", "src/app", "--merged", mystery)

    assert "mystery-doctor" in result.stderr
