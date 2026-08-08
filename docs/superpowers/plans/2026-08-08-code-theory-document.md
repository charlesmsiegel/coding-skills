# Theory Document Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `code-overview` a fifth document, `theory.html`, carrying a graded judgment of how well a unit's code expresses a coherent theory in Peter Naur's sense — scored by a panel of three independent judges whose median sets the grade and whose disagreement is itself reported as a finding.

**Architecture:** `theory_rubric.py` holds the dimensions, the ordinal ladder, the median/spread arithmetic and the verdict validator — pure, no I/O, importing its letter bands from its sibling `rubric.py`. `build_theory.py` renders the four tabs from three verdict files. The panel itself is agent work, specified in `SKILL.md`: three subagents given the code and the doctrine, never each other's output. `theory-building` gains a pointer and no code.

**Tech Stack:** Python 3.11+, stdlib only. pytest driving the CLI as a subprocess.

## Global Constraints

- **Python 3.11+, stdlib only, no network calls.** Nested same-type quotes and backslashes inside f-string expressions are 3.12+ syntax and fail CI — precompute into locals.
- **Source of truth:** `docs/superpowers/specs/2026-08-08-code-theory-document-design.md`.
- **`theory_rubric.py` imports `GRADE_BANDS`, `UNGRADED`, `grade_for` from its sibling `rubric.py`.** Never copy them — both live in `code-overview/scripts/`, so the standalone-installability rule that forced the science copy does not apply.
- **`theory-building` gains no scripts.** It is doctrine, not tooling.
- **The ladder is ordinal.** `LADDER = (0.0, 0.25, 0.5, 1.0)` at rung indices 0–3. Disagreement is `max(rung) − min(rung)`, never arithmetic distance — the steps are deliberately unevenly spaced.
- **The median must land on the ladder.** Use `statistics.median_low`, never `statistics.median`, which averages on even-length input and would invent an off-ladder step.
- **No unattributed step.** Any step below `1.0` must cite evidence; the validator rejects one that does not.
- **Exemption needs both gates**: computed size (≤3 files **and** ≤200 LOC) **and** ≥2 of 3 judges voting trivial with a reason.
- **A unit big enough to need a theory and lacking one scores badly, never `null`.** `null` is reserved for exemption.
- **The floor does not apply at repo scope** — the repo page is always graded.
- **CI gates:** `python -m ruff check .`, `python tools/validate_skills.py`, `python -m pytest -q` (the **whole** suite; 11 failures are pre-existing — 3 `tests/code_doctor/test_common.py` locale, 3 `tests/code_overview` Windows-path, 5 `tests/science_investigation`).

---

### Task 1: `theory_rubric.py` — the ladder, the panel arithmetic, the validator

**Files:**
- Create: `skills/code-overview/scripts/theory_rubric.py`
- Test: `tests/code_overview/test_theory_rubric.py`

**Interfaces:**
- Consumes: `rubric.GRADE_BANDS`, `rubric.UNGRADED`, `rubric.grade_for` (sibling module in the same `scripts/` directory).
- Produces:
  - `VERDICT_SCHEMA = "theory-verdict/1"`, `DOCUMENT_SCHEMA = "theory/1"`
  - `DIMENSIONS: tuple[tuple[str, str, float, str], ...]` — `(key, label, weight, question)`, weights totalling 100
  - `DIMENSION_KEYS`, `DIMENSION_LABELS`, `DIMENSION_WEIGHTS`, `DIMENSION_QUESTIONS`
  - `LADDER = (0.0, 0.25, 0.5, 1.0)`, `LADDER_LABELS: dict[float, str]`
  - `PANEL_SIZE = 3`, `DISAGREEMENT_RUNGS = 2`, `TRIVIAL_MAX_FILES = 3`, `TRIVIAL_MAX_LOC = 200`, `TRIVIAL_VOTES = 2`
  - `class VerdictError(ValueError)`
  - `rung(step: float) -> int`
  - `validate_verdict(verdict: dict) -> None`
  - `exemption(verdicts: list[dict], size: dict) -> tuple[bool, str]`
  - `score_panel(verdicts: list[dict], size: dict, *, allow_exemption: bool = True) -> dict`

- [ ] **Step 1: Write the failing test**

Create `tests/code_overview/test_theory_rubric.py`:

```python
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


def test_the_median_never_leaves_the_ladder(tr):
    # An averaging median of (0.25, 0.5) would invent 0.375, which is no rung at
    # all and would render as a step the ladder does not define.
    result = tr.score_panel(
        [steps(abstraction=0.25), steps(abstraction=0.5),
         steps(abstraction=0.5), steps(abstraction=1.0)], BIG)

    row = next(d for d in result["dimensions"] if d["key"] == "abstraction")
    assert row["step"] in tr.LADDER


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/code_overview/test_theory_rubric.py -q`
Expected: every test errors at fixture setup — `ModuleNotFoundError: No module named 'theory_rubric'`.

- [ ] **Step 3: Write the rubric**

Create `skills/code-overview/scripts/theory_rubric.py`:

