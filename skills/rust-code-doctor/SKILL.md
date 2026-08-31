---
name: rust-code-doctor
description: 'Critically review and simplify Rust — aggressively. Use whenever the user wants to check, compile-check, simplify, refactor, clean up or reduce complexity in .rs code; find compilation errors, borrow problems, code smells, dead code, over-engineering, naming problems or unidiomatic Rust; fix error handling (unwrap/expect/panic/swallowed errors, hand-rolled `?`); audit unsafe, lossy `as` casts, async deadlocks (a guard held across `.await`, blocking calls on the executor), needless clones and `&String`/`&Vec` parameters; audit Cargo.toml, editions and dependencies; find files no `mod` reaches (never compiled); judge whether a trait with one implementor earns its keep; build a characterization-test safety net; or review AI-generated Rust for `todo!()` stubs and tests that cannot fail. Triggers on "simplify this", "review my Rust", "is this idiomatic", "does this compile", "analyze this crate" — even on a bare paste of Rust. Deterministic detectors (run them) plus judgment guides in references/ (load them).'
---

# Rust Code Doctor

A critical-reviewer skill. Its job is to make Rust **simpler, more correct, and
more idiomatic** — by deleting what isn't needed, flattening what's tangled,
fixing the bugs that compile, and converging the crate on one good way of doing
each thing.

## Reviewer mindset (read this first)

Approach the code as **too complex until proven otherwise.** The default
question is not "is this OK?" but **"why isn't this simpler?"** Be specific and
unsparing. Two hard limits keep the criticism honest:

1. **Behavior is sacred.** Never change what the code does. If it isn't tested,
   write a characterization test that pins current behavior *before*
   refactoring. Rust makes this easy — `#[cfg(test)] mod tests` with
   `use super::*` reaches private items, so nothing has to be made `pub` for a
   test.
2. **Simpler, not cleverer.** Aim for code a tired developer reads at a glance —
   not a showcase of trait bounds. A clever line that needs a comment has
   failed, and a type that needs a comment has failed twice.

Bias toward: **deleting** code, **flattening** structure, the **standard
library** over hand-rolled machinery, **one canonical pattern** applied
everywhere, and small behavior-preserving steps. The burden of proof is on
complexity, not its removal.

**The Rust-specific bias:** prefer making the compiler responsible. A newtype,
an enum that replaces a `bool` pair, an exhaustive `match` with no `_` arm, a
`#[non_exhaustive]`, or one `[lints]` entry removes a *class* of bug
permanently — where a fix removes one instance. When a review finds three
instances of the same mistake, the deliverable is the rule that makes the fourth
impossible.

## How the skill works: two pronged

- **Deterministic scripts** find what can be found mechanically (a real Rust
  tokenizer, low false-positive). **Run them first** and triage their output
  before reviewing by hand.
- **Judgment guides** in `references/` cover what needs a reading brain —
  whether an abstraction earns its keep, whether a `.clone()` is the honest
  answer, whether a `match` was clearer than the combinator. **Load the relevant
  guide** when doing that review.

## Running the scripts

Let `SKILL=/path/to/this/skill` — the directory holding this SKILL.md. The
commands below run from the project being reviewed, so they need that prefix.

This skill is **self-contained**: everything it needs is in its own directory,
and it depends on no other skill. The detectors ship their own Rust scanner and
never invoke `cargo`, so they run against a checkout with no `~/.cargo`
registry, no network, no `target/` and no successful build — which is what makes
them usable on a repo you have just cloned or on a crate whose build is
currently broken. The only requirement is a **Python 3.11+ interpreter** to
launch them with; nothing needs installing.

`run_external_tools.py` additionally drives `cargo check`, `clippy`, `fmt`,
`test`, `audit`, `deny`, `udeps`, `tree` and coverage *when the machine has
them*; it reports the ones that are missing rather than failing, and no detector
depends on it.

**What the scanner cannot do:** it reads syntax, not types. Every question that
needs the type checker — does this compile, does this borrow outlive its owner,
is this `match` exhaustive, is this future `Send`, is this clone necessary — is
`cargo check`'s and clippy's, and `run_external_tools.py` is how you get them.
**Compilation errors come from `cargo check`, not from these detectors.** A file
that will not tokenize is named on stderr rather than silently reported clean.

