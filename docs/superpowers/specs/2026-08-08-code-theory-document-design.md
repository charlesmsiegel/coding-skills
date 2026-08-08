# code-overview's fifth document — theory.html, graded in the Naur sense

Date: 2026-08-08
Status: approved design, not yet implemented

## The question this answers

`code-overview` builds four documents per unit. Three of them describe the code:
what it contains (`codemap`), how dense its detectable defects are (`health`),
and how much of what matters is measured (`measurement`). None of them asks the
question Naur says is the actual deliverable — **does this code express a
coherent theory of the problem it solves?**

Peter Naur's argument in *Programming as Theory Building* is that a program's
real content lives in the builders' mental model of how the world maps onto the
code, and that when the model is lost the surviving text degrades however
correct it was. His evidence is a finding rather than a metaphor: a team
inheriting a compiler, given full documentation, annotated texts, design
discussion *and* access to the original authors, still proposed extensions that
"effectively destroyed its power and simplicity" — which the authors spotted
instantly. The text was complete. The theory was not in it.

This matters more when a model writes the code. Passing tests means no
*anticipated* failure occurred, and when the same model wrote code and tests,
"nobody thought of" covers the same blind spots twice.

So: a fifth document, `theory.html`, carrying a graded judgment of how well a
unit's code expresses a theory.

## The honest problem with grading this

The other two grades rest on something outside the grader. Health is a density
of defects a detector found. Measurement divides an inventory whose every row
carries `file:line` evidence. **This grade is a judgment**, and
`theory-building`'s own SKILL.md names the hazard:

> A model auditing its own abstractions is partly circular — the same weakness
> that produced repetition-with-variants also evaluates whether the repetition
> was warranted. This skill narrows the gap; it doesn't close it.

This design does not close it either. Three judges of the same model family
share priors, so the panel reduces **variance** without removing **bias**. What
it genuinely buys is *visible disagreement* and *cited evidence*, which is what
lets a human overrule it cheaply. The page says so beside the grade.

## The rubric

Five dimensions, drawn from `theory-building`'s four gates, weighted by how much
each discriminates in practice.

| Key | Dimension | Gate | Weight | The question |
|---|---|---|---|---|
| `absorption` | Absorption | 1.3 | 30 | Take 3–5 plausible next requirements. Does each land as a natural extension, or need a patch bolted alongside? |
| `world_mapping` | World-mapping | 1.1 | 25 | Can each part be mapped to an aspect of the problem, and each aspect located in the code — including where the model *stops*? |
| `abstraction` | Abstraction | 3 | 20 | Do abstractions carry domain names, make unwritten cases expressible, reduce what must be held in mind? Or is it factorization with boolean parameters? |
| `justification` | Justification | 1.2 | 15 | Is it recoverable *why* each part is what it is, and what was rejected? |
| `honest_limits` | Honest limits | 2, 4 | 10 | Are assumptions no test pins down named? Is duplication honest rather than papered over? Is there reinvention where the stdlib or an existing module would serve? |

Weights total 100. **Absorption carries the most** because Naur singles it out as
the capability nobody checks — "someone who holds the theory is *already*
prepared for the demands that will arrive" — and because a plausible-looking
codebase fails it more often than it fails the others.

### The ladder

Four coarse steps, not free scoring. Judgment picks a step; arithmetic makes the
letter. A coarse ladder is deliberate: it confines variance to a step choice
instead of inviting a 73 that means nothing more than a 68.

| Step | Meaning |
|---|---|
| `1.0` | **holds** — demonstrably present, with evidence |
| `0.5` | **partial** — present in places, absent in others, and the page says which |
| `0.25` | **strained** — nominally present, but the evidence undercuts it |
| `0.0` | **absent** |

```
score = 100 × Σ(weight × step) / Σ(weight)
```

Letter bands are health's and measurement's, copied verbatim, so a B− means one
score range on all three pages. This is the **third** copy of that table; CI
pins all three identical (see *Integration*).

## The panel

**Three independent judges per unit**, none seeing another's verdict.

Each returns a structured verdict:

```json
{"schema": "theory-verdict/1",
 "unit": "billing",
 "theory": "≤3 sentences: what this models, in the world",
 "instead_of": "the other coherent reading of the problem, and why not it",
 "trivial": false,
 "trivial_reason": "",
 "dimensions": {
   "absorption": {"step": 0.5,
                  "rationale": "two of four rehearsals need a parallel path",
                  "evidence": ["src/billing/charge.py:88"]}},
 "rehearsals": [{"requirement": "refunds settle in a second currency",
                 "verdict": "patch",
                 "why": "currency is assumed at module scope",
                 "evidence": ["src/billing/money.py:12"]}]}
```

**Median per dimension** sets the score.

**Disagreement is itself a finding.** Where the three judges differ by **two
rungs or more** on a dimension, the page says so plainly: three careful readers
could not agree on what this code models. Reporting only the median would hide
the most interesting thing the panel learned — and low agreement is a fact about
the code's clarity, not about the judges.

