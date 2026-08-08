# Document layout, navigation, and the metadata schema

## Where the files go

```
docs/code-overview.json     the package map — the input to every other script
docs/summary.html           portal: overall grade, package table, links
docs/codemap.html           repo-wide code-visualization atlas
docs/health.html            repo-wide grade + per-package grade table
docs/measurement.html       repo-wide measurement grade + per-package table
docs/theory.html            repo-wide theory grade + per-package table

<pkg-root>/docs/summary.html      package portal
<pkg-root>/docs/codemap.html      package-scoped atlas
<pkg-root>/docs/health.html       package grade
<pkg-root>/docs/measurement.html  package measurement grade
<pkg-root>/docs/theory.html       package theory grade
```

A package's docs directory defaults to `<first root>/docs` and is overridable
per package via the map's `docs` field — useful when a package's first root is
somewhere you would rather not write, or when two packages share a parent.

**A single-package repo whose root is the repo root produces the five root
documents only.** `inject_nav.py` detects this (the package's docs directory
resolves to `<repo>/docs`) and drops the package layer rather than generating a
document that links to itself.

## The package map

`docs/code-overview.json`, written once after the user confirms the proposal and
read by every later script:

```json
{
  "schema": "code-overview/1",
  "generated": "2026-08-07",
  "packages": [
    {
      "name": "billing",
      "roots": ["src/billing", "shared/billing-types"],
      "docs": "src/billing/docs",
      "language": "python",
      "doctor": "python-code-doctor"
    }
  ]
}
```

`roots` is a list because a unit is not always a directory: a Django app and its
templates, a service and the shared types only it uses. It must **be** a list
even with one entry — `["src/api"]`, never `"src/api"` — because every consumer
iterates it, and a bare string is taken apart character by character: the docs
path became `s/docs` and each letter its own scoring root. `load_map` rejects it.
**Every path is repo-relative, and that is enforced** — an absolute path or a `..` escape is
rejected by `load_map`. The scripts all resolve these as `repo / path`, so a path
that leaves the checkout would size the grade over someone else's code and point
`inject_nav.py` at documents outside the repository to rewrite. `doctor` empty means no doctor ships for that language — the
health page is then built with every category ungraded, or skipped entirely,
according to what the user chose.

**`name` must be unique across the map.** It is both the identity the scripts
match packages by and the label a reader clicks in the nav, so two packages
called `api` — easy to get from two manifests — would lose every link between
them and could point a grade row at the wrong package. `load_map` rejects
duplicates rather than guessing; rename one, usually to its path.

**`docs` must be unique too.** Two packages pointed at one docs directory is
silent data loss: the second build overwrites the first's health and summary
pages, and navigation then walks the same three files twice, so only the last
package survives. `load_map` rejects that as well, comparing normalized paths so
`src/a/docs` and `src/a/../a/docs` collide as they should.

**Only a package that *is* the repository may use `docs`.** The collapse rule
needs both halves — `roots: ["."]` *and* `docs: "docs"`. A package rooted at
`src/api` that merely points `docs` at the repo's own directory is misconfigured,
not collapsed: treating it as collapsed would drop it from navigation and every
roll-up while the root build silently overwrote its pages. `load_map` rejects it.

`discover_packages.py` emits a superset of this shape: the same `packages` list
plus `too_small`, `unassigned`, and `questions`. Strip those three when saving
the map, or leave them — the loaders ignore unknown keys.

## Navigation

`inject_nav.py` writes one block into every document. Three rows:

| Row | Contents |
|---|---|
| Up | `⌂ Overall <Type>` → the repo-level document **of the same type**, then the package name |
| Across | `Summary · Code Map · Health · Measurement · Theory` for this package, current one `aria-current="page"` |
| Sideways | The same document type in every other package |

The block is delimited by `<!-- code-overview:nav -->` and `<!-- /code-overview:nav -->`
and **replaced** on re-run, so the nav never stacks. It is inserted immediately
after `</header>`, which puts it below the page title and above
code-visualization's tab bar — the two read as one strip.

Two constraints make it safe to inject into a document this skill did not write:

- **Self-contained styling.** Its CSS ships inside the block and every custom
  property carries a literal fallback (`var(--accent, #1f6fbd)`), so it renders
  correctly in a page shell that has never heard of this skill.
- **Existence-checked links.** A document that was not generated is not linked.
  A package with no atlas gets a two-item across-row, never a dangling href.
