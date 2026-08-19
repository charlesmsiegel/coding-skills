---
name: typescript-code-doctor
description: Critically review and simplify TypeScript — aggressively. Use whenever the user wants to simplify, refactor, clean up, or reduce complexity in .ts/.tsx code; find code smells, duplication, dead code, over-engineering, naming problems, encapsulation leaks, promise bugs, mutation hazards, resource leaks, security risks, import cycles, weak tests, or dated idioms; tighten the type system (any / as / non-null / @ts-ignore / tsconfig strictness); judge whether an abstraction earns its keep; build a characterization-test safety net before refactoring; review AI-generated or vibe-coded TypeScript for stubs, ignored options, swallowed errors, and tests that cannot fail; or clean up a working-but-messy repo from cold. Triggers on "simplify this", "refactor this", "clean this up", "review my code", "is this over-engineered", "analyze this codebase" — even when the user just pastes TypeScript and asks "what do you think?". Combines deterministic detectors (run them) with judgment guides in references/ (load them).
---

# TypeScript Code Doctor

A critical-reviewer skill. Its job is to make TypeScript **simpler, more
consistent, and more correct** — by deleting what isn't needed, flattening what's
tangled, fixing real bugs, and converging the codebase on one good way of doing
each thing.

## Reviewer mindset (read this first)

Approach the code as **too complex until proven otherwise.** The default question
is not "is this OK?" but **"why isn't this simpler?"** Be specific and unsparing.
Two hard limits keep the criticism honest:

1. **Behavior is sacred.** Never change what the code does. If it isn't tested,
   write a characterization test that pins current behavior *before* refactoring.
2. **Simpler, not cleverer.** Aim for code a tired developer reads at a glance —
   not a showcase of conditional types. A clever line that needs a comment has
   failed, and a type that needs a comment has failed twice.

Bias toward: **deleting** code, **flattening** structure, the **platform** over
hand-rolled machinery, **one canonical pattern** applied everywhere, and small
behavior-preserving steps. The burden of proof is on complexity, not its removal.

**The TypeScript-specific bias:** prefer making the compiler responsible. A
narrowed type, an exhaustive `switch`, a `readonly` field, or one strictness flag
removes a class of bug permanently — where a fix removes one instance.

## How the skill works: two pronged

- **Deterministic scripts** find what can be found mechanically (a real TS/TSX
  tokenizer, low false-positive). **Run them first** and triage their output
  before reviewing by hand.
- **Judgment guides** in `references/` cover what needs a reading brain — whether
  an abstraction earns its keep, whether duplication is real, whether a cast is
  the honest answer at a boundary. **Load the relevant guide** when doing that
  review.

## Running the scripts

Let `SKILL=/path/to/this/skill` — the directory holding this SKILL.md. The
commands below run from the project being reviewed, so they need that prefix.

This skill is **self-contained**: everything it needs is in its own directory,
and it depends on no other skill. The detectors ship their own TypeScript scanner
and never invoke `node`, so they run against a checkout with no `node_modules`,
no install step and no build — which is what makes them usable on a repo you have
just cloned or on a project whose build is currently broken. The only requirement
is a **Python 3.11+ interpreter** to launch them with; nothing needs installing.

`run_external_tools.py` additionally drives `tsc`, ESLint, Biome, Prettier, madge,
knip, `npm audit` and coverage *when the project already has them*; it reports the
ones that are missing rather than failing, and no detector depends on it.

**What the scanner cannot do:** it reads syntax, not types. Questions that need
the type checker — is this promise handled, is this condition always true, is this
cast unnecessary, is `x` really an `Error` — are `tsc`'s and typescript-eslint's,
and `run_external_tools.py` is how you get them. A file that will not tokenize is
named on stderr rather than silently reported clean.

## Workflow

**Cleaning up a whole poorly-written repo from cold?** The steps below assume the
code compiles, has some tests, and is roughly formatted. When it doesn't, follow
`references/messy-repo-runbook.md` first — get `tsc --noEmit` passing, **build a
test safety net before touching anything** (`references/safety-net-and-testing.md`),
normalize formatting in one behavior-free commit, *then* return here to triage.

1. **Read tsconfig first.** `python "$SKILL/scripts/find_tsconfig_issues.py" .`
   Strictness decides how much of everything else the compiler was ever going to
   catch. A repo with `strict: false` has a different set of real problems than
   its detector output suggests.
