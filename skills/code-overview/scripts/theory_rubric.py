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
# fourth thing to keep pinned for no benefit. The bands themselves are not
# re-exported — `grade_for` already encapsulates them, and nothing reads
# `tr.GRADE_BANDS`. `UNGRADED` is: build_theory.py reads `tr.UNGRADED` for the
# roll-up's blank row, hence the noqa.
from rubric import UNGRADED, grade_for  # noqa: F401  (re-exported)

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
        # median_low, never median or a mean: a mean of unequal steps is not a
        # rung at all — (0.25, 0.5, 1.0) averages to 0.583..., which has no
        # label and cannot be rendered. At PANEL_SIZE == 3 (odd), median_low and
        # median agree, so this only matters if the panel size ever becomes
        # even; median_low is used anyway so that property survives the change
        # rather than becoming a trap for whoever makes it.
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
