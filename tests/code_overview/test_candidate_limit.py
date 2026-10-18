"""Candidate display bounds must never change analysis or hide total counts."""

import json
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills/code-overview/scripts/build_health.py"


def build_page(repo, run_script, tmp_path, count, limit=None):
    repo.write("src/app.py", "x = 1\n" * 250)
    payload = {
        "schema": "code-doctor-merge/1",
        "doctors_run": ["code-doctor"],
        "analyzers_run": {"code-doctor": ["hygiene", "secrets"]},
        "findings": [{"doctor": "code-doctor", "file": "src/app.py", "line": 1,
                      "smell_type": "hardcoded_secret_assignment", "severity": "high",
                      "description": "literal secret", "suggestion": "read environment"}],
        "candidates": [
            {"doctor": "code-doctor", "file": "src/app.py", "line": i + 1,
             "smell_type": "dead_function_candidate", "severity": "medium",
             "description": f"candidate-marker-{i:04d}",
             "also_caused_by": ["public API"], "kind": "candidate"}
            for i in range(count)
        ],
    }
    report = tmp_path / "merged.json"
    report.write_text(json.dumps(payload))
    output = tmp_path / f"health-{limit}.html"
    args = [] if limit is None else ["--candidate-limit", limit]
    run_script(SCRIPT, "--repo", repo.path, "--root-dir", "src", "--name", "app",
               "--merged", report, "--out", output, *args)
    page = output.read_text()
    meta = json.loads(re.search(r'id="code-health-meta">(.*?)</script>', page, re.S).group(1))
    return page, meta


@pytest.mark.parametrize("limit,shown", [(None, 100), (3, 3), (0, 0)])
def test_candidate_rows_are_bounded_and_omissions_are_explicit(repo, run_script, tmp_path, limit, shown):
    page, meta = build_page(repo, run_script, tmp_path, 105, limit)
    assert meta["candidates_total"] == 105
    assert meta["candidates_shown"] == shown
    assert len(re.findall(r"candidate-marker-\d+", page)) == shown
    assert f"Showing {shown} of 105 candidates; {105 - shown} omitted" in page
    assert "No candidates were reported" not in page
    assert "These did not affect the grade" in page


def test_candidate_limit_leaves_grades_and_coverage_unchanged(repo, run_script, tmp_path):
    _, bounded = build_page(repo, run_script, tmp_path, 105, 1)
    _, complete = build_page(repo, run_script, tmp_path, 105, 200)
    for key in ("score", "grade", "categories", "ungraded", "completeness", "findings_total",
                "top_findings", "candidates_total", "doctor_errors"):
        assert bounded[key] == complete[key], key
    assert bounded["findings_total"] == 1


def test_small_candidate_reports_preserve_all_rows(repo, run_script, tmp_path):
    page, meta = build_page(repo, run_script, tmp_path, 2)
    assert meta["candidates_total"] == meta["candidates_shown"] == 2
    assert "Showing 2 of 2 candidates; 0 omitted" in page
    assert "candidate-marker-0000" in page
    assert "candidate-marker-0001" in page
    assert "public API" in page


def test_empty_candidate_reports_still_say_none_reported(repo, run_script, tmp_path):
    page, meta = build_page(repo, run_script, tmp_path, 0)
    assert meta["candidates_total"] == meta["candidates_shown"] == 0
    assert "No candidates were reported" in page


def test_negative_candidate_limit_is_rejected_before_writing(repo, run_script, tmp_path):
    output = tmp_path / "invalid.html"
    result = run_script(SCRIPT, "--repo", repo.path, "--out", output,
                        "--candidate-limit", -1, expect_rc=2)
    assert "must be non-negative" in result.stderr
    assert not output.exists()
