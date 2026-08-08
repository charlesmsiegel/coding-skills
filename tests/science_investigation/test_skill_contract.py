"""The document half of the skill has to be documented to be reachable.

build_measurement.py cannot be discovered by an agent that was never told the
inventory file exists, and an audit that stops at prose silently loses the
grade this plan added.
"""

import json
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skills" / "science-investigation"


def skill_text() -> str:
    return (SKILL / "SKILL.md").read_text(encoding="utf-8")


def test_the_workflow_names_the_builder_and_the_inventory_file():
    text = skill_text()
    assert "build_measurement.py" in text
    assert "measurement-inventory/1" in text


def test_the_importance_and_credit_ladders_are_documented_where_they_are_used():
    text = skill_text()
    assert "gates a ship decision" in text
    for step in ("1.0", "0.5", "0.25", "0.0"):
        assert step in text


def test_the_denominator_rule_is_stated():
    text = skill_text().lower()
    assert "unmeasurable" in text and "denominator" in text


def test_evals_cover_the_grade_and_the_empty_unit():
    payload = json.loads((SKILL.parent.parent / "evals" / "science-investigation" /
                          "evals.json").read_text(encoding="utf-8"))
    blob = " ".join(case["prompt"] + case["expected_output"] for case in payload["evals"])
    assert "measurement.html" in blob or "build_measurement" in blob
    assert "null" in blob.lower()
