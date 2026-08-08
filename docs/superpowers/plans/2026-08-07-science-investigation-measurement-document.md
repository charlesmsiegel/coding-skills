# science-investigation Measurement Document Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `science-investigation` a graded, tabbed HTML deliverable — a measurement-coverage score computed as importance-weighted measured things over measurable things, rendered into `measurement.html` with the inventory table that makes the denominator arguable.

**Architecture:** The audit's output becomes a structured artifact. The agent writes `inventory.json` — one row per measurable thing, with its importance, its credit, and the finding that set that credit — and `build_measurement.py` renders every tab from it deterministically. No hand-written HTML, so the score on the page and the table under it cannot disagree. `rubric.py` holds the arithmetic and the validation that keeps an authored denominator honest.

**Tech Stack:** Python 3.11+, stdlib only. pytest driving the CLI as a subprocess, plus direct unit tests of the rubric through the `load_module` fixture.

## Global Constraints

- **Python 3.11+, stdlib only, no network calls.**
- **Source of truth:** `docs/superpowers/specs/2026-08-07-code-overview-companions-design.md`, section *Measurement — a coverage ratio*.
- **Independent of the other two plans.** Nothing here consumes code-doctor or code-overview.
- **The skill must work standalone.** `build_measurement.py` ships its own default shell and body template; `--template` and `--body` are overrides code-overview uses, never requirements.
- **`score = 100 × Σ(importance × credit) / Σ(importance)`** over all measurable things. Importance ∈ {3, 2, 1}. Credit ∈ {1.0, 0.5, 0.25, 0.0}.
- **Zero measurable things → `score: null`, grade `—`.** Never A+.
- **"Not measured" is null, never `0.0`.** The skill's own rule, now load-bearing for a grade.
- **No unattributed deduction.** Credit below 1.0 must name the confirmed finding that caused it; the validator rejects a row that does not.
- **Structurally unmeasurable things stay in the denominator** at credit 0, labelled as unmeasurable rather than as defects.
- **Letter bands are duplicated from `code-overview/scripts/rubric.py`, not shared** — no skill may read another skill's files. Plan 3 adds the CI test that pins them identical.
- **The shell's design tokens are copied byte-identical from `skills/code-visualization/assets/template.html`.** That is what makes the four documents in a code overview read as one artifact.
- **The tab contract is code-visualization's**, so code-overview can force one shell on both generators: slots `<!--DOC_TITLE-->`, `<!--DOC_LABEL-->`, `<!--DOC_SUBTITLE-->`, `<!--DOC_META-->`, `<!--DOC_FOOTER-->`, `<!--TABS_NAV-->`, `<!--TABS_PANELS-->`; buttons `<button role="tab" data-tab="ID" aria-selected="…" aria-controls="ID">`; panels `<section class="panel active" id="ID" role="tabpanel">`.
- **CI gates:** `ruff check .`, `python tools/validate_skills.py`, `pytest -q`, and the bug-class detector ratchet on `skills/science-investigation/scripts`.

---

### Task 1: `rubric.py` — the coverage arithmetic and the honesty validator

**Files:**
- Create: `skills/science-investigation/scripts/rubric.py`
- Test: `tests/science_investigation/test_rubric.py`

**Interfaces:**
- Consumes: nothing. Pure data and arithmetic, no I/O, so the score can be tested without building a document.
- Produces:
  - `INVENTORY_SCHEMA = "measurement-inventory/1"`, `DOCUMENT_SCHEMA = "measurement/1"`
  - `IMPORTANCE_LABELS: dict[int, str]` — `{3: "gates a ship decision", 2: "informs a decision", 1: "informational"}`
  - `CREDIT_STEPS: tuple[float, ...]` — `(1.0, 0.5, 0.25, 0.0)`
  - `CREDIT_LABELS: dict[float, str]`
  - `STATUSES = ("measured", "not_measured", "unmeasurable")`
  - `GRADE_BANDS: tuple[tuple[float, str], ...]`, `UNGRADED = "—"`
  - `class InventoryError(ValueError)`
  - `validate(rows: list[dict], findings: list[dict]) -> None` — raises `InventoryError` naming the offending row
  - `grade_for(score: float | None) -> str`
  - `score_inventory(rows: list[dict]) -> dict` — `{"score", "grade", "weight_total", "weight_measured", "by_importance"}` where `by_importance` maps the importance level (as a string key, so it survives JSON) to `{"total", "measured", "share", "rows"}`

- [ ] **Step 1: Write the failing test**

Create `tests/science_investigation/test_rubric.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/science_investigation/test_rubric.py -q`
Expected: every test errors at fixture setup — `ModuleNotFoundError: No module named 'rubric'`.

- [ ] **Step 3: Write the rubric**

Create `skills/science-investigation/scripts/rubric.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/science_investigation/test_rubric.py -q`
Expected: PASS, 20 tests.

- [ ] **Step 5: Verify the repo gates**

```bash
ruff check skills/science-investigation/scripts/rubric.py
python skills/python-code-doctor/scripts/find_exception_issues.py skills/science-investigation/scripts --format json
python skills/python-code-doctor/scripts/find_mutation_hazards.py skills/science-investigation/scripts --format json
```

Expected: ruff silent, both detectors print `[]`.

- [ ] **Step 6: Commit**

```bash
git add skills/science-investigation/scripts/rubric.py tests/science_investigation/test_rubric.py
git commit -m "science-investigation: the measurement-coverage rubric

Score is importance-weighted measured things over measurable things, so a
system with one perfect metric and four unmeasured release gates does not
score 100. Both halves are the auditor's judgment, which is why validate()
ships beside the arithmetic: a credit below 1.0 must name the finding behind
it, an aggregate must carry its N, and an unmeasurable thing stays in the
denominator at zero rather than being dropped where silence reads as success.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: The shell, the body scaffold, and `build_measurement.py`

**Files:**
- Create: `skills/science-investigation/assets/template.html`
- Create: `skills/science-investigation/assets/measurement-body.html`
- Create: `skills/science-investigation/scripts/build_measurement.py`
- Test: `tests/science_investigation/test_build_measurement.py`

**Interfaces:**
- Consumes: `rubric` (Task 1) — `INVENTORY_SCHEMA`, `DOCUMENT_SCHEMA`, `IMPORTANCE_LABELS`, `CREDIT_LABELS`, `UNGRADED`, `InventoryError`, `validate`, `score_inventory`, `grade_for`.
- Produces:
  - `read_inventory(path: Path) -> dict` — parses and validates; raises `InventoryError`
  - `fill(template: str, slots: dict[str, str]) -> str` — replaces `<!--KEY-->` with the value
  - `build(inventory: dict, *, name: str, scoped_rows, scoped_findings) -> dict` — the `measurement/1` metadata dict
  - `main(argv=None) -> int` — CLI below
- Produces the CLI:

  ```
  python build_measurement.py --out FILE --inventory JSON --name NAME
      [--repo DIR] [--subtitle TEXT] [--intro-file HTML] [--commit SHA]
      [--template FILE] [--body FILE]
  ```

  Exit 2 on an invalid inventory (naming the row), 0 otherwise.

**The inventory file** the agent writes, `measurement-inventory/1`:

```json
{"schema": "measurement-inventory/1",
 "subject": "billing",
 "rows": [{"name": "judge_accuracy", "importance": 3,
           "importance_reason": "gates the weekly model rollout",
           "credit": 0.25, "credit_reason": "computed over 3 labelled rows of 412",
           "finding": "small_n", "n": 3, "n_total": 412,
           "formula": "mean(judge_score == gold)",
           "consumer": "scripts/rollout.py:88 gates the release",
           "evidence": ["evals/judge.py:41"], "status": "measured",
           "unmeasurable_reason": ""}],
 "findings": [{"id": "small_n", "severity": "high", "title": "Judge accuracy rests on n=3",
               "detail": "3 of 412 rows carry a gold label.",
               "evidence": ["evals/judge.py:41"],
               "blast_radius": "the weekly rollout gate"}],
 "not_audited": ["the analytics dashboard — no access"]}
