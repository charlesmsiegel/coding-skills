# code-overview on three companions — code-doctor, science-investigation, code-visualization

Date: 2026-08-07
Status: approved design, not yet implemented

## The question this answers

`code-overview` orchestrates two companions today: `code-visualization` for the
atlas, and one of three language doctors for the grade. It picks the doctor
itself, per package, from the language recorded in the map — and it has a whole
section (*No doctor for this language*) devoted to what to do when no doctor
fits.

Two things change that:

- `code-doctor` is being built as a language-agnostic reviewer that routes to the
  language specialists itself. Once it exists, code-overview should call **one**
  doctor and let it decide what else runs.
- `science-investigation` audits whether a system's numbers can be believed. That
  is a dimension of codebase quality neither the atlas nor the grade can see: a
  package can grade A- on defect density and be measured by a metric computed
  over three labelled examples.

So the document set grows a fourth type, code-overview stops choosing doctors,
and one template governs the appearance of all four so they read as one artifact
rather than three tools' output.

## The document set

```
docs/code-overview.json                                    the package map
docs/{summary,codemap,health,measurement}.html             repo level
<pkg-root>/docs/{summary,codemap,health,measurement}.html  one set per package
```

| Document | Produced by | Shape | Graded |
|---|---|---|---|
| `summary.html` | code-overview `build_summary.py` | single page — the navigation portal | no; reads both grades back out |
| `codemap.html` | code-visualization `assemble.py` | tabbed | no |
| `health.html` | code-overview `build_health.py`, from code-doctor's JSON | tabbed: Grade · Findings · Candidates · Coverage | 0–100 defect density |
| `measurement.html` | science-investigation's own assembler | tabbed: Score · Inventory · Findings · Unmeasurable | 0–100 measurement coverage |

`health.html` becomes tabbed because code-doctor's report carries two output
classes that must never be read as one. **Candidates get their own tab, and that
tab states on its face that nothing in it touched the grade.** A dead-function
candidate is a lead, not a defect; a page that mixes the two is how a tool starts
recommending the deletion of live code.

**Every package gets a `measurement.html`.** A package with nothing measurable is
a short page — `score: null`, one sentence — and a row in the repo document's
*no measurement content* list. Not an A+, and not a missing file. Most packages
in a typical repo will be that page, and that is the correct output.

## Templates and visual consistency

`code-overview/assets/` owns the canonical shell:

| File | Role |
|---|---|
| `template.html` | the shell — design tokens, header, nav slot, **tab bar + tab JS**, footer |
| `health-body.html` | the health layout, extended for its four tabs |
| `summary-body.html` | the portal layout, extended for two grade cards |
| `measurement-body.html` | the tab scaffold — the Score / Inventory / Findings / Unmeasurable layout, with slots science-investigation's assembler fills |

Slots: `DOC_LABEL`, `DOC_TITLE`, `DOC_SUBTITLE`, `DOC_META`, `DOC_NAV`,
`DOC_TABS`, `DOC_BODY`, `DOC_FOOTER`. `DOC_TABS` empty renders an untabbed page,
which is what `summary.html` uses — one shell serves both shapes.

The four documents are governed at two different depths, and the difference is
deliberate. `health.html` and `measurement.html` get a **body template** because
their layout is fixed — a grade card, a category table, an inventory table — and
should be identical in every repo. `codemap.html` gets **the shell only**,
because its tab content is authored per repo and code-visualization already owns
that structure; forcing a body on it would be code-overview dictating the
contents of an analysis it does not perform.

The shell is forced on every generator via `--template`:
`code-visualization/scripts/assemble.py` already accepts the flag;
science-investigation's new assembler takes it; the two code-overview builders
read it directly.

**Each companion keeps its own default template.** That is the entire reason the
flag is an override rather than a requirement — `code-visualization` and
`science-investigation` are invoked standalone far more often than through
code-overview, and a skill that cannot render its own output alone is broken.
No skill reads another skill's files; code-overview passes a path on a documented
CLI flag, which is the direction that is already supported.