- **Percent-encoded, then HTML-escaped.** An href is a URL inside HTML and a path
  is neither. A docs directory named `a#b` emitted literally sends a browser to a
  fragment of the wrong document, while a checker resolving the same string
  against the filesystem finds the file and reports the set healthy. `--check`
  peels both layers off before touching disk.

`--check` reports what would change and validates every href without writing;
it exits 1 if any link is broken. Run it as the last step of a rebuild.

## The metadata block

Every `health.html` carries its numbers in machine-readable form:

```html
<script type="application/json" id="code-health-meta"> … </script>
```

| Field | Meaning |
|---|---|
| `schema` | `code-health/1` |
| `scope` | `package` or `repository` |
| `package`, `roots`, `language`, `doctor` | which unit this is and how it was analyzed |
| `generated`, `commit` | when, and against which sha |
| `size` | `{files, loc}` — the divisor the densities were computed against |
| `score`, `grade` | overall, 0–100 and a letter; `score` is null when nothing could be graded |
| `categories[]` | per category: `key`, `label`, `weight`, `score`, `grade`, `density`, `graded`, `findings{high,medium,low,total}` |
| `ungraded[]` | category keys that were not measured |
| `unmapped_types[]` | finding types the rubric has no home for |
| `analyzer_errors` | `{doctor: {detector: error}}` — which doctor's report the crash was in |
| `analyzers_skipped` | `{doctor: [detector]}` — detectors that doctor was told not to run, or that never ran, and no companion doctor ran either |
| `findings_out_of_scope` | findings dropped as being about code outside this unit |
| `duplicates_merged` | same defect reported by two doctors, collapsed to one |
| `sized_extensions[]` | non-code extensions counted in the denominator (templates the findings reached) |
| `findings_total`, `findings_by_severity` | counts, after scoping and deduplication |

Root-scope `packages[]` rows carry `generated: false` when a package has no
health page — the documented "codemap only" answer. Those rows stay in the table
rather than being dropped, so a roll-up missing a whole package cannot look
complete.
| `top_findings[]` | the worst N, with repo-relative paths |
| `packages[]` | root scope only: one row per package, with its grade |

Extraction needs no HTML parser beyond finding the block:

```python
import json, re, pathlib
text = pathlib.Path("docs/health.html").read_text()
meta = json.loads(re.search(r'id="code-health-meta">(.*?)</script>', text, re.S)
                  .group(1).replace("<\\/", "</"))
print(meta["grade"], meta["score"])
```

`</` inside the JSON is escaped as `<\/` when written, because a literal
`</script>` in a string would end the block early. Undo that on read, as above.

One root read gives every grade in the repo: `docs/health.html`'s `packages`
array is the whole table.

## The measurement metadata block

`measurement.html` embeds its numbers the same way `health.html` does, with the
same `</` → `<\/` escaping, so one extraction snippet reads both:

```html
<script type="application/json" id="measurement-meta"> … </script>
```

| Field | Meaning |
|---|---|
| `schema` | `measurement/1` |
| `scope` | `package` or `repository` |
| `package`, `generated`, `commit` | which unit, when, against which sha |
| `score`, `grade` | 0–100 and a letter; `score` is null when nothing measurable was found |
| `weight_total`, `weight_measured` | the two sides of the ratio |
| `by_importance` | per level: `total`, `measured`, `share`, `rows` — `["3"]["share"]` is the ship-gate cut |
| `rows[]` | every measurable thing: `name`, `importance`, `importance_reason`, `credit`, `credit_reason`, `finding`, `n`, `n_total`, `formula`, `consumer`, `evidence[]`, `status`, `unmeasurable_reason` |
| `findings[]` | `id`, `severity`, `title`, `detail`, `evidence[]`, `blast_radius` |
| `not_audited[]` | subsystems the audit could not reach |
| `rows_out_of_scope` | rows dropped as defined outside this unit |
| `packages[]` | root scope only: `name`, `score`, `grade`, `rows`, `generated` |

`score: null` with `rows: []` is the documented "no measurement content" page.
It is neither a pass nor a failure, and the roll-up says so in words rather
than showing a dash that could be read either way.

## Rebuild order

The scripts read each other's output, so order matters. The "run it once from
the repo root" rule now governs both analysis skills:

1. `discover_packages.py` → confirm with the user → write `docs/code-overview.json`
2. **code-doctor once, from the repo root** → routes to specialists →
   `merge_reports.py` → one envelope
3. **science-investigation's scripts once, from the repo root** → one inventory,
   partitioned per package by the `file:line` evidence on each row
4. per package: codemap → `build_health.py` → measurement → **three independent
   theory judges, none seeing another's verdict → `build_theory.py`** →
   `build_summary.py`