```

- [ ] **Step 1: Write the failing test**

Create `tests/science_investigation/test_build_measurement.py`:

```python
"""The measurement document.

Every tab is rendered from the inventory, so the score on the page and the
table under it cannot disagree. The tests that matter most are the ones about
what the page refuses to claim: a null score renders as a dash, an unmeasurable
row appears in the denominator, and an invalid inventory produces no document
at all rather than a document with a flattering number on it.
"""

import json
import re
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[2] / "skills" / "science-investigation"
SCRIPT = SKILL / "scripts" / "build_measurement.py"
CV_TEMPLATE = Path(__file__).resolve().parents[2] / "skills" / "code-visualization" / "assets" / "template.html"


def token_blocks(text: str) -> str:
    """The :root token block and its light-mode twin, which must not drift."""
    root = re.search(r":root\{.*?\n\}", text, re.S)
    light = re.search(r"@media \(prefers-color-scheme: light\)\{.*?\n\}\n\}", text, re.S)
    assert root and light, "template is missing its design-token blocks"
    return root.group(0) + "\n" + light.group(0)


def inventory(tmp_path, **overrides) -> Path:
    payload = {
        "schema": "measurement-inventory/1",
        "subject": "billing",
        "rows": [
            {"name": "judge_accuracy", "importance": 3,
             "importance_reason": "gates the weekly model rollout",
             "credit": 0.25, "credit_reason": "computed over 3 labelled rows of 412",
             "finding": "small_n", "n": 3, "n_total": 412,
             "formula": "mean(judge_score == gold)",
             "consumer": "scripts/rollout.py:88 gates the release",
             "evidence": ["evals/judge.py:41"], "status": "measured",
             "unmeasurable_reason": ""},
            {"name": "recall", "importance": 2,
             "importance_reason": "informs the retrieval roadmap",
             "credit": 0.0, "credit_reason": "no gold set exists", "finding": "",
             "n": None, "n_total": None, "formula": "", "consumer": "nobody",
             "evidence": ["evals/retrieval.py:12"], "status": "unmeasurable",
             "unmeasurable_reason": "no gold set exists, so recall cannot be computed"},
        ],
        "findings": [
            {"id": "small_n", "severity": "high", "title": "Judge accuracy rests on n=3",
             "detail": "3 of 412 rows carry a gold label.",
             "evidence": ["evals/judge.py:41"], "blast_radius": "the weekly rollout gate"},
        ],
        "not_audited": ["the analytics dashboard — no access"],
    }
    payload.update(overrides)
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def meta_of(page: Path) -> dict:
    text = page.read_text(encoding="utf-8")
    match = re.search(r'id="measurement-meta">(.*?)</script>', text, re.S)
    assert match, "the page carries no measurement-meta block"
    return json.loads(match.group(1).replace("<\\/", "</"))


def build(run_script, tmp_path, *extra, expect_rc=0) -> Path:
    out = tmp_path / "measurement.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path),
               "--name", "billing", *extra, expect_rc=expect_rc)
    return out


# --- the shell ------------------------------------------------------------

def test_the_design_tokens_are_byte_identical_to_code_visualizations():
    ours = (SKILL / "assets" / "template.html").read_text(encoding="utf-8")

    assert token_blocks(ours) == token_blocks(CV_TEMPLATE.read_text(encoding="utf-8")), (
        "the four documents in a code overview share one palette; this is what keeps them "
        "reading as one artifact instead of three tools' output"
    )


def test_the_shell_uses_code_visualizations_tab_contract():
    ours = (SKILL / "assets" / "template.html").read_text(encoding="utf-8")

    for slot in ("<!--DOC_TITLE-->", "<!--DOC_LABEL-->", "<!--DOC_SUBTITLE-->",
                 "<!--DOC_META-->", "<!--DOC_FOOTER-->", "<!--TABS_NAV-->",
                 "<!--TABS_PANELS-->"):
        assert slot in ours, f"{slot} is what lets code-overview force one shell on both generators"


# --- the document ---------------------------------------------------------

def test_the_page_carries_the_score_and_the_grade(run_script, tmp_path):
    page = build(run_script, tmp_path)

    meta = meta_of(page)
    # 3*0.25 + 2*0.0 = 0.75 over 5 → 15.
    assert meta["score"] == pytest.approx(15.0)
    assert meta["grade"] == "F"
    assert meta["schema"] == "measurement/1"
    assert meta["scope"] == "package"


def test_every_tab_is_present(run_script, tmp_path):
    text = build(run_script, tmp_path).read_text(encoding="utf-8")

    for tab in ("Score", "Inventory", "Findings", "Unmeasurable"):
        assert f">{tab}<" in text, f"the {tab} tab is missing"


def test_the_inventory_table_shows_every_row_with_its_weight_and_credit(run_script, tmp_path):
    text = build(run_script, tmp_path).read_text(encoding="utf-8")

    assert "judge_accuracy" in text
    assert "recall" in text
    assert "gates the weekly model rollout" in text
    assert "evals/judge.py:41" in text


def test_a_reduced_credit_names_the_finding_that_caused_it(run_script, tmp_path):
    text = build(run_script, tmp_path).read_text(encoding="utf-8")

    assert "small_n" in text
    assert "Judge accuracy rests on n=3" in text


def test_the_unmeasurable_row_is_labelled_not_counted_as_a_defect(run_script, tmp_path):
    text = build(run_script, tmp_path).read_text(encoding="utf-8")
    meta = meta_of(tmp_path / "measurement.html")

    assert "no gold set exists" in text
    assert meta["weight_total"] == pytest.approx(5.0), "it stays in the denominator"
    assert [r["name"] for r in meta["rows"] if r["status"] == "unmeasurable"] == ["recall"]


def test_the_ship_gate_share_is_reported_separately(run_script, tmp_path):
    meta = meta_of(build(run_script, tmp_path))

    assert meta["by_importance"]["3"]["share"] == pytest.approx(25.0)


def test_an_empty_inventory_scores_null_and_says_so(run_script, tmp_path):
    out = tmp_path / "measurement.html"
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"schema": "measurement-inventory/1", "subject": "utils",
                                "rows": [], "findings": [], "not_audited": []}),
                    encoding="utf-8")
    run_script(SCRIPT, "--out", out, "--inventory", path, "--name", "utils")

    meta = meta_of(out)
    assert meta["score"] is None
    assert meta["grade"] == "—"
    assert "no measurement content" in out.read_text(encoding="utf-8").lower()


