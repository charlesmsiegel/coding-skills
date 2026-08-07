# code-overview — design

**Date:** 2026-08-07
**Status:** approved

A lightweight orchestration skill. It does not analyze code itself; it decides *what
the units of analysis are*, drives `code-visualization` and the matching
`*-code-doctor` over each one, and binds the results into a navigable document set
with a consistent look and a machine-readable grade.

## Problem

The analysis skills are repo-shaped. `code-visualization` writes one
`docs/codemap.html` for a whole checkout; the doctors emit a flat findings report for
whatever path they are pointed at. On anything larger than a single package that is
the wrong granularity: one atlas for a monorepo of nine services says very little
about any of them, and a findings list of 1,400 items is not a thing anyone reads.
Nothing rolls the per-unit answers back up, nothing links them together, and nothing
gives the output a number you can track over time.

## Deliverable layout

```
docs/code-overview.json          the package map, persisted
docs/summary.html                portal — overall grade, package table, links
docs/codemap.html                repo-wide code-visualization run
docs/health.html                 repo-wide health + per-package grade table

<pkg-root>/docs/summary.html     package portal
<pkg-root>/docs/codemap.html     package-scoped atlas
<pkg-root>/docs/health.html      package health + grade
```

A package's docs directory defaults to `<first root>/docs` and is overridable in the
map. A single-package repo whose root *is* the repo root collapses the package layer:
the root three documents are the whole output.

## Navigation contract

Every document carries an injected nav block, `codemap.html` included — this skill
never hand-edits an atlas otherwise. Three rows:

1. **Up.** `⌂ Overall <Type>` links to the root document *of the same type*. A package
   health page goes up to the repo health page, not to the portal.
2. **Across.** `Summary · Code Map · Health` for the current package, current one
   marked with `aria-current`.
3. **Sideways.** `Other packages: …`, each linking to the same-type document in that
   package. Omitted when there is one package.

Links are computed with `os.path.relpath`, so the set works from `file://` and from a
static host. The block is delimited by `<!-- code-overview:nav -->` markers and is
replaced, not appended, on re-run. It ships its own CSS using
`var(--accent, #1f6fbd)`-style fallbacks, so it renders correctly inside
`code-visualization`'s page shell without that skill needing to know it exists. Only
documents that actually exist are linked — a package with no atlas degrades to a
two-item row instead of a dangling href.

## Scoring

Seven categories, each scored 0–100 with its own letter, combined by weight:

| Category | Weight | Half-life |
|---|---|---|
| Correctness | 25 | 6 |
| Security | 15 | 2 |
| Tests & safety net | 15 | 8 |
| Complexity | 15 | 12 |
| Design & structure | 12 | 10 |
| Duplication & dead code | 10 | 10 |
| Dependencies & hygiene | 8 | 25 |

Findings are weighted by severity (high 10, medium 3, low 1), summed per category, and
normalized per thousand lines of code. That density feeds

```
score = 100 · 0.5 ** (density / half_life)
```

so a category whose weighted-finding density equals its half-life scores exactly 50.
The curve is monotone, never negative, and has no cliff — doubling the findings costs
progressively less, which matches how the marginal finding actually matters. Half-lives
encode how much of a given problem is tolerable per KLOC: two weighted security
findings per KLOC is already a 50; it takes twenty-five hygiene findings to say the
same.

Bands: `A+ ≥97, A ≥93, A- ≥90, B+ ≥87, B ≥83, B- ≥80, C+ ≥77, C ≥73, C- ≥70,
D+ ≥67, D ≥63, D- ≥60, F <60`.

**Ungraded is not zero.** A category with no data — no coverage artifact, no manifest,
or a detector that crashed (the doctors report those under `meta.analyzer_errors`,
where a zero count means *unknown*, not *clean*) — is marked ungraded, dropped from
the weighted mean, and the remaining weights renormalized. It is named in the metadata
rather than quietly scored 100.

