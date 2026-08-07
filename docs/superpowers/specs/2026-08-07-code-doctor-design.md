# code-doctor — a language-agnostic quality and bug reviewer

Date: 2026-08-07
Status: approved design, not yet implemented

## The question this answers

The repo ships `python-code-doctor`, `typescript-code-doctor`, and
`django-code-doctor`. Should the non-language-specific work be extracted into one
general skill, and can the language-specific skills fold into it as sub-skills?

## What the investigation found

**There is far less shared text than the parallel filenames suggest.** Nine
reference files exist under the same name in both `python-code-doctor` and
`typescript-code-doctor`, and five scripts do. Every paired reference diffs at
roughly the sum of both files' line counts — they were written independently, not
forked. The section headings diverge too: the Python over-engineering guide opens
on "The cost model", the TypeScript one on "The structural-typing argument".

| paired reference | py lines | ts lines | overlapping |
|---|---|---|---|
| `critical-review-guide.md` | 67 | 100 | ~0 |
| `overengineering-and-abstraction.md` | 90 | 122 | ~0 |
| `safety-net-and-testing.md` | 142 | 114 | ~0 |
| `messy-repo-runbook.md` | 138 | 118 | ~0 |
| `ai-generated-code.md` | 156 | 106 | ~0 |

Scripts are the same story. `format_findings.py` is close to identical (pure
JSON→markdown), `common.py` is ~30% shared, and `analyze_all.py`,
`analyze_diff.py`, `run_external_tools.py` share a shape but no implementation.

Extracting the intersection would yield roughly six files and 400 lines — a
chapter, not a skill. **Deduplication is not a sufficient reason to do this.**

Sizes, for the merge question:

| | files | bytes |
|---|---|---|
| python-code-doctor | 51 | 610 KB |
| typescript-code-doctor | 52 | 458 KB |
| django-code-doctor | 36 | 358 KB |
| all three merged | 139 | 1.4 MB |

Nothing in `tools/validate_skills.py` caps file count or size, and nested
directories under `references/` are loaded on demand — so a merge is mechanically
possible. Its real costs are a single 1024-char description covering three
languages (each currently spends ~1000 chars of trigger surface on one), and the
loss of per-language release tags.

## Decision

Build `code-doctor` as a **new, separate, genuinely language-agnostic skill**. Do
not merge anything yet.

- It carries **no per-language knowledge**: no comment-syntax tables, no import
  resolvers, no framework awareness, no parsers.
- `django-code-doctor` stays independent **permanently**. Framework skills sit
  above the language layer and do not fold into it.
- Revisit merging `python-code-doctor` and `typescript-code-doctor` only after
  `code-doctor` exists and the shared doctrine has converged in practice.

## The boundary with code-visualization

`code-doctor` measures **code quality and bugs**. `code-visualization` explains
**architecture**. This is a hard line, and it decides what `code-doctor` may emit.

**Invariant: the reference graph is an internal primitive, never a deliverable.**
It exists only to produce findings and candidates — a cycle is a bug class, a
zero-inbound file is a dead-code lead, a god module is a maintenance defect.
`code-doctor` never emits a map, a diagram, a dependency picture, or a reading
order.

Where the two skills touch the same data (churn, complexity), they produce
different artifacts and neither reads the other's.

## Confidence discipline

This skill buys language-independence by giving up parsing, which means most of
its evidence is heuristic. The whole design is only trustworthy if it is honest
about that, so this is an invariant rather than a style note.

**Two output classes, and a detector must pick one.**

- A **finding** asserts a defect. It carries a location, a consequence, a concrete
  fix, and the reason it is worth doing. A detector may only emit findings for
  claims its evidence actually supports — a merge-conflict marker, a committed
  private key, an oversized file.
- A **candidate** reports a lead requiring verification. It carries a location,
  what was observed, **the specific ways a healthy codebase produces this same
  observation**, and no fix. Dead functions, zero-inbound files, near-duplicates,
  and decision density are candidates.

Reporting a candidate as a finding is the failure mode that makes a tool like this
useless — it recommends deleting live code — so the distinction is enforced in the
findings schema, not left to prose.

**Degrade audibly, never silently.** Every detector whose evidence can be
incomplete reports the completeness of that evidence alongside its output:
resolution rate for the reference graph, history depth for the git detectors,
classifiability for tests. A sparse graph must read as under-resolution, never as
"no coupling"; a shallow clone must read as unknown ownership, never as a bus
factor of one; an unclassifiable directory must read as inconclusive, never as
"no tests". Where completeness cannot be established, the detector suppresses the
finding rather than qualifying it in a footnote.

