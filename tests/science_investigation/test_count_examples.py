"""Tests for science-investigation's example counter.

This script exists so an auditor never quotes "n=30" from memory, which means its
counts have to be right and its notion of "labeled" has to be conservative. The
sharpest test here is the one pinning that a bare `answer` column is NOT ground
truth: in an eval file that holds what the system produced, and counting it as a
label turns an unlabeled dataset into a fully-labeled one.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "science-investigation" / "scripts" / "count_examples.py"


def run(root, *args) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(root), "--format", "json", *args],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-600:]
    return json.loads(result.stdout)


def write_jsonl(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def kinds(payload, kind) -> list:
    return [row for row in payload["candidates"] if row["kind"] == kind]


def test_partial_labels_report_the_real_n(tmp_path):
    rows = [{"id": i, "question": "q", "answer": "a"} for i in range(412)]
    for row in rows[:3]:
        row["expected_answer"] = "gold"
    write_jsonl(tmp_path / "eval.jsonl", rows)

    payload = run(tmp_path)
    partial = kinds(payload, "partial_labels")
    assert partial, "3 labeled of 412 is the finding this script exists for"
    assert "n=3, not 412" in partial[0]["detail"]
    assert "412" in payload["headline"] and "3" in payload["headline"]


def test_a_bare_answer_column_is_not_treated_as_ground_truth(tmp_path):
    """`answer` holds the system's output; counting it as a label hides the gap."""
    write_jsonl(tmp_path / "eval.jsonl", [{"id": i, "answer": "a"} for i in range(50)])
    payload = run(tmp_path)
    assert kinds(payload, "no_label_field")
    assert not kinds(payload, "partial_labels")


def test_an_empty_string_is_not_a_populated_field(tmp_path):
    write_jsonl(tmp_path / "eval.jsonl", [{"label": "yes"}, {"label": ""}, {"label": None}])
    payload = run(tmp_path)
    assert "1 of 3" in kinds(payload, "partial_labels")[0]["detail"]


def test_zero_and_false_are_real_values(tmp_path):
    """A label of 0 (or False) is a label; treating it as missing undercounts n."""
    write_jsonl(tmp_path / "eval.jsonl", [{"label": 0}, {"label": False}, {"label": 1}])
    payload = run(tmp_path)
    assert not kinds(payload, "partial_labels"), "all three rows are labeled"


def test_csv_columns_are_counted(tmp_path):
    (tmp_path / "rows.csv").write_text("id,label\n1,yes\n2,\n3,no\n", encoding="utf-8")
    payload = run(tmp_path)
    assert "2 of 3" in kinds(payload, "partial_labels")[0]["detail"]


def test_records_nested_under_a_json_key_are_found(tmp_path):
    (tmp_path / "cases.json").write_text(
        json.dumps({"version": 2, "cases": [{"gold": "a"}, {"gold": "b"}]}), encoding="utf-8"
    )
    payload = run(tmp_path)
    assert payload["counts"]["records_total"] == 2


def test_configuration_json_is_not_counted_as_a_dataset(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "x", "version": "1.0"}), encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(json.dumps({"compilerOptions": {"strict": True}}), encoding="utf-8")
    payload = run(tmp_path)
    assert payload["counts"]["datasets"] == 0


def test_a_small_dataset_is_flagged_against_the_ship_rule(tmp_path):
    write_jsonl(tmp_path / "eval.jsonl", [{"gold": "a"} for _ in range(12)])
    payload = run(tmp_path)
    assert kinds(payload, "small_n")


def test_unparseable_rows_are_reported_rather_than_dropped(tmp_path):
    (tmp_path / "eval.jsonl").write_text(
        json.dumps({"gold": "a"}) + "\n" + "{not json\n" + json.dumps({"gold": "b"}) + "\n",
        encoding="utf-8",
    )
    payload = run(tmp_path)
    bad = kinds(payload, "unparseable_rows")
    assert bad and "1 of 3" in bad[0]["detail"]


def test_a_truncated_read_still_reports_the_true_record_count(tmp_path):
    """The cap applies to the field tally, never to N.

    Counting a line is cheap and parsing it is not, so a capped run still knows
    how many records exist. Reporting the cap as the dataset's size would be a
    silent truncation committed by the tool whose whole job is deriving N.
    """
    write_jsonl(tmp_path / "eval.jsonl", [{"gold": "a"} for _ in range(50)])
    payload = run(tmp_path, "--max-rows", "10")
    assert payload["counts"]["records_total"] == 50
    assert payload["counts"]["records_read"] == 10
    assert any("--max-rows" in row["detail"] for row in kinds(payload, "dataset"))


def test_a_truncated_read_never_claims_full_label_coverage(tmp_path):
    """5 labeled rows followed by 15 unlabeled ones is not a fully-labeled set."""
    rows = [{"gold": "g"} if i < 5 else {"q": "x"} for i in range(20)]
    write_jsonl(tmp_path / "eval.jsonl", rows)
    payload = run(tmp_path, "--max-rows", "5")
    assert "fully-labeled" not in payload["headline"]
    assert "read only in part" in payload["headline"]


def test_no_datasets_says_so_instead_of_reporting_zero_examples(tmp_path):
    (tmp_path / "s.py").write_text("x = 1\n", encoding="utf-8")
    payload = run(tmp_path)
    assert payload["counts"]["datasets"] == 0
    assert "No JSON/JSONL/CSV datasets" in payload["headline"]


def test_a_single_file_can_be_counted_directly(tmp_path):
    write_jsonl(tmp_path / "eval.jsonl", [{"gold": "a"} for _ in range(5)])
    payload = run(tmp_path / "eval.jsonl")
    assert payload["counts"]["records_total"] == 5