def test_an_invalid_inventory_writes_no_document(run_script, tmp_path):
    out = tmp_path / "measurement.html"
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({
        "schema": "measurement-inventory/1", "subject": "x",
        "rows": [{"name": "acc", "importance": 3, "importance_reason": "gates release",
                  "credit": 0.5, "credit_reason": "shaky", "finding": "", "n": 10,
                  "evidence": ["a.py:1"], "status": "measured"}],
        "findings": [], "not_audited": []}), encoding="utf-8")

    result = run_script(SCRIPT, "--out", out, "--inventory", path,
                        "--name", "x", expect_rc=2)

    assert not out.exists(), "a document with an unarguable score is worse than none"
    assert "finding" in result.stderr


def test_the_intro_prose_is_placed_on_the_score_tab(run_script, tmp_path):
    intro = tmp_path / "intro.html"
    intro.write_text("<p>Billing scores itself with a judge nobody pinned.</p>",
                     encoding="utf-8")

    text = build(run_script, tmp_path, "--intro-file", intro).read_text(encoding="utf-8")

    assert "nobody pinned" in text


def test_a_missing_intro_file_is_said_on_the_page_not_silently_dropped(run_script, tmp_path):
    text = build(run_script, tmp_path).read_text(encoding="utf-8")

    assert "no written summary" in text.lower()


def test_a_template_override_is_used(run_script, tmp_path):
    custom = tmp_path / "shell.html"
    custom.write_text("<html><body>MARKER<!--TABS_NAV--><!--TABS_PANELS--></body></html>",
                      encoding="utf-8")

    text = build(run_script, tmp_path, "--template", custom).read_text(encoding="utf-8")

    assert "MARKER" in text
    assert "judge_accuracy" in text


def test_script_closing_tags_inside_the_metadata_cannot_end_the_block(run_script, tmp_path):
    path = inventory(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rows"][0]["consumer"] = "</script><script>alert(1)</script>"
    path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "measurement.html"

    run_script(SCRIPT, "--out", out, "--inventory", path, "--name", "billing")

    assert "</script><script>alert(1)" not in out.read_text(encoding="utf-8")
    assert meta_of(out)["rows"][0]["consumer"].startswith("</script>")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/science_investigation/test_build_measurement.py -q`
Expected: FAIL — no template, no script.

- [ ] **Step 3: Create the shell with copied tokens**

Run this once. It copies code-visualization's two design-token blocks verbatim and splices them into the shell, so the tokens cannot be mistyped:

```bash
python - <<'PY'
import re, pathlib

cv = pathlib.Path("skills/code-visualization/assets/template.html").read_text(encoding="utf-8")
root = re.search(r":root\{.*?\n\}", cv, re.S).group(0)
light = re.search(r"@media \(prefers-color-scheme: light\)\{.*?\n\}\n\}", cv, re.S).group(0)

shell = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title><!--DOC_TITLE--></title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
/* Design tokens are code-visualization's, copied verbatim. A measurement
   document sits in the same nav bar as a code map and a health page; sharing
   the palette is what makes them read as one artifact rather than three
   tools' output. A CI test pins these two blocks byte-identical. */
__TOKENS__

*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  font-family:var(--sans); background:var(--bg); color:var(--text);
  background-image:linear-gradient(var(--bg-grid) 1px,transparent 1px),linear-gradient(90deg,var(--bg-grid) 1px,transparent 1px);
  background-size:32px 32px; line-height:1.55; font-size:15px;
}
header.doc{padding:34px 28px 0; max-width:1280px; margin:0 auto}
.doc-label{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);display:flex;align-items:center;gap:8px}
.doc-label::before{content:"";width:8px;height:8px;background:var(--accent);border-radius:2px;display:inline-block}
h1.doc-title{font-size:30px;font-weight:700;letter-spacing:-.02em;margin-top:8px}
.doc-sub{color:var(--text-dim);margin-top:6px;max-width:900px}
.doc-meta{font-family:var(--mono);font-size:12px;color:var(--text-faint);margin-top:10px}
main{max-width:1280px;margin:0 auto;padding:26px 28px 80px}
@media (prefers-reduced-motion: reduce){*{animation:none!important;transition:none!important}}

/* ---------- tabs: code-visualization's contract, so one shell serves both -- */
nav.tabs{
  display:flex;gap:2px;flex-wrap:wrap;max-width:1280px;margin:18px auto 0;
  padding:0 28px;border-bottom:1px solid var(--border);
}
/* An untabbed page (a summary portal) leaves TABS_NAV empty; without this the
   empty bar renders as a stray rule under the header. */
nav.tabs:empty{display:none;border:none}
nav.tabs button{
  background:none;border:none;border-bottom:2px solid transparent;color:var(--text-dim);
  font-family:var(--sans);font-size:14px;padding:9px 14px;cursor:pointer;
}
nav.tabs button:hover{color:var(--text)}
nav.tabs button[aria-selected="true"]{color:var(--accent);border-bottom-color:var(--accent)}
nav.tabs button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
section.panel{display:none}
section.panel.active{display:block}

/* ---------- content primitives (shared with code-visualization) ---------- */
h2{font-size:21px;font-weight:600;letter-spacing:-.01em;margin:30px 0 10px}
h2:first-child{margin-top:0}
h3{font-size:16px;font-weight:600;margin:22px 0 8px}
p{margin:8px 0;max-width:940px;color:var(--text)}
ul,ol{margin:8px 0 8px 22px;max-width:940px}
li{margin:4px 0}
a{color:var(--accent)}
.dim{color:var(--text-dim)} .faint{color:var(--text-faint)}
code,.mono{font-family:var(--mono);font-size:.92em}
p code,li code,td code{background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:1px 5px;overflow-wrap:anywhere}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px 20px;margin:12px 0}
.kpis{display:flex;flex-wrap:wrap;gap:12px;margin:12px 0}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 18px;min-width:130px}
.kpi .n{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.kpi .l{font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--text-dim);margin-top:2px}
.kpi.warn .n{color:var(--warn)} .kpi.bad .n{color:var(--bad)} .kpi.good .n{color:var(--good)} .kpi.accent .n{color:var(--accent)}
.badge{display:inline-block;font-family:var(--mono);font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--border);vertical-align:1px;white-space:nowrap}
.badge.good{color:var(--good);background:var(--good-dim);border-color:transparent}
.badge.warn{color:var(--warn);background:var(--warn-dim);border-color:transparent}
.badge.bad{color:var(--bad);background:var(--bad-dim);border-color:transparent}
.badge.accent{color:var(--accent);background:var(--accent-dim);border-color:transparent}
.badge.neutral{color:var(--text-dim);background:var(--surface-2);border-color:transparent}
.callout{border-left:3px solid var(--accent);background:var(--accent-dim);border-radius:0 8px 8px 0;padding:12px 16px;margin:12px 0;max-width:940px}
.callout.warn{border-color:var(--warn);background:var(--warn-dim)}
.callout.bad{border-color:var(--bad);background:var(--bad-dim)}
.callout.good{border-color:var(--good);background:var(--good-dim)}
.tbl-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:10px;margin:12px 0;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:13.5px}
thead th{background:var(--surface-2);text-align:left;font-family:var(--mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-dim);padding:9px 14px;border-bottom:1px solid var(--border-strong);white-space:nowrap}
tbody td{padding:8px 14px;border-bottom:1px solid var(--border);vertical-align:top;overflow-wrap:anywhere}
tbody tr:last-child td{border-bottom:none}
tbody tr:nth-child(even){background:color-mix(in srgb,var(--surface-2) 45%,transparent)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;font-family:var(--mono);font-size:12.5px;white-space:nowrap}
td code.floc{white-space:nowrap;overflow-wrap:normal}
.bar{position:relative;background:var(--surface-2);border-radius:4px;height:14px;min-width:120px;overflow:hidden}
.bar>i{position:absolute;inset:0 auto 0 0;background:var(--accent);border-radius:4px}
.bar.warn>i{background:var(--warn)} .bar.bad>i{background:var(--bad)} .bar.good>i{background:var(--good)}