**Never assert a negative from an incomplete index.** "This is not referenced
anywhere", "these files have no dependency between them", "this directory has no
tests" are all claims a heuristic index cannot support on its own. Each is either
downgraded to a candidate or gated on positive resolution for the specific files
involved.

This is the stance `typescript-code-doctor` already takes in *Verify before
believing*, and that `code-visualization`'s `imports.py` takes with
`ResolutionStats` — "a measured gap, not a silent one". `code-doctor` needs it
more than either, because it knows less.

## Architecture: three layers

Stated explicitly in `SKILL.md`, because the honest claim differs per layer.

| layer | what it gives | needs |
|---|---|---|
| **raw** | works on any text-based repo | git + Python 3.11, nothing else |
| **the repo's own toolchain** | whatever the project already configured | the repo's `Makefile` / `justfile` / `package.json` scripts / `.pre-commit-config.yaml` / CI workflow |
| **specialist handoff** | deep, parser-backed findings | `python-code-doctor` / `typescript-code-doctor` installed alongside |

The middle layer is how `code-doctor` gets language-specific power on a Go or Rust
repo without knowing Go or Rust: the project usually already declares its own
checks, and running them beats guessing.

## Deterministic layer — 17 scripts

### Foundation

- **`common.py`** — the source walk, severity icons, console, one policy for
  unreadable files, git helpers (including the shallow-history probe below).

  The walk applies **three** filters, and all three are load-bearing:

  1. **Directory exclusion**, carried over from the existing skills: `.git`,
     `node_modules`, `vendor`, `third_party`, `.venv`, `build`, `dist`, `target`,
     framework build roots, and the tool caches — plus `.gitignore` awareness.
     This is the one that matters most for cost: vendored and generated code has
     ordinary source extensions, so an extension filter never touches it, and on
     a large project it would both dominate runtime and fill the report with
     defects in code the user does not own.
  2. **A denylist of known non-code text** — `.md`, `.rst`, `.txt`, `.json`,
     `.yaml`, `.toml`, `.lock`, `.csv`, `.svg`, minified bundles, and anything
     under a documentation root — keeping prose and generated data out of the
     code-only detectors.
  3. **A binary sniff**, so a NUL-bearing blob is never read as text.

  Filter 2 is a denylist rather than a language table on purpose: it enumerates
  what is *not* code, so an **unknown** extension is still treated as code, which
  is what keeps the skill language-blind. A table of known languages would
  silently skip the next one.

  Every detector additionally **declares its own scope**. Branch counting,
  indentation nesting, duplication shingling, and declaration heuristics run only
  over walk-classified source. Secrets, merge markers, and TODO inventory run over
  all text, because those findings are real in a YAML file too.

### Git-derived signals (no language knowledge at all, highest unique value)

- **`find_hotspots.py`** — the targeting engine, scored
  **`churn × complexity-proxy × (1 + defect signal)`**. The `1 +` is not
  cosmetic: a plain product zeroes any file whose commit messages happen not to
  match fix/bug keywords, which would drop a hot, complex, fast-moving file to the
  bottom of the list alongside dormant clean ones. Teams that fix defects inside
  feature commits are the common case, not the exception, so defect history
  boosts the ranking rather than gating it. Effort follows change frequency, never
  line count.

- **`find_change_coupling.py`** — files that repeatedly change in the same commit.
  Co-change strength is a **candidate**, not a finding: a source file and its
  test, a manifest and its lockfile, a schema and its migration, and a component
  and its snapshot all co-change constantly and healthily, so the observation
  alone establishes no defect and names no fix. The `also_caused_by` list carries
  exactly those healthy forms.

  The detector additionally **does not claim the absence of a structural
  dependency** unless `map_references.py` positively resolved references for both
  files; with heuristic-only edges it cannot support that claim, and a Go caller
  changing alongside the package it imports would otherwise be mislabelled as
  hidden coupling when it is ordinary structural coupling. Also flags files that
  change with everything.
- **`find_ownership_risks.py`** — bus factor: single-author files, and files whose
  only author has stopped committing.

Defect density is per-file density of commits whose messages match fix/bug/
hotfix/revert. It is an empirical answer to "where do bugs live", as opposed to
complexity's guess.

**All four git-derived detectors probe history depth first**
(`git rev-parse --is-shallow-repository`, plus the age of the oldest reachable
commit against the requested window). A shallow CI checkout exposes only the most
recent committer, which turns multi-author files into apparent single-author files
and produces a confidently wrong ownership report. On truncated history these
detectors skip or mark every finding incomplete; they never quietly compute over
the fragment. `code-visualization/analyze_hotspots.py` already does this probe —
same reasoning.