2. **Run the analyzer.** `python "$SKILL/scripts/analyze_all.py" <path>` (add
   `--format json` for tooling). Triage deterministic findings first — don't spend
   judgment on what a tool already caught.
3. **Run the project's own tools.** `python "$SKILL/scripts/run_external_tools.py" .`
   `tsc` and typescript-eslint see types and these detectors do not; their findings
   outrank overlapping ones here.
4. **Find the hot files.** Effort follows *change frequency*, not line count:
   ```bash
   git log --since="1 year ago" --name-only --pretty=format: \
     | grep -E '\.(ts|tsx)$' | sort | uniq -c | sort -rn | head -30
   ```
   High-churn × high-complexity = top priority. **Don't refactor cold code.**
5. **Review the hot files with the judgment guides open** (see the reference index).
6. **Produce a findings artifact.** One smell → one entry → one small PR:
   `python "$SKILL/scripts/analyze_all.py" . --format json | python "$SKILL/scripts/format_findings.py"`.
   This is the deliverable — see *Output & ticketing*; never create tickets in a
   tracker without asking.
7. **Ratchet.** When a whole class of problem is cleared, turn on the check that
   keeps it gone: a tsconfig flag, an ESLint rule, or one of these scripts in CI.

## Output & ticketing

The deliverable is always an **artifact, never a side effect.** Produce one of: a
findings **list** (markdown table), detailed **cards**, a **JSON** array, or the
full report from `analyze_all.py` — saved as a file in the workspace or returned
inline. `scripts/format_findings.py` renders any detector's JSON into these shapes.

This skill does **not** create tickets in any system on its own. When the user
wants findings filed as real tickets, **ask which ticket software or MCP to use**
(e.g. Jira, Linear, GitHub Issues, Asana, or a connected MCP) and create them
through that tool — never assume or fabricate a tracker.

## Deterministic scripts

`analyze_all.py` parses each file once and asks every detector about that one
tree, across as many processes as there are cores — the parser here is a
hand-written scanner, so re-parsing per detector was most of the runtime on a
large repository. `--jobs N` sets the worker count; `--jobs 1` runs everything in
one process, which is what to use when a crash needs a clean traceback or a
large tree will not fit in memory at N workers.

`tsproject.load_project` returns the same `Project` for a repeated root, which
is how the whole-tree detectors avoid parsing the tree once each. **Treat it as
read-only** — a detector that rewrote it would change what every later detector
sees.

```bash
python "$SKILL/scripts/analyze_all.py" /path           # Run everything, unified report
python "$SKILL/scripts/analyze_all.py" . --format json > report.json
python "$SKILL/scripts/analyze_all.py" . --jobs 1      # one process, for a clean traceback

# The type system (start here — this is what makes it TypeScript)
python "$SKILL/scripts/find_tsconfig_issues.py" .      # strict, noUncheckedIndexedAccess, suppressions
python "$SKILL/scripts/find_type_gaps.py" .            # any, as, as unknown as, !, @ts-ignore, Function/{}, all-optional types

# Correctness bugs (these find real bugs, not style)
python "$SKILL/scripts/find_async_issues.py" .         # floating promises, forEach(async), await-in-loop, swallowed rejections
python "$SKILL/scripts/find_mutation_hazards.py" .     # mutated arguments/imports, mutation during iteration, mutable exported constants
python "$SKILL/scripts/find_exception_issues.py" .     # empty catch, swallowed errors, thrown non-Errors, return-in-finally
python "$SKILL/scripts/find_resource_leaks.py" .       # timers/listeners/subscriptions without cleanup, effects without teardown
python "$SKILL/scripts/find_security_issues.py" .      # eval, innerHTML, shell interpolation, weak hashes, hardcoded secrets

# Structure, design and encapsulation
python "$SKILL/scripts/analyze_complexity.py" .        # cyclomatic/cognitive complexity, nesting, size, arity
python "$SKILL/scripts/find_encapsulation_issues.py" . # public mutable fields, exported let, leaked internals, message chains
python "$SKILL/scripts/find_design_smells.py" .        # type switches, flag parameters, data clumps, temporary fields, refused bequest
python "$SKILL/scripts/find_coupling_issues.py" .      # feature envy, low cohesion (LCOM), middle man
python "$SKILL/scripts/find_overengineering.py" .      # single-impl interfaces, all-static classes, pass-through modules (YAGNI)
python "$SKILL/scripts/find_code_smells.py" .          # ==, var, nested ternaries, switch without default, god class

# Architecture & repo structure (cross-file)
python "$SKILL/scripts/find_module_issues.py" .        # import cycles, barrel files, god modules, deep relative paths
python "$SKILL/scripts/find_dependency_issues.py" .    # package.json vs imports: missing, unused, misplaced, unpinned
python "$SKILL/scripts/find_dead_code.py" .            # unused imports/exports, unreachable code, dead private members
python "$SKILL/scripts/find_duplicates.py" .           # duplicated blocks, identical type shapes, repeated literals

# Safety net (build this BEFORE refactoring — see references/safety-net-and-testing.md)
python "$SKILL/scripts/find_untested_modules.py" .     # source modules no test imports; "no tests in repo" alarm
python "$SKILL/scripts/find_test_smells.py" .          # assertion-less tests, .only/.skip, unawaited rejects, over-mocking

# Simplification & hygiene
python "$SKILL/scripts/find_loop_simplifications.py" . # index loops, for..in over arrays, push-loops, filter().length
python "$SKILL/scripts/find_outdated_idioms.py" .      # require/module.exports, namespace, <T>casts, arguments, x && x.y, || defaults
python "$SKILL/scripts/find_naming_issues.py" .        # casing, I-prefix, shadowed browser globals, boolean naming
python "$SKILL/scripts/find_ai_scaffolding.py" .       # stubs, ignored options parameters, duplicate definitions, merge markers
python "$SKILL/scripts/find_debug_leftovers.py" .      # debugger, console noise, blanket eslint-disable
python "$SKILL/scripts/find_comment_smells.py" .       # commented-out code, TODO inventory, JSDoc that restates types

# Format findings as a portable artifact (does NOT create tickets)
python "$SKILL/scripts/format_findings.py" report.json                       # markdown list
<any detector> --format json | python "$SKILL/scripts/format_findings.py" --format cards
<any detector> --format json | python "$SKILL/scripts/format_findings.py" --format json --min-severity high
```

