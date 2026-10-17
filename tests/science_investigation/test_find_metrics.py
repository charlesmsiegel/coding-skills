"""Tests for science-investigation's metric enumerator.

The script's job is to hand an auditor a short list of places to read, so most of
these pin what it must *not* say: a helper used by its own module is not dead
measurement, a comparison is not a definition, and `learning_rate` is not a metric.
A candidate list nobody trusts gets skipped, and then the audit is done from memory.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "science-investigation" / "scripts" / "find_metrics.py"


@pytest.fixture
def finder(load_module):
    return load_module(SCRIPT.parent, "find_metrics")


def run(root, *args) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(root), "--format", "json", *args],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-600:]
    return json.loads(result.stdout)


def kinds(payload, kind) -> list:
    return [row for row in payload["candidates"] if row["kind"] == kind]


# ---- name classification ---------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "accuracy", "recall_at_10", "ndcgAt5", "quality_score", "win_rate",
    "faithfulness", "judge_verdict", "MEAN_DELTA", "passRate",
])
def test_measure_names_are_recognized(finder, name):
    assert finder.is_metric_name(name)


@pytest.mark.parametrize("name", [
    "learning_rate", "sample_rate", "retry_count", "user_id", "timeout_seconds",
    "bitrate", "flush_interval",
])
def test_ordinary_names_are_not_measures(finder, name):
    assert not finder.is_metric_name(name)


def test_a_limit_is_not_a_measurement_threshold(finder):
    """`max_retries = 3` is retry config; flagging it buries the real thresholds."""
    assert not finder.is_threshold_name("max_retries")
    assert finder.is_threshold_name("quality_threshold")
    assert finder.is_threshold_name("SCORE_CUTOFF")


# ---- what it finds ----------------------------------------------------------- #

def test_definitions_thresholds_and_weights_are_found(tmp_path):
    (tmp_path / "score.py").write_text(
        "QUALITY_THRESHOLD = 0.75\n"
        "WEIGHTS = {'relevance': 0.4, 'latency': 0.2, 'quality': 0.4}\n"
        "\n"
        "def accuracy(rows):\n"
        "    return sum(rows) / len(rows)\n"
        "\n"
        "def gate(value):\n"
        "    return value >= QUALITY_THRESHOLD\n",
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert kinds(payload, "threshold"), "a named constant that gates a decision must surface"
    assert kinds(payload, "composite_weight"), "hand-written weights must surface"
    assert any(row["detail"].startswith("accuracy") for row in kinds(payload, "metric_definition"))


def test_every_row_is_labelled_a_candidate_with_a_confirm_step(tmp_path):
    """The whole discipline: a script row is a hypothesis, never a finding."""
    (tmp_path / "s.py").write_text("accuracy = 0.9\n", encoding="utf-8")
    payload = run(tmp_path)
    assert payload["candidates"]
    for row in payload["candidates"]:
        assert row["status"] == "candidate"
        assert row["confirm"].strip()
    assert payload["headline"] and payload["caveat"]


def test_a_renormalizing_composite_is_flagged(tmp_path):
    (tmp_path / "s.py").write_text(
        "def composite(parts):\n"
        "    got = [v for v in parts.values() if v is not None]\n"
        "    return sum(got) / len(got)\n",
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert kinds(payload, "renormalized_composite")


def test_an_error_path_returning_zero_is_flagged(tmp_path):
    (tmp_path / "s.py").write_text(
        "def score(row):\n"
        "    if row is None:\n"
        "        return 0.0\n"
        "    return row['v']\n",
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert kinds(payload, "zero_default")


# ---- what it must not say ---------------------------------------------------- #

def test_a_helper_used_by_its_own_module_is_not_dead_measurement(tmp_path):
    (tmp_path / "s.py").write_text(
        "def judge_score(row):\n"
        "    return 1.0\n"
        "\n"
        "def run(rows):\n"
        "    return [judge_score(r) for r in rows]\n",
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert not kinds(payload, "no_consumer"), "one reference anywhere clears a name"


def test_a_metric_referenced_nowhere_is_reported_as_a_dead_measurement_candidate(tmp_path):
    (tmp_path / "s.py").write_text(
        "def unused_recall(rows):\n"
        "    return 0.5\n",
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert [row for row in kinds(payload, "no_consumer") if "unused_recall" in row["detail"]]
    assert "dead measurement" in payload["headline"]


def test_a_local_variable_is_never_a_dead_measurement_candidate(tmp_path):
    (tmp_path / "s.py").write_text(
        "def run(parts):\n"
        "    scores_total = sum(parts)\n"
        "    return scores_total\n",
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert not kinds(payload, "no_consumer")


def test_a_comparison_is_not_read_as_a_definition(tmp_path):
    (tmp_path / "s.py").write_text("quality_score == 0.75\n", encoding="utf-8")
    payload = run(tmp_path)
    assert not [row for row in kinds(payload, "metric_definition") if "quality_score" in row["detail"]]
    assert kinds(payload, "threshold"), "it is a threshold comparison, and should be reported as one"


def test_comments_are_ignored(tmp_path):
    (tmp_path / "s.py").write_text("# accuracy = 0.99 was the old value\n", encoding="utf-8")
    assert run(tmp_path)["candidates"] == []


def test_a_tree_with_no_measurement_says_so_rather_than_inventing_findings(tmp_path):
    (tmp_path / "s.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    payload = run(tmp_path)
    assert payload["candidates"] == []
    assert "No metric definitions" in payload["headline"]


def test_an_empty_tree_is_reported_as_nothing_to_scan(tmp_path):
    payload = run(tmp_path)
    assert payload["counts"]["files_scanned"] == 0
    assert "Nothing to scan" in payload["headline"]


def test_the_limit_caps_rows_but_not_the_reported_total(tmp_path):
    (tmp_path / "s.py").write_text("".join(
        "accuracy_%d = 0.5\n" % i for i in range(40)
    ), encoding="utf-8")
    payload = run(tmp_path, "--limit", "5")
    assert len(payload["candidates"]) == 5
    assert payload["counts"]["candidates_total"] > 5, "a silent cap is exactly what this skill audits for"


def test_a_missing_path_exits_two(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "nope")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2


# ---- review fixes ------------------------------------------------------------ #

def test_exported_typescript_metrics_are_found(tmp_path):
    """`export const` is the ordinary shape of a TS metrics module."""
    (tmp_path / "metrics.ts").write_text(
        "export const qualityScore = (rows) => rows.length;\n"
        "export default function recallAt10(rows) { return 1; }\n",
        encoding="utf-8",
    )
    found = {row["detail"].split(" ")[0] for row in kinds(run(tmp_path), "metric_definition")}
    assert "qualityScore" in found and "recallAt10" in found


def test_two_dead_definitions_of_one_name_do_not_clear_each_other(tmp_path):
    """Excluding only the first definition made the second look like a consumer."""
    (tmp_path / "a.py").write_text("def quality_score(r):\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def quality_score(r):\n    return 2\n", encoding="utf-8")
    rows = kinds(run(tmp_path), "no_consumer")
    assert sorted(row["file"] for row in rows) == ["a.py", "b.py"]


def test_pytest_entry_points_are_not_dead_measurement(tmp_path):
    """pytest calls these by collection, so 'referenced nowhere' says nothing."""
    (tmp_path / "test_scoring.py").write_text(
        "def test_precision_is_computed():\n"
        "    assert True\n"
        "\n"
        "def test_baseline_matches():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert not kinds(payload, "no_consumer")
    assert "dead measurement" not in payload["headline"]


def test_dot_env_files_are_scanned(tmp_path):
    """`Path('.env').suffix` is '', so a suffix test never sees it."""
    (tmp_path / ".env").write_text("QUALITY_THRESHOLD=0.75\n", encoding="utf-8")
    assert run(tmp_path)["counts"]["files_scanned"] == 1


def test_an_unread_file_is_reported_as_unread_not_as_scanned(tmp_path):
    big = tmp_path / "huge_metrics.py"
    big.write_text("accuracy = 0.5\n" + ("# padding\n" * 200_000), encoding="utf-8")
    assert big.stat().st_size > 2_000_000
    payload = run(tmp_path)
    assert payload["counts"]["files_skipped_unread"] == 1
    assert payload["counts"]["files_scanned"] == 0
    assert "NOT read" in payload["headline"], "a file nobody read must not read as clean"


# ---- second review round ------------------------------------------------------ #

def test_go_receiver_and_c_family_methods_are_found(tmp_path):
    (tmp_path / "runner.go").write_text(
        "package main\n"
        "func (r *Runner) Accuracy(rows []int) float64 { return 1 }\n",
        encoding="utf-8",
    )
    (tmp_path / "metrics.cpp").write_text("double accuracy(int n) { return 1.0; }\n", encoding="utf-8")
    found = {row["detail"].split(" ")[0] for row in kinds(run(tmp_path), "metric_definition")}
    assert found == {"Accuracy", "accuracy"}


def test_c_family_files_are_scanned_at_all(tmp_path):
    (tmp_path / "Metrics.cs").write_text("// nothing\n", encoding="utf-8")
    assert run(tmp_path)["counts"]["files_scanned"] == 1


@pytest.mark.parametrize("line,literal", [
    ("if quality_score > .75:", ".75"),
    ("if pvalue < 1e-3:", "1e-3"),
])
def test_leading_dot_and_exponent_thresholds_are_found(tmp_path, line, literal):
    """`pvalue < 1e-3` is the single most common threshold in a stats codebase."""
    (tmp_path / "s.py").write_text(line + "\n    pass\n", encoding="utf-8")
    assert any(literal in row["detail"] for row in kinds(run(tmp_path), "threshold"))


def test_the_headline_never_contradicts_the_rows_it_printed(tmp_path):
    """An indented config key is a definition; the headline used to deny it."""
    (tmp_path / "c.yaml").write_text("metrics:\n  accuracy: 0.90\n", encoding="utf-8")
    payload = run(tmp_path)
    assert kinds(payload, "metric_definition")
    assert "No metric definitions found" not in payload["headline"]


def test_thresholds_without_a_metric_definition_are_still_headlined(tmp_path):
    (tmp_path / "s.py").write_text("if quality_score >= 0.8:\n    pass\n", encoding="utf-8")
    payload = run(tmp_path)
    assert "threshold site(s)" in payload["headline"]


def test_zero_defaults_in_a_tree_with_no_measurement_are_not_reported(tmp_path):
    """`return 0` in a CLI is not a measurement site; reporting it manufactures one."""
    (tmp_path / "util.py").write_text(
        "def main():\n"
        "    return 0\n"
        "\n"
        "def opts(settings):\n"
        "    return settings.get('retries', 0)\n",
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert not kinds(payload, "zero_default")
    assert "No metric definitions found" in payload["headline"]


def test_a_zero_default_beside_a_metric_is_still_reported(tmp_path):
    (tmp_path / "score.py").write_text(
        "def accuracy(rows):\n"
        "    if not rows:\n"
        "        return 0.0\n"
        "    return sum(rows) / len(rows)\n",
        encoding="utf-8",
    )
    assert kinds(run(tmp_path), "zero_default")


# ---- third review round ------------------------------------------------------- #

def test_repeated_definitions_in_one_file_each_get_a_row(tmp_path):
    """The headline's site count has to match the rows printed under it."""
    (tmp_path / "m.py").write_text(
        "def quality_score(r):\n    return 1\n\n"
        "def quality_score(r):\n    return 2\n",
        encoding="utf-8",
    )
    rows = kinds(run(tmp_path), "no_consumer")
    assert [row["line"] for row in rows] == [1, 4]