### Reference graph (heuristic, self-reporting)

- **`map_references.py`** — builds two indexes and emits them as JSON: a
  file-to-file reference graph, and a symbol occurrence index.

  The graph is built without any import syntax knowledge. For each file it
  extracts candidate tokens — quoted strings containing `/` or `.`, and bare
  identifiers in the file's header region where imports live in essentially every
  language — then matches them against the tree's file basenames (sans extension)
  and path suffixes, preferring the longest path match and requiring
  distinctiveness (length ≥ 3, not a common word).

  **It reports its own resolution rate.** A sparse graph must read as
  under-resolution, never as "no coupling". Package-style imports (Go, JVM, Rust)
  resolve only insofar as package names match directory names; re-exports,
  aliased packages, and vendored-vs-first-party are known misses and are named as
  such in the output and in `references/unknown-language-review.md`.

- **`find_unreferenced.py`** — consumes the map. Emits two **candidate** classes,
  never fixes (see *Confidence discipline*): **dead-function candidates** (a
  distinctive identifier — length ≥ 4, not a common word — occurring exactly once
  in the whole tree in declaration-shaped context) and **zero-inbound files**.

  Zero inbound edges is not orphanhood. Executable entry points, convention-loaded
  plugins, a library's public surface, and CI configuration all legitimately have
  no internal referrer — and the graph independently under-resolves package-style
  and aliased references, so the two errors compound. The detector therefore
  **never recommends deleting or wiring in a file**. It reports the candidate,
  names the reasons a live file lands in this set, and prints the graph's
  resolution rate beside it.

  **Entry points are excluded on manifest evidence only, never on a filename
  convention.** A `go.mod`, `Cargo.toml`, `package.json`, `pyproject.toml`, or
  build config that *declares* a binary, an entry, or a script is evidence this
  repo supplied about itself. A baked-in list of `main.go` / `main.rs` / `cmd/*`
  would be per-language knowledge — the thing this layer does not carry — and a
  partial one at that, silently mishandling Ruby `bin/*`, Zig `src/main.zig`, and
  JVM main classes while appearing to handle entry points in general. Where no
  manifest declares entry points, nothing is excluded and every zero-inbound file
  is simply reported as the candidate it is.

  Both classes trade recall hard for precision, matching the repo's existing bias
  of false negatives over false positives.

- **`find_structure_issues.py`** — consumes the map. Import cycles, god modules
  (high fan-in *and* fan-out), and low directory cohesion (a directory whose files
  never reference each other is not a module).

  **All three are candidates.** An earlier draft of this spec called cycles
  findings, on the reasoning that the graph's misses can only hide a cycle rather
  than invent one. That reasoning is wrong: a token match is evidence that a
  filename appeared in another file, not that it appeared *as a reference*. Two
  modules naming each other in header comments, docstrings, or a changelog produce
  both edges and a cycle that does not exist. Resolution rate measures whether
  tokens matched files, never whether the match was semantic — so nothing built on
  these edges can assert a defect. God modules and low cohesion rest on edge
  counts, which under-resolution distorts downward besides. All three carry the
  resolution rate.

### Duplication

- **`find_duplication.py`** — DRY. Normalizes each line, then shingles N-line
  windows. Reports **exact** duplicates and **near-duplicates via Jaccard
  similarity over shingle sets**. Near-duplicate detection is the point:
  copy-paste-with-mutation is what actually rots a codebase, and exact matching
  misses all of it.

  **Normalization order matters, and it is the one place this skill touches
  comment syntax.** String and number literals are blanked *first*, then a minimal
  universal comment-prefix set (`//`, `#`, `--`, `;`, `/* */`) is handled, then
  whitespace is collapsed. Blanking literals first is what stops
  `url = "https://x"; do_unique_work()` from losing its trailing call to a `//`
  that was never a comment.

  **Ambiguous lines are retained, not stripped.** An earlier draft argued the
  ambiguity was safe because a mis-normalized line would merely fail to match its
  twin. That is only half true, and the wrong half: stripping is a *deleting*
  operation, so removing two blocks' distinct Rust attributes or C preprocessor
  directives makes their residues **more** alike, manufacturing a near-duplicate
  that does not exist. The error runs in both directions. So a line whose prefix
  is ambiguous in context — `#` at the start of a line, `;` anywhere — is kept
  verbatim in the shingle rather than being cut. Only unambiguous whole-line
  comments are dropped. Both directions are tested: a fixture pair that must not
  match, and a fixture pair that must.

  This concession is scoped to normalization only. It is a five-token text fact,
  not a language table, and no detector branches on which language a file is.
  Duplication output is a **candidate** regardless — the same shape is not the
  same decision.

