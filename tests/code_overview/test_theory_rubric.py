"""The theory rubric: an ordinal ladder, a panel median, and a validator.

This grade is a judgment, unlike the other two, so almost everything here is
about making the judgment disputable: a coarse ladder so variance lands on a
step rather than a spurious 73, a median that cannot leave the ladder, a spread
that is reported rather than smoothed away, and a validator that refuses a
lowered step nobody cited evidence for.
"""

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-overview" / "scripts"


@pytest.fixture
def tr(load_module):
    return load_module(SCRIPTS, "theory_rubric")


def verdict(**overrides) -> dict:
    base = {
        "schema": "theory-verdict/1",
        "unit": "billing",
        "theory": "Money moves between accounts; every move is an idempotent event.",
        "instead_of": "A mutable balance per account, rejected because replays double-charge.",
        "trivial": False,
        "trivial_reason": "",
        "dimensions": {
            "absorption": {"step": 1.0, "rationale": "all four rehearsals extend",
                           "evidence": ["src/billing/charge.py:12"]},
            "world_mapping": {"step": 1.0, "rationale": "every part maps",
                              "evidence": ["src/billing/money.py:3"]},
            "abstraction": {"step": 1.0, "rationale": "Ledger is a domain noun",
                            "evidence": ["src/billing/ledger.py:1"]},
            "justification": {"step": 1.0, "rationale": "alternatives recorded",
                              "evidence": ["src/billing/README.md:1"]},
            "honest_limits": {"step": 1.0, "rationale": "assumptions named",
                              "evidence": ["src/billing/charge.py:88"]},
        },
        "rehearsals": [
            {"requirement": "refunds in a second currency", "verdict": "extension",
             "why": "currency is a field on Money", "evidence": ["src/billing/money.py:8"]},
            {"requirement": "partial captures", "verdict": "extension",
             "why": "events already carry an amount", "evidence": ["src/billing/charge.py:40"]},
            {"requirement": "chargebacks", "verdict": "extension",
             "why": "a reversing event", "evidence": ["src/billing/ledger.py:60"]},
        ],
    }
    base.update(overrides)
    return base


def steps(**per_dimension) -> dict:
    """A verdict whose dimensions carry the given steps, evidence attached."""
    dims = {}
    for key in ("absorption", "world_mapping", "abstraction", "justification", "honest_limits"):
        step = per_dimension.get(key, 1.0)
        dims[key] = {"step": step, "rationale": "r", "evidence": ["a.py:1"]}
    return verdict(dimensions=dims)


BIG = {"files": 40, "loc": 4000}
TINY = {"files": 2, "loc": 90}


# --- ladder and arithmetic -------------------------------------------------

def test_the_ladder_is_ordinal_and_rungs_are_indices(tr):
    assert tr.LADDER == (0.0, 0.25, 0.5, 1.0)
    assert [tr.rung(s) for s in tr.LADDER] == [0, 1, 2, 3]


def test_weights_total_one_hundred(tr):
    assert sum(weight for _, _, weight, _ in tr.DIMENSIONS) == pytest.approx(100.0)


def test_absorption_carries_the_most_weight(tr):
    heaviest = max(tr.DIMENSIONS, key=lambda d: d[2])
    assert heaviest[0] == "absorption", "Naur's least-checked capability should dominate"


def test_a_unanimous_panel_of_holds_scores_one_hundred(tr):
    result = tr.score_panel([steps(), steps(), steps()], BIG)

    assert result["score"] == pytest.approx(100.0)
    assert result["grade"] == "A+"
    assert result["exempt"] is False


def test_the_median_is_taken_per_dimension_not_over_the_whole_score(tr):
    result = tr.score_panel(
        [steps(absorption=0.0), steps(absorption=1.0), steps(absorption=1.0)], BIG)

    row = next(d for d in result["dimensions"] if d["key"] == "absorption")
    assert row["step"] == 1.0, "two of three said holds"


def test_the_chosen_step_is_always_a_real_rung(tr):
    # A mean of (0.25, 0.5, 1.0) is 0.583..., which is no rung: it has no label
    # and cannot be rendered. The median is what keeps the chosen step on the
    # ladder, and averaging is the mutation this pins.
    result = tr.score_panel(
        [steps(abstraction=0.25), steps(abstraction=0.5), steps(abstraction=1.0)], BIG)

    row = next(d for d in result["dimensions"] if d["key"] == "abstraction")
    assert row["step"] in tr.LADDER
    assert row["step"] == 0.5
    assert row["step_label"] == "partial"


def test_a_shapeless_package_scores_badly_and_is_not_null(tr):
    absent = {key: 0.0 for key in
              ("absorption", "world_mapping", "abstraction", "justification", "honest_limits")}
    result = tr.score_panel([steps(**absent), steps(**absent), steps(**absent)], BIG)

    assert result["score"] == pytest.approx(0.0), "no theory is a finding, not an absence"
    assert result["grade"] == "F"
    assert result["exempt"] is False


# --- disagreement ----------------------------------------------------------

def test_two_rungs_apart_is_reported_as_disputed(tr):
    result = tr.score_panel(
        [steps(world_mapping=1.0), steps(world_mapping=0.25), steps(world_mapping=1.0)], BIG)

    row = next(d for d in result["dimensions"] if d["key"] == "world_mapping")
    assert row["spread"] == 2
    assert row["disputed"] is True
    assert "world_mapping" in result["disputed"]