**Drift ratchet.** A repo test extracts the `:root{…}` token block and its
`@media (prefers-color-scheme: light)` twin from all three templates and asserts
they are byte-identical. Today the shared palette is a comment and a promise
("design tokens are code-visualization's, verbatim"); this makes divergence fail
CI. Without it the four documents stop looking like one artifact within a few
edits, and the failure is invisible until someone opens two of them side by side.

## code-doctor as the router

code-overview makes **one** doctor call, from the repo root. The existing
"never once per package directory" rule survives and matters more, not less: a
doctor pointed at `src/billing` cannot see the root manifest or the top-level
test tree, and reports fabricated findings about their absence.

### Detection is on manifest evidence, never filename convention

This is already code-doctor's stated invariant for entry points. The router
applies the same rule to language and framework detection.

| Evidence | Routes to |
|---|---|
| `pyproject.toml` / `setup.py` / `requirements*.txt` | `python-code-doctor` |
| `django` in declared dependencies, or `manage.py` beside a settings module defining `INSTALLED_APPS` | `django-code-doctor` **in addition** |
| `package.json` declaring TypeScript, or a `tsconfig.json` | `typescript-code-doctor` |
| none of the above | raw layer only, stated on the page |

A repo can match several. A Django site routes to Python *and* Django — the
pairing code-overview's SKILL.md currently asks the agent to arrange by hand,
because `django-code-doctor` has no general duplication or dead-code detector and
grades those categories ungraded on its own.

The handoff stays as code-doctor's design specifies: its `SKILL.md` **names the
specialist skill for the agent to load and run**. No script reaches into another
skill's internals.

### `merge_reports.py`

New, in code-doctor. Unions the reports into one envelope:

```json
{ "doctors_run": ["code-doctor", "python-code-doctor", "django-code-doctor"],
  "analyzers_run": { "python-code-doctor": ["find_security_issues", "..."] },
  "doctor_errors": { "django-code-doctor": "crashed reading settings" },
  "completeness": { "reference_graph": {"resolution_rate": 0.41},
                    "history": {"shallow": false, "depth_days": 730},
                    "test_classification": {"inconclusive_dirs": ["src/rust-core"]} },
  "findings":   [ { "doctor": "python-code-doctor", "severity": "high", "...": "..." } ],
  "candidates": [ { "doctor": "code-doctor", "also_caused_by": ["..."], "...": "..." } ] }
```

**It attributes; it does not deduplicate.** Collapsing the same hardcoded
`SECRET_KEY` reported by two security detectors is a grading decision — which
severity survives, whether the count is a max or a sum — so it stays in
`build_health.py`, which already does it and records `duplicates_merged`.
Merge is attribution; dedup is grading.

### What this simplifies in code-overview

- **`--findings <doctor>:<path>` labelling is no longer needed** on the normal
  path: attribution is per finding in the envelope. The flag stays for
  hand-assembled inputs, and the mis-attribution hazard it guards against —
  a TypeScript report granting a Python package coverage nothing measured —
  is now structurally impossible in the normal path.
- **Coverage is read, not declared.** `doctors_run` and `analyzers_run` are the
  evidence. `--doctor` stops being the thing that grants a coverage profile;
  `--covers` survives as the escape hatch for hand-assembled input. Both existing
  safety rules survive verbatim: a bare JSON list grades nothing, and a
  zero-byte findings file ungrades everything.
- **`doctor_errors` ungrades the categories the failed doctor covered**, instead
  of the surviving report silently scoring them A+. This is today's zero-byte
  rule made precise enough to charge the gap to specific categories rather than
  to all of them.

## The two rubrics

### Health — code-doctor's finding types

`rubric.py` gains a `DETECTOR_CATEGORIES` block for code-doctor:

| code-doctor output | Rubric category |
|---|---|
| secrets, committed `.env` | security |
| oversized files and lines, decision density, nesting depth | complexity |
| near-duplicates, exact duplicates, zero-inbound files, dead-function candidates | duplication |
| import cycles, god modules, low directory cohesion, change coupling | design |
| test gaps, tests that assert nothing | tests |
| TODO age, large binaries, ownership risk | hygiene |
| merge-conflict markers, **git-confirmed unmerged only** | correctness |