### Complexity

- **`analyze_complexity.py`** — decision density, nesting depth by indentation,
  file and function length via declaration-shaped segmentation, and an arity proxy
  from commas between parens on declaration lines.

  The branch-token set is `if` / `else` / `for` / `while` / `case` / `catch` /
  `&&` / `||`. **`?` is deliberately excluded**: in TypeScript it marks optional
  properties, optional parameters, optional chaining, and conditional types, none
  of which is a decision, and in Rust it is error propagation. Counting it would
  report type-heavy files with no control flow as complex.

  Decision density and the arity and function-boundary proxies are all marked
  **heuristic** — they rest on declaration-shaped segmentation, not parsing. Only
  nesting depth and raw length are reported as facts.

### Correctness and hygiene

- **`find_hygiene_issues.py`** — merge conflict markers, TODO/FIXME/HACK/XXX
  inventory with git-blame age, commented-out code, oversized files and lines,
  committed `.env` files and large binaries, debug-print leftovers.

  Commented-out code uses the same five-token comment-prefix set as
  `find_duplication.py`, with literals blanked first, and is a **candidate**: a
  commented line that looks like code may be a documentation example, and `#` may
  be an attribute rather than a comment.

  **Merge markers are gated on repository state.** The marker text is positive
  textual evidence, but this detector scans all text — and a documentation
  example, a snapshot, or a fixture that exists precisely to test conflict
  handling will legitimately contain `<<<<<<<` / `=======` / `>>>>>>>`. (This
  repo's own test suite writes such fixtures.) Where git is available, a marker
  in a path git reports as **unmerged** (`git ls-files -u`, `git diff --check`) is
  a **finding** at high severity; a marker anywhere else — or anywhere at all when
  git is unavailable — is a **candidate** naming documentation, snapshots, and
  conflict-handling fixtures as the benign forms.

  Oversized files and lines, and committed `.env` files, remain findings: none of
  them depends on comment syntax or on repository state.
- **`find_secrets.py`** — key material (private key blocks, cloud credentials,
  JWTs) and high-entropy values assigned to names containing key/token/secret/
  password.

### Tests

- **`find_test_gaps.py`** — test-to-source mapping by the union of universal
  filename conventions, assertion density, test files that assert nothing, and
  test:source ratio per directory.

  **Filename conventions cannot see tests embedded in source files.** Rust's
  `#[cfg(test)] mod tests` lives in the production `.rs` file; D, Zig, and Go
  example functions have similar habits. A fully tested Rust directory would
  otherwise report a zero test:source ratio — a confident claim of "no tests"
  about code that is tested. So the detector first asks whether tests are
  **classifiable** in this tree: filename-convention hits, an in-file test marker,
  or a test target declared in the manifest or toolchain inventory. When none of
  those resolve for a directory, it reports **"test classification inconclusive"**
  and suppresses the ratio and mapping findings for it rather than reporting zero.

  **"Asserts nothing" is a candidate, never a finding.** Assertion density is
  counted over a generic marker set, and a test that verifies plenty can match
  none of it: a Go test delegating to a helper that calls `t.Fatal`, a custom
  matcher or DSL, an expected-exception test, a snapshot test, a table-driven
  harness, or a property-based generator. Establishing that a test verifies
  nothing needs framework knowledge this layer does not have, so the record names
  those forms in `also_caused_by` and stops there.

### The repo's own toolchain

- **`find_project_checks.py`** — inventory what the repo already defines:
  `Makefile` targets, `justfile` recipes, `package.json` scripts,
  `.pre-commit-config.yaml`, `tox.ini`, CI workflow steps. Reports each with its
  **literal command text**, so what would run is visible before anything runs.
  This is the default entry point, and it executes nothing.

- **`run_project_checks.py`** — runs selected checks and normalizes their output
  into this skill's findings shape.

  **A check-shaped name is not evidence of non-mutating behavior, so the name is
  never sufficient to run it.** `"lint": "eslint . --fix"` is an ordinary thing to
  find in a `package.json`, and an opaque `make check` recipe can regenerate
  fixtures, write to a database, or call an external service. The script therefore:

  1. **Never runs anything without explicit opt-in.** No target list, no run.
  2. **Classifies each command by inspecting the command text**, not the target
     name — a recognized read-only invocation of a known checker, versus anything
     carrying a mutation flag (`--fix`, `--write`, `-i`, `format` without
     `--check`), versus opaque (a recipe whose body it cannot read through, a shell
     script, a nested `make`).
  3. **Escalates anything not provably read-only to the agent, via a two-stage
     protocol.** A Python subprocess cannot call `AskUserQuestion` — that is an
     agent tool, and a script that claims to use it would either crash or, worse,
     skip the safeguard and run the command. So the script instead **exits without
     running**, emitting a structured `confirmation_required` payload: each
     command's literal text, its classification, and a digest of that text. The
     *agent* puts the question to the user, and reruns with
     `--confirm <digest>[,<digest>…]`. A digest that does not match the command
     found on rerun is refused, so a confirmation cannot be replayed against
     changed content. Read-only-classified commands run without this round trip.
  4. Never installs anything.

  Opaque recipes are the common case, not the edge case, so the confirmation
  round trip is the expected path rather than a rare fallback. `SKILL.md`
  documents both stages, since the agent is half the protocol.

### Orchestration and output

- **`analyze_all.py`** — unified report, `--skip cat1,cat2`.
- **`analyze_diff.py`** — the diff lens. Runs the file-level detectors against the
  changed files and then, **by default, keeps only findings anchored to
  added or modified lines** — matching both specialist `analyze_diff.py`
  implementations. Changed *files* is the wrong scope: a one-line edit to a large
  legacy file would otherwise report every pre-existing hygiene and complexity hit
  as though the change introduced it, which is how a review tool trains people to
  ignore it. Findings anchored elsewhere are baseline-gated. Git-based, so already
  language-agnostic.
- **`format_findings.py`** — list / cards / JSON artifact renderer. This file is
  where a future python+typescript merge would start.

All detectors share the existing interface: `--format text|json`, `--ignore
type1,type2`, 🔴/🟡/🟢 severities, flat JSON findings list.

## Judgment layer — 7 references

The genuinely language-neutral doctrine, written once:

| file | loaded when |
|---|---|
| `critical-review-guide.md` | reading critically — the per-unit questions, the triage rubric, the finding format, verify-before-believing, what not to raise |
| `overengineering-and-abstraction.md` | judging whether an abstraction pays rent; DRY vs the wrong abstraction; YAGNI |
| `refactoring-catalog.md` | diagnosing smells — the classic catalog, language-neutral |
| `safety-net-and-testing.md` | building the net before refactoring — characterization tests, coverage as a map, hollow tests |
| `messy-repo-runbook.md` | cleaning a working-but-messy repo from cold — the phased campaign |
| `ai-generated-code.md` | reviewing AI-written or vibe-coded changes |
| `unknown-language-review.md` | **new** — reviewing a language you do not know well: read the manifest, find the entry points, trust the repo's own toolchain over instinct, and know what each heuristic can and cannot support |

`unknown-language-review.md` is the file that earns the skill. It is also where the
reference graph's known misses are documented, so a thin graph is never mistaken
for a clean one.

## Handoff

The `brutal-review` precedent: probe for the sibling directory, degrade audibly.

```bash
SPECIALIST="$(dirname "$SKILL")/python-code-doctor"
if [ -f "$SPECIALIST/scripts/analyze_all.py" ]; then
    # tell the agent to load that skill
else
    echo "python-code-doctor not installed — raw layer only"
fi
```

No skill reads another skill's *contents*; it tests for presence and names the
skill to load. `tests/test_standalone_install.py` keeps this honest.

`SKILL.md` defers explicitly: Python → `python-code-doctor`, TS/TSX →
`typescript-code-doctor`, Django → `django-code-doctor`, adversarial diff review →
`brutal-review`, architecture understanding → `code-visualization`.

## Repo integration

- `tests/code_doctor/` — each CLI driven as a subprocess over throwaway git repos
  built by the `Repo` fixture in `tests/conftest.py`. Fixtures in **Go, Rust, and
  Ruby** specifically, to prove the heuristics are actually language-blind rather
  than accidentally Python-shaped.
- `evals/code-doctor/evals.json` — the judgment half, per the validator's pairing
  requirement.
- Added to the CI ratchet alongside every other skill that ships `scripts/`.
- `README.md` updated with the skill and with the code-doctor / code-visualization
  boundary.

25 shipped files (17 scripts, 7 references, `SKILL.md`) — half of either existing
code-doctor skill.

## Explicitly out of scope

- Per-language detectors, parsers, or syntax tables of any kind.
- Any map, diagram, or architecture narrative — that is `code-visualization`.
- Merging `python-code-doctor` and `typescript-code-doctor`. Deferred until this
  skill exists and the doctrine has converged.
- Folding in `django-code-doctor`. Permanently out.
