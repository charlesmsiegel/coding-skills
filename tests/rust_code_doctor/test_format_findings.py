"""format_findings.py must not turn an unverified lead into a refactoring ticket."""

import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "rust-code-doctor" / "scripts"

RECORDS = [
    {"file": "src/a.rs", "line": 1, "smell_type": "unwrap_in_fallible_fn", "description": "a defect",
     "suggestion": "fix it", "severity": "high", "kind": "finding"},
    {"file": "src/a.rs", "line": 2, "smell_type": "narrowing_cast", "description": "a lead",
     "suggestion": "", "severity": "low", "kind": "candidate",
     "also_caused_by": ["the value was narrowed by a runtime check"]},
]


@pytest.fixture
def fmt(load_module):
    return load_module(SCRIPTS_DIR, "format_findings")


def test_list_marks_candidates_in_the_severity_column(fmt):
    out = fmt._render_list(RECORDS)
    assert "1 finding(s), 1 candidate(s)" in out
    assert "| ❓ candidate | narrowing_cast |" in out
    assert "Unverified lead" in out


def test_cards_file_candidates_as_investigations_not_refactors(fmt):
    out = fmt._render_cards(RECORDS)
    assert "### [Refactor] unwrap_in_fallible_fn" in out
    assert "### [Investigate] narrowing_cast" in out
    assert "Also caused by: the value was narrowed by a runtime check" in out
    assert "closed as not a defect" in out


def test_json_tickets_carry_kind_and_reasons(fmt):
    tickets = json.loads(fmt._render_json(RECORDS))
    lead = next(t for t in tickets if t["smell"] == "narrowing_cast")
    assert lead["kind"] == "candidate"
    assert lead["also_caused_by"] == ["the value was narrowed by a runtime check"]
    assert "kind:candidate" in lead["labels"]
    assert "kind" not in next(t for t in tickets if t["smell"] == "unwrap_in_fallible_fn")