All detectors share one interface: `--format text|json`, `--ignore type1,type2`,
and 🔴/🟡/🟢 severities. JSON output is a flat list of findings; `analyze_all.py`
aggregates them and can drop whole categories with `--skip cat1,cat2`
(`--skip-duplicates` is shorthand for the slowest one). They are deliberately
conservative (false negatives over false positives) so the output stays
trustworthy, and several apply a lighter standard inside test files — a cast that
installs a mock is not a claim about the product's types.

## Use the project's own tools when they exist

The detectors read syntax. The compiler reads types, and **that is where the
expensive bugs are**. `run_external_tools.py` resolves each tool from the
project's own `node_modules/.bin` (then PATH), runs every available one in
non-mutating check mode, and merges the output into this skill's findings shape:

```bash
python "$SKILL/scripts/run_external_tools.py" .                 # run all available (check only)
python "$SKILL/scripts/run_external_tools.py" . --format json   # {tools_run, missing_tools, findings}
python "$SKILL/scripts/run_external_tools.py" . --tools tsc,eslint
python "$SKILL/scripts/run_external_tools.py" . --fix           # also run prettier/eslint --fix (MUTATES)
python "$SKILL/scripts/run_external_tools.py" . --run-coverage  # run the test suite first (SLOW, EXECUTES CODE)
```

Four of these answer questions the detectors structurally **cannot**:

- **tsc** — the types themselves. `find_type_gaps.py` finds where checking was
  switched *off*; only the compiler finds what it would have caught. Run
  `tsc --noEmit` before believing any refactor preserved behavior.
- **typescript-eslint (type-aware rules)** — `no-floating-promises`,
  `no-misused-promises`, `no-unnecessary-condition`, `no-unsafe-*`. The async
  detector here catches the syntactic cases; these catch the rest, including
  the one it deliberately skips (an async callback handed to something that
  discards its promise, such as a React `onClick`).
- **npm/pnpm/yarn audit** — known advisories. `find_dependency_issues.py` can say
  a range is unpinned; only an advisory database can say the version you have is
  vulnerable.
- **coverage** — what actually executed. `find_untested_modules.py` answers "does
  a test import this"; coverage answers "does this code ever run".

**When a tool is missing, ask before installing.** The script never installs
anything and never runs `npx` in download mode; it lists each absent tool with an
`npm install --save-dev` hint under `missing_tools`. When that list is non-empty
and the tools would help, **ask the user whether to install them** (e.g. via the
AskUserQuestion tool) and only install on confirmation.

