# Authoring the judgment tabs

The automated tabs measure the diff; these tabs judge it. Their shared purpose is to help a reviewer **zero in on bad choices**: gaps between what the change claims and what it does, callers left behind, asymmetries, and violated invariants. Restating the diff is not analysis — a reviewer can read the diff. Say what the diff *means* and where it is likely wrong.

Fragment format: file in the tabs dir, first line `<!-- tab: Title -->`, body uses the template's styled primitives only (no `<style>`/custom `<script>`):

- `card` / `card hero`, `callout` (`warn`/`bad`/`good`), `badge good|warn|bad|accent|neutral`
- `kpis` → `kpi` blocks; `grid cols-2|cols-3`
- `tbl-wrap` + `table` (`class="sortable"`, `class="num"` on numeric cells)
- `plusminus` (`<span class="p">+12</span> <span class="m">−4</span>`) for add/del counts
- `diff-snippet` for quoted diff lines:
  ```html
  <div class="diff-snippet"><div class="fh">api/orders.py — lines 140–147</div>
  <span class="ln ctx">  def place_order(user, cart):</span>
  <span class="ln del">-     if cart.total > 0:</span>
  <span class="ln add">+     if cart.total >= 0:</span>
  </div>
  ```
- Mermaid diagrams in a pan/zoom wrapper:
  ```html
  <div class="mermaid-wrap"><div class="mviewport"><pre class="mermaid">
  sequenceDiagram
    ...
  </pre></div></div>
  ```
  (Escape `<`/`>` in labels; avoid `stateDiagram-v2` if labels contain colons/parens — use `flowchart LR` with quoted edge labels.)

Cite `file:line` for every claim so it's verifiable. Quote the actual diff lines (in `diff-snippet`) for your most important findings — evidence beats assertion. Mark uncertain claims as unverified (`badge warn`) rather than guessing silently.


**Citation format** (machine-checkable): cite files inside `<code>` as `path/from/repo/root.py`, with lines as `path.py:142` or `path.py:140-152`. `scripts/verify_citations.py` parses exactly these forms and checks them against the repo — full paths beat bare filenames (bare names get flagged when several files share them). When you deliberately cite a file that no longer exists (a removed or renamed artifact), the verifier will flag it as missing — that flag then serves as confirmation of the removal, not an error to fix.
---

## Tab 1 — Summary (file `01-summary.html`)

A behavioral summary written **from the diff itself, independent of the author's description**, then compared against that description if one was provided.

- **Hero card**: what this change actually does to the system's behavior, 2–5 sentences. Behavior, not files: "orders with total 0 are now accepted and skip payment" — not "modified orders.py".
- **Claimed vs. actual** (if a PR title/description is available): a small table — each claim → verdict badge (`good` matches / `warn` partially / `bad` diff does something else or something extra). Undisclosed behavior changes are the single most valuable thing a reviewer can learn; give each one a `callout warn` with the diff evidence.
- **Change classification**: feature / bugfix / refactor / mixed — and if mixed, say which files belong to which concern; bundled unrelated changes are a reviewable defect in themselves.
- **What did NOT change** when a reader would expect it to (config but no docs, behavior but no tests, new failure mode but no alerting). Absence is invisible in a diff; naming it is your job.

## Tab 5 — Flow Impact (file `05-flow-impact.html`)

Before/after for the 1–3 runtime flows this change touches. Skip flows the change doesn't alter.

- For each affected flow: two Mermaid `sequenceDiagram`s side by side (`grid cols-2`), **Before** and **After** — or one diagram with the changed messages annotated, when the delta is small. Include error paths (`alt`/`opt`) — that's usually where the change's risk lives.
- Under each: a "what moved" list — steps added/removed/reordered, new external calls, changed transaction or lock boundaries, changed error propagation. Each item cites the file.
- If the change is purely structural (no runtime flow change), write one `callout good` saying so with the reasoning, and keep this tab minimal — an honest short tab beats an invented diagram.

## Tab 6 — Review Walkthrough (file `06-review-walkthrough.html`)

The tab a busy senior reviewer would want first. Work through the risk-ordered file list from `analyze_diff` (top entries at minimum), and for each significant file give a verdict, not a summary: what to check, what looks wrong, what's fine. Then run the **asymmetry checklist** against the whole diff — each asymmetry is a classic latent bug:

| Opened... | ...but closed? |
|---|---|
| resource acquired (file, conn, lock) | released on *all* paths, incl. exceptions/early returns? |
| state written (cache, flag, session) | invalidated/cleared anywhere? |
| new config/env var read | defaulted, validated, documented? |
| retry added | bounded? idempotent operation? |
| new error caught | handled, or swallowed? |
| feature flag added | removal path or permanent fork? |
| symbol renamed/moved | all callers updated (cross-check Blast Radius tab)? |
| serialization format touched | old data still readable? |

Present only the rows that apply, as a table with verdict badges and citations, or as `callout`s for the serious ones.

- **Invariant impact**: any codebase invariant this diff weakens or now must maintain in one more place ("uniqueness previously guaranteed by X is now also assumed by Y"). If a codebase atlas exists for this repo, check its Invariants tab.
- Close with a **verdict card** (`card hero`): merge-readiness in one line, then the ordered list of must-fix / should-fix / nit items, each citing file:line. This list is the deliverable of the whole review; make every item concrete enough to act on.

---

**Inputs you should use**: the JSON summaries printed by `analyze_diff.py` (risk-ordered files with reasons, signature changes, risky added lines with line numbers, untested files) and `analyze_blast_radius.py` (untouched callers). Read the actual diff (`git diff base...head -- <file>`) for every file you make claims about — the summaries point, the diff proves.

**Skipping**: an empty-body fragment is dropped by the assembler. Skip Flow Impact for pure-docs or pure-test PRs rather than padding it.

**Length**: Summary and Flow Impact ≤ 2 minutes each to read; Walkthrough can be longer but push per-file detail into `<details>` once you're past the top risks.