```python
#!/usr/bin/env python3
"""The theory rubric: what a Naur-sense grade divides, and what it refuses.

The other two grades in this document set rest on something outside the grader
— a detector's findings, an inventory of measurable things with citations. This
one is a judgment, and `theory-building` is explicit that a model auditing its
own abstractions is partly circular. Nothing here closes that. What this file
does is make the judgment *disputable*:

- a coarse four-rung ladder, so variance lands on a step choice rather than on
  a spurious 73 that means nothing more than a 68;
- a median taken per dimension from a panel of independent judges, so one
  reader's idiosyncrasy does not become the grade;
- a spread reported rather than smoothed away, because three careful readers
  disagreeing about what code models is a fact about the code;
- a validator that refuses a lowered step nobody cited evidence for.

Pure data and arithmetic — no I/O — so a grade can be recomputed and tested
without building a document.
"""

from __future__ import annotations

import statistics

# Imported, not copied: this module ships inside code-overview alongside
# rubric.py, so the standalone-installability rule that forces a duplicate of
# these bands into science-investigation does not apply here. A copy would be a
# fourth thing to keep pinned for no benefit.
from rubric import GRADE_BANDS, UNGRADED, grade_for  # noqa: F401  (re-exported)

VERDICT_SCHEMA = "theory-verdict/1"
DOCUMENT_SCHEMA = "theory/1"

# key, label, weight, the question the judge answers.
#
# Absorption carries the most weight because Naur singles it out as the
# capability nobody checks — "someone who holds the theory is *already*
# prepared for the demands that will arrive" — and because a plausible-looking
# codebase fails it more often than it fails the others.
DIMENSIONS: tuple[tuple[str, str, float, str], ...] = (
    ("absorption", "Absorption", 30.0,
     "Take 3-5 plausible next requirements. Does each land as a natural "
     "extension, or need a patch bolted alongside?"),
    ("world_mapping", "World-mapping", 25.0,
     "Can each part be mapped to an aspect of the problem, and each aspect "
     "located in the code — including where the model stops?"),
    ("abstraction", "Abstraction", 20.0,
     "Do the abstractions carry domain names, make unwritten cases "
     "expressible, and reduce what must be held in mind?"),
    ("justification", "Justification", 15.0,
     "Is it recoverable why each part is what it is, and what was rejected?"),
    ("honest_limits", "Honest limits", 10.0,
     "Are assumptions no test pins down named? Is duplication honest rather "
     "than papered over? Is there reinvention where something existing served?"),
)

DIMENSION_KEYS = tuple(key for key, _, _, _ in DIMENSIONS)
DIMENSION_LABELS = {key: label for key, label, _, _ in DIMENSIONS}
DIMENSION_WEIGHTS = {key: weight for key, _, weight, _ in DIMENSIONS}
DIMENSION_QUESTIONS = {key: question for key, _, _, question in DIMENSIONS}

# Ordinal. The index is the rung; the value is what the rung is worth. The two
# are deliberately different, and code that conflates them will measure
# disagreement wrongly — see `spread_rungs`.
LADDER: tuple[float, ...] = (0.0, 0.25, 0.5, 1.0)
LADDER_LABELS: dict[float, str] = {
    1.0: "holds",
    0.5: "partial",
    0.25: "strained",
    0.0: "absent",
}

PANEL_SIZE = 3
DISAGREEMENT_RUNGS = 2

# Both gates, because either alone is gameable: size alone exempts a dense
# 150-line parser, and a vote alone lets a bad grade be talked away.
TRIVIAL_MAX_FILES = 3
TRIVIAL_MAX_LOC = 200
TRIVIAL_VOTES = 2

REHEARSAL_VERDICTS = ("extension", "patch")
MIN_REHEARSALS = 3


class VerdictError(ValueError):
    """A judge's verdict asserts something its own fields do not support."""


def rung(step: float) -> int:
    """The ladder index of a step. Raises if the step is not on the ladder."""
    return LADDER.index(step)


def _fail(where: str, message: str) -> None:
    raise VerdictError(f"{where}: {message}")


def validate_verdict(verdict: dict) -> None:
    """Refuse a verdict that cannot support the score it would produce."""
    unit = str(verdict.get("unit") or "<unnamed>")

    if verdict.get("schema") != VERDICT_SCHEMA:
        _fail(unit, f"schema must be {VERDICT_SCHEMA!r}, got {verdict.get('schema')!r}")

    if not str(verdict.get("theory") or "").strip():
        _fail(unit, "theory must state what this models; if it cannot be stated, "
                    "there is no theory and the dimensions should say so")
    if not str(verdict.get("instead_of") or "").strip():
        _fail(unit, "instead_of must name the other coherent reading and why it was "
                    "rejected — the reader cannot recover a discarded reading from the code")

    if verdict.get("trivial") and not str(verdict.get("trivial_reason") or "").strip():
        _fail(unit, "a trivial vote must carry a trivial_reason")

    dimensions = verdict.get("dimensions")
    if not isinstance(dimensions, dict):
        _fail(unit, "dimensions must be an object")

    for key in DIMENSION_KEYS:
        entry = dimensions.get(key)
        if not isinstance(entry, dict):
            _fail(unit, f"{key} is missing from dimensions")
        step = entry.get("step")
        if step not in LADDER:
            _fail(unit, f"{key}: step must be on the ladder {list(LADDER)}, got {step!r}")
        if not str(entry.get("rationale") or "").strip():
            _fail(unit, f"{key}: rationale must say why this step and not the one above")
        cited = [c for c in (entry.get("evidence") or []) if str(c).strip()]
        if step < 1.0 and not cited:
            _fail(unit, f"{key}: a step below 1.0 must cite the evidence that lowered it — "
                        "an unattributed deduction cannot be argued with")

    rehearsals = verdict.get("rehearsals") or []
    if len(rehearsals) < MIN_REHEARSALS:
        _fail(unit, f"absorption needs at least {MIN_REHEARSALS} rehearsal(s); a theory "
                    "that only accounts for what was already built is a description")
    for item in rehearsals:
        if not isinstance(item, dict):
            _fail(unit, "each rehearsal must be an object")
        if not str(item.get("requirement") or "").strip():
            _fail(unit, "each rehearsal must name the requirement it rehearses")
        if item.get("verdict") not in REHEARSAL_VERDICTS:
            _fail(unit, f"rehearsal verdict must be one of {list(REHEARSAL_VERDICTS)}, "
                        f"got {item.get('verdict')!r}")
        if not str(item.get("why") or "").strip():
            _fail(unit, "each rehearsal must say why it extends or patches")


def spread_rungs(steps: list[float]) -> int:
    """How far apart the judges are, in rungs.

    Rungs, not arithmetic distance. The ladder is unevenly spaced on purpose,
    so `0.5` beside `1.0` and `0.0` beside `0.25` are both one rung of
    disagreement even though one gap is twice the other. Measuring the values
    would make the same disagreement register differently depending on where on
    the ladder it happened to sit.
    """
    indices = [rung(step) for step in steps]
    return max(indices) - min(indices)


def exemption(verdicts: list[dict], size: dict) -> tuple[bool, str]:
    """Whether this unit is too small to warrant a theory, and why.

    Both gates must hold. A unit that clears the size test but that the panel
    thinks is substantial is not exempt, and neither is a large unit the panel
    would rather not grade.
    """
    votes = [v for v in verdicts if v.get("trivial")]
    small = (size.get("files", 0) <= TRIVIAL_MAX_FILES
             and size.get("loc", 0) <= TRIVIAL_MAX_LOC)
    if not small or len(votes) < TRIVIAL_VOTES:
        return False, ""
    reasons = "; ".join(sorted({str(v.get("trivial_reason") or "").strip() for v in votes}))
    return True, (f"{size.get('files', 0)} file(s), {size.get('loc', 0)} line(s); "
                  f"{len(votes)} of {len(verdicts)} judges called it trivial — {reasons}")


def score_panel(verdicts: list[dict], size: dict, *,
                allow_exemption: bool = True) -> dict:
    """Median per dimension, weighted into a letter, with the spread kept."""
    if len(verdicts) != PANEL_SIZE:
        _fail("panel", f"a panel is {PANEL_SIZE} independent judges ('three'), "
                       f"got {len(verdicts)}")
    for verdict in verdicts:
        validate_verdict(verdict)

    exempt, reason = exemption(verdicts, size) if allow_exemption else (False, "")

    rows = []
    disputed = []
    weighted = 0.0
    for key in DIMENSION_KEYS:
        entries = [v["dimensions"][key] for v in verdicts]
        steps = [entry["step"] for entry in entries]
        # median_low, never median: the mean of two adjacent rungs is not a rung,
        # and a step the ladder does not define cannot be labelled or rendered.
        chosen = statistics.median_low(steps)
        spread = spread_rungs(steps)
        is_disputed = spread >= DISAGREEMENT_RUNGS
        if is_disputed:
            disputed.append(key)
        weighted += DIMENSION_WEIGHTS[key] * chosen
        rows.append({
            "key": key,
            "label": DIMENSION_LABELS[key],
            "question": DIMENSION_QUESTIONS[key],
            "weight": DIMENSION_WEIGHTS[key],
            "step": chosen,
            "step_label": LADDER_LABELS[chosen],
            "rung": rung(chosen),
            "spread": spread,
            "disputed": is_disputed,
            "steps": steps,
            "rationales": [str(entry.get("rationale") or "") for entry in entries],
            "evidence": sorted({str(c) for entry in entries
                                for c in (entry.get("evidence") or []) if str(c).strip()}),
        })

    total_weight = sum(DIMENSION_WEIGHTS[key] for key in DIMENSION_KEYS)
    score = None if exempt else 100.0 * weighted / total_weight

    return {
        "score": score,
        "grade": grade_for(score),
        "exempt": exempt,
        "exempt_reason": reason,
        "dimensions": rows,
        "disputed": disputed,
        "panel_size": len(verdicts),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/code_overview/test_theory_rubric.py -q`
Expected: PASS — every test in the file. Do not chase a specific count; if the number differs from what you expect, count the test functions rather than assuming a test is missing.

- [ ] **Step 5: Verify the repo gates**

```bash
python -m ruff check .
python skills/python-code-doctor/scripts/find_exception_issues.py skills/code-overview/scripts --format json
python skills/python-code-doctor/scripts/find_mutation_hazards.py skills/code-overview/scripts --format json
```

Expected: ruff prints `All checks passed!`; both detectors print `[]`.

- [ ] **Step 6: Commit**