## Reviewing a change request (diff lens)

For an AI-written feature or PR, review *what changed*, not the legacy around it.
`analyze_diff.py` runs the file-level detectors against only the changed files,
and by default only the added/modified lines:

```bash
python "$SKILL/scripts/analyze_diff.py"                 # working tree vs. merge-base with the default branch
python "$SKILL/scripts/analyze_diff.py" origin/main     # vs. an explicit base ref
python "$SKILL/scripts/analyze_diff.py" --format json | python "$SKILL/scripts/format_findings.py"
```

Whole-tree detectors (tsconfig, import cycles, dependency hygiene, dead code,
duplication, over-engineering, untested modules) need the full project — run those
with `analyze_all.py` separately. See `references/ai-generated-code.md` for the
review stance.

## Reference index (load on demand)

Keep `SKILL.md` lean; pull in depth only when a review needs it.

| Load this when… | File |
|---|---|
| Reading a file critically — the per-function checklist and the finding-triage rubric | `references/critical-review-guide.md` |
| Deciding what a type should be: `any` vs `unknown`, when a cast is honest, discriminated unions, `satisfies`, generics, branded types | `references/type-system.md` |
| Turning strictness on in a repo that has been running without it, without a six-month branch | `references/strictness-migration.md` |
| Judging async design — floating promises, cancellation, sequential vs parallel, races, error propagation | `references/async-and-concurrency.md` |
| Deciding whether an abstraction should exist; DRY vs the wrong abstraction; YAGNI in a structurally-typed language | `references/overengineering-and-abstraction.md` |
| Diagnosing design smells — the classic catalog in its TypeScript spellings, and triaging detector candidates | `references/refactoring-catalog.md` |
| Executing a fix — named refactoring techniques and their safe step-by-step mechanics | `references/refactoring-techniques.md` |
| Deciding what a module should expose: ESM/CJS, barrels, cycles, `import type`, package boundaries | `references/modules-and-dependencies.md` |
| Deciding who may change what — `private` vs `#`, `readonly`, immutability at the boundary | `references/encapsulation-and-boundaries.md` |
| Judging names, comments and function shape; deleting comments that lie | `references/naming-comments-readability.md` |
| Choosing the right pattern AND making the codebase use it consistently; the TypeScript-native form of each GoF pattern | `references/patterns-and-consistency.md` |
| You want concrete before/after idiom swaps | `references/typescript-idioms.md` |
| Cleaning up a whole poorly-written-but-working repo from cold — the phased campaign | `references/messy-repo-runbook.md` |
| Building the test safety net before refactoring — characterization tests, spotting hollow tests | `references/safety-net-and-testing.md` |
| Reviewing AI-generated or vibe-coded TypeScript — hallucinated APIs, plausible-but-wrong types, scaffolding, tests that can't fail | `references/ai-generated-code.md` |

## Over-engineering anti-patterns (quick reference)

| Pattern | Problem | Fix |
|---|---|---|
| Interface with one implementation | Abstraction over one thing | Use the class/type directly — structural typing means a test double needs no interface |
| Abstract class with one subclass | A class split across two files | Collapse the hierarchy |
| All-static class | A namespace with a `class` keyword | Export the functions |
| `*Service`/`*Manager` with one method, no state | A function wearing a costume | `export function doTheThing(…)` |
| Factory that builds one type | Ceremony around `new` | Direct construction |
| Pass-through module / middle man | Only forwards calls | Import the real thing |
| Single-use type parameter `<T>` | `any` with extra syntax | Use the concrete type, or `unknown` |
| `namespace` | Pre-ESM module system | One file, named exports |
| Enum where a union would do | Runtime object for a compile-time set | `as const` object + `typeof x[keyof typeof x]` |
| Deep inheritance (3+ levels) | Behaviour scattered across ancestors | Composition |
| Speculative options bag | Code for "future needs" | Delete it (YAGNI) |

## Code smells (quick reference → fix)