5. root: codemap → `build_health.py --root` → measurement `--root` →
   **`build_theory.py --root`, `--package name:path` per package for the
   roll-up table** (no exemption floor at this scope) → `build_summary.py --root`
6. `inject_nav.py`, then `inject_nav.py --check`

Steps 2 and 3 share the same failure mode, which is why both are stated rather
than left to the agent. A doctor pointed at a package directory loses the
project context several of its detectors depend on — the dependency manifest,
the test tree, Django's settings and app registry — and the result is not a
smaller report but a *wrong* one: fabricated findings about a missing manifest
and missing tests, alongside real findings it can no longer see.
`find_metrics.py` pointed at `src/billing` cannot see `evals/`, so it reports a
package with a thoroughly-measured pipeline as having no measurement — a
fabricated null, indistinguishable on the page from an honest one.

Rows whose evidence spans packages — a metric defined in `evals/` that scores a
service's output — are assigned to the package that **defines** the metric. The
repo-level document keeps every row regardless, so nothing is lost by the
partition.

Running `build_health.py --root` before the package health pages exist is not an
error — it warns per missing package and leaves them out of the table. Re-run it
after. Do re-run it: the root denominator is narrowed to packages that have a
graded health page, so a first pass with none of them built measures every
doctored package instead, which is the looser answer. `build_measurement.py
--root` degrades the same way for a missing `--package` file — its row reads
`not generated` rather than `no measurement content`, the two states this
section already distinguishes — though it does so without a matching stderr
note, so re-running it after the package pages exist is worth doing on faith
rather than waiting to be told.

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
| `generated`, `commit` | the date, and the revision judged — `--commit`, else the repo's short SHA, like `health.html` |
| `disputed[]` | dimension keys where the judges were ≥2 rungs apart; rendered as labels, not keys, everywhere they are shown |
| `theory` | the first judge's theory statement, verbatim. Carried for whatever reads this block; **nothing in this document set renders it.** It was once described here as being "for roll-ups" — it is not, and should not be: the roll-up shows medians and disagreement, and printing one judge's wording as the package's theory would give a single reading the authority of a panel |
| `verdicts[]` | all three verdicts verbatim |
| `packages[]` | root scope only, and only when `--package` was passed |

`packages[]` is thinner than `health.html`'s or `measurement.html`'s: each row
is exactly what `read_package_grade` reads back out of that package's own
`theory-meta` block — `name`, `score`, `grade`, `exempt`, `disputed[]`, and
`generated` (false, with `score: null` and `grade: "—"`, when the package's
`theory.html` does not exist yet or carries no metadata block). There is no
`roots` or `size` on the row; the page just states, per package, whether it
has a theory grade yet and what it was.

## The theory roll-up, at root scope

`build_theory.py --root` accepts repeated `--package NAME:PATH` — `PATH`
pointing at that package's `theory.html` — and refuses the flag without
`--root` (`--package builds the repository roll-up table and needs --root`).
Each package is read back out of its own document, the same discipline as the
health and measurement roll-ups, so the table cannot disagree with the pages
it summarizes. A package whose `theory.html` is missing or unparsable renders
as **not generated** rather than being dropped from the table or scored as a
failure — verified by running the script with a `--package` pointing at a
path that does not exist.

A `--root-dir` that does not exist, or that holds no source files, is
**refused**: exit 2, a message on stderr, no page. `build_health.py` ungrades
in the same situation; this one cannot, because its ungraded shape — `score:
null`, `grade: "—"` — is already what *too small to warrant a theory* looks
like, so an ungraded theory page would read as an exemption for a package that
may be enormous. The grade here comes from the verdicts, not the size, so
nothing about measuring nothing lowers the letter: before the guard, a typo'd
`--root-dir` rendered **A+ (100.0)** over "0 files · 0 lines", and with two
trivial votes it rendered exempt/null — the size gate passing for free
collapses the two exemption gates into the one that is gameable.

The roll-up table's state column says both when both hold: a package that is
exempt *and* split the panel reads `too small to warrant a theory; panel
disagreed on Abstraction`. Dimensions are named by their labels everywhere
they are shown — inside `theory.html`, in the roll-up, and on the portal card
— never by their keys.

## Generating the atlases

Pass `--exclude docs` to the `code-visualization` analyzers. Without it, a
rebuild counts the previously-generated HTML as source: a small package's own
`docs/codemap.html` can be 90% of its measured lines, and the inventory tab then
describes the documentation rather than the code.