Findings map to categories by `category` when the doctor supplies one
(`python-code-doctor` and `typescript-code-doctor` do), then by an explicit
smell-type table (`django-code-doctor` emits a flat list), then by keyword. Anything
still unmatched lands in hygiene *and* is recorded under `unmapped_types`, so an
extension to a doctor shows up as a rubric gap instead of silently skewing a grade.

## Root scoring

The root health page recomputes the score from the **union of every package's findings
plus repo-wide-only findings**, over total LOC — not as a mean of package grades. The
root grade is therefore the same kind of measurement as a package grade and directly
comparable to one. The accepted consequence: a repo of one small bad package and one
large good package grades close to the large one. The per-package grade table on the
same page is what keeps the small bad package visible.

## Metadata

Embedded in each health page, self-contained, no sidecar file:

```html
<script type="application/json" id="code-health-meta">
{"schema":"code-health/1","package":"billing","roots":["src/billing"],
 "language":"python","doctor":"python-code-doctor","generated":"2026-08-07",
 "commit":"abc1234","size":{"files":82,"loc":9431},"score":78.4,"grade":"C+",
 "categories":[{"key":"correctness","label":"Correctness","weight":25,"score":71,
                "grade":"C-","density":2.6,"graded":true,
                "findings":{"high":3,"medium":9,"low":4,"total":16}}],
 "ungraded":[],"unmapped_types":[],"findings_total":141,"top_findings":[…]}
</script>
```

The root page emits the same block plus a `packages` array, so one file read yields
every grade in the repo.

## Components

| File | Responsibility |
|---|---|
| `scripts/rubric.py` | Categories, weights, half-lives, severity weights, the finding→category table, grade bands, the scoring function. Pure data + arithmetic, no I/O. |
| `scripts/common.py` | Findings loading (both report shapes), template rendering, LOC counting, metadata-block read/write, relative-link helpers, map I/O. |
| `scripts/discover_packages.py` | Proposes packages from manifests, Django `INSTALLED_APPS`, package layout, then top-level source dirs. Emits candidates with evidence and an explicit `questions` list of what it could not decide. |
| `scripts/build_health.py` | Findings → categories → score → `health.html` + metadata block. `--root` mode adds the package grade table. |
| `scripts/build_summary.py` | `summary.html` for a package or the root, from the health metadata plus agent-authored prose. |
| `scripts/inject_nav.py` | Injects/replaces the nav in every document in the map. Idempotent, existence-checked. |

Templates live in this skill: `assets/template.html` is the one shell carrying the CSS
(the design tokens are `code-visualization`'s, so the three document types read as one
set), with `assets/health-body.html` and `assets/summary-body.html` supplying the two
layouts.

## Workflow

Discover → **ask the user** to confirm, split, or merge the proposed package map, and
to decide what a package with no matching doctor should get → persist the map → per
package, run `code-visualization` scoped to its roots and the matching doctor, then
`build_health.py` and `build_summary.py` → at the root, a repo-wide
`code-visualization` run plus `build_health.py --root` and `build_summary.py --root` →
`inject_nav.py` across everything → verify every href resolves.

Per-package atlases run the full `code-visualization` workflow, judgment tabs included,
scaled to package size; a small package may warrant automated tabs only. Re-runs load
the persisted map and ask only about drift.

## Constraints from the repo

Companion skills are addressed through a variable (`CV=/path/to/code-visualization`),
never `../code-visualization/` — `tests/test_standalone_install.py` forbids sibling
reach because a release archive contains one skill. Every companion gets a documented
fallback for being absent, following `brutal-review`'s precedent. Every script answers
`--help` from a foreign working directory and imports siblings off its own directory.

## Testing

`tests/code_overview/` covers discovery over synthetic monorepos (src-layout Python,
npm workspaces, Go modules, Django apps, multi-root packages), scoring determinism and
the grade bands, ungraded renormalization, metadata round-tripping, nav idempotency and
relative-path correctness at depth, and an end-to-end pass asserting that every href in
every generated document resolves to a file that exists.