```bash
git add skills/code-overview/scripts/theory_rubric.py tests/code_overview/test_theory_rubric.py
git commit -m "code-overview: the theory rubric — ordinal ladder, panel median, validator

This grade is a judgment where the other two are not, so the arithmetic spends
its effort on making the judgment disputable rather than authoritative. The
ladder is coarse so variance lands on a step choice. The median is median_low
so it cannot invent a rung the ladder does not define. Disagreement is measured
in rungs rather than values, because the steps are unevenly spaced on purpose
and arithmetic distance would rank the same disagreement differently depending
on where it sat. And a step below full credit must cite what lowered it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `build_theory.py` and the tab scaffold

**Files:**
- Create: `skills/code-overview/assets/theory-body.html`
- Create: `skills/code-overview/scripts/build_theory.py`
- Test: `tests/code_overview/test_build_theory.py`

**Interfaces:**
- Consumes: `theory_rubric` (Task 1); `common.esc`, `common.json_block`, `common.render`, `common.read_asset`, `common.measure`, `common.warn`, `common.CODE_EXTENSIONS`; `build_health.grade_class` and `build_health.panels` are **not** imported — copy the small `panels` helper, as `build_measurement.py` does, since each builder owns its own tab assembly.
- Produces:
  - `read_verdicts(paths: list[Path]) -> list[dict]` — parse and validate; raises `theory_rubric.VerdictError`
  - `render_grade_tab`, `render_theory_tab`, `render_dimensions_tab`, `render_disagreement_tab`
  - `panels(fragments: list[str]) -> tuple[str, str]`
  - `main(argv=None) -> int` — CLI: `--out FILE --verdict FILE (×3) --name NAME [--repo DIR] [--root-dir DIR ...] [--subtitle TEXT] [--intro-file HTML] [--template FILE] [--body FILE] [--commit SHA] [--generated DATE] [--model NAME]`. Exit 2 on an invalid verdict, writing no document.
  - Metadata block `<script type="application/json" id="theory-meta">`, schema `theory/1`.

- [ ] **Step 1: Write the failing test**

Create `tests/code_overview/test_build_theory.py`:

```python
"""The theory document.

Every tab is rendered from the verdicts, so the letter and the rows under it
cannot disagree. The assertions here are panel-scoped on purpose: the verdicts
are embedded in the page's metadata block, so a whole-document substring check
would match the JSON rather than the rendering — the failure shape that has bitten
this project repeatedly.
"""

import json
import re
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "code-overview"
          / "scripts" / "build_theory.py")


def panel_of(page: str, tab_id: str) -> str:
    """Just that tab's own markup, cut at its own closing tag."""
    marker = f'id="{tab_id}"'
    assert marker in page, f"no panel {tab_id}"
    chunk = page.split(marker, 1)[1]
    return chunk.split("\n</section>")[0]


def meta_of(page: Path) -> dict:
    match = re.search(r'id="theory-meta">(.*?)</script>',
                      page.read_text(encoding="utf-8"), re.S)
    assert match, "the page carries no theory-meta block"
    return json.loads(match.group(1).replace("<\\/", "</"))


def verdict_file(tmp_path, index: int, **overrides) -> Path:
    dims = {}
    for key in ("absorption", "world_mapping", "abstraction", "justification", "honest_limits"):
        dims[key] = {"step": overrides.get(key, 1.0),
                     "rationale": f"judge {index} on {key}",
                     "evidence": [f"src/billing/{key}.py:{index + 1}"]}
    payload = {
        "schema": "theory-verdict/1",
        "unit": "billing",
        "theory": "Money moves between accounts; every move is an idempotent event.",
        "instead_of": "A mutable balance per account, rejected because replays double-charge.",
        "trivial": overrides.get("trivial", False),
        "trivial_reason": overrides.get("trivial_reason", ""),
        "dimensions": dims,
        "rehearsals": [
            {"requirement": "refunds in a second currency", "verdict": "extension",
             "why": "currency is a field on Money", "evidence": ["src/billing/money.py:8"]},
            {"requirement": "partial captures", "verdict": "patch",
             "why": "capture assumes the full amount", "evidence": ["src/billing/charge.py:40"]},
            {"requirement": "chargebacks", "verdict": "extension",
             "why": "a reversing event", "evidence": ["src/billing/ledger.py:60"]},
        ],
    }
    path = tmp_path / f"verdict-{index}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def repo_with_code(repo):
    for i in range(12):
        repo.write(f"src/billing/mod{i}.py", "def f():\n    return 1\n" * 20)
    repo.commit()
    return repo


def build(repo, run_script, tmp_path, *files, extra=(), expect_rc=0) -> Path:
    out = tmp_path / "theory.html"
    args = []
    for path in files:
        args += ["--verdict", str(path)]
    run_script(SCRIPT, "--out", out, "--name", "billing", "--repo", repo.path,
               "--root-dir", "src/billing", *args, *extra, expect_rc=expect_rc)
    return out


def three(tmp_path, **overrides) -> tuple:
    return tuple(verdict_file(tmp_path, i, **overrides) for i in range(3))


# --- the grade -------------------------------------------------------------

def test_a_unanimous_panel_renders_its_grade(repo_with_code, run_script, tmp_path):
    page = build(repo_with_code, run_script, tmp_path, *three(tmp_path))

    meta = meta_of(page)
    assert meta["score"] == pytest.approx(100.0)
    assert meta["grade"] == "A+"
    assert meta["schema"] == "theory/1"
    assert meta["scope"] == "package"
    assert meta["panel_size"] == 3


def test_the_visible_grade_card_carries_the_letter(repo_with_code, run_script, tmp_path):
    text = build(repo_with_code, run_script, tmp_path,
                 *three(tmp_path)).read_text(encoding="utf-8")

    card = re.search(r'<div class="letter">([^<]*)</div>', panel_of(text, "tab-grade"))
    assert card and card.group(1).strip() == "A+"


def test_all_four_tabs_render(repo_with_code, run_script, tmp_path):
    text = build(repo_with_code, run_script, tmp_path,
                 *three(tmp_path)).read_text(encoding="utf-8")

    assert text.count('<button role="tab"') == 4
    for title in ("Grade", "Theory", "Dimensions", "Disagreement"):
        assert f">{title}<" in text


# --- the theory itself -----------------------------------------------------

def test_the_theory_statement_and_the_rejected_reading_render(repo_with_code, run_script,
                                                              tmp_path):
    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *three(tmp_path)).read_text(encoding="utf-8"), "tab-theory")

    assert "idempotent event" in panel
    assert "replays double-charge" in panel, "the rejected reading is not recoverable from code"


def test_the_rehearsals_render_verbatim_with_their_verdicts(repo_with_code, run_script,
                                                            tmp_path):
    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *three(tmp_path)).read_text(encoding="utf-8"), "tab-theory")

    assert "partial captures" in panel
    assert "patch" in panel
    assert "capture assumes the full amount" in panel


# --- dimensions ------------------------------------------------------------

def test_every_dimension_renders_with_its_step_and_evidence(repo_with_code, run_script,
                                                            tmp_path):
    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *three(tmp_path)).read_text(encoding="utf-8"), "tab-dimensions")

    for label in ("Absorption", "World-mapping", "Abstraction", "Justification",
                  "Honest limits"):
        assert label in panel
    assert "holds" in panel
    assert "src/billing/absorption.py:1" in panel


# --- disagreement ----------------------------------------------------------

def test_a_disputed_dimension_is_named_on_the_disagreement_tab(repo_with_code, run_script,
                                                               tmp_path):
    files = (verdict_file(tmp_path, 0, abstraction=1.0),
             verdict_file(tmp_path, 1, abstraction=0.25),
             verdict_file(tmp_path, 2, abstraction=1.0))

    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *files).read_text(encoding="utf-8"), "tab-disagreement")

    assert "Abstraction" in panel
    assert "could not agree" in panel.lower()


def test_an_undisputed_panel_says_so_rather_than_showing_an_empty_tab(repo_with_code,
                                                                      run_script, tmp_path):
    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *three(tmp_path)).read_text(encoding="utf-8"), "tab-disagreement")

    assert "agreed" in panel.lower()


def test_one_rung_of_disagreement_does_not_raise_the_banner(repo_with_code, run_script,
                                                            tmp_path):
    files = (verdict_file(tmp_path, 0, abstraction=1.0),
             verdict_file(tmp_path, 1, abstraction=0.5),
             verdict_file(tmp_path, 2, abstraction=1.0))

    meta = meta_of(build(repo_with_code, run_script, tmp_path, *files))
    assert meta["disputed"] == []


# --- honesty ---------------------------------------------------------------

