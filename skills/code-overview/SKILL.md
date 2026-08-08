---
name: code-overview
description: Build a navigable documentation set for a codebase, one per package rather than one per repo. Works out what the packages/modules/subsystems are (asking the user to confirm, and about anything ambiguous), then per package runs code-visualization into <package>/docs/codemap.html and the matching code doctor into <package>/docs/health.html — a graded page with a 0-100 score, a letter grade, and a JSON metadata block for later extraction — plus a summary.html linking both. Collates these into repo-level docs/summary.html, codemap.html and health.html, and injects navigation so every page links up to the overall document of its own type, across to its siblings, and back down. Use when the user wants an overview, health report, grade or score for a codebase, per-package docs, or docs for a monorepo — "document this repo", "how healthy is each package", "grade this codebase", "overview of every service". For one unscored atlas use code-visualization; for one PR use pr-visualization.
---

# Code Overview — a graded, navigable document set

This skill **orchestrates**; it does not analyze. The analysis comes from
`code-visualization` and the code doctors. What this skill adds is the thing none
of them can do alone: deciding what the units are, running the others per unit,
scoring the result, and binding it all into one navigable set with a consistent
appearance.

Let `SKILL=/path/to/this/skill`, and let `CV` and `DOCTOR` be the paths to the
installed `code-visualization` and `*-code-doctor` skills. Needs **Python
3.11+**; the scripts are stdlib-only.

**If a companion is not installed**, say so and degrade rather than substitute:
without `code-visualization` there are no code maps — build health and summary
pages, and the nav simply omits the missing link. Without a doctor for a
language, see *No doctor for this language* below. Never hand-write a codemap or
invent findings to fill a page.

## What gets built

```
docs/code-overview.json      the package map — the input to every other script
docs/{summary,codemap,health}.html          repo level
<pkg-root>/docs/{summary,codemap,health}.html   one set per package
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

## 2. Run the doctors once, from the repo root

**Not once per package directory.** This is the step that is easy to get wrong
and expensive to get wrong, because the failure is silent:

```bash
python "$DOCTOR/scripts/analyze_all.py" <repo> --format json > $WORK/findings.json
```

Pointed at `src/billing` instead, a doctor cannot see the repo's `pyproject.toml`
or its top-level `tests/`, so it reports a missing dependency manifest and "no
test files were found" — two fabricated findings, one of them high severity in
the Tests category — about a project that has both. `django-code-doctor` is worse:
its whole-project class graph is built from settings and `INSTALLED_APPS`, so an
app directory alone yields a fraction of the real findings, and a package that
isn't recognized as Django at all yields *none*, which grades as an A+.

`analyze_django.py` for `django-code-doctor`. Run every doctor the map needs —
one per language present — and keep each report; they all get passed along
below. Do not pass `--skip`/`--skip-duplicates` unless you mean it: a skipped
detector leaves its rubric category ungraded (which is correct, and lowers what
the grade covers).

**A Django project deserves both doctors.** `django-code-doctor` has no general
duplication or dead-code detector, so on its own those categories come back
ungraded. Run `python-code-doctor` over the repo too, pass both JSON files, and
use `--doctor python-code-doctor` so the union is credited with full coverage.

## 3. Per package

For each package in the map, with `ROOTS` its roots and `DOCS` its docs dir:

**The atlas.** Run the full `code-visualization` workflow scoped to the package's
roots, `--out <DOCS>/codemap.html`. **Pass `--exclude docs` to its analyzers** —
otherwise a rebuild counts the previously-generated HTML as source, and a small
package's inventory tab ends up describing its own documentation. Scale judgment
tabs to size: a 400-line package may warrant the automated tabs only, and
code-visualization's own proportion rule covers this.

**The grade.** The repo-wide findings are partitioned by path, so each package's
page carries only findings about its own code:

```bash
python "$SKILL/scripts/build_health.py" --out <DOCS>/health.html --repo <repo> \
  --findings $WORK/findings.json --name <pkg> --root-dir <root> \
  --language python --doctor python-code-doctor
```

`--scope` defaults to the `--root-dir` values, so a multi-root package
(repeat `--root-dir`) keeps exactly its own findings. The count dropped as
out-of-scope is printed to stderr and recorded in the metadata — it should
roughly account for the rest of the repo, and if it accounts for *everything*
the paths don't line up.

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

## 4. The repo level

Same three documents, one scope up.

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
- **Summary**: `build_summary.py --root --map docs/code-overview.json`.

## 5. Navigation, last

```bash
python "$SKILL/scripts/inject_nav.py" --map docs/code-overview.json --repo <repo>
python "$SKILL/scripts/inject_nav.py" --map docs/code-overview.json --repo <repo> --check
```

Injects the nav into every document that exists, `codemap.html` included — this
is the only time this skill touches an atlas. Idempotent: re-runs replace the
block rather than stacking it. `--check` writes nothing, reports what would
change, and **exits 1 if any link is broken** — run it as the final gate and fix
what it names.

## No doctor for this language

Doctors ship for Python, Django, and TypeScript. For anything else — Go, Rust,
plain JS, Java, a mixed package — **ask the user** which they want:

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
- **Every document is generated, never hand-edited.** Fix the input and re-run.
- **No dangling links.** `inject_nav.py --check` exits 0 or the set is not done.
- **Say what was skipped.** Packages you did not build, a doctor that was
  missing, a language the graph could not read — one honest line each beats a
  document set that looks complete.

## Reference index

| Load this when… | File |
|---|---|
| A grade looks wrong; a doctor grew a detector the rubric has not seen; explaining what a letter means | `references/scoring.md` |
| Wiring the documents together — file layout, the package map, the nav contract, the metadata schema and how to extract it, rebuild order | `references/doc-layout.md` |

## Scripts

```bash
python "$SKILL/scripts/discover_packages.py" <repo> [--format text|json] [--exclude d1,d2] [--min-files N]
python "$SKILL/scripts/build_health.py"  --out FILE --findings JSON [--findings JSON ...] [--root]
python "$SKILL/scripts/build_summary.py" --out FILE [--root] [--intro-file HTML] [--highlight TEXT]
python "$SKILL/scripts/inject_nav.py"    --map docs/code-overview.json --repo <repo> [--check]
```

Templates live in `assets/` — `assets/template.html` is the shared page shell
(its design tokens are code-visualization's, which is what makes the three
document types read as one artifact), with `assets/health-body.html` and
`assets/summary-body.html` supplying the two layouts.
