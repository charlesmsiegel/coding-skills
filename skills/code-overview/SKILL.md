---
name: code-overview
description: Build a navigable documentation set for a codebase, one per package rather than one per repo. Works out the packages/modules (confirming with the user), then per package runs code-visualization into codemap.html, code-doctor into health.html (0-100 score, letter grade), science-investigation into measurement.html (null when nothing is measurable), and a three-judge panel into theory.html grading whether the code expresses a coherent theory of its problem, in Naur's sense — plus a summary.html linking all four. Collates these at repo level too, and injects navigation linking every page up, across, and down. Use when the user wants an overview, health report, grade, measurement audit, theory judgment, or score for a codebase, per-package docs, or docs for a monorepo — "document this repo", "how healthy is each package", "grade this codebase", "overview of every service". For one unscored atlas use code-visualization; for one PR use pr-visualization.
---

# Code Overview — a graded, navigable document set

This skill **orchestrates**; it does not analyze. The analysis comes from
`code-visualization` and the code doctors. What this skill adds is the thing none
of them can do alone: deciding what the units are, running the others per unit,
scoring the result, and binding it all into one navigable set with a consistent
appearance.

Let `SKILL=/path/to/this/skill`, and let `CV`, `DOCTOR`, and `SCIENCE` be the
paths to the installed `code-visualization`, `code-doctor`, and
`science-investigation` skills. Needs **Python 3.11+**; the scripts are
stdlib-only.

**If a companion is not installed**, say so and degrade rather than substitute:
without `code-visualization` there are no code maps — build health, measurement,
and summary pages, and the nav simply omits the missing link. Without
`code-doctor` itself there is no merged envelope at all — build codemap,
measurement, and summary pages, and skip health entirely unless a
hand-assembled report exists for `--findings`. Without a specialist for a
package's language, see *No specialist for this language* below. Without
`science-investigation` there is no measurement page — say so on the summary and
in the nav. Never hand-write a codemap or invent findings or metrics to fill a
page.

## What gets built

```
docs/code-overview.json      the package map — the input to every other script
docs/{summary,codemap,health,measurement,theory}.html          repo level
<pkg-root>/docs/{summary,codemap,health,measurement,theory}.html   one set per package
```

Full layout, the nav contract, and the metadata schema: `references/doc-layout.md`.

## 1. Work out the packages — and ask

```bash
python "$SKILL/scripts/discover_packages.py" <repo> --format text
```

It proposes candidates from manifests (pyproject, package.json plus npm
workspaces, go.mod, Cargo workspaces, gradle/maven, csproj), Django
`INSTALLED_APPS`, importable package layout, and finally top-level source
directories — each with the evidence it was found by. It also reports what it
could not settle.

**Put its `questions` to the user before building anything** (AskUserQuestion is
the right tool). They are not ceremony: the right unit is a design judgment, and
guessing it silently produces a document set organized around the wrong thing.
The recurring ones are nesting (`src/` contains four real subsystems — which
level do you think in?), structural names (`app/` is a layout convention, not a
subsystem), unclaimed code, and candidates dropped by `--min-files`.

Ask about anything else the proposal cannot see: **a unit that spans folders**
(a service plus the shared types only it uses; a Django app plus its templates
directory) is one package with several `roots`, and only the user knows that.

