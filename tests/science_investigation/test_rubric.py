"""The measurement-coverage rubric.

The score is importance-weighted measured things over measurable things, so
both halves are judgments an author supplies. That is defensible only if the
validator refuses the shapes that would quietly inflate it: credit with no
finding behind it, an aggregate with no N, an unmeasurable thing dropped from
the denominator instead of counted at zero.
"""

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "science-investigation" / "scripts"


@pytest.fixture
def rubric(load_module):
    return load_module(SCRIPTS, "rubric")


def row(**overrides) -> dict:
    base = {
        "name": "judge_accuracy",
        "importance": 3,
        "importance_reason": "gates the weekly model rollout",
        "credit": 1.0,
        "credit_reason": "computed over the full labelled set",
        "finding": "",
        "n": 412,
        "n_total": 412,
        "formula": "mean(judge_score == gold)",
        "consumer": "scripts/rollout.py:88",
        "evidence": ["evals/judge.py:41"],
        "status": "measured",
        "unmeasurable_reason": "",
    }
    return {**base, **overrides}


FINDING = {"id": "small_n", "severity": "high", "title": "n=3",
           "detail": "3 of 412 rows are labelled", "evidence": ["evals/judge.py:41"],
           "blast_radius": "the rollout gate"}


# --- arithmetic -----------------------------------------------------------

def test_everything_measured_soundly_scores_one_hundred(rubric):
    result = rubric.score_inventory([row(), row(name="latency_p95", importance=1)])

    assert result["score"] == pytest.approx(100.0)
    assert result["grade"] == "A+"


def test_nothing_measurable_is_null_not_a_hundred(rubric):
    result = rubric.score_inventory([])

    assert result["score"] is None
    assert result["grade"] == rubric.UNGRADED


def test_nothing_measured_is_zero_not_null(rubric):
    result = rubric.score_inventory([row(credit=0.0, status="not_measured",
                                         credit_reason="never computed", n=None)])

    assert result["score"] == pytest.approx(0.0), "zero measurable coverage is a measurement"
    assert result["grade"] == "F"


def test_importance_weights_the_ratio(rubric):
    # A release gate at credit 0 and an informational metric at credit 1:
    # 1*1.0 / (3 + 1) = 25.
    result = rubric.score_inventory([
        row(importance=3, credit=0.0, status="not_measured", credit_reason="never computed", n=None),
        row(name="dashboard_hits", importance=1, credit=1.0),
    ])

    assert result["score"] == pytest.approx(25.0)


def test_partial_credit_lands_between(rubric):
    result = rubric.score_inventory([row(credit=0.25, finding="small_n",
                                         credit_reason="n=3 of 412", n=3)])

    assert result["score"] == pytest.approx(25.0)


def test_unmeasurable_things_stay_in_the_denominator(rubric):
    measured_only = rubric.score_inventory([row()])
    with_gap = rubric.score_inventory([
        row(),
        row(name="recall", credit=0.0, status="unmeasurable", n=None,
            credit_reason="no gold set exists", unmeasurable_reason="no gold set exists"),
    ])

    assert measured_only["score"] == pytest.approx(100.0)
    assert with_gap["score"] < 100.0, "dropping it would read silence as success"


def test_the_by_importance_breakdown_isolates_the_ship_gates(rubric):
    result = rubric.score_inventory([
        row(importance=3, credit=0.25, finding="small_n", credit_reason="n=3", n=3),
        row(name="dashboard_hits", importance=1, credit=1.0),
    ])

    gates = result["by_importance"]["3"]
    assert gates["total"] == pytest.approx(3.0)
    assert gates["measured"] == pytest.approx(0.75)
    assert gates["share"] == pytest.approx(25.0)
    assert gates["rows"] == 1


def test_grade_bands_match_the_standard_letter_scale(rubric):
    assert rubric.grade_for(97.0) == "A+"
    assert rubric.grade_for(83.0) == "B"
    assert rubric.grade_for(59.9) == "F"
    assert rubric.grade_for(None) == rubric.UNGRADED


# --- validation -----------------------------------------------------------

def test_an_unknown_importance_is_rejected(rubric):
    with pytest.raises(rubric.InventoryError, match="importance"):
        rubric.validate([row(importance=5)], [FINDING])


def test_a_credit_off_the_ladder_is_rejected(rubric):
    with pytest.raises(rubric.InventoryError, match="credit"):
        rubric.validate([row(credit=0.9)], [FINDING])


def test_reduced_credit_must_name_a_finding_that_exists(rubric):
    with pytest.raises(rubric.InventoryError, match="finding"):
        rubric.validate([row(credit=0.5, credit_reason="feels shaky")], [FINDING])

    with pytest.raises(rubric.InventoryError, match="no finding with id"):
        rubric.validate([row(credit=0.5, finding="ghost", credit_reason="x")], [FINDING])


def test_full_credit_needs_no_finding(rubric):
    rubric.validate([row()], [])


def test_an_importance_with_no_decision_named_is_rejected(rubric):
    with pytest.raises(rubric.InventoryError, match="importance_reason"):
        rubric.validate([row(importance_reason="  ")], [])


def test_an_unmeasurable_row_must_say_why_and_carry_zero_credit(rubric):
    with pytest.raises(rubric.InventoryError, match="unmeasurable_reason"):
        rubric.validate([row(status="unmeasurable", credit=0.0, n=None)], [])

    with pytest.raises(rubric.InventoryError, match="credit 0"):
        rubric.validate([row(status="unmeasurable", credit=1.0,
                             unmeasurable_reason="no gold set")], [])


def test_a_not_measured_row_must_carry_zero_credit(rubric):
    with pytest.raises(rubric.InventoryError, match="credit 0"):
        rubric.validate([row(status="not_measured", credit=0.5, finding="small_n")], [FINDING])


def test_credit_above_zero_requires_the_n_it_was_computed_over(rubric):
    with pytest.raises(rubric.InventoryError, match="n"):
        rubric.validate([row(n=None)], [])


def test_duplicate_row_names_are_rejected(rubric):
    with pytest.raises(rubric.InventoryError, match="duplicate"):
        rubric.validate([row(), row()], [])


def test_a_row_with_no_evidence_is_rejected(rubric):
    with pytest.raises(rubric.InventoryError, match="evidence"):
        rubric.validate([row(evidence=[])], [])


def test_a_valid_inventory_passes_silently(rubric):
    rubric.validate([
        row(),
        row(name="recall", importance=2, importance_reason="informs the retrieval roadmap",
            credit=0.0, status="unmeasurable", n=None, credit_reason="no gold set exists",
            unmeasurable_reason="no gold set exists"),
        row(name="judge_agreement", importance=3, credit=0.25, finding="small_n",
            credit_reason="n=3 of 412 labelled", n=3),
    ], [FINDING])