def test_the_page_says_the_grade_is_a_reading_not_a_measurement(repo_with_code, run_script,
                                                                tmp_path):
    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *three(tmp_path)).read_text(encoding="utf-8"), "tab-grade")

    lowered = panel.lower()
    assert "reading" in lowered
    assert "circular" in lowered, "the panel narrows the circularity; it does not close it"


def test_the_model_and_panel_size_are_stamped(repo_with_code, run_script, tmp_path):
    page = build(repo_with_code, run_script, tmp_path, *three(tmp_path),
                 extra=("--model", "claude-opus-5"))

    meta = meta_of(page)
    assert meta["model"] == "claude-opus-5"
    assert meta["panel_size"] == 3


def test_an_exempt_unit_scores_null_and_says_why(repo, run_script, tmp_path):
    repo.write("src/tiny/a.py", "x = 1\n")
    repo.write("src/tiny/b.py", "y = 2\n")
    repo.commit()
    files = (verdict_file(tmp_path, 0, trivial=True, trivial_reason="two constants"),
             verdict_file(tmp_path, 1, trivial=True, trivial_reason="two constants"),
             verdict_file(tmp_path, 2))
    out = tmp_path / "theory.html"
    args = []
    for path in files:
        args += ["--verdict", str(path)]
    run_script(SCRIPT, "--out", out, "--name", "tiny", "--repo", repo.path,
               "--root-dir", "src/tiny", *args)

    meta = meta_of(out)
    assert meta["score"] is None
    assert meta["grade"] == "—"
    assert "too small" in panel_of(out.read_text(encoding="utf-8"), "tab-grade").lower()


def test_an_invalid_verdict_writes_no_document(repo_with_code, run_script, tmp_path):
    bad = tmp_path / "bad.json"
    payload = json.loads(verdict_file(tmp_path, 0).read_text(encoding="utf-8"))
    payload["dimensions"]["abstraction"] = {"step": 0.5, "rationale": "thin", "evidence": []}
    bad.write_text(json.dumps(payload), encoding="utf-8")

    out = tmp_path / "theory.html"
    result = run_script(SCRIPT, "--out", out, "--name", "billing", "--repo", repo_with_code.path,
                        "--root-dir", "src/billing",
                        "--verdict", str(verdict_file(tmp_path, 1)),
                        "--verdict", str(verdict_file(tmp_path, 2)),
                        "--verdict", str(bad), expect_rc=2)

    assert not out.exists(), "a page with an unarguable grade is worse than none"
    assert "evidence" in result.stderr


def test_two_verdicts_are_refused(repo_with_code, run_script, tmp_path):
    out = tmp_path / "theory.html"
    files = three(tmp_path)
    args = []
    for path in files[:2]:
        args += ["--verdict", str(path)]
    result = run_script(SCRIPT, "--out", out, "--name", "billing", "--repo", repo_with_code.path,
                        "--root-dir", "src/billing", *args, expect_rc=2)

    assert "three" in result.stderr
    assert not out.exists()


def test_script_closing_tags_in_a_verdict_cannot_end_the_metadata_block(repo_with_code,
                                                                        run_script, tmp_path):
    files = list(three(tmp_path))
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    payload["theory"] = "</script><script>alert(1)</script>"
    files[0].write_text(json.dumps(payload), encoding="utf-8")

    page = build(repo_with_code, run_script, tmp_path, *files)

    assert "</script><script>alert(1)" not in page.read_text(encoding="utf-8")
    assert meta_of(page)["theory"].startswith("</script>")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/code_overview/test_build_theory.py -q`
Expected: FAIL — `build_theory.py` does not exist.

- [ ] **Step 3: Write the tab scaffold**

Create `skills/code-overview/assets/theory-body.html`:

```html
<!-- Tab scaffold for theory.html, filled by build_theory.py. Four panels: the
     grade and what it is worth, the theory itself (the actual deliverable — the
     artifact Naur says gets lost when only the diff is handed over), the
     dimension rows the grade was computed from, and the places three careful
     readers could not agree. -->
<!--TAB_GRADE-->
<!--TAB_THEORY-->
<!--TAB_DIMENSIONS-->
<!--TAB_DISAGREEMENT-->
```

- [ ] **Step 4: Write the builder**

Create `skills/code-overview/scripts/build_theory.py`:

```python
#!/usr/bin/env python3
"""Render a panel's verdicts into theory.html.

Every tab comes from the verdict files, so the letter and the rows under it
cannot disagree — there is no path by which a grade is typed onto the page by
hand.

The Theory tab is the one that matters most. The grade is a byproduct of having
tried to state the theory; the statement itself is what a next reader actually
needs, and it is precisely the artifact that gets lost when only the code is
handed over. A reader who ignores the letter and reads that tab has still got
the value of this document.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import date
from pathlib import Path

import theory_rubric as tr
from common import CODE_EXTENSIONS, measure, warn

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def json_block(data) -> str:
    """`</` is escaped so a `</script>` inside a string cannot end the block."""
    return json.dumps(data, separators=(",", ":"), sort_keys=False).replace("</", "<\\/")


def fill(template: str, slots: dict[str, str]) -> str:
    for key, value in slots.items():
        template = template.replace(f"<!--{key}-->", value)
    return template


def grade_class(grade: str) -> str:
    letter = (grade or "").strip()[:1].lower()
    return f"g-{letter}" if letter in "abcdf" else "g-none"


def read_verdicts(paths: list[Path]) -> list[dict]:
    verdicts = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise tr.VerdictError(f"{path}: a verdict must be a JSON object")
        tr.validate_verdict(data)
        verdicts.append(data)
    return verdicts


def panels(fragments: list[str]) -> tuple[str, str]:
    """Turn `<!-- tab: Title -->` fragments into code-visualization's markup."""
    nav, sections = [], []
    for fragment in fragments:
        header, _, body = fragment.partition("\n")
        title = header.removeprefix("<!-- tab:").removesuffix("-->").strip()
        tab_id = "tab-" + title.lower().replace(" ", "-")
        selected = "true" if not nav else "false"
        active = " active" if not sections else ""
        nav.append(f'<button role="tab" data-tab="{tab_id}" aria-selected="{selected}" '
                   f'aria-controls="{tab_id}">{esc(title)}</button>')
        sections.append(f'<section class="panel{active}" id="{tab_id}" role="tabpanel">\n'
                        f"{body}\n</section>")
    return "\n".join(nav), "\n".join(sections)


# --------------------------------------------------------------------------
# tabs
# --------------------------------------------------------------------------

def render_grade_tab(scored: dict, intro: str, size: dict) -> str:
    score = scored["score"]
    shown = "—" if score is None else f"{score:.1f}"

    if scored["exempt"]:
        headline = ('<div class="callout"><strong>Too small to warrant a theory.</strong> '
                    f"{esc(scored['exempt_reason'])}. Scored null — not zero, and not a "
                    "pass. Applying these gates to a handful of helpers is its own kind "
                    "of failure.</div>")
    else:
        headline = ""

    disputed = scored["disputed"]
    if disputed:
        names = ", ".join(esc(tr.DIMENSION_LABELS[key]) for key in disputed)
        dispute_note = ('<div class="callout warn"><strong>The panel disagreed on: </strong>'
                        f"{names}. Three careful readers reached different conclusions about "
                        "what this code models, which is a fact about the code rather than "
                        "about the readers. See the Disagreement tab.</div>")
    else:
        dispute_note = ""

    card = (
        f'<section class="gradecard {grade_class(scored["grade"])}">'
        f'<div><div class="letter">{esc(scored["grade"])}</div>'
        f'<div class="score">{shown} / 100</div></div>'
        '<div class="what"><h2>Theory</h2>'
        '<p class="dim">How well this unit&#39;s code expresses a coherent theory of the '
        "problem it solves, in Peter Naur&#39;s sense: can the model be mapped to the "
        "world, justified, and can it absorb the next requirement.</p>"
        "</div></section>"
    )

    return (
        '<!-- tab: Grade -->\n'
        + card + headline + dispute_note + intro +
        '<div class="callout warn"><strong>This is a reading, not a measurement.</strong> '
        "The other grades in this document set rest on something outside the grader — a "
        "detector's findings, an inventory with citations. This one is a judgment, and a "
        "model auditing abstractions is partly <em>circular</em>: the weakness that "
        "produces repetition-with-variants also evaluates whether the repetition was "
        "warranted. Three independent judges narrow that; they do not close it. Compare "
        "the rows and the evidence, not this letter — and letters from different models "
        "are not comparable at all.</div>"
        f'<div class="kpis"><div class="kpi accent"><div class="n">{scored["panel_size"]}</div>'
        '<div class="l">independent judges</div></div>'
        f'<div class="kpi"><div class="n">{size.get("files", 0)}</div>'
        '<div class="l">files</div></div>'
        f'<div class="kpi"><div class="n">{size.get("loc", 0)}</div>'
        '<div class="l">lines</div></div></div>'
    )