## Workflow

**Cleaning up a whole poorly-written repo from cold?** The steps below assume
the code compiles, has some tests, and is roughly formatted. When it doesn't,
follow `references/messy-repo-runbook.md` first — get `cargo check
--all-targets` passing, **build a test safety net before touching anything**
(`references/safety-net-and-testing.md`), normalize formatting in one
behavior-free commit, *then* return here to triage.

1. **Read the manifest first.** `python "$SKILL/scripts/find_cargo_issues.py" .`
   The edition decides how much of everything else the compiler was ever going
   to catch, and whether a `[lints]` table exists decides whether a cleared
   class of finding stays cleared. A crate on the 2015 edition has a different
   set of real problems than its detector output suggests.
2. **Run the compiler.** `python "$SKILL/scripts/run_external_tools.py" .`
   `cargo check` and clippy see types and these detectors do not; their findings
   outrank overlapping ones here, and a borrow error nobody has seen yet is
   worth more than every style finding combined.
3. **Run the analyzer.** `python "$SKILL/scripts/analyze_all.py" <path>` (add
   `--format json` for tooling). Triage deterministic findings first — don't
   spend judgment on what a tool already caught.
4. **Check what rustc never compiled.**
   `python "$SKILL/scripts/find_module_issues.py" .` A `.rs` file under `src/`
   that no `mod` declaration reaches is not dead code — it is not code at all.
   It never type-checks, its tests never run, and people find out after fixing a
   bug in it.
5. **Find the hot files.** Effort follows *change frequency*, not line count:
   ```bash
   git log --since="1 year ago" --name-only --pretty=format: \
     | grep -E '\.rs$' | sort | uniq -c | sort -rn | head -30
   ```
   High-churn × high-complexity = top priority. **Don't refactor cold code.**
6. **Review the hot files with the judgment guides open** (see the reference
   index).
7. **Produce a findings artifact.** One smell → one entry → one small PR:
   `python "$SKILL/scripts/analyze_all.py" . --format json | python "$SKILL/scripts/format_findings.py"`.
   This is the deliverable — see *Output & ticketing*; never create tickets in a
   tracker without asking.
8. **Ratchet.** When a whole class of problem is cleared, turn on the check that
   keeps it gone: a `[lints]` entry in Cargo.toml, a clippy level, or one of
   these scripts in CI.

## Output & ticketing

The deliverable is always an **artifact, never a side effect.** Produce one of:
a findings **list** (markdown table), detailed **cards**, a **JSON** array, or
the full report from `analyze_all.py` — saved as a file in the workspace or
returned inline. `scripts/format_findings.py` renders any detector's JSON into
these shapes.

This skill does **not** create tickets in any system on its own. When the user
wants findings filed as real tickets, **ask which ticket software or MCP to use**
(e.g. Jira, Linear, GitHub Issues, Asana, or a connected MCP) and create them
through that tool — never assume or fabricate a tracker.

## Deterministic scripts

`analyze_all.py` parses each file once and asks every detector about that one
tree, across as many processes as there are cores — the parser here is a
hand-written scanner, so re-parsing per detector was most of the runtime on a
large crate. `--jobs N` sets the worker count; `--jobs 1` runs everything in one
process, which is what to use when a crash needs a clean traceback or a large
tree will not fit in memory at N workers.

`rsproject.load_project` returns the same `Project` for a repeated root, which
is how the whole-tree detectors avoid parsing the tree once each. **Treat it as
read-only** — a detector that rewrote it would change what every later detector
sees.