@pytest.mark.parametrize("directory", ["node_modules", ".venv", "__pycache__"])
def test_vendored_trees_are_skipped(tmp_path, directory):
    vendored = tmp_path / directory
    vendored.mkdir()
    write_jsonl(vendored / "eval.jsonl", [{"gold": "a"} for _ in range(9)])
    assert run(tmp_path)["counts"]["datasets"] == 0


# ---- review fixes ------------------------------------------------------------ #

def test_every_label_field_keeps_its_own_coverage(tmp_path):
    """A full `expected` must not hide a sparse `human_rating`: two fields, two n's."""
    rows = [{"expected": "gold"} for _ in range(100)]
    for row in rows[:5]:
        row["human_rating"] = 4
    write_jsonl(tmp_path / "eval.jsonl", rows)

    payload = run(tmp_path)
    partial = kinds(payload, "partial_labels")
    assert [row["detail"].split(" ")[0] for row in partial] == ["human_rating"]
    assert "n=5, not 100" in partial[0]["detail"]
    assert "human_rating" in payload["headline"] and "5" in payload["headline"]


def test_a_corrupted_dataset_is_not_reported_as_an_absent_one(tmp_path):
    (tmp_path / "eval.json").write_text('{"cases": [{"gold": "a"},\n', encoding="utf-8")
    payload = run(tmp_path)
    assert kinds(payload, "unparseable_dataset")
    assert payload["counts"]["datasets_unparseable"] == 1
    assert "do not parse" in payload["headline"]
    assert "No JSON/JSONL/CSV datasets found" not in payload["headline"]


def test_json_that_is_simply_not_a_dataset_stays_silent(tmp_path):
    """Only a *parse failure* is surfaced; a well-formed config is not a corruption."""
    (tmp_path / "settings.json").write_text('{"retries": 3, "region": "eu"}', encoding="utf-8")
    payload = run(tmp_path)
    assert payload["candidates"] == []
    assert payload["counts"]["datasets_unparseable"] == 0


# ---- second review round ------------------------------------------------------ #

def test_an_empty_dataset_is_reported_as_n_zero_not_as_no_dataset(tmp_path):
    """`[]` says the input exists and is empty — a different fact from absent."""
    (tmp_path / "eval.json").write_text("[]", encoding="utf-8")
    payload = run(tmp_path)
    assert kinds(payload, "empty_dataset")
    assert "zero records" in payload["headline"]
    assert "No JSON/JSONL/CSV datasets" not in payload["headline"]


def test_an_empty_record_list_under_a_named_key_is_also_a_dataset(tmp_path):
    (tmp_path / "cases.json").write_text('{"cases": []}', encoding="utf-8")
    assert kinds(run(tmp_path), "empty_dataset")


def test_a_json_list_of_scalars_is_still_not_a_dataset(tmp_path):
    (tmp_path / "allow.json").write_text('["a", "b", "c"]', encoding="utf-8")
    assert run(tmp_path)["counts"]["datasets"] == 0


# ---- third review round ------------------------------------------------------- #

@pytest.mark.parametrize("payload", ['["bad", {"gold": "a"}]', '[{"gold": "a"}, "bad"]'])
def test_a_mixed_json_array_is_read_the_same_way_in_either_order(tmp_path, payload):
    """Testing only the first element made the answer depend on record order."""
    (tmp_path / "eval.json").write_text(payload, encoding="utf-8")
    result = run(tmp_path)
    assert result["counts"]["datasets"] == 1
    assert result["counts"]["records_total"] == 2
    assert "1 of 2" in kinds(result, "unparseable_rows")[0]["detail"]


def test_an_unreadable_jsonl_file_reports_a_parse_error_like_the_other_readers(tmp_path, load_module):
    """An eval set that cannot be opened is not the same as one that isn't there.

    Driven at the function level: a permission error cannot be staged in a
    container running as root, and the defect was that this reader alone returned
    a key (`error`) that `analyze()` had no branch for, so the file fell through
    into the "no datasets found" headline.
    """
    counter = load_module(SCRIPT.parent, "count_examples")
    unopenable = tmp_path / "as_a_directory.jsonl"
    unopenable.mkdir()
    assert "parse_error" in counter.read_jsonl(unopenable, 100)


# ---- fourth review round ------------------------------------------------------ #

def test_a_non_object_jsonl_row_is_malformed_not_an_unlabeled_record(tmp_path):
    (tmp_path / "eval.jsonl").write_text('{"gold":"a"}\n42\n', encoding="utf-8")
    payload = run(tmp_path)
    assert "1 of 2" in kinds(payload, "unparseable_rows")[0]["detail"]


def test_a_populated_record_key_wins_over_an_earlier_empty_one(tmp_path):
    """`{"data": [], "examples": [...]}` held real records under the second key."""
    (tmp_path / "eval.json").write_text('{"data": [], "examples": [{"gold": "a"}]}', encoding="utf-8")
    payload = run(tmp_path)
    assert payload["counts"]["records_total"] == 1
    assert not kinds(payload, "empty_dataset")


def test_a_broken_config_file_is_not_reported_as_corrupted_measurement_input(tmp_path):
    """Manufacturing a dataset finding out of a malformed settings.json is the
    same error as manufacturing a metric out of `return 0`."""
    (tmp_path / "settings.json").write_text("{bad json\n", encoding="utf-8")
    payload = run(tmp_path)
    assert not kinds(payload, "unparseable_dataset")
    assert payload["counts"]["datasets_unparseable"] == 0


def test_a_broken_file_that_looks_like_data_is_still_reported(tmp_path):
    (tmp_path / "eval.json").write_text("{bad json\n", encoding="utf-8")
    assert kinds(run(tmp_path), "unparseable_dataset")