def render_theory_tab(verdicts: list[dict]) -> str:
    parts = ["<!-- tab: Theory -->\n",
             "<p>The theory is the deliverable; the grade is a byproduct of having tried "
             "to state it. Each judge wrote independently — where they describe the same "
             "system differently, that difference is the finding.</p>"]

    for index, verdict in enumerate(verdicts, start=1):
        rehearsals = []
        for item in verdict.get("rehearsals") or []:
            badge = "good" if item.get("verdict") == "extension" else "warn"
            citations = " ".join(f'<code class="floc">{esc(c)}</code>'
                                 for c in item.get("evidence") or [])
            rehearsals.append(
                f'<tr><td>{esc(item.get("requirement"))}</td>'
                f'<td><span class="badge {badge}">{esc(item.get("verdict"))}</span></td>'
                f'<td>{esc(item.get("why"))}{"<br>" + citations if citations else ""}</td></tr>'
            )
        parts.append(
            f'<div class="card"><h3>Judge {index}</h3>'
            f'<p><strong>Theory: </strong>{esc(verdict.get("theory"))}</p>'
            f'<p class="dim"><strong>Instead of: </strong>{esc(verdict.get("instead_of"))}</p>'
            '<div class="tbl-wrap"><table><thead><tr><th>Plausible next requirement</th>'
            "<th>Lands as</th><th>Why</th></tr></thead><tbody>"
            + "".join(rehearsals) + "</tbody></table></div></div>"
        )
    return "".join(parts)


def render_dimensions_tab(scored: dict) -> str:
    rows = []
    for row in scored["dimensions"]:
        spread = (f'<span class="badge bad">{row["spread"]} rungs apart</span>'
                  if row["disputed"] else f'{row["spread"]} rung(s)')
        judges = ", ".join(tr.LADDER_LABELS[step] for step in row["steps"])
        citations = "".join(f'<code class="floc">{esc(c)}</code><br>'
                            for c in row["evidence"])
        why = "<br>".join(esc(text) for text in row["rationales"] if text)
        rows.append(
            f'<tr><td><strong>{esc(row["label"])}</strong>'
            f'<br><span class="faint">{esc(row["question"])}</span></td>'
            f'<td class="num">{row["weight"]:.0f}</td>'
            f'<td>{esc(row["step_label"])}<br><span class="faint">{esc(judges)}</span></td>'
            f"<td>{spread}</td>"
            f"<td>{why}</td>"
            f"<td>{citations}</td></tr>"
        )
    return (
        '<!-- tab: Dimensions -->\n'
        "<p>The rows the letter was computed from. A step below <em>holds</em> must cite "
        "the evidence that lowered it — dispute the rows, not the grade.</p>"
        '<div class="tbl-wrap"><table><thead><tr><th>Dimension</th>'
        '<th class="num">Weight</th><th>Median step</th><th>Spread</th>'
        "<th>Why</th><th>Evidence</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></div>"
    )


def render_disagreement_tab(scored: dict) -> str:
    disputed = [row for row in scored["dimensions"] if row["disputed"]]
    if not disputed:
        return ('<!-- tab: Disagreement -->\n<p class="dim">The three judges agreed within '
                "one rung on every dimension. That is weak evidence the code reads "
                "consistently — it is not evidence the grade is right.</p>")

    cards = []
    for row in disputed:
        readings = "".join(
            f"<li><strong>{esc(tr.LADDER_LABELS[step])}</strong> — {esc(why)}</li>"
            for step, why in zip(row["steps"], row["rationales"])
        )
        cards.append(
            f'<div class="card"><h3>{esc(row["label"])}</h3>'
            f'<p class="dim">{row["spread"]} rungs apart. The median '
            f"({esc(row['step_label'])}) is what scored; these are the readings behind "
            f"it.</p><ul>{readings}</ul></div>"
        )
    return (
        '<!-- tab: Disagreement -->\n'
        "<p>Three careful readers <strong>could not agree</strong> on these dimensions. "
        "Taking the median alone would hide the most interesting thing the panel learned: "
        "ambiguity about what the code models is a property of the code.</p>"
        + "".join(cards)
    )


