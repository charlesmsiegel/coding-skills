# Reading TypeScript critically

The detectors have already found what a machine can find. This is the pass that
needs a reading brain.

## Before opening a file

Establish three things, or the review is guesswork:

1. **Is `strict` on?** With it off, every type silently includes `null` and
   `undefined`, and half of what looks like careful code is decorative.
   `find_tsconfig_issues.py` answers this first for a reason.
2. **Does `tsc --noEmit` pass?** If not, everything below is provisional.
3. **What churns?** `git log --since="1 year ago" --name-only --pretty=format: |
   grep -E '\.tsx?$' | sort | uniq -c | sort -rn | head -20`. Cold code that
   blocks nothing is not worth your attention no matter how ugly it is.

## Per-function questions

Ask in this order. The first three find bugs; the rest find cost.

1. **What is the contract?** Read only the signature. Can you say what it does,
   what it needs, and what it returns? If the return type is inferred and the body
   is long, nobody has written the contract down.
2. **What happens on the failure path?** Every `await`, every parse, every index.
   Does a failure become an exception, a typed result, or a wrong value?
3. **What can be `undefined` that the code assumes is not?** Especially array
   indexing, `find`, `Map.get`, optional props, and anything that crossed a
   boundary.
4. **Who can change this state?** A public mutable field, an exported `let`, or a
   returned internal array means the answer is "anyone".
5. **Is the type doing the work, or is the code?** An `if` chain checking a string
   tag is a discriminated union that was never declared. Repeated `x!` is an
   invariant the type does not express.
6. **How many decisions?** Cyclomatic complexity is a proxy for how many tests it
   needs. If you cannot enumerate the branches, neither can the test suite.
7. **Could the name replace a comment?** If the comment explains *what*, the name
   is wrong. If it explains *why*, keep it.
8. **What would deleting this break?** Ask it of every abstraction, every wrapper,
   every option. If the answer is "nothing", that is the finding.

## Per-module questions

- **What does it export, and who imports each one?** An export nobody imports is
  a promise being kept for nobody.
- **Does it have one reason to change?** Two groups of exports that never
  reference each other are two modules.
- **Is it in a cycle?** Cycles compile and fail at runtime, non-deterministically.
- **Does importing it run anything?** Side-effectful module bodies execute in an
  order the bundler chooses.

## Triaging detector output

Not every finding deserves a change. Sort by this, not by count:

| Priority | What | Why |
|---|---|---|
| 1 | Correctness: floating promises, swallowed errors, mutation of shared state, resource leaks, unreachable code | These are defects. They fail in production, not in review. |
| 2 | Security leads | Cheap to verify, expensive to miss. |
| 3 | Type-safety holes on the **public surface** of hot modules | `any` at a boundary poisons everything downstream. |
| 4 | Structural: cohesion, cycles, god modules, duplication in code that churns | These set the cost of every future change. |
| 5 | Idioms, naming, formatting | Real, but only worth a dedicated pass when a class of them can be cleared at once. |

**Verify before believing.** These are heuristics over syntax, not type-checked
facts. The findings with real false-positive modes:

- `floating_promise` — a same-named local shadowing an async function; a promise
  handled by a wrapper the scanner cannot see.
- `unused_export` — reached dynamically, by a build tool, or by a consumer outside
  the scanned tree.
- `duplicate_block` — the same *shape* is not the same *decision*.
- `unreleased_resource` — released in a different file, or intentionally
  process-lifetime.
- `single_implementation_interface` — a second implementation may live in a
  package this scan did not include.

When you report a finding you have not verified, say so.

## Writing the finding

Each one needs four things, and none of them is a lecture:

1. **Location** — `file.ts:120`.
2. **What is wrong**, in one sentence, in terms of consequence.
3. **The concrete fix**, ideally as a diff.
4. **Why it is worth doing** — the input that breaks it, the bug it caused, or
   the change it will make expensive.

If you cannot write (4), it is a preference, not a finding. Say that too — a
review that separates "this is a bug" from "I would have done this differently"
gets acted on; one that mixes them gets skimmed.

## What not to raise

- Formatting a formatter owns. Run Prettier or Biome; do not review whitespace.
- Style the project has consciously chosen and applied consistently.
- Complexity forced by an external API, a framework contract, or a real
  requirement — a cast at a boundary you do not control, with validation beside
  it, is correct.
- Anything in code being deleted next sprint.
