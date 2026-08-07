# Document layout, navigation, and the metadata schema

## Where the files go

```
docs/code-overview.json     the package map — the input to every other script
docs/summary.html           portal: overall grade, package table, links
docs/codemap.html           repo-wide code-visualization atlas
docs/health.html            repo-wide grade + per-package grade table

<pkg-root>/docs/summary.html    package portal
<pkg-root>/docs/codemap.html    package-scoped atlas
<pkg-root>/docs/health.html     package grade
```

A package's docs directory defaults to `<first root>/docs` and is overridable
per package via the map's `docs` field — useful when a package's first root is
somewhere you would rather not write, or when two packages share a parent.

**A single-package repo whose root is the repo root produces the three root
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
templates, a service and the shared types only it uses. Every path is
repo-relative. `doctor` empty means no doctor ships for that language — the
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
package survives. `load_map` rejects that as well.

`discover_packages.py` emits a superset of this shape: the same `packages` list
plus `too_small`, `unassigned`, and `questions`. Strip those three when saving
the map, or leave them — the loaders ignore unknown keys.

## Navigation

`inject_nav.py` writes one block into every document. Three rows:

| Row | Contents |
|---|---|
| Up | `⌂ Overall <Type>` → the repo-level document **of the same type**, then the package name |
| Across | `Summary · Code Map · Health` for this package, current one `aria-current="page"` |
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
| `analyzer_errors` | detector → error, from the doctor's own report |
| `analyzers_skipped[]` | detectors the doctor was told not to run, or that never ran |
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

## Rebuild order

The scripts read each other's output, so order matters:

1. `discover_packages.py` → confirm with the user → save `docs/code-overview.json`
2. **the doctors, once each, from the repo root** — not per package directory
3. per package: atlas → `build_health.py` (partitions the repo-wide findings by
   path) → `build_summary.py`
4. root: atlas → `build_health.py --root --map …` (reads every package
   `health.html`) → `build_summary.py --root --map …`
5. `inject_nav.py` (needs every document to exist, so it can skip what does not)

Step 2 is the one worth stating twice. A doctor pointed at a package directory
loses the project context several of its detectors depend on — the dependency
manifest, the test tree, Django's settings and app registry — and the result is
not a smaller report but a *wrong* one: fabricated findings about a missing
manifest and missing tests, alongside real findings it can no longer see.

Running `build_health.py --root` before the package health pages exist is not an
error — it warns per missing package and leaves them out of the table. Re-run it
after.

## Generating the atlases

Pass `--exclude docs` to the `code-visualization` analyzers. Without it, a
rebuild counts the previously-generated HTML as source: a small package's own
`docs/codemap.html` can be 90% of its measured lines, and the inventory tab then
describes the documentation rather than the code.