*Rungs, not arithmetic distance.* The ladder is ordinal — `absent`, `strained`,
`partial`, `holds` at indices 0–3 — so disagreement is measured as
`max(index) − min(index)`. `holds` beside `strained` is two rungs and flags;
`holds` beside `partial` is one and does not. Using the numeric values instead
would make the same disagreement register differently depending on where on the
ladder it sat, since the steps are deliberately unevenly spaced.

Independence is structural, not requested: each judge is a separate agent given
the code and the doctrine, never another judge's output. A judge that saw the
others would converge on them, and the spread — the one signal the panel exists
to produce — would vanish.

## The exemption floor

A package substantial enough to need a theory and lacking one **scores badly**.
That is the finding. Recording it as "unmeasurable" would be the dodge this
whole document set refuses — and Naur is explicit: "If the theory can't be
stated, there isn't one. What exists instead is an ad-hoc answer shaped like
code."

But `theory-building` is equally explicit that applying these gates to a
five-line throwaway "is its own kind of failure". So a genuinely trivial unit is
exempt, and exemption requires **both** gates:

1. **A computed size test** — at most **3 source files** and at most **200 LOC**,
   using the same sizing `build_health.py` already performs.
2. **At least 2 of the 3 judges independently** marking `trivial: true`, each
   with a `trivial_reason`.

Both, because either alone is gameable: size alone exempts a dense 150-line
parser, and judgment alone lets a bad grade be talked away. An exempted unit
scores `null` — "too small to warrant a theory" — and the floor is printed on
the page so a reader sees what was applied and can disagree.

`null` here means the same as on the measurement page: not zero, not a pass.

**The floor does not apply at repo scope.** A repository whose every package is
individually trivial still has a system-level question worth asking — whether the
units compose into anything coherent — and that question is not made trivial by
the size of its parts. The repo-level page is therefore always graded, and the
2-of-3 trivial vote is not offered to its judges.

## Honesty rules

- **Every dimension ships as a row** — step, rationale, citation, and the spread
  across the three judges. A letter with no table is not a shippable page.
- **No unattributed step.** Any step below `1.0` must cite the evidence that
  lowered it; the validator rejects one that does not.
- **The absorption rehearsals ship verbatim**, so a reader can dispute the
  requirements chosen rather than only the verdicts. A rehearsal set of four
  softballs is a weaker audit than a hard one, and only the reader can say so.
- **The theory statement is the deliverable, not the grade.** It is what a next
  reader actually needs — the artifact Naur says gets lost when only the diff is
  delivered. The grade is a byproduct of having tried to write it.
- **The page says what it is**: a reading, not a measurement. Metadata stamps
  model, date and panel size, and the page states that comparing letters
  produced by different models is meaningless.
- **The circularity is stated on the page**, in a line, beside the grade.

## Integration

Five document types:

```
docs/{summary,codemap,health,measurement,theory}.html            repo level
<pkg-root>/docs/{summary,codemap,health,measurement,theory}.html per package
```

- **`code-overview` owns the code**: `scripts/theory_rubric.py` (dimensions,
  ladder, median, spread, exemption, arithmetic — pure, no I/O) and
  `scripts/build_theory.py` (renders `theory.html` from the three verdicts),
  plus `assets/theory-body.html`.
- **`theory-building` stays code-free.** It gains a pointer to this use and
  nothing else. Its character is doctrine, not tooling, and the judging is a
  `code-overview` concern: `theory-building` governs code being written now,
  while this judges an existing unit. It supplies the doctrine the panel judges
  against.
- **`DOC_KINDS` grows to five**, so navigation, existence-checking,
  percent-encoding and the `--check` gate extend without per-kind code — the
  property Task 6 of the four-document plan verified.
- **`summary.html` carries three grade cards**, all read back out of the
  generated documents rather than passed in.
- **Repo level** asks the sharper question — does the *system* have a theory —
  over its own panel of three, with `--package name:path` rows read back from
  each package's page.
- **CI**: the letter-band pin in `tests/test_shared_assets.py` extends from two
  `rubric.py` copies to three.

Tabs: `Grade` · `Theory` (the statement, the rejected reading, the rehearsals) ·
`Dimensions` (the rows, with spread) · `Disagreement` (only where spread ≥ 2).

## Testing

- **Rubric arithmetic**: median per dimension; spread; weighted mean; both
  exemption gates; `null` when exempt; a worked table → its letter.
- **Validator**: an off-ladder step; a step below `1.0` with no citation; an
  exemption passing size but failing the 2-of-3 vote, and vice versa; a verdict
  missing `theory` or `instead_of`.
- **Rendering pinned by mutation**, through the `panel_of` helper that cuts at
  the panel's own closing tag — never a whole-page substring. Every renderer
  must die when deleted; this document embeds its verdicts in metadata exactly
  as the measurement page does, so a whole-page assertion would match the JSON.
- **The disagreement banner** appears when spread ≥ 2 and not otherwise.
- **Navigation** across five types; `inject_nav.py --check` exits 0.
- **Evals** for the judgment half: a shapeless package that must score badly
  rather than null, and a three-file utility that must be exempted.

## Explicitly out of scope

- Changing `theory-building`'s doctrine, or adding scripts to it.
- Any attempt to remove the circularity. It is narrowed and stated, not closed.
- Grading anything but a unit in the package map — no per-file theory scores.