```bash
python "$SKILL/scripts/analyze_all.py" /path           # Run everything, unified report
python "$SKILL/scripts/analyze_all.py" . --format json > report.json
python "$SKILL/scripts/analyze_all.py" . --jobs 1      # one process, for a clean traceback

# The manifest (start here — it calibrates everything else)
python "$SKILL/scripts/find_cargo_issues.py" .         # edition, [lints], wildcard/git deps, declared-vs-imported

# Correctness bugs (these find real bugs, not style)
python "$SKILL/scripts/find_error_handling.py" .       # unwrap in fallible fns, swallowed errors, panic in a library, hand-rolled `?`
python "$SKILL/scripts/find_concurrency_issues.py" .   # guard across .await, blocking calls in async, dropped JoinHandles
python "$SKILL/scripts/find_unsafe_issues.py" .        # unsafe with no SAFETY comment, static mut, transmute, _unchecked
python "$SKILL/scripts/find_type_issues.py" .          # narrowing/sign-changing `as` casts, all-Option structs, Option<Option>
python "$SKILL/scripts/find_security_issues.py" .      # shell strings, SQL interpolation, disabled TLS checks, weak hashes, secrets

# Structure, design and ownership
python "$SKILL/scripts/analyze_complexity.py" .        # cyclomatic/cognitive complexity, nesting, size, arity
python "$SKILL/scripts/find_ownership_issues.py" .     # clones in loops, &String/&Vec params, Rc<RefCell<..>>, owned-but-only-read
python "$SKILL/scripts/find_design_smells.py" .        # flag params, data clumps, god impls, feature envy, refused bequest
python "$SKILL/scripts/find_api_hygiene.py" .          # missing Debug, new() without Default, #[must_use], docs, #[non_exhaustive]
python "$SKILL/scripts/find_code_smells.py" .          # blanket #[allow], magic numbers, `if` ladders, `.len() - 1`, wildcard arms

# Architecture & repo structure (cross-file)
python "$SKILL/scripts/find_module_issues.py" .        # files no `mod` reaches, missing module files, globs, god modules
python "$SKILL/scripts/find_dead_code.py" .            # unreachable code, unreferenced private items
python "$SKILL/scripts/find_overengineering.py" .      # traits with one implementor, fieldless "namespace" structs, pass-through newtypes
python "$SKILL/scripts/find_duplicates.py" .           # duplicated blocks, repeated literals, identical struct shapes

# Safety net (build this BEFORE refactoring — see references/safety-net-and-testing.md)
python "$SKILL/scripts/find_untested_modules.py" .     # modules with no tests; "no tests in crate" alarm
python "$SKILL/scripts/find_test_smells.py" .          # assertion-less tests, bare #[should_panic], #[ignore], sleeps

# Simplification & hygiene
python "$SKILL/scripts/find_unrustic.py" .             # match that is unwrap_or, is_some+unwrap, .len()==0, eager fallbacks
python "$SKILL/scripts/find_loop_simplifications.py" . # index loops, push loops, manual sum/find/flatten
python "$SKILL/scripts/find_outdated_idioms.py" .      # extern crate, try!, Box<Trait> without dyn, lazy_static, uninlined format args
python "$SKILL/scripts/find_naming_issues.py" .        # casing, get_ prefix, as_/to_/into_ conventions, boolean names
python "$SKILL/scripts/find_ai_scaffolding.py" .       # todo!() stubs, ignored parameters, placeholder prose, merge markers
python "$SKILL/scripts/find_debug_leftovers.py" .      # dbg!, println! in a library, crate-level #![allow(warnings)]
python "$SKILL/scripts/find_comment_smells.py" .       # commented-out code, TODO inventory, docs that restate the signature

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
trustworthy, and several apply a lighter standard inside test code — an
`unwrap()` in a `#[test]` is an assertion, not a landmine.

## Use the toolchain — that is where compilation errors live

The detectors read syntax. The compiler reads types, and **that is where the
expensive bugs are.** `run_external_tools.py` resolves each tool from the
machine's `cargo`, runs every available one in non-mutating check mode, and
merges the output into this skill's findings shape:

```bash
python "$SKILL/scripts/run_external_tools.py" .                 # run all available (check only)
python "$SKILL/scripts/run_external_tools.py" . --format json   # {tools_run, missing_tools, findings}
python "$SKILL/scripts/run_external_tools.py" . --tools check,clippy
python "$SKILL/scripts/run_external_tools.py" . --fix           # also run cargo fmt / clippy --fix (MUTATES)
python "$SKILL/scripts/run_external_tools.py" . --run-tests     # run the suite under coverage (SLOW, EXECUTES CODE)
```

Four of these answer questions the detectors structurally **cannot**:

- **cargo check** — the compiler. Borrow errors, trait resolution, lifetime
  problems, exhaustiveness. Nothing in this skill substitutes for it; run
  `cargo check --all-targets` before believing any refactor preserved behaviour.
- **clippy** — 700+ lints *with type information*. Its `redundant_clone`,
  `manual_map` and `needless_pass_by_value` are the type-aware versions of what
  the detectors here can only guess at syntactically.
- **cargo audit / cargo deny** — published advisories. `find_cargo_issues.py`
  can say a version range is unpinned; only an advisory database can say the
  version you have is vulnerable.
- **coverage** — what actually executed. `find_untested_modules.py` answers
  "does a test import this"; `cargo llvm-cov` answers "does this code ever run".

**When a tool is missing, ask before installing.** The script never installs
anything; it lists each absent tool with its `cargo install` or `rustup
component add` hint under `missing_tools`. When that list is non-empty and the
tools would help, **ask the user whether to install them** (e.g. via the
AskUserQuestion tool) and only install on confirmation. If `cargo` itself is
absent, say plainly that no compilation check was performed rather than implying
the code is clean.

## Reviewing a change request (diff lens)

For an AI-written feature or a PR, review *what changed*, not the legacy around
it. `analyze_diff.py` runs the file-level detectors against only the changed
files, and by default only the added/modified lines:

```bash
python "$SKILL/scripts/analyze_diff.py"                 # working tree vs. merge-base with the default branch
python "$SKILL/scripts/analyze_diff.py" origin/main     # vs. an explicit base ref
python "$SKILL/scripts/analyze_diff.py" --format json | python "$SKILL/scripts/format_findings.py"
```

Whole-tree detectors (Cargo manifest, module graph, dead code, duplication,
over-engineering, untested modules) need the full project — run those with
`analyze_all.py` separately. So does the compiler: `cargo check` needs the whole
crate, so a clean diff review is never on its own a claim that the change
builds. See `references/ai-generated-code.md` for the review stance.

## Reference index (load on demand)

Keep `SKILL.md` lean; pull in depth only when a review needs it.

| Load this when… | File |
|---|---|
| Reading a file critically — the per-function checklist and the finding-triage rubric | `references/critical-review-guide.md` |
| Deciding what to return and when to panic; `Result` vs `Option`, thiserror vs anyhow, the `unwrap` question | `references/error-handling.md` |
| Judging a `.clone()`, a `&String` parameter, or an `Rc<RefCell<T>>`; what the borrow checker is actually telling you | `references/ownership-and-borrowing.md` |
| Judging async design — guards across `.await`, blocking on the executor, sequential awaits, cancellation | `references/async-and-concurrency.md` |
| Reviewing `unsafe` — the SAFETY argument, `unsafe fn` vs `unsafe {}`, `unsafe impl Send`, FFI boundaries | `references/unsafe-and-ffi.md` |
| Deciding between a generic, an `impl Trait` and a `dyn`; whether a trait earns its keep; derives worth having | `references/traits-and-generics.md` |
| Deciding what the crate exposes: what is breaking to change, visibility levels, naming contracts, module layout, features | `references/api-design-and-modules.md` |
| Deciding whether an abstraction should exist; DRY vs the wrong abstraction; why a trait is not needed to mock | `references/overengineering-and-abstraction.md` |
| Diagnosing design smells — the classic catalog in its Rust spellings, and triaging detector candidates | `references/refactoring-catalog.md` |
| Executing a fix — named refactoring techniques and their safe step-by-step mechanics | `references/refactoring-techniques.md` |
| Building the test safety net before refactoring — characterization tests, spotting hollow tests | `references/safety-net-and-testing.md` |
| You want concrete before/after idiom swaps | `references/rust-idioms.md` |
| Judging names, comments and function shape; the `as_`/`to_`/`into_` contract | `references/naming-comments-readability.md` |
| Choosing the right pattern AND making the crate use it consistently; the Rust-native form of each GoF pattern | `references/patterns-and-consistency.md` |
| Reading Cargo.toml, editions, the `[lints]` ratchet, and the CI that keeps a cleanup clean | `references/cargo-and-toolchain.md` |
| Cleaning up a whole poorly-written-but-working crate from cold — the phased campaign | `references/messy-repo-runbook.md` |
| Reviewing AI-generated or vibe-coded Rust — unwrap piles, `todo!()` stubs, ignored parameters, tests that can't fail | `references/ai-generated-code.md` |