| Smell | Fix |
|---|---|
| `any`, `as any`, `as unknown as T` | `unknown` + narrowing; validate at the boundary and return the real type |
| `!` non-null assertion | Handle absence (`?.`, guard, default), or make the type honest |
| `@ts-ignore` | `@ts-expect-error` with a reason — it fails once the underlying error is fixed |
| `strict: false` in tsconfig | Turn it on; migrate file by file (see the strictness guide) |
| `Function`, `Object`, `{}` as types | The real signature / `Record<string, unknown>` / a named interface |
| Interface where everything is optional | Split into the states that actually occur (discriminated union) |
| Floating promise | `await`, `return`, or `.catch` it |
| `arr.forEach(async …)` | `for…of` + `await`, or `await Promise.all(arr.map(…))` |
| `arr.filter(async …)` | The predicate gets a `Promise`, always truthy — resolve first |
| `new Promise(async …)` | Drop the wrapper; return the async function's promise |
| `await` inside a loop over independent items | `Promise.all` |
| `catch {}` / `catch { console.log(e) }` | Handle, re-throw with `{ cause }`, or return a typed failure |
| `throw 'string'` | `throw new Error(...)` |
| `return` inside `finally` | Move it out — it discards the in-flight exception |
| Mutating an argument or an imported object | Return a new value; `{ ...input, field }` |
| `export let` | Export a getter — importers otherwise see a binding that changes under them |
| Public mutable class field | `private` / `readonly` |
| Getter+setter that only forward | A public field |
| Method returning the private array itself | Return a copy or `ReadonlyArray` |
| `setInterval` / `addEventListener` with no teardown | Pair every acquire with a release; effects return a cleanup |
| `eval`, `new Function`, `innerHTML`, `exec` with a template string | The concrete operation; `textContent`; `execFile` with an argument array |
| `==` / `!=` (except `== null`) | `===` / `!==` |
| `var` | `const`, or `let` when genuinely reassigned |
| `x && x.y` / `x \|\| 0` | `x?.y` / `x ?? 0` |
| `require()` / `module.exports` / `namespace` | ESM `import` / `export` |
| `<T>value` cast | `value as T` |
| `if/else if` ladder on `typeof x` or `x.kind` | Discriminated union + exhaustive `switch` with `assertNever` |
| Boolean flag parameter | Split the function, or take a named option |
| 3+ adjacent `string` parameters | Options object, or branded types |
| Index loop over an array | `for…of`, `map`, `filter` |
| `.filter(…).length > 0` / `.filter(…)[0]` | `.some(…)` / `.find(…)` |
| Barrel `index.ts` re-exporting a folder | Import from the defining module |
| Import cycle | Move the shared piece out, or invert one dependency |
| Package imported but not in package.json | Declare it — it resolves today only by accident |
| Test with no assertion; `it.only` | Assert the value; remove the `.only` before committing |
| `expect(...).rejects` without `await` | The assertion never runs — `await` it |
| `throw new Error('Not implemented')` stub | Finish it or delete it |
| Options parameter the body never reads | Every caller's setting is silently ignored — use it or remove it |
| Commented-out code | Delete it (git remembers) |

## When NOT to simplify

- Untested code — write a characterization test first, *then* refactor.
- Hot paths — measure before trading clarity for speed.
- Code being replaced or retired soon.
- Complexity genuinely forced by an external API, a framework's contract, or a
  real present requirement. A cast at a boundary you do not control, with a
  runtime validation beside it, is the right answer — not a smell.
- Cold code that never changes and blocks nothing — leave it; fix what churns.

## Relationship to ESLint and tsc

These scripts complement the ecosystem's tools; they do not replace them. Some
detectors overlap rule sets: `eqeqeq`, `no-var`, `prefer-const`,
`@typescript-eslint/no-explicit-any`, `no-non-null-assertion`, `ban-ts-comment`,
`no-floating-promises`, `prefer-optional-chain`, `prefer-nullish-coalescing`,
`prefer-includes`, `no-namespace`, `no-extraneous-class`, `no-debugger`,
`no-console`. If the project already runs those rules, disable the matching
detector via `--ignore` (or drop its category with `analyze_all.py --skip`) to
avoid double-reporting — or better, run the real tools with
`run_external_tools.py` and lean on the detectors only for what the tools do not
cover.

The unique value here is the **design and architecture detectors** (encapsulation
leaks, cohesion, feature envy, data clumps, temporary fields, over-engineering,
import cycles, duplication, type-shape duplication), the **repo-level checks**
(tsconfig strictness audit, dependency reconciliation, untested-module and
test-smell detection that scaffold a safety net), the **AI-code tells**
(scaffolding, ignored options, duplicate definitions), the fact that it **runs
with no install and no node_modules** — plus the **judgment guides**, which no
linter provides.