@pytest.mark.parametrize("line,name", [
    ("double accuracy = 0.5;", "accuracy"),
    ("float quality_score = 0.8f;", "quality_score"),
])
def test_typed_variable_declarations_are_definitions(tmp_path, line, name):
    (tmp_path / "m.cpp").write_text(line + "\n", encoding="utf-8")
    assert any(row["detail"].startswith(name) for row in kinds(run(tmp_path), "metric_definition"))


def test_a_statement_keyword_is_never_read_as_a_declaration_type(tmp_path):
    """`return x = 1` must not register `x` as a metric declaration."""
    (tmp_path / "m.py").write_text("def f():\n    return score = 1\n", encoding="utf-8")
    assert not [row for row in kinds(run(tmp_path), "metric_definition") if row["detail"].startswith("score")]


def test_a_semicolon_terminated_zero_return_is_flagged(tmp_path):
    """`return 0;` is how JS, Java, C, C++ and C# write the same failure path."""
    (tmp_path / "m.js").write_text(
        "function accuracy(rows) {\n"
        "  if (!rows.length) return 0;\n"
        "  return 1;\n"
        "}\n",
        encoding="utf-8",
    )
    assert kinds(run(tmp_path), "zero_default")


# ---- fourth review round ------------------------------------------------------ #

