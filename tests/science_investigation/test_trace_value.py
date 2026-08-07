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

import pytest

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


# ---- review fixes ------------------------------------------------------------ #

@pytest.mark.parametrize("line", ["x = 0.7e3", "x = 0.7_5", "x = 0.75"])
def test_no_numeric_continuation_is_ever_a_match(tmp_path, line):
    """0.7 is not 0.75, not 0.7e3, not 0.7_5 — an inflated count is the whole failure."""
    (tmp_path / "a.py").write_text(line + "\n", encoding="utf-8")
    assert run("0.7", tmp_path)["candidates"] == []


def test_two_roles_on_one_line_are_both_counted(tmp_path):
    (tmp_path / "a.py").write_text("CUTOFF = 0.7 if mean > 0.7 else 1\n", encoding="utf-8")
    counts = run("0.7", tmp_path)["counts"]
    assert counts["definition"] == 1 and counts["comparison"] == 1


def test_a_repeated_literal_in_one_role_is_one_site(tmp_path):
    """A site is a (line, role) pair: two identical rows give an auditor one place to look."""
    (tmp_path / "a.py").write_text("if score > 0.7 and backup > 0.7:\n    pass\n", encoding="utf-8")
    assert run("0.7", tmp_path)["counts"]["sites"] == 1


def test_a_file_too_large_to_read_is_reported_as_unread(tmp_path):
    (tmp_path / "huge.py").write_text("# padding 0.7\n" * 200_000, encoding="utf-8")
    payload = run("0.7", tmp_path)
    assert payload["counts"]["files_skipped_unread"] == 1
    assert "NOT read" in payload["headline"]


def test_dot_env_sites_are_classified_as_config_not_as_code_definitions(tmp_path):
    """The walk and the classifier must agree on what a config file is."""
    (tmp_path / ".env").write_text("QUALITY_THRESHOLD=0.75\n", encoding="utf-8")
    (tmp_path / ".env.production").write_text("QUALITY_THRESHOLD=0.75\n", encoding="utf-8")
    counts = run("0.75", tmp_path)["counts"]
    assert counts["config"] == 2
    assert "definition" not in counts


@pytest.mark.parametrize("needle", [".75", "-.75"])
def test_a_leading_dot_literal_is_traced_as_a_number(tmp_path, needle):
    """`\\b` cannot match before a dot, so these used to report 'appears nowhere'."""
    (tmp_path / "s.py").write_text("if score > " + needle + ":\n    pass\n", encoding="utf-8")
    payload = run(needle, tmp_path)
    assert payload["candidates"]
    assert "appears nowhere" not in payload["headline"]


def test_an_unsigned_needle_does_not_match_a_negative_literal(tmp_path):
    """A tree holding both signs must not report two sites for one value."""
    (tmp_path / "a.py").write_text("A = -0.7\n", encoding="utf-8")
    assert run("0.7", tmp_path)["candidates"] == []
    assert run("-0.7", tmp_path)["candidates"]


def test_an_r_arrow_assignment_is_a_definition_not_a_comparison(tmp_path):
    """The `<` in `<-` read as a relational operator, so the tracer said an R
    metric was compared against and defined nowhere."""
    (tmp_path / "m.R").write_text("quality_score <- compute_score(rows)\n", encoding="utf-8")
    payload = run("quality_score", tmp_path)
    assert payload["counts"].get("definition") == 1
    assert "defined nowhere" not in payload["headline"]


def test_a_kotlin_declaration_is_a_definition(tmp_path):
    (tmp_path / "M.kt").write_text(
        "fun quality_score(rows: List<Int>): Double = 1.0\n", encoding="utf-8"
    )
    assert run("quality_score", tmp_path)["counts"].get("definition") == 1


def test_a_name_on_the_right_hand_side_is_a_consumer_not_a_definition(tmp_path):
    """`reported = quality_score` reads the metric; counting it as a second
    definition produced a false 'defined independently' headline."""
    (tmp_path / "a.py").write_text("def quality_score(r):\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("reported = quality_score\n", encoding="utf-8")
    payload = run("quality_score", tmp_path)
    assert payload["counts"].get("definition") == 1
    assert "defined independently" not in payload["headline"]


def test_an_r_right_hand_reference_is_a_consumer(tmp_path):
    """The R-arrow precheck bypassed the general right-hand-side rule."""
    (tmp_path / "a.R").write_text("quality_score <- compute(rows)\n", encoding="utf-8")
    (tmp_path / "b.R").write_text("reported <- quality_score\n", encoding="utf-8")
    payload = run("quality_score", tmp_path)
    assert payload["counts"].get("definition") == 1
    assert "defined independently" not in payload["headline"]


def test_a_commented_out_definition_is_not_a_source_of_truth(tmp_path):
    (tmp_path / "s.py").write_text(
        "# QUALITY_THRESHOLD = 0.7\nif score > 0.7:\n    pass\n", encoding="utf-8"
    )
    payload = run("0.7", tmp_path)
    assert payload["counts"].get("comment") == 1
    assert "defined nowhere" in payload["headline"]


def test_a_configured_value_is_not_reported_as_defined_nowhere(tmp_path):
    (tmp_path / "c.yaml").write_text("QUALITY_THRESHOLD: 0.7\n", encoding="utf-8")
    (tmp_path / "s.py").write_text("if score > 0.7:\n    pass\n", encoding="utf-8")
    headline = run("0.7", tmp_path)["headline"]
    assert "configured in c.yaml" in headline
    assert "defined nowhere" not in headline
