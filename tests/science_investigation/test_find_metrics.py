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