Two of its questions decide what a grade can mean, so do not skip them:
`mixed-languages` (only the dominant language's doctor runs, while the other
language's lines still swell the size the grade is divided by) and `no-doctor`
(below). **Package names must be unique** — they are both the identity used to
link the documents and the label a reader clicks — so if two manifests yield the
same name, rename one when you write the map.

Then write the confirmed map to `docs/code-overview.json` — the shape is in
`references/doc-layout.md`. Everything downstream reads it.

**On a re-run, load the existing map and ask only about drift** — directories
that appeared or vanished since. Re-litigating a settled package structure wastes
the user's time and churns the documents.

## 2. Run code-doctor once, from the repo root

**One doctor call.** code-doctor detects the languages and frameworks the
repository's manifests declare, names the specialists that justifies, and merges
every report into one envelope.

```bash
python "$DOCTOR/scripts/route.py" <repo> --format json
# load and run each named specialist over <repo>, then:
python "$DOCTOR/scripts/merge_reports.py" \
  --report code-doctor:$WORK/raw.json \
  --report python-code-doctor:$WORK/py.json \
  --out $WORK/merged.json --format json
```

**Not once per package directory.** This is the step that is easy to get wrong
and expensive to get wrong, because the failure is silent. Pointed at
`src/billing` instead, a doctor cannot see the repo's `pyproject.toml` or its
top-level `tests/`, so it reports a missing dependency manifest and "no test
files were found" — two fabricated findings about a project that has both.

The envelope replaces three things this skill used to be told:

- **Attribution.** Every record carries the doctor that produced it, so
  `--findings <doctor>:<path>` is no longer needed on the normal path. It stays
  for hand-assembled input.
- **Coverage.** `doctors_run` and `analyzers_run` are the evidence; `--doctor`
  no longer grants a coverage profile. A doctor in `coverage_unknown` — a bare
  JSON list, which `analyze_django.py` and a single detector both emit — grants
  nothing, because nothing in that shape distinguishes a full run from one
  detector that found nothing.
- **Defect vs lead.** Findings are scored; **candidates are not**. A candidate
  asserts no defect and carries no fix, and charging one to the grade would
  punish a doctor for being honest about what it could not prove.

**Failures are named, not absorbed.** `doctor_errors` ungrades the categories
the failed doctor covered rather than letting the surviving report score them.
`completeness` does the same per evidence class: a reference graph the detector
reports as inadequate ungrades Design, and inconclusive test classification
ungrades Tests. **code-doctor alone never grades Correctness** — a merge marker
in a git-confirmed unmerged path is the only correctness-class defect its raw
layer can prove.

## 3. Per package

For each package in the map, with `ROOTS` its roots and `DOCS` its docs dir:

**The atlas.** Run the full `code-visualization` workflow scoped to the package's
roots, `--out <DOCS>/codemap.html`. **Pass `--exclude docs` to its analyzers** —
otherwise a rebuild counts the previously-generated HTML as source, and a small
package's inventory tab ends up describing its own documentation. Scale judgment
tabs to size: a 400-line package may warrant the automated tabs only, and
code-visualization's own proportion rule covers this.

**The measurement audit.** Run `science-investigation`'s scripts **once from the
repo root** — pointed at a package, `find_metrics.py` cannot see `evals/` and
reports a thoroughly-measured pipeline as having no measurement. Write the
inventory once, then build each package's page from it:

```bash
python "$SCIENCE/scripts/build_measurement.py" --out <DOCS>/measurement.html \
  --inventory $WORK/inventory.json --name <pkg> --repo <repo> \
  --root-dir <root> --template "$SKILL/assets/template.html" \
  --body "$SKILL/assets/measurement-body.html" --intro-file $WORK/<pkg>-measure.html
```

**Every package gets one.** A package with nothing measurable is a short page
scoring `null` — not zero, not A+ — and appears in the repo document's *no
measurement content* list. Most packages in a typical repo are that page, and
that is the correct output.

Pass `--template "$SKILL/assets/template.html"` to `code-visualization`'s
`assemble.py` too. That one flag is what makes the five documents read as one
artifact instead of three tools' output.

**The grade.** Pass the merged envelope from step 2; `build_health.py` partitions
its findings and candidates by path, so each package's page carries only
records about its own code:

```bash
python "$SKILL/scripts/build_health.py" --out <DOCS>/health.html --repo <repo> \
  --merged $WORK/merged.json --name <pkg> --root-dir <root> --language python
```

`--scope` defaults to the `--root-dir` values, so a multi-root package
(repeat `--root-dir`) keeps exactly its own findings. The count dropped as
out-of-scope is printed to stderr and recorded in the metadata — it should
roughly account for the rest of the repo, and if it accounts for *everything*
the paths don't line up.

**`--findings`, `--doctor`, and `--covers` still work** — they are how a
hand-assembled report (a third-party tool, a report from before this skill
called `code-doctor`) reaches the grade. The rest of this section documents
what they mean; skip it on the normal path, where `--merged` supplies all of it.

**Coverage is evidence, not capability.** Only `analyze_all.py`'s report — the
envelope naming `analyzers_run` — is evidence, and it is believed per category
(capped by the `--doctor` profile). **Every other shape grades nothing**: a bare
JSON list, a single detector's `{"issues": […]}`, an empty file. That is not
excessive caution — `analyze_django.py` and `find_duplicates.py --format json`
emit the *same* bare-list shape, so nothing in the file distinguishes a full
Django run from one detector that found nothing, and inferring the doctor's
profile from it scored the latter A+ in all seven categories.

So: **the Python/TypeScript doctors need no flag** (their report is an envelope),
and **`django-code-doctor` run alone needs `--covers`**:

```bash
--covers correctness,security,tests,complexity,design,hygiene   # django's profile
```

The recommended Python+Django merge needs no flag either, since the Python report
carries the evidence. `--assume-full-coverage` credits the whole rubric.

**A zero-byte findings file ungrades everything, even beside a full report.** It
means a doctor *failed*, not that it found nothing, and no file says which one —
so the gap cannot be charged to particular categories. If Django crashes during
the merge, the clean Python report alone would otherwise grade A+ on categories
Django was supposed to help measure. Re-run the failed doctor; if you cannot,
`--covers` declares what the surviving reports examined.

**Findings from two doctors are deduplicated** on `(file, line, type)`, keeping
the higher severity — both security detectors flag the same hardcoded
`SECRET_KEY`, and charging one defect twice inflates the penalty by a third.
The merge is across reports only: one detector legitimately emits two findings on
one line, so an identity's count is the maximum any report gave it, not the sum.

**Do not pass `--skip`/`--skip-duplicates` casually.** A skipped detector
ungrades its *whole* rubric category — skipping `exception_issues` ungrades
Correctness — because a partly-measured category can only miss findings, never
invent them. That is correct behaviour, but it shrinks what the grade covers.

**The summary.** Write a short HTML fragment first — what this package is, why it
is shaped this way, what you noticed reading it — and pass it as `--intro-file`.
That prose is the one part no script can produce, and a summary without it says
so on the page.

```bash
python "$SKILL/scripts/build_summary.py" --out <DOCS>/summary.html --repo <repo> \
  --name <pkg> --intro-file $WORK/<pkg>-intro.html \
  --highlight "Two import cycles run through models.py" \
  --highlight "No tests touch the reconciliation path"
```

It reads the grade back out of `health.html` rather than being told it, so the
two documents cannot disagree.

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

## 5. The repo level

Same five documents, one scope up.

- **Atlas**: a genuine repo-wide `code-visualization` run into `docs/codemap.html`.
  Cross-package coupling and cycles only exist at this scope, so this is not a
  collation of the package atlases.
- **Health**: `build_health.py --root --map docs/code-overview.json`, passing every
  doctor's findings file. The root grade is recomputed from the union rather than
  averaged from package grades, so it is the same measurement as a package grade
  and comparable to one. With `--map` and no explicit `--root-dir`, it is sized
  over the union of the mapped packages' roots — code the user chose to leave
  unassigned contributes no findings, so it must not pad the denominator either.
  Repo-wide findings are kept here even though they sit in no package: files
  directly in the repo root (`tsconfig.json`, the root manifest), *and* findings
  reported against the root directory itself — `no_tests_in_repo` and
  `no_dependency_manifest` carry `file=<repo root>` because no single file is to
  blame, and dropping them made a repo with neither tests nor a manifest roll up
  to an A+. Packages with **no doctor**, and packages with **no graded health
  page**, are left out of the repo grade's size — their lines would dilute
  findings they cannot contribute — while still appearing in the table as
  ungraded. A doctor named in the map is an intention; only a graded health page
  proves the code was examined, so **build the package pages first and re-run
  `--root` after them.** Run before they exist, it falls back to every doctored
  package and says so. A package with no health page stays in the table marked
  *not generated* rather than vanishing from it.
- **Measurement**: `build_measurement.py --root --out docs/measurement.html
  --inventory $WORK/inventory.json --name <repo> --package <pkg>:<pkg-docs>/measurement.html
  [--package ... ]`. Reads each package's grade back out of its own
  `measurement.html`, the same discipline as the summary and health roll-ups, so
  the table cannot disagree with the pages it summarizes. Every mapped package
  gets a row, including the ones that scored `null` — the table calls those *no
  measurement content*, not a blank cell.
- **Theory**: `build_theory.py --root --out docs/theory.html --name <repo>
  --root-dir <root> [--root-dir ...] --verdict ... (×3, one panel over the whole
  scope)`. No exemption floor at this scope — a repository of individually
  trivial packages still has a system-level question worth asking. Unlike
  health and measurement, `build_theory.py` has no `--package` flag and writes
  no per-package roll-up; a root `theory.html` is one more panel-scored page,
  not a table of every package's grade.
- **Summary**: `build_summary.py --root --map docs/code-overview.json`.

## 6. Navigation, last

```bash
python "$SKILL/scripts/inject_nav.py" --map docs/code-overview.json --repo <repo>
python "$SKILL/scripts/inject_nav.py" --map docs/code-overview.json --repo <repo> --check
```

Injects the nav into every document that exists, `codemap.html` included — this
is the only time this skill touches an atlas. Idempotent: re-runs replace the
block rather than stacking it. `--check` writes nothing, reports what would
change, and **exits 1 if any link is broken** — run it as the final gate and fix
what it names.

## No specialist for this language

Specialists ship for Python, Django, and TypeScript; `code-doctor` itself (step
2) is language-blind and always runs, so its merged envelope exists for every
package regardless. For a language with no specialist — Go, Rust, plain JS,
Java, a mixed package — **ask the user** what to do beyond that:

- **Codemap plus an ungraded health page.** Build `health.html` with `--doctor`
  naming whatever produced the findings (or nothing at all): with no coverage
  profile, every category comes back ungraded and the score is null. The package
  appears in the roll-up as ungraded rather than dragging the average down with a
  fabricated F — or, worse, floating it with a fabricated A+. **`--findings` is
  optional for exactly this case** — omit it entirely rather than writing an
  empty JSON file, which is a distinct thing meaning "a doctor crashed".
- **Codemap only.** No `health.html`; the nav drops the link and the summary says
  why.
- **The closest doctor anyway.** Sometimes right for a JS package with no
  TypeScript. Record it in the metadata via `--doctor` and say so on the page.

What is never acceptable is a grade computed from a doctor that could not read
the language. An empty findings list from a doctor that parsed nothing would be
an A+, and that is a lie — which is why an unrecognized `--doctor` grades
nothing at all rather than everything.

## Quality bar

- **The grade is a density of detectable problems, not a verdict on the design.**
  Say so when presenting it. Its value is comparability over time. A package can
  score A- and still be architecturally wrong — which is exactly why
  `summary.html` links the code map beside it.
- **Ungraded is not zero and not a hundred.** A category nothing measured is
  dropped from the mean and named on the page. Same for a detector that crashed,
  one that was skipped, and one that never ran: their zero counts mean *unknown*,
  not *clean*.
- **Findings and the lines they are divided by must cover the same code.** That
  is what `--scope` and the root's map-derived sizing are for. Grading a package
  over findings from elsewhere, or over lines nothing analyzed, produces a number
  that looks like the others and means something different.
- **The measurement grade is a different question from the health grade, even
  though it sits beside it.** Health asks how dense the detectable defects are;
  measurement asks how much of what matters is actually measured. A package can
  be A on one and null on the other, and both are honest — say which is which
  when presenting them together.
- **Every document is generated, never hand-edited.** Fix the input and re-run.
- **No dangling links.** `inject_nav.py --check` exits 0 or the set is not done.
- **Say what was skipped.** Packages you did not build, a doctor that was
  missing, a language the graph could not read — one honest line each beats a
  document set that looks complete.

## Reference index

| Load this when… | File |
|---|---|
| A grade looks wrong; a doctor grew a detector the rubric has not seen; explaining what a letter means | `references/scoring.md` |
| A measurement grade looks wrong; explaining what the coverage ratio divides | `references/scoring.md` |
| Wiring the documents together — file layout, the package map, the nav contract, the metadata schema and how to extract it, rebuild order | `references/doc-layout.md` |

## Scripts

```bash
python "$SKILL/scripts/discover_packages.py" <repo> [--format text|json] [--exclude d1,d2] [--min-files N]
python "$SKILL/scripts/build_health.py"  --out FILE [--merged JSON] [--findings JSON ...] [--root]
python "$SCIENCE/scripts/build_measurement.py" --out FILE --inventory JSON --name NAME [--root]
python "$SKILL/scripts/build_theory.py"  --out FILE --name NAME --verdict JSON --verdict JSON --verdict JSON [--root] [--model ID]
python "$SKILL/scripts/build_summary.py" --out FILE [--root] [--intro-file HTML] [--highlight TEXT]
python "$SKILL/scripts/inject_nav.py"    --map docs/code-overview.json --repo <repo> [--check]
```

Templates live in `assets/` — `assets/template.html` is the shared page shell
(its design tokens are code-visualization's, which is what makes the five
document types read as one artifact), with `assets/health-body.html`,
`assets/summary-body.html`, `assets/measurement-body.html`, and
`assets/theory-body.html` supplying the four layouts. `measurement-body.html`
is a byte-identical copy of `science-investigation`'s own — one source of
truth, pinned by `tests/test_shared_assets.py`.
