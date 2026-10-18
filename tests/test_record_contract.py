"""Every doctor enforces the same finding/candidate contract, at construction.

The parametrization is discovered from the tree, not listed: a new
`*-code-doctor` directory is held to the contract the moment it has a
`scripts/common.py`, and a doctor that drifts fails here rather than in a
consumer that graded a lead as a defect.
"""

import dataclasses
import json
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parent.parent / "skills"
DOCTORS = sorted(p.parent.parent.name for p in SKILLS.glob("*code-doctor/scripts/common.py"))


@pytest.fixture(params=DOCTORS)
def common(request, load_module):
    return load_module(SKILLS / request.param / "scripts", "common")


def test_every_doctor_is_discovered():
    assert {"code-doctor", "python-code-doctor", "typescript-code-doctor",
            "rust-code-doctor", "django-code-doctor"} <= set(DOCTORS)


def test_kinds_are_exactly_finding_and_candidate(common):
    assert common.VALID_KINDS == frozenset({"finding", "candidate"})
    assert issubclass(common.SchemaError, ValueError)


def test_valid_finding_and_candidate_construct(common):
    finding = common.Finding(file="a", line=1, smell_type="x", description="d", suggestion="fix")
    candidate = common.Finding(file="a", line=2, smell_type="y", description="d",
                               kind="candidate", also_caused_by=["benign"])
    assert finding.kind == "finding" and candidate.suggestion == ""


def test_finding_without_a_suggestion_is_rejected(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a", line=1, smell_type="x", description="d", suggestion="  ")


def test_finding_with_benign_explanations_is_rejected(common):
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a", line=1, smell_type="x", description="d",
                       suggestion="fix", also_caused_by=["benign"])


def test_candidate_with_a_suggestion_is_rejected(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a", line=1, smell_type="x", description="d",
                       kind="candidate", suggestion="fix", also_caused_by=["benign"])


def test_candidate_without_benign_explanations_is_rejected(common):
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a", line=1, smell_type="x", description="d",
                       kind="candidate", also_caused_by=[" "])


def test_unknown_kind_is_rejected(common):
    with pytest.raises(common.SchemaError, match="kind"):
        common.Finding(file="a", line=1, smell_type="x", description="d",
                       suggestion="fix", kind="maybe")


def test_record_is_frozen_with_tuple_collections(common):
    record = common.Finding(file="a", line=1, smell_type="x", description="d",
                            kind="candidate", also_caused_by=["benign"], related_lines=[2])
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.kind = "finding"
    assert isinstance(record.also_caused_by, tuple)
    assert isinstance(record.related_lines, tuple)


def test_serialized_shape_carries_kind_and_round_trips(common):
    record = common.Finding(file="a", line=1, smell_type="x", description="d",
                            kind="candidate", also_caused_by=["benign"], related_lines=[2])
    payload = json.loads(json.dumps(dataclasses.asdict(record)))
    assert payload["kind"] == "candidate"
    assert common.Finding(**payload) == record
