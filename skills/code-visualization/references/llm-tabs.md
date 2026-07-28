# Authoring the judgment tabs

These tabs are the ones scripts can't produce: they require reading the code and forming a view. Their purpose is to make **mismatches** visible — intent vs. implementation, actual structure vs. sensible structure. A tab that merely restates the code is decoration; every section should either orient the reader or surface something checkable.

Write each tab as a fragment file in the tabs dir. Format:

```html
<!-- tab: Overview -->
<h2>...</h2>
<p>...</p>
```

`scripts/lint_fragments.py --tabs-dir TABS` is the mechanical check of this protocol (tab header on line 1, no `<style>`, no `<script>` except the JSON data blocks described below, each paired with a `.viz` container) — run it before assembling.

Use the primitives from the template (they're already styled — no `<style>` blocks in fragments):

- `<div class="card">`, `<div class="card hero">` — the hero card for the single most important takeaway of a tab
- `<div class="callout">` / `callout warn` / `callout bad` / `callout good` — findings, ranked by severity
- `<span class="badge good|warn|bad|accent|neutral">` — inline status markers
- `<div class="kpis"><div class="kpi"><div class="n">42</div><div class="l">label</div></div></div>` — headline numbers; add `good|warn|bad|accent` on the `.kpi` to color the number
- `<div class="grid cols-2">` / `cols-3` — side-by-side layout; bare `grid` (no `cols-*`) stacks children in one column
- `<div class="tbl-wrap"><table class="sortable">...</table></div>` — tables (add `class="num"` to numeric `th`/`td`); when a cell's text isn't a plain number (`1.2k`, `n/a`), put the raw value in `data-sort` on the `td` so sorting stays numeric
- `<div class="bar"><i style="width:63%"></i></div>` — inline fill bar for table cells; `bar warn|bad|good` recolors the fill
- `<details><summary>...</summary><div class="body">...</div></details>` — secondary material
- `class="dim"` for secondary text, `class="faint"` for tertiary; `<code>` for identifiers and paths

For diagrams, use Mermaid inside a pan/zoom wrapper:

```html
<div class="mermaid-wrap"><div class="mviewport"><pre class="mermaid">
sequenceDiagram
  participant C as Client
  participant S as server.py
  C->>S: POST /orders
</pre></div></div>
```

Mermaid caveats: escape `<` and `>` in labels or avoid them; `stateDiagram-v2` transition labels break on colons/parentheses — use `flowchart LR` with quoted edge labels (`|"label"|`) when labels need punctuation. Keep each diagram under ~15 participants/nodes; split large flows into several diagrams.

For quantitative structure, the template ships two data-driven renderers. Each is a `<div class="viz" data-render="...">` whose **immediately following sibling** is a `<script type="application/json">` data block; a `style="height:...px"` on the `.viz` div sizes the canvas (set one — the box collapses without it). Both get tooltips, and the force graph gets pan/zoom and node drag, for free.

Treemap — `value` drives area, `metric` drives heat color (`metricMax` pins the color scale; it defaults to the max metric present):

```html
<div class="viz" data-render="treemap" style="height:480px"></div>
<script type="application/json">
{"valueLabel":"lines","metricLabel":"churn","metricMax":40,
 "items":[{"name":"api/orders.py","value":812,"metric":31,"meta":"12 commits/90d"},
          {"name":"api/auth.py","value":214,"metric":4}]}
</script>
```

Force graph — node color is auto-assigned per `group`, radius scales with `size`, `weight` thickens an edge, `cycle: true` draws it red; `fanIn`/`fanOut`/`meta` feed the tooltip and `legendExtra` (an HTML string) appends to the legend:

```html
<div class="viz" data-render="forcegraph" style="height:560px"></div>
<script type="application/json">
{"nodes":[{"id":"api","label":"api","group":"web","size":1200,"fanIn":0,"fanOut":3,"meta":"entry layer"},
          {"id":"store","label":"store","group":"data","size":400,"fanIn":3,"fanOut":1}],
 "links":[{"source":"api","target":"store","weight":12},
          {"source":"store","target":"api","weight":1,"cycle":true}],
 "legendExtra":"<span>red edge = cycle</span>"}
</script>
```

Ground every claim in the code: cite file paths (and line numbers when specific) in `<code>` so a reader can verify. Prefer "X is enforced only in `api/orders.py:142`" over "X is mostly enforced." When you are unsure, say so with a `badge warn` marked "unverified" rather than asserting.


**Citation format** (machine-checkable): cite files inside `<code>` as `path/from/repo/root.py`, with lines as `path.py:142` or `path.py:140-152`. `scripts/verify_citations.py` parses exactly these forms and checks them against the repo — full paths beat bare filenames (bare names get flagged when several files share them). This is an atlas of the code as it stands: cite only files that exist now. If a removal or rename is worth mentioning, describe it in prose without a `<code>` path, so every citation stays verifiable.

---

## Tab 1 — Overview (file `01-overview.html`)

The orientation a senior engineer would give a new teammate.

- **Hero card**: what this system is, in 2–4 sentences — its job, its users/callers, its architectural style (layered monolith, plugin core, pipeline, service, library...). This is a thesis, not a description of directories.
- **Entry points**: where execution starts (CLI mains, HTTP routes, handlers, exported API surface), as a table: entry point → what it kicks off.
- **Reading order**: a numbered tour of 5–10 files: "start here, then here, skip these". Say *why* each stop matters and what to ignore (generated code, vendored code, legacy areas).
- **Layer/component map**: a small Mermaid `flowchart TD` of the intended architecture (5–10 boxes max) — the mental model, not the full dependency graph (the Dependencies tab has the real one). If the real graph contradicts this intended model, say so here with a `callout warn` — that contradiction is a finding.
- **Key decisions & constraints**: notable choices visible in the code (framework, concurrency model, storage, error strategy) and any constraint that explains a weird-looking design, if discoverable from comments/docs/ADRs.

## Tab 5 — Critical Flows (file `05-flows.html`)

2–4 sequence diagrams for the flows where correctness matters most (main request path, auth, persistence/transaction path, startup/shutdown, job processing — pick what fits this codebase). For each flow:

- A Mermaid `sequenceDiagram` of the *actual* implementation — include the unhappy parts: retries, timeouts, error returns (use `alt`/`opt` blocks).
- A short "seams" list under each diagram: the risky boundaries the diagram exposes (partial failure between steps 3–4, no timeout on the outbound call, transaction opened in one layer and committed in another...). Each item cites the file.

## Tab 6 — Boundaries & Ownership (file `06-boundaries.html`)

Who owns each concern — and which concerns are smeared.

- **Concern ownership table**: concern (auth, validation, retries, caching, config, logging, serialization, ...) → owning module(s) → verdict badge: `good` single owner / `warn` 2 owners / `bad` smeared. Only include concerns that actually exist in this codebase.
- **Data ownership**: for each store/table/shared structure, who writes it. Multiple writers ⇒ `callout warn` with the writer list.
- **Boundary violations**: concrete places where a layer reaches around another (UI touching the DB, domain logic importing the web framework...), each with file citation. If the Dependencies tab found cycles, explain *what* each cycle means semantically here.

## Tab 7 — Invariants & Risks (file `07-invariants.html`)

The most judgment-heavy tab. Three sections:

- **Invariants**: table of invariants the code appears to rely on ("a user has exactly one active cart", "IDs are unique per tenant", "handler X never throws") → where enforced (file:line) → verdict: `good` enforced / `warn` partially / `bad` assumed but enforced nowhere. Unenforced invariants are the headline finding of this tab.
- **Failure handling**: where errors are swallowed, logged-and-continued, or handled far from their cause; missing timeouts; unbounded retries. Cite each.
- **Concurrency**: shared mutable state, locks and their ordering, async/thread boundaries. If the codebase is single-threaded/synchronous, one sentence saying so is the correct content.
- Close with a **risk ranking**: top 3–5 things a maintainer should worry about, each one line, ordered by (likelihood × blast radius). Use `callout bad`/`warn` for the top items.

## Tab 8 — Glossary (file `08-glossary.html`)

Domain language vs. code language — drift here *is* a design problem.

- Table: domain term → code name(s) → files → note. Flag with `badge warn` where one domain concept has several code names, or one code name means several things.
- A short list of misleading names (things named X that actually do Y), if any exist. If naming is clean, say so in one `callout good` and keep the tab short — don't pad.

---

**Skipping**: if a tab genuinely has no content for this codebase (e.g., a tiny library with no lifecycle states), either fold a one-line note into a neighboring tab or write the fragment with an empty body — the assembler drops empty fragments. Don't fabricate content to fill a tab.

**Length discipline**: each tab should be readable in 2–4 minutes. Move anything longer into `<details>`.