## Over-engineering anti-patterns (quick reference)

| Pattern | Problem | Fix |
|---|---|---|
| Trait with one implementor | Abstraction over one thing | Use the concrete type — a generic parameter already mocks, so "I need it for tests" is not a reason in Rust |
| Trait with no implementor | Written for code that never arrived | Delete it, or document it as an extension point |
| Fieldless struct + associated fns | A module with a `struct` keyword | Make it a module and export the functions |
| Newtype that only forwards | A name and a maintenance cost | Give it an invariant, or delete it |
| Builder for a two-field struct | Ceremony around `Foo::new(a, b)` | `new`, or `..Default::default()` |
| `Box<dyn Trait>` with one impl | A vtable for a known type | The concrete type |
| Type parameter used once | A concrete type with a letter for a name | Name the type, or take `impl Trait` |
| Deep supertrait chain | Every implementor satisfies the whole chain | Flatten; a bound can list several traits |
| Module that only re-exports | Indirection between caller and definition | Import from where the item lives |
| `Rc<RefCell<T>>` for a tree | Runtime borrow checking for a static shape | Indices into a `Vec`, or single ownership |
| Speculative options struct | Code for "future needs" | Delete it (YAGNI) |

## Code smells (quick reference → fix)

| Smell | Fix |
|---|---|
| `.unwrap()` inside a function returning `Result` | `?` — it was one character away |
| `.unwrap()` in library code | Return `Result`, or `.expect("why")` if the invariant is real |
| `panic!` / `todo!()` / `unimplemented!()` in a library | Return `Err(...)`; finish or delete the stub |
| `match x { Ok(v) => v, Err(e) => return Err(e) }` | `x?` — and `?` applies `From` for you |
| `Err(_) => {}` / `.ok();` / `let _ = fallible();` | Handle, propagate, or log. A swallowed error is a wrong answer |
| `.map_err(\|_\| MyError::X)` | Keep the cause — `#[from]`, `#[source]`, or `.context(…)` |
| `Result<_, String>` / `Box<dyn Error>` in a library | A concrete error enum (thiserror) callers can match on |
| `unsafe { … }` with no `// SAFETY:` | Write the invariant and why it holds here |
| `static mut` | An atomic, a `OnceLock`, or a `Mutex` (and Rust 2024 forbids references to it) |
| `mem::transmute`, `get_unchecked`, `set_len` | The checked form, unless a profile said otherwise — then a SAFETY comment |
| `big as u32` (narrowing) | `u32::try_from(big)?` — the truncation becomes a `Result` |
| `f64 as i32` | It truncates *and* saturates, and NaN becomes 0. Round deliberately, convert checked |
| Lock guard alive across `.await` | Narrow the scope, or use the runtime's async mutex |
| `std::thread::sleep` / `std::fs` in an `async fn` | `tokio::time::sleep`, `tokio::fs`, or `spawn_blocking` |
| `tokio::spawn(x);` with the handle dropped | Keep it and await/abort it, or name it `_worker` with a reason |
| `.await` in a loop over independent work | `try_join_all`, or `buffer_unordered(n)` for a large list |
| `fn f(s: &String)` / `&Vec<T>` / `&PathBuf` | `&str` / `&[T]` / `&Path` — they accept strictly more |
| Owned `String`/`Vec` parameter the body only reads | Borrow it; the caller should not have to allocate |
| `.clone()` inside a loop, on a value from outside it | Hoist it, or borrow |
| `Rc<RefCell<T>>` field | Runtime borrow checks; restructure so one owner holds it |
| `for i in 0..v.len() { v[i] }` | `for x in &v` |
| `while i < v.len() { … i += 1 }` | `for` — a `continue` in that body hangs the program |
| Loop that only pushes / only adds | `.map().collect()` / `.sum()` |
| `if x.is_some() { x.unwrap() }` | `if let Some(v) = x` |
| `match x { Some(v) => v, None => d }` | `x.unwrap_or(d)` / `unwrap_or_else` / `map_or` |
| `.unwrap_or(expensive())` | `.unwrap_or_else(expensive)` — the argument form always runs |
| `.filter(p).next()` | `.find(p)` |
| `.len() == 0` / `s == ""` | `.is_empty()` |
| `format!("{}", x)` / `format!("{}", name)` | `x.to_string()` / `format!("{name}")` |
| `Foo { x: x }` | `Foo { x }` |
| `return expr;` as the last expression | `expr` |
| `match cond { true => …, false => … }` | `if cond { … } else { … }` |
| `if c { true } else { false }` | `c` |
| `map.contains_key(k)` then `map[k]` | `if let Some(v) = map.get(k)` — one hash, no panic |
| `v.len() - 1` | `checked_sub` — it wraps to a huge `usize` in release |
| Public type with no `Debug` | `#[derive(Debug)]` — its absence propagates to every containing type |
| `pub` fields on a type with an inherent `impl` | Private fields plus the accessors the invariants allow |
| Public enum without `#[non_exhaustive]` | Adding a variant later is a breaking change |
| `new()` with no arguments and no `Default` | `#[derive(Default)]` — generic code needs the trait |
| `fn get_name()` | `fn name()` — `get_` is for the fallible indexed form |
| `fn into_x(&self)` / `fn as_x(self)` | `into_` consumes, `as_` borrows, `to_` allocates |
| `#![allow(warnings)]` / `#![allow(dead_code)]` | Fix them; a crate-level allow hides everything added later |
| `dbg!` / `println!` in a library | Delete; use `tracing`/`log` and let the binary decide |
| `extern crate` / `try!` / `Box<Trait>` without `dyn` | 2018+ spellings; `cargo fix --edition` does most of it |
| A `.rs` file no `mod` reaches | It is never compiled at all — add the `mod`, or delete the file |
| `#[test]` with no assertion; bare `#[should_panic]` | Assert the value; add `expected = "…"` |
| Commented-out code | Delete it (git remembers) |
| An unused import | rustc already reports it precisely — this skill defers rather than guess worse |

