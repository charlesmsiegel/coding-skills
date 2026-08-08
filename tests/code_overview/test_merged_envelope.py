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
        "analyzers_run": {"code-doctor": ["hygiene", "secrets"],
                          "python-code-doctor": ["find_security_issues"]},
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
                    analyzers_run={"code-doctor": ["hygiene", "secrets"]})

    meta = meta_of(build(repo_with_code, run_script, tmp_path, solo))

    assert "correctness" in meta["ungraded"]


def test_a_failed_doctor_ungrades_what_it_covered(repo_with_code, run_script, tmp_path):
    broken = envelope(tmp_path, doctors_run=["code-doctor"],
                      analyzers_run={"code-doctor": ["hygiene"]},
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