def test_a_threshold_written_with_the_literal_first_is_the_same_gate(tmp_path):
    (tmp_path / "s.py").write_text("if 0.75 <= quality_score:\n    pass\n", encoding="utf-8")
    rows = kinds(run(tmp_path), "threshold")
    assert rows and "quality_score >= 0.75" in rows[0]["detail"], "normalized to metric-first"


def test_the_same_comparison_is_not_reported_twice(tmp_path):
    (tmp_path / "s.py").write_text("if quality_score >= 0.75:\n    pass\n", encoding="utf-8")
    assert len(kinds(run(tmp_path), "threshold")) == 1


@pytest.mark.parametrize("line,name", [
    ("accuracy <- function(rows) mean(rows)", "accuracy"),
    ("quality_score <- 0.8", "quality_score"),
])
def test_r_arrow_assignments_are_definitions(tmp_path, line, name):
    """`.r` and `.R` are scanned, so `<-` has to be an assignment operator."""
    (tmp_path / "m.R").write_text(line + "\n", encoding="utf-8")
    assert any(row["detail"].startswith(name) for row in kinds(run(tmp_path), "metric_definition"))


# ---- fifth review round ------------------------------------------------------- #

def test_a_line_matching_two_definition_patterns_counts_once(tmp_path):
    """`const accuracy = function accuracy(...)` matched both patterns, so the
    headline's site count disagreed with the rows and with candidates_total."""
    (tmp_path / "m.js").write_text(
        "const accuracy = function accuracy(rows) { return 1; };\n", encoding="utf-8"
    )
    payload = run(tmp_path)
    assert payload["counts"]["candidates_total"] == len(payload["candidates"])
    assert len(kinds(payload, "no_consumer")) == 1