**code-doctor alone does not claim Correctness coverage.** A merge marker in an
unmerged path is the only correctness-class defect the raw layer can prove, and
crediting the whole category on that is the "empty findings list from a doctor
that parsed nothing is an A+" lie in a new costume. Correctness is graded when a
specialist ran and left ungraded when none did.

Two more categories are conditionally ungraded, driven by the envelope's
`completeness` block rather than by a flag:

- **Design** — ungraded when the envelope's
  `completeness.reference_graph.adequate` is false. The **threshold is
  code-doctor's to set and report as a boolean**, not a number code-overview
  interprets: only the detector knows what resolution rate its own edges need,
  and a grader inventing a cutoff would silently disagree with the skill that
  measured it. A sparse graph understates coupling, so a
  clean design score computed from it means "we could not resolve the edges",
  not "the structure is sound".
- **Tests** — ungraded for a unit whose test classification came back
  inconclusive. A fully tested Rust directory using `#[cfg(test)] mod tests`
  would otherwise score as untested.

**Candidates never enter the score.** They carry no fix and assert no defect;
penalizing a repo for its unresolved leads produces a grade that punishes
honesty. They are rendered, counted, and excluded from the arithmetic, and the
Candidates tab says so.

### Measurement — a coverage ratio

Lives in `science-investigation/scripts/rubric.py`, because the inventory it
divides is the science skill's own output and a standalone run should carry a
real score. code-overview's `references/scoring.md` documents both rubrics so a
reader comparing two letters can see what each one divides by.

```
score = 100 × Σ(importance_i × credit_i) / Σ(importance_i)     over all measurable things
```

| Importance | Meaning |
|---|---|
| 3 | gates a ship / release / rollout decision |
| 2 | informs a decision someone actually makes |
| 1 | informational |

| Credit | Meaning |
|---|---|
| 1.0 | measured, and the audit found nothing against it |
| 0.5 | measured, one confirmed medium finding (unpinned judge prompt, no multiple-comparisons correction) |
| 0.25 | measured, one confirmed high finding (n=3, judge confound, fail-soft scoring `0.0`) |
| 0.0 | not measured, **or structurally unmeasurable with today's data** |

Credit is set by the **worst** confirmed finding against that item. No measurable
things at all → `score: null`, grade `none` — never A+.

**Letter bands are duplicated, not shared.** science-investigation must render
its own page standalone, and no skill may read another skill's files, so the
score→letter table is copied into `science-investigation/scripts/rubric.py`. A
copy that drifts is worse than no copy — a B- would mean two different score
ranges on two pages a reader compares side by side — so the drift ratchet covers
it: the CI test that pins the design tokens also asserts the band table in the
two `rubric.py` files is identical. The bands are the only thing duplicated; the
arithmetic above them is genuinely different and stays separate.

