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

Then write the confirmed map to `docs/code-overview.json` — the shape is in
`references/doc-layout.md`. Everything downstream reads it.

**On a re-run, load the existing map and ask only about drift** — directories
that appeared or vanished since. Re-litigating a settled package structure wastes
the user's time and churns the documents.

## 2. Per package

For each package in the map, with `ROOTS` its roots and `DOCS` its docs dir:

**The atlas.** Run the full `code-visualization` workflow scoped to the package's
roots, `--out <DOCS>/codemap.html`. **Pass `--exclude docs` to its analyzers** —
otherwise a rebuild counts the previously-generated HTML as source, and a small
package's inventory tab ends up describing its own documentation. Scale judgment
tabs to size: a 400-line package may warrant the automated tabs only, and
code-visualization's own proportion rule covers this.

**The findings.**

```bash
python "$DOCTOR/scripts/analyze_all.py" <root> --format json > $WORK/<pkg>.json
```

`analyze_django.py` for `django-code-doctor`. For a multi-root package, run the
doctor once per root and pass every file to `--findings`.

**A Django app deserves both doctors.** `django-code-doctor` has no general
duplication or dead-code detector, so on its own those categories come back
ungraded. Run `python-code-doctor` over the same roots too, pass both JSON files,
and use `--doctor python-code-doctor` so the union is credited with full
coverage.

**The grade.**

```bash
python "$SKILL/scripts/build_health.py" --out <DOCS>/health.html --repo <repo> \
  --findings $WORK/<pkg>.json --name <pkg> --root-dir <root> \
  --language python --doctor python-code-doctor
```

Repeat `--root-dir` for each root. It prints the grade to stderr.

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

## 3. The repo level

Same three documents, one scope up.

- **Atlas**: a genuine repo-wide `code-visualization` run into `docs/codemap.html`.
  Cross-package coupling and cycles only exist at this scope, so this is not a
  collation of the package atlases.
- **Health**: `build_health.py --root --map docs/code-overview.json`, passing
  **every** package's findings file plus any repo-wide-only run. The root grade is
  recomputed from the union over total LOC — the same measurement as a package
  grade, and comparable to one, rather than an average of averages. It reads each
  package's `health.html` for the grade table, so build those first.
- **Summary**: `build_summary.py --root --map docs/code-overview.json`.

## 4. Navigation, last

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

- **Codemap plus an ungraded health page.** Build `health.html` from
  language-agnostic signals only and let the rubric mark every category ungraded.
  The package appears in the roll-up as ungraded rather than dragging the
  average down with a fabricated F.
- **Codemap only.** No `health.html`; the nav drops the link and the summary says
  why.
- **The closest doctor anyway.** Sometimes right for a JS package with no
  TypeScript. Record it in the metadata via `--doctor` and say so on the page.

What is never acceptable is a grade computed from a doctor that could not read
the language. An empty findings list from a doctor that parsed nothing is an
A+, and that is a lie.

## Quality bar

- **The grade is a density of detectable problems, not a verdict on the design.**
  Say so when presenting it. Its value is comparability over time. A package can
  score A- and still be architecturally wrong — which is exactly why
  `summary.html` links the code map beside it.
- **Ungraded is not zero and not a hundred.** A category nothing measured is
  dropped from the mean and named on the page. Same for a detector that crashed:
  its zero count means *unknown*, not *clean*.
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