/* ---------- grade components ---------- */
.g-a{--grade:var(--good)} .g-b{--grade:var(--accent)} .g-c{--grade:var(--warn)}
.g-d{--grade:var(--bad)} .g-f{--grade:var(--bad)} .g-none{--grade:var(--text-faint)}
.gradecard{display:flex;flex-wrap:wrap;align-items:center;gap:26px;background:var(--surface);border:1px solid var(--border-strong);border-radius:12px;padding:22px 26px;margin:16px 0;background-image:linear-gradient(110deg,color-mix(in srgb,var(--grade) 12%,transparent),transparent 55%)}
.gradecard .letter{font-size:64px;font-weight:700;line-height:1;letter-spacing:-.04em;color:var(--grade);font-variant-numeric:tabular-nums;min-width:96px}
.gradecard .score{font-family:var(--mono);font-size:13px;color:var(--text-dim);margin-top:6px}
.gradecard .what{flex:1 1 260px;min-width:0}
.gradecard .what h2{margin:0 0 4px;font-size:19px}
footer.doc{max-width:1280px;margin:0 auto;padding:0 28px 34px;color:var(--text-faint);font-family:var(--mono);font-size:11.5px}
footer.doc a{color:var(--text-dim)}
@media(max-width:720px){header.doc,main,nav.tabs,footer.doc{padding-left:16px;padding-right:16px} h1.doc-title{font-size:24px} .gradecard .letter{font-size:48px;min-width:72px}}
</style>
</head>
<body>
<header class="doc">
  <div class="doc-label"><!--DOC_LABEL--></div>
  <h1 class="doc-title"><!--DOC_TITLE--></h1>
  <p class="doc-sub"><!--DOC_SUBTITLE--></p>
  <div class="doc-meta"><!--DOC_META--></div>