def build_metadata(scored: dict, verdicts: list[dict], size: dict, args) -> dict:
    return {
        "schema": tr.DOCUMENT_SCHEMA,
        "scope": "repository" if args.root else "package",
        "package": args.name,
        "generated": args.generated or date.today().isoformat(),
        "commit": args.commit or "",
        "model": args.model or "",
        "panel_size": scored["panel_size"],
        "score": scored["score"],
        "grade": scored["grade"],
        "exempt": scored["exempt"],
        "exempt_reason": scored["exempt_reason"],
        "size": size,
        "theory": verdicts[0].get("theory", ""),
        "dimensions": scored["dimensions"],
        "disputed": scored["disputed"],
        "verdicts": verdicts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a panel of theory verdicts into theory.html.")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--verdict", action="append", default=[], dest="verdicts",
                        type=Path, metavar="FILE")
    parser.add_argument("--name", required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--root-dir", action="append", default=[], dest="root_dirs")
    parser.add_argument("--root", action="store_true")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--intro-file", type=Path, default=None)
    parser.add_argument("--commit", default="")
    parser.add_argument("--generated", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--template", type=Path, default=ASSETS / "template.html")
    parser.add_argument("--body", type=Path, default=ASSETS / "theory-body.html")
    args = parser.parse_args(argv)

    try:
        verdicts = read_verdicts(args.verdicts)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except tr.VerdictError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    roots = [args.repo / part for part in (args.root_dirs or ["."])]
    size = measure(roots, CODE_EXTENSIONS)

    try:
        # The floor is never offered at repo scope: a repository of individually
        # trivial packages still has a system-level question worth asking.
        scored = tr.score_panel(verdicts, size, allow_exemption=not args.root)
    except tr.VerdictError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    intro = ""
    if args.intro_file:
        try:
            intro = args.intro_file.read_text(encoding="utf-8")
        except OSError as exc:
            warn(f"{args.intro_file}: {exc}")

    body = fill(args.body.read_text(encoding="utf-8"), {
        "TAB_GRADE": render_grade_tab(scored, intro, size),
        "TAB_THEORY": render_theory_tab(verdicts),
        "TAB_DIMENSIONS": render_dimensions_tab(scored),
        "TAB_DISAGREEMENT": render_disagreement_tab(scored),
    })
    # [1:] drops everything before the first marker — the scaffold's leading
    # comment is not a tab, and treating it as one makes it a bogus first panel
    # that renders active and hides the grade card.
    fragments = [f"<!-- tab:{part}" for part in body.split("<!-- tab:")[1:] if part.strip()]
    nav, sections = panels(fragments)

    meta = build_metadata(scored, verdicts, size, args)
    sections += (f'\n<script type="application/json" id="theory-meta">'
                 f"{json_block(meta)}</script>")

    scope = "repository" if args.root else "package"
    page = fill(args.template.read_text(encoding="utf-8"), {
        "DOC_TITLE": esc(f"{args.name} — Code Theory"),
        "DOC_LABEL": "CODE THEORY",
        "DOC_SUBTITLE": esc(args.subtitle or
                            f"Does the {args.name} {scope} express a coherent theory?"),
        "DOC_META": esc(" · ".join(part for part in (
            f"generated {meta['generated']}", meta["commit"],
            f"{scored['panel_size']} judges", meta["model"]) if part)),
        "TABS_NAV": nav,
        "TABS_PANELS": sections,
        "DOC_BODY": "",
        "DOC_FOOTER": ("Generated by code-overview. This grade is a judgment, not a "
                       "measurement — read the Dimensions tab's evidence rather than the "
                       "letter, and do not compare letters across models."),
    })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    shown = "—" if scored["score"] is None else f"{scored['score']:.1f}"
    print(f"wrote {args.out} — {scored['grade']} ({shown})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/code_overview/test_build_theory.py -q`
Expected: PASS — every test in the file.

- [ ] **Step 6: Verify the repo gates**

```bash
python -m ruff check .
python tools/validate_skills.py
python -m pytest tests/code_overview -q
```

Expected: ruff `All checks passed!`; validator exit 0; `tests/code_overview` shows only its 3 pre-existing failures.

- [ ] **Step 7: Commit**

```bash
git add skills/code-overview/assets/theory-body.html skills/code-overview/scripts/build_theory.py tests/code_overview/test_build_theory.py
git commit -m "code-overview: render the theory panel into a graded document

Four tabs, all rendered from the verdict files, so the letter and the rows
under it cannot disagree. The Theory tab is the one that matters: the grade is
a byproduct of having tried to state the theory, and the statement is the
artifact Naur says gets lost when only the code is handed over.

The Disagreement tab exists because taking the median alone would hide the most
interesting thing a panel learns. Three careful readers reaching different
conclusions about what code models is a property of the code.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: The fifth document kind — navigation, portal, roll-up

**Files:**
- Modify: `skills/code-overview/scripts/common.py` (`DOC_KINDS`, `DOC_TITLES`, add `THEORY_BLOCK_ID`)
- Modify: `skills/code-overview/scripts/inject_nav.py` (`HOME_LABELS`)
- Modify: `skills/code-overview/scripts/build_summary.py` (third grade card, repo table column)
- Modify: `skills/code-overview/assets/summary-body.html`
- Modify: `skills/code-overview/scripts/build_theory.py` (repo roll-up: `--package NAME:PATH`)
- Test: `tests/code_overview/test_navigation.py` (append), `tests/code_overview/test_summary_theory.py` (create)

**Interfaces:**
- Consumes: `theory_rubric`, `build_theory` (Tasks 1–2); `common.read_meta(path, block_id)`.
- Produces:
  - `common.DOC_KINDS = ("summary", "codemap", "health", "measurement", "theory")`
  - `common.THEORY_BLOCK_ID = "theory-meta"`
  - `build_summary.render_theory_card(meta: dict | None) -> str`
  - `build_theory.read_package_grade(name: str, path: Path) -> dict` and `render_package_table(packages: list[dict]) -> str`; `--package NAME:PATH` (repeatable, requires `--root`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/code_overview/test_navigation.py`:

```python
def test_the_across_row_links_all_five_documents(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    docs.mkdir(parents=True)
    for kind in ("summary", "codemap", "health", "measurement", "theory"):
        (docs / f"{kind}.html").write_text("<html><body><header></header></body></html>",
                                           encoding="utf-8")
    mapping = repo.path / "docs" / "code-overview.json"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text(json.dumps({"schema": "code-overview/1", "packages": [
        {"name": "app", "roots": ["src/app"], "docs": "src/app/docs",
         "language": "python", "doctor": "code-doctor"}]}), encoding="utf-8")

    run_script(INJECT_NAV, "--map", mapping, "--repo", repo.path)

    text = (docs / "summary.html").read_text(encoding="utf-8")
    assert "theory.html" in text
    assert "Theory" in text


def test_check_fails_on_a_theory_document_deleted_after_injection(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    docs.mkdir(parents=True)
    for kind in ("summary", "theory"):
        (docs / f"{kind}.html").write_text("<html><body><header></header></body></html>",
                                           encoding="utf-8")
    mapping = repo.path / "docs" / "code-overview.json"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text(json.dumps({"schema": "code-overview/1", "packages": [
        {"name": "app", "roots": ["src/app"], "docs": "src/app/docs",
         "language": "python", "doctor": "code-doctor"}]}), encoding="utf-8")
    run_script(INJECT_NAV, "--map", mapping, "--repo", repo.path)

    (docs / "theory.html").unlink()
    result = run_script(INJECT_NAV, "--map", mapping, "--repo", repo.path, "--check",
                        expect_rc=1)

    assert "BROKEN LINK" in result.stderr
```

Create `tests/code_overview/test_summary_theory.py`:

```python
"""The portal's third grade card, read back out of theory.html."""

import json
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "code-overview"
          / "scripts" / "build_summary.py")


def theory_page(path: Path, score, grade, disputed=()) -> Path:
    meta = {"schema": "theory/1", "scope": "package", "package": path.parent.name,
            "score": score, "grade": grade, "exempt": score is None,
            "exempt_reason": "two constants" if score is None else "",
            "panel_size": 3, "dimensions": [], "disputed": list(disputed),
            "theory": "Money moves between accounts.", "verdicts": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<html><body><script type="application/json" id="theory-meta">'
                    + json.dumps(meta) + "</script></body></html>", encoding="utf-8")
    return path


def test_the_theory_grade_is_read_back_out_of_the_document(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    theory_page(docs / "theory.html", 72.5, "C-")
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    text = out.read_text(encoding="utf-8")
    assert "C-" in text and "72.5" in text
    assert "theory.html" in text


def test_an_exempt_theory_reads_as_too_small_not_as_a_pass(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    theory_page(docs / "theory.html", None, "—")
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    assert "too small" in out.read_text(encoding="utf-8").lower()


def test_a_missing_theory_document_is_not_linked(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    docs.mkdir(parents=True)
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    assert 'href="theory.html"' not in out.read_text(encoding="utf-8")


def test_a_disputed_panel_is_flagged_on_the_portal(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    theory_page(docs / "theory.html", 61.0, "D-", disputed=["abstraction"])
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    assert "disagreed" in out.read_text(encoding="utf-8").lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/code_overview/test_navigation.py tests/code_overview/test_summary_theory.py -q`
Expected: FAIL — `theory` is not a document kind and the portal knows nothing about it.

- [ ] **Step 3: Add the fifth kind**

In `skills/code-overview/scripts/common.py`:

```python
THEORY_BLOCK_ID = "theory-meta"

DOC_KINDS = ("summary", "codemap", "health", "measurement", "theory")
DOC_TITLES = {"summary": "Summary", "codemap": "Code Map", "health": "Health",
              "measurement": "Measurement", "theory": "Theory"}
```

In `skills/code-overview/scripts/inject_nav.py`:

```python
HOME_LABELS = {"summary": "Overall Summary", "codemap": "Overall Code Map",
               "health": "Overall Health", "measurement": "Overall Measurement",
               "theory": "Overall Theory"}
```

- [ ] **Step 4: Add the portal's third card**

In `skills/code-overview/assets/summary-body.html`, after `<!--MEASUREMENT_CARD-->`:

```html
<!--THEORY_CARD-->
```

In `skills/code-overview/scripts/build_summary.py`:

```python
def render_theory_card(meta: dict | None) -> str:
    """The third grade, read from theory.html rather than passed in.

    Deliberately worded to keep it from being mistaken for the other two: it is
    a judgment, and a reader comparing three letters side by side needs to know
    that one of them was produced differently.
    """
    if meta is None:
        return ""
    score = meta.get("score")
    if score is None:
        return ('<div class="callout"><strong>Theory: too small to warrant one.</strong> '
                f'{esc(meta.get("exempt_reason") or "")} Scored null — not zero, not a pass.'
                "</div>")
    grade = str(meta.get("grade", "—"))
    disputed = meta.get("disputed") or []
    note = ""
    if disputed:
        note = ('<p class="dim"><strong>The panel disagreed</strong> on '
                + esc(", ".join(str(key) for key in disputed))
                + " — see the Theory document.</p>")
    return (f'<section class="gradecard {grade_class(grade)}">'
            f'<div><div class="letter">{esc(grade)}</div>'
            f'<div class="score">{score:.1f} / 100</div></div>'
            '<div class="what"><h2>Theory</h2>'
            '<p class="dim">Whether the code expresses a coherent theory of its problem, '
            "judged by a panel of three. A reading, not a measurement — read its evidence "
            f"rather than this letter.</p>{note}</div></section>")
```

Read it beside the measurement card in `build(args)`:

```python
    theory_path = Path(args.out).parent / "theory.html"
    theory = read_meta(theory_path, common.THEORY_BLOCK_ID)
```

Add to the `render(read_asset("summary-body.html"), {...})` dict:

```python
        "THEORY_CARD": render_theory_card(theory),
```

and one more entry to the `links` string for `DOC_LINKS`:

```python
        doc_link("theory", "theory.html",
                 "Does this unit's code express a coherent theory of its problem? A "
                 "panel of three judges, their evidence, and where they disagreed.",
                 theory_path.is_file()),
```

- [ ] **Step 5: Add the repo roll-up to `build_theory.py`**

Add `import re` if absent, then:

```python
def read_package_grade(name: str, path: Path) -> dict:
    """A package row for the root table, read back out of its own document."""
    blank = {"name": name, "score": None, "grade": tr.UNGRADED,
             "exempt": False, "disputed": [], "generated": False}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return blank
    match = re.search(r'id="theory-meta">(.*?)</script>', text, re.S)
    if not match:
        return blank
    try:
        meta = json.loads(match.group(1).replace("<\\/", "</"))
    except json.JSONDecodeError:
        return blank
    return {"name": name, "score": meta.get("score"),
            "grade": meta.get("grade", tr.UNGRADED),
            "exempt": bool(meta.get("exempt")),
            "disputed": meta.get("disputed") or [], "generated": True}


def render_package_table(packages: list[dict]) -> str:
    if not packages:
        return ""
    rows = []
    for item in packages:
        score = "—" if item["score"] is None else f"{item['score']:.1f}"
        if not item["generated"]:
            state = "not generated"
        elif item["exempt"]:
            state = "too small to warrant a theory"
        elif item["disputed"]:
            state = "panel disagreed on " + ", ".join(str(k) for k in item["disputed"])
        else:
            state = "graded"
        rows.append(f"<tr><td>{esc(item['name'])}</td>"
                    f'<td class="num">{score}</td>'
                    f'<td class="num">{esc(item["grade"])}</td>'
                    f"<td>{esc(state)}</td></tr>")
    return ("<h3>Packages</h3><p class=\"dim\">A package too small to warrant a theory is "
            "listed as such, not as passing.</p>"
            '<div class="tbl-wrap"><table><thead><tr><th>Package</th>'
            '<th class="num">Score</th><th class="num">Grade</th><th>State</th>'
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>")
```

Add the argument and guard in `main`:

```python
    parser.add_argument("--package", action="append", default=[], dest="packages",
                        metavar="NAME:PATH")
```

after `parse_args`:

```python
    if args.packages and not args.root:
        parser.error("--package builds the repository roll-up table and needs --root")
```

before the metadata is built:

```python
    packages = []
    for spec in args.packages:
        label, _, location = spec.partition(":")
        packages.append(read_package_grade(label.strip(), Path(location.strip())))
```

Widen `render_grade_tab` by one argument and append the table:

```python
def render_grade_tab(scored: dict, intro: str, size: dict, package_table: str = "") -> str:
    ...
        '<div class="l">lines</div></div></div>' + package_table
    )
```

and its call site: `render_grade_tab(scored, intro, size, render_package_table(packages))`.
Add `"packages": packages` to `build_metadata`'s dict when `args.root` is set.

- [ ] **Step 6: Run the tests**

```bash
python -m pytest tests/code_overview -q
```

Expected: PASS apart from the 3 pre-existing failures. `inject_nav.py` is driven by `DOC_KINDS`, so existence-checking, percent-encoding and `--check` extend without per-kind code.

- [ ] **Step 7: Commit**

```bash
git add skills/code-overview/scripts/common.py skills/code-overview/scripts/inject_nav.py skills/code-overview/scripts/build_summary.py skills/code-overview/scripts/build_theory.py skills/code-overview/assets/summary-body.html tests/code_overview/test_navigation.py tests/code_overview/test_summary_theory.py
git commit -m "code-overview: navigate and roll up the fifth document type

Everything in inject_nav.py is driven by DOC_KINDS, so adding theory extends
the existence checks, the percent-encoding and the --check gate on their own.
The portal's third card is read back out of theory.html like the other two, and
is worded to keep a judgment from being mistaken for a measurement now that
three letters sit side by side.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: The panel protocol, the workflow, and the evals

**Files:**
- Modify: `skills/code-overview/SKILL.md`
- Modify: `skills/code-overview/references/doc-layout.md`, `references/scoring.md`
- Modify: `skills/theory-building/SKILL.md` (one pointer, no code)
- Modify: `evals/code-overview/evals.json`, `README.md`
- Test: `tests/code_overview/test_end_to_end.py` (append)

**Interfaces:**
- Consumes: everything above. Produces no code.

- [ ] **Step 1: Write the failing test**

Append to `tests/code_overview/test_end_to_end.py`:

```python
def test_a_five_document_set_survives_the_link_gate(repo, run_script, tmp_path):
    import json as _json

    docs = repo.path / "src" / "app" / "docs"
    docs.mkdir(parents=True)
    root_docs = repo.path / "docs"
    root_docs.mkdir(parents=True, exist_ok=True)
    for base in (docs, root_docs):
        for kind in ("summary", "codemap", "health", "measurement", "theory"):
            (base / f"{kind}.html").write_text(
                "<html><body><header></header></body></html>", encoding="utf-8")
    mapping = root_docs / "code-overview.json"
    mapping.write_text(_json.dumps({"schema": "code-overview/1", "packages": [
        {"name": "app", "roots": ["src/app"], "docs": "src/app/docs",
         "language": "python", "doctor": "code-doctor"}]}), encoding="utf-8")

    inject = CO / "inject_nav.py"
    run_script(inject, "--map", mapping, "--repo", repo.path)
    run_script(inject, "--map", mapping, "--repo", repo.path, "--check", expect_rc=0)

    text = (docs / "theory.html").read_text(encoding="utf-8")
    assert "Overall Theory" in text
    assert text.count("<!-- code-overview:nav -->") == 1


def test_the_skill_documents_the_panel_protocol():
    text = (CO.parent / "SKILL.md").read_text(encoding="utf-8")
    section = text.split("## 4. The theory panel", 1)
    assert len(section) == 2, "no theory-panel section"
    body = section[1].split("\n## ", 1)[0]

    assert "build_theory.py" in body
    assert "independent" in body.lower()
    assert "median" in body.lower()
    for phrase in ("three", "disagree"):
        assert phrase in body.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/code_overview/test_end_to_end.py -q`
Expected: the panel-protocol test FAILS (no such section); the link-gate test passes once Task 3 landed.

- [ ] **Step 3: Add the panel protocol to `SKILL.md`**

Insert a new section after *3. Per package* and before *4. The repo level*, renumbering the later headings so the repo level becomes **5.** and navigation becomes **6.**:

```markdown
## 4. The theory panel

`theory.html` asks Naur's question: does this unit's code express a coherent
theory of the problem it solves? Unlike health and measurement, **this grade is
a judgment** — so the protocol is built to make the judgment disputable rather
than authoritative.

**Dispatch three judges per unit, independently.** Each gets the package's code
and `theory-building`'s doctrine; **none gets another judge's output**. A judge
that saw the others would converge on them, and the spread — the one signal a
panel exists to produce — would vanish.

Each judge writes a `theory-verdict/1` JSON file:

```json
{"schema": "theory-verdict/1", "unit": "billing",
 "theory": "≤3 sentences: what this models, in the world",
 "instead_of": "the other coherent reading, and why not it",
 "trivial": false, "trivial_reason": "",
 "dimensions": {"absorption": {"step": 0.5, "rationale": "two of four rehearsals need a parallel path", "evidence": ["src/billing/charge.py:88"]}},
 "rehearsals": [{"requirement": "refunds settle in a second currency", "verdict": "patch", "why": "currency is assumed at module scope", "evidence": ["src/billing/money.py:12"]}]}
```

Five dimensions, every one required: `absorption` (30), `world_mapping` (25),
`abstraction` (20), `justification` (15), `honest_limits` (10). Steps come from
a four-rung ladder — `1.0` holds, `0.5` partial, `0.25` strained, `0.0` absent —
and **a step below 1.0 must cite the evidence that lowered it**. At least three
absorption rehearsals, each landing as `extension` or `patch`.

```bash
python "$SKILL/scripts/build_theory.py" --out <DOCS>/theory.html --name <pkg> \
  --repo <repo> --root-dir <root> --model <model-id> \
  --verdict $WORK/<pkg>-judge1.json --verdict $WORK/<pkg>-judge2.json \
  --verdict $WORK/<pkg>-judge3.json --template "$SKILL/assets/template.html"
```

The median per dimension sets the score. **Where the judges differ by two rungs
or more, that is reported as a finding** — three careful readers disagreeing
about what code models is a fact about the code, and the median alone would hide
it.

**A unit with no theory scores badly; it is not null.** Naur: "If the theory
can't be stated, there isn't one." Recording that as unmeasurable is the dodge
this document set refuses. `null` is reserved for units genuinely **too small to
warrant a theory**, which requires *both* a size test (≤3 files and ≤200 lines,
computed) and ≥2 of the 3 judges voting trivial with a reason. Either gate alone
is gameable.

**The floor is not offered at repo scope** — a repository of individually
trivial packages still has a system-level question worth asking.

**Say what this grade is when you present it.** A model auditing abstractions is
partly circular: the weakness that produces repetition-with-variants also
evaluates whether the repetition was warranted. Three judges narrow that; they do
not close it. The theory statement itself is the deliverable — it is the artifact
that gets lost when only the code is handed over — and the grade is a byproduct
of having tried to write it.
```

- [ ] **Step 4: Update the references**

In `references/doc-layout.md`: add `theory.html` to both layout blocks and the
*Across* row of the navigation table; extend the rebuild order with the panel
step; and append:

```markdown
## The theory metadata block

`<script type="application/json" id="theory-meta">`, schema `theory/1`, same
`</` → `<\/` escaping as the other two.

| Field | Meaning |
|---|---|
| `scope` | `package` or `repository` |
| `score`, `grade` | 0–100 and a letter; `null` only when exempt |
| `exempt`, `exempt_reason` | too small to warrant a theory, and the evidence for that |
| `panel_size`, `model` | how many judges, and which model — letters from different models are not comparable |
| `dimensions[]` | per dimension: `key`, `label`, `weight`, `step`, `step_label`, `rung`, `spread`, `disputed`, `steps[]`, `rationales[]`, `evidence[]` |
| `disputed[]` | dimension keys where the judges were ≥2 rungs apart |
| `theory` | the first judge's theory statement, for roll-ups |
| `verdicts[]` | all three verdicts verbatim |
| `packages[]` | root scope only |
```

In `references/scoring.md`, append:

```markdown
## The theory grade

    score = 100 × Σ(weight × median step) / Σ(weight)

A third question again: health asks how dense the detectable defects are,
measurement how much of what matters is measured, theory whether the code
expresses a coherent model of its problem at all.

Its ladder is ordinal — `absent`, `strained`, `partial`, `holds` — and
disagreement between judges is counted in **rungs**, not in the values, because
the values are unevenly spaced on purpose and arithmetic distance would rank the
same disagreement differently depending on where it sat.

Its bands are the same as the other two, imported directly from `rubric.py`
rather than copied — `theory_rubric.py` ships in the same skill, so the
standalone-installability rule that forces the science-investigation copy does
not apply.

**Unlike the other two, this grade is a judgment.** Present it with that said
out loud, and never compare letters produced by different models.
```

- [ ] **Step 5: Point `theory-building` at this use**

Append to `skills/theory-building/SKILL.md`, immediately before *## Sources*:

```markdown
## Judging code you did not write

The gates above govern code being produced now. To apply the same doctrine to an
existing codebase — a package you are inheriting, or one you are about to hand
over — `code-overview` builds a `theory.html` per unit: three judges score the
five dimensions drawn from these gates, the median sets a grade, and the places
they disagree are reported rather than averaged away.

This skill ships no code for that on purpose. It is doctrine; the judging, the
arithmetic and the document belong to the skill that owns the document set.
```

- [ ] **Step 6: Append the eval cases**

Add to `evals/code-overview/evals.json`:

```json
{
  "id": "a-shapeless-package-scores-badly-not-null",
  "prompt": "The theory page for our utils package came back with an F. It's just a pile of helper functions people add to — surely that's a null, not a fail?",
  "expected_output": "Explains that null is reserved for units too small to warrant a theory, which requires both a size test and a 2-of-3 judge vote, and that a package big enough to accumulate helpers is exactly a package that should have a theory. Points at the dimension rows and their evidence rather than defending the letter, and notes the grade is a judgment whose rows are the arguable part."
},
{
  "id": "theory-panel-disagreement-is-a-finding",
  "prompt": "Two of the three judges said our scheduler's abstraction 'holds' and one said 'strained'. Just take the majority and move on?",
  "expected_output": "Notes that one rung of disagreement does not raise the banner but two does, checks which it was, and treats a two-rung split as a finding about the code's clarity rather than about the judges. Does not simply discard the minority reading — the Disagreement tab exists because the median hides it."
}
```

- [ ] **Step 7: Update the README**

Extend `README.md`'s `code-overview` row to name the fifth document.

- [ ] **Step 8: Verify everything**

```bash
python -m pytest -q
python -m ruff check .
python tools/validate_skills.py
```

Expected: the whole suite green apart from the 11 pre-existing failures; ruff clean; validator exit 0. `SKILL.md`'s frontmatter `description` must stay ≤ 1024 characters — trim the description if the fifth document pushes it over, never the body.

- [ ] **Step 9: Commit**

```bash
git add skills/code-overview/SKILL.md skills/code-overview/references skills/theory-building/SKILL.md evals/code-overview/evals.json README.md tests/code_overview/test_end_to_end.py
git commit -m "code-overview: document the theory panel and its protocol

The agent is half this protocol — three judges, dispatched independently,
none seeing another's verdict, because a judge that saw the others would
converge and the spread is the one signal a panel exists to produce.

Documents what null means here and what it does not: a unit too small to
warrant a theory, on both a size test and a vote, and never a unit that simply
has no theory. Naur is explicit that if the theory cannot be stated there
isn't one, and recording that as unmeasurable is the dodge this set refuses.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Self-review notes

| Spec requirement | Task |
|---|---|
| Five weighted dimensions from the gates; absorption heaviest | 1 |
| Four-rung ordinal ladder; median never leaves it | 1 |
| Disagreement in rungs, ≥2 flagged | 1, 2 |
| Panel of three, independent, median per dimension | 1 (arithmetic), 4 (protocol) |
| Exemption on both gates; `null` only when exempt | 1, 2 |
| No theory ⇒ bad score, never null | 1 |
| Floor not offered at repo scope | 2 (`allow_exemption=not args.root`) |
| Every dimension a row with step, rationale, spread, evidence | 2 |
| No unattributed step | 1 (validator) |
| Rehearsals verbatim | 2 |
| Theory statement is the deliverable | 2 (its own tab, first after Grade) |
| Page states it is a reading; circularity named; model stamped | 2 |
| Bands imported, not copied; no new CI pin | 1 |
| Fifth `DOC_KINDS`; nav; third grade card; roll-up | 3 |
| `theory-building` stays code-free | 4 (pointer only) |
| Rendering pinned by panel-scoped assertions | 2, 3 |

**Deliberately not built:** any attempt to remove the circularity; per-file
theory scores; scripts in `theory-building`.
