#!/usr/bin/env python3
"""The measurement-coverage rubric: what the score divides, and what it refuses.

    score = 100 * sum(importance * credit) / sum(importance)

over every *measurable* thing — not every measured one. That denominator is the
whole point: a system with one perfectly-measured metric and four release gates
nobody measures should not score 100, and it only fails to if the four gates are
counted.

Both halves are judgments the auditor supplies, which is exactly why this file
also holds `validate`. A number nobody can dispute is a number nobody can trust,
so every row must arrive with the decision its importance claims to serve, the
finding that reduced its credit, and the N it was computed over. The validator
refuses the shapes that would quietly inflate the score; prose asking for them
politely does not survive contact with an auditor in a hurry.

Pure data and arithmetic — no I/O — so a score can be recomputed and tested
without building a document.
"""

from __future__ import annotations

INVENTORY_SCHEMA = "measurement-inventory/1"
DOCUMENT_SCHEMA = "measurement/1"

# Importance is blast radius, not enthusiasm: what decision moves because of
# this number. Three levels, because a finer scale invites precision the
# evidence cannot carry and makes every row an argument.
IMPORTANCE_LABELS: dict[int, str] = {
    3: "gates a ship decision",
    2: "informs a decision",
    1: "informational",
}

# Credit is set by the WORST confirmed finding against the thing. The ladder is
# coarse for the same reason the importance scale is.
CREDIT_STEPS: tuple[float, ...] = (1.0, 0.5, 0.25, 0.0)
CREDIT_LABELS: dict[float, str] = {
    1.0: "measured, nothing found against it",
    0.5: "measured, one medium finding",
    0.25: "measured, one high finding",
    0.0: "not measured, or unmeasurable with today's data",
}

STATUSES = ("measured", "not_measured", "unmeasurable")

# Duplicated from code-overview/scripts/rubric.py, not shared: this skill has to
# render its own page with no sibling installed, and no skill may read another
# skill's files. A CI test pins the two copies identical, because a B- that
# means two different score ranges on two pages a reader compares side by side
# is worse than no letter at all.
GRADE_BANDS: tuple[tuple[float, str], ...] = (
    (97.0, "A+"), (93.0, "A"), (90.0, "A-"),
    (87.0, "B+"), (83.0, "B"), (80.0, "B-"),
    (77.0, "C+"), (73.0, "C"), (70.0, "C-"),
    (67.0, "D+"), (63.0, "D"), (60.0, "D-"),
)

UNGRADED = "—"


class InventoryError(ValueError):
    """An inventory row asserts something its own fields do not support."""


def grade_for(score: float | None) -> str:
    if score is None:
        return UNGRADED
    for threshold, letter in GRADE_BANDS:
        if score >= threshold:
            return letter
    return "F"


def _fail(name: str, message: str) -> None:
    raise InventoryError(f"{name}: {message}")


def validate(rows: list[dict], findings: list[dict]) -> None:
    """Refuse an inventory whose rows do not support the score they produce."""
    known_findings = {str(f.get("id")) for f in findings if f.get("id")}
    seen: set[str] = set()

    for entry in rows:
        name = str(entry.get("name") or "<unnamed>")
        if name in seen:
            _fail(name, "duplicate row name; each measurable thing appears once")
        seen.add(name)

        if entry.get("importance") not in IMPORTANCE_LABELS:
            _fail(name, f"importance must be one of {sorted(IMPORTANCE_LABELS)}, "
                        f"got {entry.get('importance')!r}")
        if not str(entry.get("importance_reason") or "").strip():
            _fail(name, "importance_reason must name the decision this number drives; "
                        "'important' is not a justification")

        credit = entry.get("credit")
        if credit not in CREDIT_STEPS:
            _fail(name, f"credit must be one of {list(CREDIT_STEPS)}, got {credit!r}")

        status = entry.get("status")
        if status not in STATUSES:
            _fail(name, f"status must be one of {list(STATUSES)}, got {status!r}")

        if not [item for item in (entry.get("evidence") or []) if str(item).strip()]:
            _fail(name, "evidence must cite at least one file:line you personally opened")

        if status == "unmeasurable":
            if credit != 0.0:
                _fail(name, "an unmeasurable thing carries credit 0 — it stays in the "
                            "denominator so silence is not read as success")
            if not str(entry.get("unmeasurable_reason") or "").strip():
                _fail(name, "unmeasurable_reason must say what today's data cannot supply "
                            "(no gold set, no control arm, no outcomes)")
        elif status == "not_measured":
            if credit != 0.0:
                _fail(name, "a thing that is not measured carries credit 0")
        else:
            if credit == 0.0:
                _fail(name, "status 'measured' with credit 0 is contradictory; use "
                            "'not_measured' or 'unmeasurable'")
            n = entry.get("n")
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                _fail(name, "credit above zero requires n, the number of real examples it "
                            "was computed over; '0.8 over 30' and '0.8 over 3' are "
                            "different facts")
            if credit < 1.0:
                finding_id = str(entry.get("finding") or "").strip()
                if not finding_id:
                    _fail(name, "credit below 1.0 must name the finding that caused it — "
                                "an unattributed deduction cannot be argued with")
                if finding_id not in known_findings:
                    _fail(name, f"no finding with id {finding_id!r} is in the report")


def score_inventory(rows: list[dict]) -> dict:
    """Importance-weighted measured things over measurable things."""
    weight_total = 0.0
    weight_measured = 0.0
    buckets: dict[str, dict] = {
        str(level): {"total": 0.0, "measured": 0.0, "share": None, "rows": 0}
        for level in sorted(IMPORTANCE_LABELS, reverse=True)
    }

    for entry in rows:
        importance = float(entry.get("importance") or 0)
        credit = float(entry.get("credit") or 0.0)
        weight_total += importance
        weight_measured += importance * credit

        bucket = buckets.get(str(entry.get("importance")))
        if bucket is not None:
            bucket["total"] += importance
            bucket["measured"] += importance * credit
            bucket["rows"] += 1

    for bucket in buckets.values():
        if bucket["total"]:
            bucket["share"] = 100.0 * bucket["measured"] / bucket["total"]

    # No measurable things is not full coverage and not zero coverage; it is
    # the absence of a measurement question. Zero, by contrast, is a real and
    # damning measurement: everything that matters here is unmeasured.
    score = 100.0 * weight_measured / weight_total if weight_total else None

    return {
        "score": score,
        "grade": grade_for(score),
        "weight_total": weight_total,
        "weight_measured": weight_measured,
        "by_importance": buckets,
    }