`build_measurement.py` (science-investigation's assembler) takes `--root-dir`
and `--scope` with the same meaning `build_health.py` gives them, so one
repo-wide inventory produces each package's page from exactly its own rows, and
the count dropped as out-of-scope is printed and recorded.

Four rules keep an authored denominator arguable rather than arbitrary:

- **Every row ships on the Inventory tab**: the measurable thing, its importance
  *with the decision it drives named in a clause*, its credit, the finding that
  set that credit, the N it was last computed over, its `file:line` evidence, and
  its consumer. A score without its table is not a shippable page — the
  denominator is a judgment, so it has to be visible enough to dispute.
- **No unattributed deduction.** Any credit below 1.0 names the confirmed finding
  that caused it.
- **Structurally unmeasurable things stay in the denominator** at credit 0,
  labelled as unmeasurable rather than as defects. Dropping them is exactly how
  silence gets read as success — recall with no gold set, calibration with no
  outcomes, causal effect with no control arm.
- **"Not measured" is null, never `0.0`.** The science skill's own rule, now
  load-bearing for a grade.

The Score tab adds the decision-relevant cut: **of the weight at importance 3,
how much is soundly measured.** An A- built entirely on informational metrics
while every release gate is unmeasured is a finding, and this is what makes it
visible on the page rather than buried in a table.

**Metadata block** mirrors health's exactly —
`<script type="application/json" id="measurement-meta">`, schema
`measurement/1`, the same `</` → `<\/` escaping — so one extraction snippet reads
both document types. It carries `score`, `grade`, `weight_total`,
`weight_measured`, the per-importance breakdown, every inventory row, and, at
root scope, a `packages[]` array that is the whole table.

## Navigation

`inject_nav.py` gains the fourth type. The three rows keep their meaning:

| Row | Contents |
|---|---|
| Up | `⌂ Overall <Type>` → the repo-level document of the same type, then the package name |
| Across | `Summary · Code Map · Health · Measurement`, current one `aria-current="page"` |
| Sideways | The same document type in every other package |

Links stay existence-checked, percent-encoded then HTML-escaped, and the block
stays idempotent on re-run. `--check` remains the final gate and now exits 1 on a
broken measurement link too.

`summary.html` carries **two** grade cards — health and measurement — both read
back out of the generated pages rather than passed in, so the portal cannot
disagree with the documents it links. The repo summary's package table gains a
measurement column plus the explicit *no measurement content* list.

## Rebuild order

The "run it once from the repo root" rule now governs both analysis skills:

1. `discover_packages.py` → confirm with the user → write `docs/code-overview.json`
2. **code-doctor once, from the repo root** → routes to specialists →
   `merge_reports.py` → one envelope
3. **science-investigation's scripts once, from the repo root** → one inventory,
   partitioned per package by the `file:line` evidence on each row
4. per package: codemap → `build_health.py` → measurement → `build_summary.py`
5. root: codemap → `build_health.py --root` → measurement `--root` →
   `build_summary.py --root`
6. `inject_nav.py`, then `inject_nav.py --check`

Step 3 has the same failure mode as step 2, and it is the reason it is stated
rather than left to the agent: `find_metrics.py` pointed at `src/billing` cannot
see `evals/`, so it reports a package with a thoroughly-measured pipeline as
having no measurement — a fabricated null, indistinguishable on the page from an
honest one.

Rows whose evidence spans packages — a metric defined in `evals/` that scores a
service's output — are assigned to the package that **defines** the metric. The
repo-level document keeps every row regardless, so nothing is lost by the
partition.

Step 5 still depends on step 4: the root grade is narrowed to packages that have
a graded page, so running it first measures a looser set and says so.

## Degradation

Each of these is one honest line on the page, never a gap:

| Situation | Behaviour |
|---|---|
| A companion skill is not installed | Its document and its nav link are omitted; the summary says which |
| code-doctor present, no specialist installed | Raw layer only; Correctness ungraded, affected categories named |
| A specialist crashed | `doctor_errors` ungrades the categories it covered |
| A package has nothing measurable | Short measurement page, `score: null`, listed at the repo level |
| Templates diverge | CI fails on the token-identity test |

## Tests

- **Token blocks byte-identical** across `code-overview`, `code-visualization`,
  and `science-investigation` templates; **letter bands identical** between
  code-overview's and science-investigation's `rubric.py`.
- `inject_nav.py` over a four-type set; `--check` exits 1 on a dangling
  measurement link; re-run replaces rather than stacks the block.
- **Measurement rubric arithmetic**: a worked importance/credit table → its
  score; zero rows → `null`; all-zero credit → `0`, which is a different fact.
- `merge_reports.py`: attribution survives the union, nothing is deduplicated,
  `doctor_errors` is surfaced, an envelope claiming a doctor that contributed no
  `analyzers_run` is an error rather than full coverage.
- `build_health.py`: candidates never move the score; `doctor_errors` ungrades
  the right categories; code-doctor alone never claims Correctness; a
  `completeness` block below threshold ungrades Design and Tests.
- Roll-up over a mix of graded, ungraded, and absent measurement pages.
- `evals/code-overview/` and `evals/science-investigation/` gain cases for the
  four-document set and for the empty-measurement package.

## Out of scope

- **code-doctor's remaining detectors.** Its own plan
  (`2026-08-07-code-doctor-foundation.md`) owns those; this spec adds the router
  and `merge_reports.py`, which that plan reserves as Task 9.
- Merging `python-code-doctor` and `typescript-code-doctor`.
- Any change to `code-visualization`'s analysis. It gains nothing here but a
  `--template` value it already accepts.
- Fixing anything either audit finds. Both skills report; remediation is separate
  work with its own review.