## When NOT to simplify

- Untested code — write a characterization test first, *then* refactor.
- Hot paths — measure before trading clarity for speed. A `.clone()` a profiler
  cleared is not a finding.
- Code being replaced or retired soon.
- Complexity genuinely forced by an FFI boundary, a `#[repr(C)]` layout, a
  framework's trait, or a real present requirement. An `unsafe` block at an FFI
  edge with a SAFETY comment beside it is the right answer, not a smell.
- Cold code that never changes and blocks nothing — leave it; fix what churns.
- `unwrap()` in tests, examples and `build.rs`. It is an assertion there.
- A wide `match` on an enum. Exhaustiveness is the design, not a smell.

## Relationship to clippy and rustc

These scripts complement the toolchain; they do not replace it. Some detectors
overlap lints: `clippy::unwrap_used`, `needless_range_loop`, `ptr_arg`,
`len_zero`, `redundant_field_names`, `needless_return`, `or_fun_call`,
`manual_map`, `redundant_closure`, `new_without_default`, `missing_docs`,
`undocumented_unsafe_blocks`, `cast_possible_truncation`, `non_snake_case`,
`dead_code`. If the crate already runs those at `deny`, disable the matching
detector via `--ignore` (or drop its category with `analyze_all.py --skip`) to
avoid double-reporting — or better, run the real tools with
`run_external_tools.py` and lean on the detectors only for what the tools do not
cover.

Where a lint is strictly better, this skill does not compete: unused imports are
rustc's, because it can tell a trait imported for method resolution from a dead
name and a syntax scan cannot. A worse second answer to a question already
answered well is noise, and noise is how a report stops being read.

The unique value here is: the **module-graph check** (a `.rs` file rustc never
compiles, which no lint can report because the compiler never sees it), the
**design and architecture detectors** (over-engineering, feature envy, data
clumps, refused bequest, duplication, identical type shapes), the **repo-level
checks** (manifest audit, dependency reconciliation, untested-module and
test-smell detection that scaffold a safety net), the **AI-code tells**
(scaffolding, ignored parameters, placeholder prose), the fact that it **runs
with no cargo registry, no network and no successful build** — plus the
**judgment guides**, which no linter provides.