def test_the_zero_default_headline_counts_the_definitions_it_printed(tmp_path):
    """An indented config metric is a definition row but not a top-level name."""
    (tmp_path / "s.py").write_text(
        'CONFIG = {\n    "accuracy": 0.9,\n}\n\n'
        'def load(cfg):\n    return cfg.get("accuracy", 0)\n',
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert kinds(payload, "zero_default")
    assert "alongside 0 metric name(s)" not in payload["headline"]


def test_dropping_nones_without_an_aggregate_is_not_a_composite(tmp_path):
    """What makes it a composite is the averaging, not the list comprehension."""
    (tmp_path / "util.py").write_text(
        "def clean(values):\n    return [v for v in values if v is not None]\n", encoding="utf-8"
    )
    payload = run(tmp_path)
    assert not kinds(payload, "renormalized_composite")
    assert "No metric definitions" in payload["headline"]


def test_kotlin_expression_bodied_functions_are_definitions(tmp_path):
    """`.kt` is scanned, and Kotlin's keyword is `fun`, which was not in the list."""
    (tmp_path / "M.kt").write_text(
        "fun accuracy(rows: List<Int>): Double = rows.size.toDouble()\n", encoding="utf-8"
    )
    assert any(row["detail"].startswith("accuracy")
               for row in kinds(run(tmp_path), "metric_definition"))


# ---- sixth review round ------------------------------------------------------- #

def test_a_c_family_suffixed_literal_reports_its_real_value(tmp_path):
    """`0.8f` backtracked to `0.` — a threshold value that is not in the source."""
    (tmp_path / "m.cpp").write_text("if (quality_score >= 0.8f) { }\n", encoding="utf-8")
    rows = kinds(run(tmp_path), "threshold")
    assert rows and "quality_score >= 0.8 " in rows[0]["detail"]


def test_a_gate_on_a_metric_call_is_a_threshold(tmp_path):
    (tmp_path / "s.py").write_text("if accuracy(rows) >= 0.8:\n    pass\n", encoding="utf-8")
    rows = kinds(run(tmp_path), "threshold")
    assert rows and "accuracy >= 0.8" in rows[0]["detail"]


def test_ruby_methods_without_parentheses_are_definitions(tmp_path):
    (tmp_path / "m.rb").write_text("def accuracy rows\n  1\nend\n", encoding="utf-8")
    assert any(row["detail"].startswith("accuracy")
               for row in kinds(run(tmp_path), "metric_definition"))


def test_a_zero_default_naming_a_metric_survives_in_a_file_that_defines_none(tmp_path):
    """`result.get("accuracy", 0)` in a dashboard is exactly where 'not measured'
    turns into a zero; file-local scoping alone hid it."""
    (tmp_path / "metric.py").write_text("def accuracy(rows):\n    return 1\n", encoding="utf-8")
    (tmp_path / "dashboard.py").write_text(
        'def show(result):\n    return result.get("accuracy", 0)\n', encoding="utf-8"
    )
    rows = kinds(run(tmp_path), "zero_default")
    assert [row["file"] for row in rows] == ["dashboard.py"]


def test_a_zero_default_naming_nothing_measurable_is_still_dropped(tmp_path):
    (tmp_path / "util.py").write_text(
        'def opts(settings):\n    return settings.get("retries", 0)\n', encoding="utf-8"
    )
    assert run(tmp_path)["candidates"] == []


# ---- seventh review round ----------------------------------------------------- #

def test_a_sql_metric_alias_is_a_definition(tmp_path):
    (tmp_path / "m.sql").write_text("SELECT AVG(correct) AS accuracy FROM evals;\n", encoding="utf-8")
    assert any(row["detail"].startswith("accuracy")
               for row in kinds(run(tmp_path), "metric_definition"))


def test_a_hyphenated_config_key_is_a_metric_definition(tmp_path):
    (tmp_path / "c.yaml").write_text("quality-score: 0.8\n", encoding="utf-8")
    assert kinds(run(tmp_path), "metric_definition")


def test_a_comparison_inside_a_message_is_not_a_threshold(tmp_path):
    """`message = "quality_score > 0.8"` gates nothing."""
    (tmp_path / "s.py").write_text('message = "quality_score > 0.8"\n', encoding="utf-8")
    assert not kinds(run(tmp_path), "threshold")


def test_a_comparison_in_a_config_value_is_still_read(tmp_path):
    """Config quotes hold the value itself, so they are not masked."""
    (tmp_path / "c.yaml").write_text('rule: "quality_score > 0.8"\n', encoding="utf-8")
    assert kinds(run(tmp_path), "threshold")


# ---- eighth review round ------------------------------------------------------ #

def test_notebook_code_cells_are_read_as_source(tmp_path):
    """`.ipynb` is JSON: read raw, its code hides inside quoted strings."""
    notebook = {
        "cells": [
            {"cell_type": "markdown", "source": ["# accuracy notes\n"]},
            {"cell_type": "code", "source": ["accuracy = 0.8\n", "if accuracy > 0.7:\n", "    pass\n"]},
        ],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }
    (tmp_path / "nb.ipynb").write_text(json.dumps(notebook), encoding="utf-8")
    payload = run(tmp_path)
    assert kinds(payload, "metric_definition")
    assert "No metric definitions" not in payload["headline"]


def test_a_minified_json_config_exposes_every_key(tmp_path):
    (tmp_path / "metrics.json").write_text('{"accuracy":0.8,"quality_score":0.7}\n', encoding="utf-8")
    found = {row["detail"].split(" ")[0] for row in kinds(run(tmp_path), "metric_definition")}
    assert found == {"accuracy", "quality_score"}


def test_javascript_nullish_zero_defaults_are_flagged(tmp_path):
    (tmp_path / "m.ts").write_text(
        "function f(result) { return result.accuracy ?? 0; }\n", encoding="utf-8"
    )
    assert kinds(run(tmp_path), "zero_default")


# ---- ninth review round ------------------------------------------------------- #

def test_lowercase_sql_aliases_are_definitions(tmp_path):
    """SQL keywords are case-insensitive."""
    (tmp_path / "m.sql").write_text("select avg(correct) as accuracy from evals;\n", encoding="utf-8")
    assert kinds(run(tmp_path), "metric_definition")


def test_a_commented_out_sql_query_is_not_a_live_metric(tmp_path):
    (tmp_path / "q.sql").write_text("-- SELECT AVG(correct) AS accuracy FROM evals\n", encoding="utf-8")
    assert run(tmp_path)["candidates"] == []


def test_notebook_candidates_report_an_openable_cell_location(tmp_path):
    """A minified notebook is one physical line, so `nb.ipynb:7` cannot be opened."""
    notebook = {
        "cells": [
            {"cell_type": "markdown", "source": ["# notes\n"]},
            {"cell_type": "code", "source": ["x = 1\n", "accuracy = 0.8\n"]},
        ],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }
    (tmp_path / "nb.ipynb").write_text(json.dumps(notebook), encoding="utf-8")
    row = kinds(run(tmp_path), "metric_definition")[0]
    assert row["file"] == "nb.ipynb#cell1"
    assert row["line"] == 2, "line is relative to the cell, not to the extracted stream"


# ---- tenth review round ------------------------------------------------------- #

def test_a_threshold_in_a_trailing_comment_is_not_a_live_gate(tmp_path):
    (tmp_path / "s.py").write_text("result = run()  # legacy: quality_score > 0.8\n", encoding="utf-8")
    assert not kinds(run(tmp_path), "threshold")


def test_a_not_equal_comparison_is_a_gate(tmp_path):
    (tmp_path / "s.py").write_text("if accuracy != 1.0:\n    pass\n", encoding="utf-8")
    assert kinds(run(tmp_path), "threshold")


def test_every_alias_in_one_sql_query_is_inventoried(tmp_path):
    (tmp_path / "q.sql").write_text(
        "SELECT AVG(correct) AS accuracy, AVG(score) AS quality_score FROM evals;\n",
        encoding="utf-8",
    )
    found = {row["detail"].split(" ")[0] for row in kinds(run(tmp_path), "metric_definition")}
    assert found == {"accuracy", "quality_score"}


def test_a_corrupt_notebook_cell_skips_the_file_rather_than_the_audit(tmp_path):
    """`{"source": 42}` raised TypeError out of read_source and killed the scan."""
    (tmp_path / "nb.ipynb").write_text(
        '{"cells":[{"cell_type":"code","source":42}],"metadata":{},"nbformat":4,"nbformat_minor":5}',
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert payload["counts"]["files_skipped_unread"] == 1
    assert "NOT read" in payload["headline"]


# ---- a real repository (hardy) ------------------------------------------------ #
# Run from the root of a real project: 17 rows came from a committed bundle, all
# 26 composite weights were arithmetic inside strings (Lean proofs in JSON, a test
# polynomial), 20 of 42 zero defaults were a command's exit status, and "map"
# matched `source_map` and `identity_map` 40 times. Each fix sits beside a test
# that the true shape is still reported.

def test_a_minified_bundle_is_not_mined_for_metrics(tmp_path):
    bundle = tmp_path / "static" / "assets" / "index-U_FZj3ND.js"
    bundle.parent.mkdir(parents=True)
    line = "function a(e){var accuracy=0.4*e.r+0.6*e.f;if(score>=0.8)return 0;return accuracy}" * 12
    bundle.write_text((line + "\n") * 3, encoding="utf-8")
    payload = run(tmp_path)
    assert payload["candidates"] == []
    assert payload["counts"]["files_skipped_generated"] == 1
    assert "index-U_FZj3ND.js" in payload["caveat"]


def test_the_same_code_written_by_hand_is_still_mined(tmp_path):
    (tmp_path / "score.js").write_text(
        "function a(e) {\n"
        "  var accuracy = 0.4 * e.r + 0.6 * e.f;\n"
        "  if (score >= 0.8) return 0;\n"
        "  return accuracy;\n"
        "}\n",
        encoding="utf-8",
    )
    payload = run(tmp_path)
    assert kinds(payload, "composite_weight") and kinds(payload, "threshold")
    assert payload["counts"]["files_skipped_generated"] == 0


def test_a_one_line_metrics_json_is_still_read(tmp_path):
    (tmp_path / "metrics.json").write_text(
        '{"pad": "' + "x" * 800 + '", "accuracy": 0.81}', encoding="utf-8"
    )
    assert any(row["detail"].startswith("accuracy") for row in kinds(run(tmp_path), "metric_definition"))


def test_an_overlong_line_is_read_up_to_the_bound_and_says_so(tmp_path):
    (tmp_path / "metrics.json").write_text(
        '{"accuracy": 0.81, "pad": "' + "x" * 20_000 + '"}\n', encoding="utf-8"
    )
    payload = run(tmp_path)
    assert kinds(payload, "metric_definition")
    assert payload["counts"]["lines_truncated"] == 1
    assert "1 line(s)" in payload["caveat"]


@pytest.mark.parametrize("name,body", [
    ("trajectory.json", '{"proof": "by\\n  nlinarith [sq_nonneg (2 * a + 3 * b)]"}\n'),
    ("test_cas.py", 'assert out == "x**4 + 4*x**3*y + 6*x**2*y**2"\n'),
    ("notes.json", '{"note": "the weight of evidence is 0.5"}\n'),
])
def test_arithmetic_inside_a_string_is_not_a_composite_weight(tmp_path, name, body):
    (tmp_path / name).write_text(body, encoding="utf-8")
    assert not kinds(run(tmp_path), "composite_weight")


@pytest.mark.parametrize("name,body", [
    ("score.py", "quality = 0.4 * relevance + 0.6 * faithfulness\n"),
    ("weights.yaml", "relevance_weight: 0.4\n"),
    ("weights.json", '{"relevance_weight": 0.4, "latency_weight": 0.6}\n'),
    ("rubric.yaml", "composite: 0.4 * relevance + 0.6 * faithfulness\n"),
])
def test_a_weight_written_as_a_value_is_still_found(tmp_path, name, body):
    (tmp_path / name).write_text(body, encoding="utf-8")
    assert kinds(run(tmp_path), "composite_weight")


def test_a_commands_exit_status_is_not_a_zero_default(tmp_path):
    """hardy's library and evals commands return 0 for success, like any CLI."""
    (tmp_path / "commands.py").write_text(
        "import argparse\n"
        "import sys\n"
        "ACCURACY_THRESHOLD = 0.8\n"
        "\n"
        "def _list(held) -> int:\n"
        "    for record in held.records():\n"
        "        print(record.accuracy)\n"
        "    return 0\n"
        "\n"
        "def run_todo(args: argparse.Namespace, config) -> int:\n"
        "    if not args.problems:\n"
        "        print('nothing to do', file=sys.stderr)\n"
        "        return 2\n"
        "    print(summarize(args))\n"
        "    return 0\n"
        "\n"
        "def main(argv=None) -> int:\n"
        "    return run_todo(parse(argv), None)\n"
        "\n"
        "def report(\n"
        "    cost,\n"
        "    *,\n"
        "    calls=None,\n"
        ") -> int:\n"
        '    """Print the measurement; exit nonzero when it could not answer."""\n'
        "    print(cost)\n"
        "    if cost.median is None:\n"
        "        return 1\n"
        "    if calls is not None:\n"
        "        return 0 if cost.consistent else 1\n"
        "    return 0\n",
        encoding="utf-8",
    )
    assert not kinds(run(tmp_path), "zero_default")


@pytest.mark.parametrize("body", [
    "def accuracy(rows):\n    if not rows:\n        return 0\n    return sum(rows) / len(rows)\n",
    # Printing does not make a metric a command: it still returns a computed value.
    "def accuracy(rows):\n    if not rows:\n        print('empty')\n        return 0\n"
    "    return correct(rows) / len(rows)\n",
    # Nor does returning only integers, when nothing about it is a command.
    "def win_rate(rows):\n    if not rows:\n        return 0\n    return 1\n",
])
def test_a_metric_returning_zero_is_still_a_zero_default(tmp_path, body):
    (tmp_path / "score.py").write_text(body, encoding="utf-8")
    assert kinds(run(tmp_path), "zero_default")


@pytest.mark.parametrize("name", ["source_map", "identity_map", "structural_map", "map_span"])
def test_map_inside_an_identifier_is_not_a_measure(finder, name):
    assert not finder.is_metric_name(name)


@pytest.mark.parametrize("name", ["map_at_10", "mapAt5", "MAP_AT_K", "val_map_score"])
def test_a_metric_shaped_map_is_still_a_measure(finder, name):
    assert finder.is_metric_name(name)


def test_a_limit_on_a_map_is_not_a_measurement_threshold(finder):
    assert not finder.is_threshold_name("MAX_MAP_NODES")
    assert finder.is_threshold_name("MIN_MAP_AT_10")


@pytest.mark.parametrize("name,body", [
    ("Chat.jsx", "const map = new Map();\n"),
    ("tools.py", 'payload = {"map": [entry(e) for e in shown], "map_truncated": False}\n'),
    ("library.py", "def _map(args, held) -> int:\n    return 1\n"),
    ("tree.py", "source_map = build(tree)\nidentity_map = {}\n"),
])
def test_a_mapping_is_not_reported_as_a_metric(tmp_path, name, body):
    (tmp_path / name).write_text(body, encoding="utf-8")
    payload = run(tmp_path)
    assert not kinds(payload, "metric_definition") and not kinds(payload, "threshold")


@pytest.mark.parametrize("name,body,kind", [
    ("results.json", '{"map": 0.42, "ndcg": 0.51}\n', "metric_definition"),
    ("gate.py", "if map >= 0.5:\n    ship()\n", "threshold"),
])
def test_a_standalone_map_with_a_number_is_still_a_measure(tmp_path, name, body, kind):
    (tmp_path / name).write_text(body, encoding="utf-8")
    assert any(row["detail"].startswith("map") for row in kinds(run(tmp_path), kind))


def test_a_bundle_still_counts_as_a_consumer(tmp_path):
    """Not mined is not unread: a bundle may be the only committed reader of a
    number, and calling the metric dead would manufacture a finding."""
    (tmp_path / "metric.py").write_text("quality_score = compute()\n", encoding="utf-8")
    bundle = tmp_path / "assets" / "index-abc123.js"
    bundle.parent.mkdir()
    bundle.write_text(("render(row.quality_score);" * 40 + "\n") * 2, encoding="utf-8")
    payload = run(tmp_path)
    assert payload["counts"]["files_skipped_generated"] == 1
    assert not kinds(payload, "no_consumer")


@pytest.mark.parametrize("line,leaf", [
    ("if scores[kind].precision >= 0.5:\n    ship()\n", "precision"),
    ("if evaluate(rows).accuracy >= 0.8:\n    ship()\n", "accuracy"),
    ("ok = (report\n    .accuracy >= 0.8)\n", "accuracy"),
])
def test_a_gate_on_an_attribute_of_a_subscript_or_call_is_a_threshold(tmp_path, line, leaf):
    """Anchoring the comparison at the start of a name must not lose `x[k].precision`."""
    (tmp_path / "gate.py").write_text(line, encoding="utf-8")
    assert any(row["detail"].startswith(leaf) for row in kinds(run(tmp_path), "threshold"))