</header>
<!--DOC_NAV-->
<nav class="tabs" role="tablist"><!--TABS_NAV--></nav>
<main><!--TABS_PANELS--><!--DOC_BODY--></main>
<footer class="doc"><!--DOC_FOOTER--></footer>
<script>
(function(){
  const btns=[...document.querySelectorAll('nav.tabs button')];
  function activate(id){
    document.querySelectorAll('section.panel').forEach(p=>p.classList.toggle('active',p.id===id));
    btns.forEach(b=>b.setAttribute('aria-selected',b.dataset.tab===id?'true':'false'));
    window.dispatchEvent(new CustomEvent('tab:shown',{detail:{id}}));
  }
  btns.forEach(b=>b.addEventListener('click',()=>activate(b.dataset.tab)));
})();
</script>
</body>
</html>
'''

out = pathlib.Path("skills/science-investigation/assets/template.html")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(shell.replace("__TOKENS__", root + "\n" + light), encoding="utf-8")
print(f"wrote {out}")
PY
```

- [ ] **Step 4: Write the body scaffold**

Create `skills/science-investigation/assets/measurement-body.html`. This is the fixed layout the assembler fills — a separate file so code-overview can carry a byte-identical copy and force it, the way it forces the shell:

```html
<!-- Tab scaffold for measurement.html, filled by build_measurement.py.
     Four panels: what the score is, the table it was computed from, the
     findings that reduced it, and what today's data cannot measure at all.
     The Inventory panel is not decoration — the denominator is an authored
     judgment, so the page has to show every row it divided by. -->
<!--TAB_SCORE-->
<!--TAB_INVENTORY-->
<!--TAB_FINDINGS-->
<!--TAB_UNMEASURABLE-->
```

- [ ] **Step 5: Write the assembler**

Create `skills/science-investigation/scripts/build_measurement.py`:

```python
#!/usr/bin/env python3
"""Render an audited inventory into measurement.html.

Every tab comes from the inventory file, so the score in the grade card and the
table it was computed from cannot disagree — there is no path by which a number
is typed onto the page by hand.

The Inventory tab is the load-bearing one. This score's denominator is an
authored judgment: somebody decided which things are measurable and how much
each matters. A reader who cannot see those rows can only accept or reject the
letter, and a letter nobody can dispute is a letter nobody should trust. So the
page ships the whole table — every row with its weight, its credit, the finding
that set that credit, the N it was computed over, and the file:line it came
from.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import date
from pathlib import Path

import rubric

ASSETS = Path(__file__).resolve().parent.parent / "assets"

SEVERITY_CLASS = {"high": "bad", "medium": "warn", "low": "neutral"}


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def json_block(data) -> str:
    """Serialize for embedding in a <script> block.

    `</` is escaped because a `</script>` inside a JSON string would end the
    block early and spill the rest of the payload into the document body.
    """
    return json.dumps(data, separators=(",", ":"), sort_keys=False).replace("</", "<\\/")


def fill(template: str, slots: dict[str, str]) -> str:
    for key, value in slots.items():
        template = template.replace(f"<!--{key}-->", value)
    return template


def grade_class(grade: str) -> str:
    letter = (grade or "").strip()[:1].lower()
    return f"g-{letter}" if letter in "abcdf" else "g-none"


def read_inventory(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise rubric.InventoryError("inventory must be a JSON object")
    rows = data.get("rows") or []
    findings = data.get("findings") or []
    if not isinstance(rows, list) or not isinstance(findings, list):
        raise rubric.InventoryError("`rows` and `findings` must both be lists")
    rubric.validate(rows, findings)
    return {"subject": str(data.get("subject") or ""), "rows": rows, "findings": findings,
            "not_audited": data.get("not_audited") or []}


# --------------------------------------------------------------------------
# tabs
# --------------------------------------------------------------------------

def render_score_tab(scored: dict, rows: list[dict], intro: str, not_audited: list) -> str:
    score = scored["score"]
    shown = "—" if score is None else f"{score:.1f}"
    gates = scored["by_importance"].get("3") or {}
    gate_share = gates.get("share")

    if score is None:
        headline = ('<div class="callout warn"><strong>No measurement content here.</strong> '
                    "Nothing in this unit produces a quality, accuracy or score number, so "
                    "there is nothing to grade. That is an honest null, not a pass.</div>")
    else:
        gate_line = ("no ship-gating numbers were inventoried"
                     if gate_share is None
                     else f"{gate_share:.0f}% of the weight that gates a ship decision is "
                          "soundly measured")
        headline = (f'<div class="callout"><strong>{esc(gate_line)}.</strong> '
                    "The headline score averages every importance level; this line is the "
                    "cut that decides releases.</div>")

    kpis = "".join([
        f'<div class="kpi accent"><div class="n">{len(rows)}</div>'
        '<div class="l">measurable things</div></div>',
        f'<div class="kpi"><div class="n">{scored["weight_measured"]:.2f}</div>'
        '<div class="l">weight measured</div></div>',
        f'<div class="kpi"><div class="n">{scored["weight_total"]:.0f}</div>'
        '<div class="l">weight total</div></div>',
    ])

    breakdown_parts = []
    for level, bucket in scored["by_importance"].items():
        # Precomputed rather than nested inside the f-string: nested same-type
        # quotes in an f-string expression are a syntax error before 3.12, and
        # this repo's floor is 3.11.
        share = "—" if bucket["share"] is None else f"{bucket['share']:.0f}%"
        breakdown_parts.append(
            f"<tr><td>{esc(rubric.IMPORTANCE_LABELS[int(level)])}</td>"
            f'<td class="num">{bucket["rows"]}</td>'
            f'<td class="num">{bucket["total"]:.0f}</td>'
            f'<td class="num">{bucket["measured"]:.2f}</td>'
            f'<td class="num">{share}</td></tr>'
        )
    breakdown_rows = "".join(breakdown_parts)

    gaps = ""
    if not_audited:
        items = "".join(f"<li>{esc(item)}</li>" for item in not_audited)
        gaps = ("<h3>Not audited</h3><p class=\"dim\">A silent gap reads as a clean bill of "
                f"health, so it is listed instead.</p><ul>{items}</ul>")

    return (
        '<!-- tab: Score -->\n'
        f'<section class="gradecard {grade_class(scored["grade"])}">'
        f'<div><div class="letter">{esc(scored["grade"])}</div>'
        f'<div class="score">{shown} / 100</div></div>'
        '<div class="what"><h2>Measurement coverage</h2>'
        '<p class="dim">Importance-weighted measured things over measurable things. '
        'It says how much of what matters is actually measured — not whether the code '
        'is correct.</p></div></section>'
        f"{headline}{intro}"
        f'<div class="kpis">{kpis}</div>'
        "<h3>By importance</h3>"
        '<div class="tbl-wrap"><table><thead><tr><th>Importance</th>'
        '<th class="num">Things</th><th class="num">Weight</th>'
        '<th class="num">Measured</th><th class="num">Share</th></tr></thead>'
        f"<tbody>{breakdown_rows}</tbody></table></div>{gaps}"
    )


def render_inventory_tab(rows: list[dict]) -> str:
    if not rows:
        return ('<!-- tab: Inventory -->\n<p class="dim">Nothing measurable was '
                "inventoried in this unit.</p>")

    parts = []
    for entry in rows:
        # Every branch is computed before the f-string. Nested same-type quotes
        # and backslashes inside f-string expressions are both 3.12+ syntax,
        # and this repo runs on 3.11.
        formula = (f'<br><code class="mono">{esc(entry.get("formula"))}</code>'
                   if entry.get("formula") else "")
        finding = (f'<br><span class="badge bad">{esc(entry["finding"])}</span>'
                   if entry.get("finding") else "")
        n_shown = "—" if entry.get("n") is None else str(entry["n"])
        if entry.get("n_total") not in (None, ""):
            n_shown += f" / {esc(entry['n_total'])}"
        citations = "".join(
            f'<code class="floc">{esc(item)}</code><br>'
            for item in entry.get("evidence") or []
        )
        parts.append(
            "<tr>"
            f"<td><strong>{esc(entry['name'])}</strong>{formula}</td>"
            f'<td>{esc(rubric.IMPORTANCE_LABELS[int(entry["importance"])])}'
            f'<br><span class="faint">{esc(entry.get("importance_reason"))}</span></td>'
            f'<td class="num">{float(entry["credit"]):.2f}</td>'
            f'<td>{esc(entry.get("credit_reason"))}{finding}</td>'
            f'<td class="num">{n_shown}</td>'
            f'<td>{esc(entry.get("consumer") or "nobody reads it")}</td>'
            f"<td>{citations}</td>"
            "</tr>"
        )
    body = "".join(parts)
    return (
        '<!-- tab: Inventory -->\n'
        "<p>Every measurable thing this audit found, with the weight and credit that "
        "produced the score. The denominator is a judgment — dispute the rows, not the "
        "letter.</p>"
        '<div class="tbl-wrap"><table><thead><tr><th>Thing</th><th>Importance</th>'
        '<th class="num">Credit</th><th>Why</th><th class="num">N</th><th>Consumer</th>'
        f"<th>Evidence</th></tr></thead><tbody>{body}</tbody></table></div>"
    )


def render_findings_tab(findings: list[dict]) -> str:
    if not findings:
        return ('<!-- tab: Findings -->\n<p class="dim">No confirmed findings against the '
                "measurement itself. That is not the same as the numbers being right — see "
                "the Unmeasurable tab for what nothing here could check.</p>")

    order = {"high": 0, "medium": 1, "low": 2}
    ranked = sorted(findings, key=lambda f: order.get(str(f.get("severity")), 3))
    cards = "".join(
        f'<div class="card"><div><span class="badge '
        f'{SEVERITY_CLASS.get(str(item.get("severity")), "neutral")}">'
        f'{esc(item.get("severity"))}</span> <code>{esc(item.get("id"))}</code></div>'
        f"<h3>{esc(item.get('title'))}</h3><p>{esc(item.get('detail'))}</p>"
        f'<p class="dim"><strong>Blast radius:</strong> {esc(item.get("blast_radius") or "not stated")}</p>'
        + "".join(f'<code class="floc">{esc(cite)}</code> ' for cite in item.get("evidence") or [])
        + "</div>"
        for item in ranked
    )
    return ('<!-- tab: Findings -->\n<p>Ranked by likelihood × blast radius on the decisions '
            f"the number drives, not by how clever the finding is.</p>{cards}")


def render_unmeasurable_tab(rows: list[dict]) -> str:
    gaps = [entry for entry in rows if entry.get("status") == "unmeasurable"]
    if not gaps:
        return ('<!-- tab: Unmeasurable -->\n<p class="dim">Nothing was found that today\'s '
                "data structurally cannot measure.</p>")
    body = "".join(
        f"<tr><td><strong>{esc(entry['name'])}</strong></td>"
        f'<td>{esc(rubric.IMPORTANCE_LABELS[int(entry["importance"])])}</td>'
        f"<td>{esc(entry.get('unmeasurable_reason'))}</td></tr>"
        for entry in gaps
    )
    return (
        '<!-- tab: Unmeasurable -->\n'
        "<p>Structurally unmeasurable with today's data — recall with no gold set, "
        "calibration with no outcomes, causal effect with no control arm. These are "
        "<strong>not defects</strong>, and they stay in the denominator on purpose: "
        "dropping them is how silence gets read as success.</p>"
        '<div class="tbl-wrap"><table><thead><tr><th>Thing</th><th>Importance</th>'
        f"<th>What today's data cannot supply</th></tr></thead><tbody>{body}</tbody>"
        "</table></div>"
    )


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


def build_metadata(inventory: dict, scored: dict, name: str, args) -> dict:
    return {
        "schema": rubric.DOCUMENT_SCHEMA,
        "scope": "package",
        "package": name,
        "generated": args.generated or date.today().isoformat(),
        "commit": args.commit or "",
        "score": scored["score"],
        "grade": scored["grade"],
        "weight_total": scored["weight_total"],
        "weight_measured": scored["weight_measured"],
        "by_importance": scored["by_importance"],
        "rows": inventory["rows"],
        "findings": inventory["findings"],
        "not_audited": inventory["not_audited"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render an audited measurement inventory.")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--intro-file", type=Path, default=None)
    parser.add_argument("--commit", default="")
    parser.add_argument("--generated", default="")
    parser.add_argument("--template", type=Path, default=ASSETS / "template.html")
    parser.add_argument("--body", type=Path, default=ASSETS / "measurement-body.html")
    args = parser.parse_args(argv)

    try:
        inventory = read_inventory(args.inventory)
    except rubric.InventoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {args.inventory}: {exc}", file=sys.stderr)
        return 2

    scored = rubric.score_inventory(inventory["rows"])

    intro = ""
    if args.intro_file:
        try:
            intro = args.intro_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"warning: {args.intro_file}: {exc}", file=sys.stderr)
    if not intro.strip():
        intro = ('<div class="callout warn">No written summary was supplied. The prose that '
                 "says what this unit measures and why is the one part no script can "
                 "produce, and this page is weaker without it.</div>")

    body = fill(args.body.read_text(encoding="utf-8"), {
        "TAB_SCORE": render_score_tab(scored, inventory["rows"], intro,
                                      inventory["not_audited"]),
        "TAB_INVENTORY": render_inventory_tab(inventory["rows"]),
        "TAB_FINDINGS": render_findings_tab(inventory["findings"]),
        "TAB_UNMEASURABLE": render_unmeasurable_tab(inventory["rows"]),
    })
    fragments = [part for part in body.split("<!-- tab:") if part.strip()]
    nav, sections = panels([f"<!-- tab:{part}" for part in fragments])

    meta = build_metadata(inventory, scored, args.name, args)
    sections += (f'\n<script type="application/json" id="measurement-meta">'
                 f"{json_block(meta)}</script>")

    page = fill(args.template.read_text(encoding="utf-8"), {
        "DOC_TITLE": esc(f"{args.name} — Measurement"),
        "DOC_LABEL": "MEASUREMENT AUDIT",
        "DOC_SUBTITLE": esc(args.subtitle or
                            f"Can the numbers {args.name} reports be believed?"),
        "DOC_META": esc(" · ".join(part for part in (
            f"generated {meta['generated']}", meta["commit"],
            f"{len(inventory['rows'])} measurable thing(s)") if part)),
        "TABS_NAV": nav,
        "TABS_PANELS": sections,
        "DOC_BODY": "",
        "DOC_FOOTER": ("Generated by science-investigation. The score is measurement "
                       "coverage, not code quality: it says how much of what matters is "
                       "measured, never whether the code is correct."),
    })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    shown = "—" if scored["score"] is None else f"{scored['score']:.1f}"
    print(f"wrote {args.out} — {scored['grade']} ({shown})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/science_investigation/test_build_measurement.py -q`
Expected: PASS, 14 tests.

- [ ] **Step 7: Verify the repo gates**

```bash
ruff check skills/science-investigation
python skills/python-code-doctor/scripts/find_exception_issues.py skills/science-investigation/scripts --format json
python skills/python-code-doctor/scripts/find_resource_leaks.py skills/science-investigation/scripts --format json
pytest tests/science_investigation -q
```

Expected: ruff silent, detectors print `[]`, suite green.

- [ ] **Step 8: Commit**

```bash
git add skills/science-investigation/assets skills/science-investigation/scripts/build_measurement.py tests/science_investigation/test_build_measurement.py
git commit -m "science-investigation: render the audit as a graded, tabbed document

Every tab comes from the inventory file, so the grade card and the table it
was computed from cannot disagree — no number is typed onto the page by hand.
The Inventory tab is the load-bearing one: this denominator is an authored
judgment, and a reader who cannot see the rows can only accept or reject the
letter. An invalid inventory writes no document at all, because a page with
an unarguable score is worse than no page.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Scoping and the repository roll-up

**Files:**
- Modify: `skills/science-investigation/scripts/build_measurement.py`
- Test: `tests/science_investigation/test_build_measurement_scope.py`

**Interfaces:**
- Consumes: everything from Task 2.
- Produces, added to the CLI:
  - `--root-dir DIR` (repeatable) — keep only rows whose **defining** evidence (`evidence[0]`) sits under one of these
  - `--scope DIR` (repeatable) — defaults to the `--root-dir` values
  - `--repo DIR` — the root the evidence paths are relative to (default `.`)
  - `--root` — repository scope; sets `scope: "repository"` in the metadata and keeps every row
  - `--package NAME:PATH` (repeatable, `--root` only) — a package's `measurement.html`, read for its grade
- Produces, added to the metadata: `rows_out_of_scope: int`, and at root scope `packages: [{"name", "score", "grade", "rows", "generated"}]`

- [ ] **Step 1: Write the failing test**

Create `tests/science_investigation/test_build_measurement_scope.py`:

```python
"""Scoping and roll-up.

One repo-wide audit produces every package's page, because a script pointed at
src/billing cannot see evals/ and would report a thoroughly-measured pipeline
as having no measurement — a fabricated null that looks exactly like an honest
one. So the rows are gathered once and partitioned here.
"""

import json
import re
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[2] / "skills" / "science-investigation"
SCRIPT = SKILL / "scripts" / "build_measurement.py"


def meta_of(page: Path) -> dict:
    match = re.search(r'id="measurement-meta">(.*?)</script>',
                      page.read_text(encoding="utf-8"), re.S)
    assert match
    return json.loads(match.group(1).replace("<\\/", "</"))


def row(name, evidence, importance=3, credit=1.0, n=100) -> dict:
    return {"name": name, "importance": importance,
            "importance_reason": "gates the release", "credit": credit,
            "credit_reason": "full labelled set", "finding": "", "n": n,
            "n_total": n, "formula": "x", "consumer": "ci.yml",
            "evidence": evidence, "status": "measured", "unmeasurable_reason": ""}


def inventory(tmp_path) -> Path:
    payload = {
        "schema": "measurement-inventory/1", "subject": "repo",
        "rows": [
            row("billing_accuracy", ["src/billing/metrics.py:10"]),
            # Defined in evals/, but it scores billing's output. It belongs to
            # whoever DEFINES it, which is why evidence[0] decides.
            row("billing_judge", ["evals/judge.py:4", "src/billing/api.py:80"]),
            row("search_ndcg", ["src/search/rank.py:22"]),
        ],
        "findings": [], "not_audited": [],
    }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_root_dir_keeps_only_the_rows_defined_under_it(run_script, tmp_path):
    out = tmp_path / "billing.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "billing",
               "--repo", tmp_path, "--root-dir", "src/billing")

    meta = meta_of(out)
    assert [r["name"] for r in meta["rows"]] == ["billing_accuracy"]
    assert meta["rows_out_of_scope"] == 2


def test_a_row_defined_elsewhere_belongs_to_the_package_that_defines_it(run_script, tmp_path):
    out = tmp_path / "evals.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "evals",
               "--repo", tmp_path, "--root-dir", "evals")

    assert [r["name"] for r in meta_of(out)["rows"]] == ["billing_judge"]


def test_a_package_with_no_rows_scores_null_rather_than_vanishing(run_script, tmp_path):
    out = tmp_path / "utils.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "utils",
               "--repo", tmp_path, "--root-dir", "src/utils")

    meta = meta_of(out)
    assert meta["score"] is None
    assert meta["rows"] == []
    assert "no measurement content" in out.read_text(encoding="utf-8").lower()


def test_scope_can_differ_from_root_dir(run_script, tmp_path):
    out = tmp_path / "wide.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "billing",
               "--repo", tmp_path, "--root-dir", "src/billing", "--scope", "src")

    assert {r["name"] for r in meta_of(out)["rows"]} == {"billing_accuracy", "search_ndcg"}


def test_root_scope_keeps_every_row(run_script, tmp_path):
    out = tmp_path / "root.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "repo",
               "--repo", tmp_path, "--root")

    meta = meta_of(out)
    assert meta["scope"] == "repository"
    assert len(meta["rows"]) == 3


def test_the_root_document_tables_every_package_it_was_given(run_script, tmp_path):
    package = tmp_path / "billing.html"
    run_script(SCRIPT, "--out", package, "--inventory", inventory(tmp_path), "--name", "billing",
               "--repo", tmp_path, "--root-dir", "src/billing")

    out = tmp_path / "root.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "repo",
               "--repo", tmp_path, "--root", "--package", f"billing:{package}")

    packages = meta_of(out)["packages"]
    assert packages[0]["name"] == "billing"
    assert packages[0]["grade"] == "A+"
    assert packages[0]["generated"] is True


def test_a_package_with_no_document_stays_in_the_table_marked_not_generated(run_script, tmp_path):
    out = tmp_path / "root.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "repo",
               "--repo", tmp_path, "--root",
               "--package", f"ghost:{tmp_path / 'absent.html'}")

    row_ = meta_of(out)["packages"][0]
    assert row_["name"] == "ghost"
    assert row_["generated"] is False
    assert row_["score"] is None


def test_a_null_scoring_package_is_listed_as_no_measurement_content(run_script, tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"schema": "measurement-inventory/1", "subject": "utils",
                                 "rows": [], "findings": [], "not_audited": []}),
                     encoding="utf-8")
    package = tmp_path / "utils.html"
    run_script(SCRIPT, "--out", package, "--inventory", empty, "--name", "utils")

    out = tmp_path / "root.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "repo",
               "--repo", tmp_path, "--root", "--package", f"utils:{package}")

    text = out.read_text(encoding="utf-8")
    assert "no measurement content" in text.lower()
    assert meta_of(out)["packages"][0]["score"] is None


def test_package_is_rejected_without_root(run_script, tmp_path):
    result = run_script(SCRIPT, "--out", tmp_path / "x.html", "--inventory", inventory(tmp_path),
                        "--name", "x", "--package", "a:b.html", expect_rc=2)

    assert "--root" in result.stderr
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/science_investigation/test_build_measurement_scope.py -q`
Expected: FAIL — `unrecognized arguments: --root-dir`.

- [ ] **Step 3: Add scoping to `build_measurement.py`**

Add these functions above `main`:

```python
def defining_path(entry: dict) -> str:
    """Where the measurable thing is *defined* — the first evidence citation.

    A metric defined in evals/ that scores a service's output cites both. It
    belongs to whoever defines it: assigning it to the service would give two
    packages the same row and double-count it in the repository denominator.
    """
    for citation in entry.get("evidence") or []:
        text = str(citation).strip()
        if text:
            return text.split(":", 1)[0].replace("\\", "/").removeprefix("./")
    return ""


def in_scope(entry: dict, scopes: list[str]) -> bool:
    if not scopes:
        return True
    path = defining_path(entry)
    return any(path == scope or path.startswith(scope.rstrip("/") + "/")
               for scope in scopes)


def read_package_grade(name: str, path: Path) -> dict:
    """A package row for the root table, read back out of its own document."""
    blank = {"name": name, "score": None, "grade": rubric.UNGRADED,
             "rows": 0, "generated": False}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return blank
    match = re.search(r'id="measurement-meta">(.*?)</script>', text, re.S)
    if not match:
        return blank
    try:
        meta = json.loads(match.group(1).replace("<\\/", "</"))
    except json.JSONDecodeError:
        return blank
    return {"name": name, "score": meta.get("score"),
            "grade": meta.get("grade", rubric.UNGRADED),
            "rows": len(meta.get("rows") or []), "generated": True}


def render_package_table(packages: list[dict]) -> str:
    if not packages:
        return ""
    parts = []
    for item in packages:
        score = "—" if item["score"] is None else f"{item['score']:.1f}"
        if not item["generated"]:
            state = "not generated"
        elif item["score"] is None:
            state = "no measurement content"
        else:
            state = "graded"
        parts.append(
            f"<tr><td>{esc(item['name'])}</td>"
            f'<td class="num">{score}</td>'
            f'<td class="num">{esc(item["grade"])}</td>'
            f'<td class="num">{item["rows"]}</td>'
            f"<td>{state}</td></tr>"
        )
    body = "".join(parts)
    return ("<h3>Packages</h3><p class=\"dim\">A package with nothing measurable is listed "
            "as having no measurement content, not as passing.</p>"
            '<div class="tbl-wrap"><table><thead><tr><th>Package</th><th class="num">Score</th>'
            '<th class="num">Grade</th><th class="num">Things</th><th>State</th></tr></thead>'
            f"<tbody>{body}</tbody></table></div>")
```

Add `import re` to the imports. Then in `main`, add the arguments:

```python
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--root-dir", action="append", default=[], dest="root_dirs")
    parser.add_argument("--scope", action="append", default=[], dest="scopes")
    parser.add_argument("--root", action="store_true")
    parser.add_argument("--package", action="append", default=[], dest="packages",
                        metavar="NAME:PATH")
```

After `args = parser.parse_args(argv)`, add the guard and the partition:

```python
    if args.packages and not args.root:
        parser.error("--package builds the repository roll-up table and needs --root")
```

After `inventory = read_inventory(...)` succeeds, partition before scoring:

```python
    total_rows = len(inventory["rows"])
    if not args.root:
        scopes = [str(s).replace("\\", "/").strip("/")
                  for s in (args.scopes or args.root_dirs)]
        inventory["rows"] = [entry for entry in inventory["rows"]
                             if in_scope(entry, scopes)]
        kept_findings = {str(entry.get("finding")) for entry in inventory["rows"]
                         if entry.get("finding")}
        inventory["findings"] = [f for f in inventory["findings"]
                                 if str(f.get("id")) in kept_findings]
    out_of_scope = total_rows - len(inventory["rows"])
    if out_of_scope:
        print(f"note: {out_of_scope} row(s) dropped as defined outside this unit",
              file=sys.stderr)
```

In `build_metadata`, take the extra values and record them:

```python
def build_metadata(inventory: dict, scored: dict, name: str, args,
                   out_of_scope: int, packages: list[dict]) -> dict:
    meta = {
        "schema": rubric.DOCUMENT_SCHEMA,
        "scope": "repository" if args.root else "package",
        ...
        "rows_out_of_scope": out_of_scope,
    }
    if args.root:
        meta["packages"] = packages
    return meta
```

Build the package rows before the metadata and append the table to the Score tab:

```python
    packages = []
    for spec in args.packages:
        label, _, location = spec.partition(":")
        packages.append(read_package_grade(label.strip(), Path(location.strip())))
```

Then widen `render_score_tab` by one argument and emit the table after the gaps
block. Change its signature and its final return to exactly:

```python
def render_score_tab(scored: dict, rows: list[dict], intro: str,
                     not_audited: list, package_table: str = "") -> str:
    ...
        f"<tbody>{breakdown_rows}</tbody></table></div>{gaps}{package_table}"
```

and its call site in `main` to:

```python
        "TAB_SCORE": render_score_tab(scored, inventory["rows"], intro,
                                      inventory["not_audited"],
                                      render_package_table(packages)),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/science_investigation -q`
Expected: PASS — both new files plus the Task 1 and 2 suites.

- [ ] **Step 5: Commit**

```bash
git add skills/science-investigation/scripts/build_measurement.py tests/science_investigation/test_build_measurement_scope.py
git commit -m "science-investigation: scope one repo-wide audit into per-package pages

A script pointed at src/billing cannot see evals/, so it would report a
thoroughly-measured pipeline as having no measurement — a fabricated null
indistinguishable from an honest one. The audit runs once at the root and the
rows partition by where each thing is DEFINED, so a metric that lives in
evals/ and scores billing belongs to evals and is counted once.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: The workflow section, evals, and README

**Files:**
- Modify: `skills/science-investigation/SKILL.md` (new section after step 6, before *Red flags*)
- Modify: `evals/science-investigation/evals.json`
- Modify: `README.md`
- Test: `tests/science_investigation/test_skill_contract.py`

**Interfaces:**
- Consumes: the CLI from Tasks 2 and 3.
- Produces: no code.

- [ ] **Step 1: Write the failing test**

Create `tests/science_investigation/test_skill_contract.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/science_investigation/test_skill_contract.py -q`
Expected: FAIL — `SKILL.md` says nothing about the builder.

- [ ] **Step 3: Add the section to `SKILL.md`**

Insert after step 6 (*Verify counts before shipping*), before *Red flags*:

```markdown
### 7. Ship it as a graded document

The report can be prose. It is better as `measurement.html` — a tabbed page
carrying a **measurement-coverage score**, which answers the question prose
tends to dodge: *how much of what matters here is actually measured?*

Write the audit as an inventory file (`measurement-inventory/1`), one row per
measurable thing:

```json
{"schema": "measurement-inventory/1", "subject": "billing",
 "rows": [{"name": "judge_accuracy", "importance": 3,
           "importance_reason": "gates the weekly model rollout",
           "credit": 0.25, "credit_reason": "computed over 3 labelled rows of 412",
           "finding": "small_n", "n": 3, "n_total": 412,
           "formula": "mean(judge_score == gold)",
           "consumer": "scripts/rollout.py:88 gates the release",
           "evidence": ["evals/judge.py:41"], "status": "measured"}],
 "findings": [{"id": "small_n", "severity": "high",
               "title": "Judge accuracy rests on n=3",
               "detail": "3 of 412 rows carry a gold label.",
               "evidence": ["evals/judge.py:41"],
               "blast_radius": "the weekly rollout gate"}],
 "not_audited": ["the analytics dashboard — no access"]}
```

```bash
python "$SKILL/scripts/build_measurement.py" --out docs/measurement.html \
  --inventory $WORK/inventory.json --name billing --intro-file $WORK/intro.html
```

**Importance is blast radius**, and the reason must name the decision: `3` gates
a ship decision, `2` informs a decision someone makes, `1` is informational.
**Credit is set by the worst confirmed finding**: `1.0` nothing found, `0.5` one
medium finding, `0.25` one high finding, `0.0` not measured — or unmeasurable
with today's data.

    score = 100 × Σ(importance × credit) / Σ(importance)

**Everything measurable stays in the denominator, including what today's data
structurally cannot measure** — recall with no gold set, calibration with no
outcomes, causal effect with no control arm. Dropping those rows is how silence
gets read as success, and it is the single easiest way to make this score a lie.

The builder refuses an inventory that cannot support its own score, and writes
no document when it does: a credit below 1.0 with no finding named, an aggregate
with no N, an unmeasurable row carrying credit. Fix the row rather than
loosening the rule — the whole value of the number is that the table under it
can be argued with.

A unit with nothing measurable scores **null**, not zero and not A+, and says
"no measurement content here" on the page. Most packages in a typical repo are
that page, and that is the correct output.

`--root-dir` partitions one repository-wide audit into per-package pages by
where each thing is *defined*; `--root` with `--package name:path` builds the
repository roll-up. Run the audit **once from the repository root**: pointed at
a subdirectory, `find_metrics.py` cannot see `evals/` and reports a
thoroughly-measured pipeline as having no measurement.
```

- [ ] **Step 4: Append the eval cases**

Add to `evals/science-investigation/evals.json`:

```json
{
  "id": "grades-measurement-coverage-not-defect-density",
  "prompt": "Audit the measurement in this repo and give me a page I can share with the team.",
  "expected_output": "Runs the enumeration scripts, confirms candidates by reading, then writes an inventory file and builds measurement.html with build_measurement.py. Every row carries its importance with the decision named, its credit with the finding that set it, and the N. Explains that the score is measurement coverage — how much of what matters is measured — and not a verdict on code quality."
},
{
  "id": "a-package-with-no-metrics-scores-null",
  "prompt": "Run the measurement audit over src/utils — it's a helpers module.",
  "expected_output": "Reports that there is no real measurement content in one line and produces a page scoring null, not zero and not A+. Does not manufacture metrics or pad the audit to fill the tabs."
}
```

- [ ] **Step 5: Update the README**

Extend the `science-investigation` row of `README.md`'s skill table with:

```
Ships the audit as a graded measurement.html — importance-weighted measured things over measurable things, with the inventory table the score was computed from.
```

- [ ] **Step 6: Verify everything**

```bash
pytest tests/science_investigation -q
python tools/validate_skills.py
ruff check .
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add skills/science-investigation/SKILL.md evals/science-investigation/evals.json README.md tests/science_investigation/test_skill_contract.py
git commit -m "science-investigation: document the graded measurement document

The score answers the question prose tends to dodge — how much of what
matters here is actually measured. Documenting the denominator rule beside
the workflow is the point: everything measurable stays in it, including what
today's data structurally cannot measure, because dropping those rows is how
silence gets read as success.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Self-review notes

Checked against the spec's *Measurement — a coverage ratio* section:

| Spec requirement | Task |
|---|---|
| `score = 100 × Σ(importance × credit) / Σ(importance)` | 1 |
| Importance 3/2/1 with the decision named | 1 (`validate` rejects a blank `importance_reason`) |
| Credit ladder 1.0 / 0.5 / 0.25 / 0.0, worst finding wins | 1 |
| Zero measurable things → null, never A+ | 1, 2 |
| Letter bands duplicated, not shared | 1 (the CI pin lands in Plan 3) |
| Every row on the Inventory tab with weight, credit, finding, N, evidence, consumer | 2 |
| No unattributed deduction | 1 (`validate`) |
| Unmeasurable stays in the denominator, labelled | 1, 2 (its own tab) |
| "Not measured" is null, never 0.0 | 1 (`n` required above zero credit; status split) |
| Ship-gate share on the Score tab | 1 (`by_importance`), 2 |
| `measurement-meta` block, `measurement/1`, `</` escaped | 2 |
| `--root-dir` / `--scope` with `build_health.py`'s meaning | 3 |
| Root `packages[]` array | 3 |
| Its own default template so the skill works standalone | 2 |
| Tokens byte-identical to code-visualization's | 2 (test in this plan; CI wiring in Plan 3) |

**One refinement on the spec**, worth knowing before Plan 3: the spec assigns
`measurement-body.html` to code-overview's assets. This plan puts the canonical
copy in **science-investigation** (which needs it to render standalone) and has
code-overview carry a byte-identical copy pinned by CI — the pattern the repo
already uses for code-visualization and pr-visualization's shared scripts. The
observable behaviour is identical; the difference is that there is one source of
truth instead of two files that merely look alike.
