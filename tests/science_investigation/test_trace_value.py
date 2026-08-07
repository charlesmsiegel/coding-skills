"""Tests for science-investigation's value tracer.

Two things must hold or the tool is worse than grep: `0.7` must not match inside
`0.75` (a wrong count is exactly the failure this skill audits for), and the
headline has to name the *shape* of the spread — one definition with literals
repeated elsewhere, versus several independent definitions of the same name.
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "science-investigation" / "scripts" / "trace_value.py"


def run(needle, root, *args) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), needle, str(root), "--format", "json", *args],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-600:]
    return json.loads(result.stdout)


def test_a_numeric_needle_does_not_match_inside_a_longer_number(tmp_path):
    (tmp_path / "a.py").write_text("CUTOFF = 0.75\nOTHER = 10.7\nMINE = 0.7\n", encoding="utf-8")
    rows = run("0.7", tmp_path)["candidates"]
    assert [row["line"] for row in rows] == [3]


def test_a_name_needle_matches_on_word_boundaries(tmp_path):
    (tmp_path / "a.py").write_text("quality_score = 1\nquality_score_v2 = 2\n", encoding="utf-8")
    rows = run("quality_score", tmp_path)["candidates"]
    assert [row["line"] for row in rows] == [1]


def test_a_threshold_defined_once_and_repeated_as_a_literal_is_the_headline(tmp_path):
    (tmp_path / "score.py").write_text("QUALITY_THRESHOLD = 0.75\n", encoding="utf-8")
    (tmp_path / "panel.py").write_text("if mean > 0.75:\n    pass\n", encoding="utf-8")
    headline = run("0.75", tmp_path)["headline"]
    assert "defined once" in headline and "panel.py" in headline


def test_several_independent_definitions_are_called_out(tmp_path):
    (tmp_path / "a.py").write_text("quality_score = compute_a()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("quality_score = compute_b()\n", encoding="utf-8")
    headline = run("quality_score", tmp_path)["headline"]
    assert "defined independently in 2 place(s)" in headline


def test_a_literal_with_no_definition_anywhere_is_called_out(tmp_path):
    (tmp_path / "a.py").write_text("if score > 0.9:\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("if other >= 0.9:\n    pass\n", encoding="utf-8")
    assert "defined nowhere" in run("0.9", tmp_path)["headline"]


def test_sites_are_classified_by_where_they_live(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "gate.py").write_text("if score >= 0.8:\n    pass\n", encoding="utf-8")
    (tmp_path / "app" / "config.yaml").write_text("threshold: 0.8\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("The gate is 0.8 today.\n", encoding="utf-8")
    (tmp_path / "tests" / "test_gate.py").write_text("assert gate(0.8)\n", encoding="utf-8")

    counts = run("0.8", tmp_path)["counts"]
    assert counts["comparison"] == 1
    assert counts["config"] == 1
    assert counts["doc"] == 1
    assert counts["test"] == 1


def test_a_needle_that_appears_nowhere_says_so(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    payload = run("0.42", tmp_path)
    assert payload["candidates"] == []
    assert "appears nowhere" in payload["headline"]


def test_regex_mode_is_opt_in(tmp_path):
    (tmp_path / "a.py").write_text("p95 = 1\np99 = 2\n", encoding="utf-8")
    assert run("p9[59]", tmp_path)["candidates"] == [], "a literal needle stays literal"
    assert len(run("p9[59]", tmp_path, "--regex")["candidates"]) == 2


def test_a_bad_regex_exits_two_rather_than_traceback(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "p9[59", str(tmp_path), "--regex"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "bad regular expression" in result.stderr


def test_every_row_is_a_candidate_with_a_confirm_step(tmp_path):
    (tmp_path / "a.py").write_text("THRESHOLD = 0.6\n", encoding="utf-8")
    for row in run("0.6", tmp_path)["candidates"]:
        assert row["status"] == "candidate"
        assert row["confirm"].strip()
