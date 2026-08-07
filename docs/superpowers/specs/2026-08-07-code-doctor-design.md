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
It exists only to produce findings that name a fix — a cycle is a bug class, an
orphan file is dead code, a god module is a maintenance defect. `code-doctor`
never emits a map, a diagram, a dependency picture, or a reading order. Every
output is a finding with a location, a consequence, a concrete fix, and a reason
it is worth doing.

Where the two skills touch the same data (churn, complexity), they produce
different artifacts and neither reads the other's.

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

- **`common.py`** — extension-agnostic source walk (binary sniff, vendor and
  build directory exclusion, `.gitignore` aware), severity icons, console,
  one policy for unreadable files, git helper.

### Git-derived signals (no language knowledge at all, highest unique value)

- **`find_hotspots.py`** — the targeting engine: churn × complexity-proxy ×
  defect density. Effort follows change frequency, never line count.
- **`find_change_coupling.py`** — files that repeatedly change in the same commit
  but have no structural dependency between them. Hidden coupling, invisible to
  static analysis in any language. Also flags files that change with everything.
- **`find_ownership_risks.py`** — bus factor: single-author files, and files whose
  only author has stopped committing.

Defect density is per-file density of commits whose messages match fix/bug/
hotfix/revert. It is an empirical answer to "where do bugs live", as opposed to
complexity's guess.

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

- **`find_unreferenced.py`** — consumes the map. Two findings: **dead-function
  candidates** (a distinctive identifier — length ≥ 4, not a common word —
  occurring exactly once in the whole tree in declaration-shaped context) and
  **orphan files** (nothing in the tree references them). Deliberately
  conservative: recall is traded for precision, matching the repo's existing bias
  of false negatives over false positives.

- **`find_structure_issues.py`** — consumes the map. Import cycles, god modules
  (high fan-in *and* fan-out), and low directory cohesion (a directory whose files
  never reference each other is not a module). Each emitted as a finding with a
  fix, per the invariant above.

### Duplication

- **`find_duplication.py`** — DRY. Strips comments, blanks string and number
  literals, normalizes whitespace, then shingles N-line windows. Reports **exact**
  duplicates and **near-duplicates via Jaccard similarity over shingle sets**.
  Near-duplicate detection is the point: copy-paste-with-mutation is what actually
  rots a codebase, and exact matching misses all of it.

### Complexity

- **`analyze_complexity.py`** — decision density over a near-universal branch-token
  set (`if`/`else`/`for`/`while`/`case`/`catch`/`&&`/`||`/`?`), nesting depth by
  indentation, file and function length via declaration-shaped segmentation, and an
  arity proxy from commas between parens on declaration lines. The arity and
  function-boundary findings are marked low-confidence; the nesting and length ones
  are not.

### Correctness and hygiene

- **`find_hygiene_issues.py`** — merge conflict markers, TODO/FIXME/HACK/XXX
  inventory with git-blame age, commented-out code, oversized files and lines,
  committed `.env` files and large binaries, debug-print leftovers.
- **`find_secrets.py`** — key material (private key blocks, cloud credentials,
  JWTs) and high-entropy values assigned to names containing key/token/secret/
  password.

### Tests

- **`find_test_gaps.py`** — test-to-source mapping by the union of universal
  filename conventions, assertion density, test files that assert nothing, and
  test:source ratio per directory.

### The repo's own toolchain

- **`find_project_checks.py`** — inventory what the repo already defines:
  `Makefile` targets, `justfile` recipes, `package.json` scripts,
  `.pre-commit-config.yaml`, `tox.ini`, CI workflow steps.
- **`run_project_checks.py`** — run the check-shaped ones in non-mutating mode and
  normalize their output into this skill's findings shape. Never installs
  anything; never runs a target it cannot identify as a check.

### Orchestration and output

- **`analyze_all.py`** — unified report, `--skip cat1,cat2`.
- **`analyze_diff.py`** — the diff lens, file-level detectors against changed files
  only. Git-based, so it is already language-agnostic.
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