def test_one_rung_apart_is_not_disputed(tr):
    result = tr.score_panel(
        [steps(world_mapping=1.0), steps(world_mapping=0.5), steps(world_mapping=1.0)], BIG)

    row = next(d for d in result["dimensions"] if d["key"] == "world_mapping")
    assert row["spread"] == 1
    assert row["disputed"] is False


def test_spread_is_counted_in_rungs_not_arithmetic_distance(tr):
    # 0.5 vs 1.0 is 0.5 apart but ONE rung; 0.0 vs 0.25 is 0.25 apart but also
    # one rung. Arithmetic distance would rank these differently for no reason.
    near_top = tr.score_panel(
        [steps(abstraction=0.5), steps(abstraction=1.0), steps(abstraction=1.0)], BIG)
    near_bottom = tr.score_panel(
        [steps(abstraction=0.0), steps(abstraction=0.25), steps(abstraction=0.25)], BIG)

    top_row = next(d for d in near_top["dimensions"] if d["key"] == "abstraction")
    bottom_row = next(d for d in near_bottom["dimensions"] if d["key"] == "abstraction")
    assert top_row["spread"] == bottom_row["spread"] == 1


def test_every_judges_step_and_rationale_survives_into_the_row(tr):
    result = tr.score_panel(
        [steps(honest_limits=0.0), steps(honest_limits=0.5), steps(honest_limits=1.0)], BIG)

    row = next(d for d in result["dimensions"] if d["key"] == "honest_limits")
    assert sorted(row["steps"]) == [0.0, 0.5, 1.0]
    assert len(row["rationales"]) == 3
    assert row["evidence"]


# --- exemption -------------------------------------------------------------

def test_exemption_needs_both_size_and_votes(tr):
    trivial = [verdict(trivial=True, trivial_reason="three helpers, no model"),
               verdict(trivial=True, trivial_reason="three helpers, no model"),
               verdict()]

    exempt, _ = tr.exemption(trivial, TINY)
    assert exempt is True

    exempt_big, _ = tr.exemption(trivial, BIG)
    assert exempt_big is False, "size alone must not be dodgeable by vote"

    one_vote = [verdict(trivial=True, trivial_reason="small"), verdict(), verdict()]
    exempt_votes, _ = tr.exemption(one_vote, TINY)
    assert exempt_votes is False, "a single judge cannot exempt a unit"


def test_an_exempt_unit_scores_null_not_zero(tr):
    trivial = [verdict(trivial=True, trivial_reason="two helpers"),
               verdict(trivial=True, trivial_reason="two helpers"),
               verdict()]

    result = tr.score_panel(trivial, TINY)

    assert result["exempt"] is True
    assert result["score"] is None
    assert result["grade"] == tr.UNGRADED
    assert "two helpers" in result["exempt_reason"]


def test_exemption_can_be_refused_outright_for_the_repo_page(tr):
    trivial = [verdict(trivial=True, trivial_reason="small"),
               verdict(trivial=True, trivial_reason="small"),
               verdict()]

    result = tr.score_panel(trivial, TINY, allow_exemption=False)

    assert result["exempt"] is False
    assert result["score"] is not None, "the system-level question is never trivial"


# --- the validator ---------------------------------------------------------

def test_an_off_ladder_step_is_rejected(tr):
    bad = verdict()
    bad["dimensions"]["absorption"]["step"] = 0.7
    with pytest.raises(tr.VerdictError, match="ladder"):
        tr.validate_verdict(bad)


def test_a_lowered_step_must_cite_evidence(tr):
    bad = verdict()
    bad["dimensions"]["abstraction"] = {"step": 0.5, "rationale": "feels thin", "evidence": []}
    with pytest.raises(tr.VerdictError, match="evidence"):
        tr.validate_verdict(bad)


def test_full_credit_needs_no_evidence(tr):
    ok = verdict()
    ok["dimensions"]["abstraction"] = {"step": 1.0, "rationale": "holds", "evidence": []}
    tr.validate_verdict(ok)


def test_a_missing_dimension_is_rejected(tr):
    bad = verdict()
    del bad["dimensions"]["justification"]
    with pytest.raises(tr.VerdictError, match="justification"):
        tr.validate_verdict(bad)


def test_a_verdict_without_a_theory_or_a_rejected_reading_is_rejected(tr):
    with pytest.raises(tr.VerdictError, match="theory"):
        tr.validate_verdict(verdict(theory="  "))
    with pytest.raises(tr.VerdictError, match="instead_of"):
        tr.validate_verdict(verdict(instead_of=""))


def test_absorption_requires_at_least_three_rehearsals(tr):
    bad = verdict(rehearsals=verdict()["rehearsals"][:2])
    with pytest.raises(tr.VerdictError, match="rehearsal"):
        tr.validate_verdict(bad)


def test_a_rehearsal_verdict_must_be_extension_or_patch(tr):
    bad = verdict()
    bad["rehearsals"][0]["verdict"] = "fine"
    with pytest.raises(tr.VerdictError, match="extension"):
        tr.validate_verdict(bad)


def test_a_trivial_vote_must_carry_a_reason(tr):
    with pytest.raises(tr.VerdictError, match="trivial_reason"):
        tr.validate_verdict(verdict(trivial=True, trivial_reason=""))


def test_the_wrong_schema_is_rejected(tr):
    with pytest.raises(tr.VerdictError, match="schema"):
        tr.validate_verdict(verdict(schema="theory-verdict/9"))


def test_a_valid_verdict_passes_silently(tr):
    tr.validate_verdict(verdict())


def test_score_panel_rejects_a_panel_of_the_wrong_size(tr):
    with pytest.raises(tr.VerdictError, match="three"):
        tr.score_panel([verdict(), verdict()], BIG)
