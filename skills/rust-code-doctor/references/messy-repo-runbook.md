# Cleaning up a working-but-messy Rust repo

A phased campaign for a codebase that runs in production and that nobody wants
to touch. The order matters: each phase makes the next one safe.

Do not start at Phase 4. That is how a cleanup becomes an outage.

## Phase 0 — Find out what you have

```sh
cargo check --all-targets 2>&1 | tail -40      # does it even build?
cargo test 2>&1 | tail -20                      # is there a suite, does it pass?
python "$SKILL/scripts/find_cargo_issues.py" .  # edition, lints, dependencies
python "$SKILL/scripts/find_module_issues.py" . # files rustc never compiles
git log --since="1 year ago" --name-only --pretty=format: \
  | grep '\.rs$' | sort | uniq -c | sort -rn | head -30
```

Four facts decide the whole campaign: **does it compile**, **is there a test
suite**, **what edition**, and **which files churn**. Write them down before
doing anything.

## Phase 1 — Make it compile, cleanly

Nothing else is possible until `cargo check --all-targets` is green. If it is
not, that is the entire first task.

Then deal with the warnings, and specifically with anything hiding them:
`#![allow(warnings)]`, `#![allow(dead_code)]`, a crate-level
`#![allow(unused)]`. Those are the compiler's own analysis switched off, and
`find_debug_leftovers.py` reports them. Turn them off one at a time and see
what appears; some of it is real.

`find_module_issues.py`'s `file_never_compiled` findings belong here too. A file
no `mod` reaches has never been type-checked, so whatever is in it is unknown —
and adding the `mod` line is how you find out.

## Phase 2 — Normalise the formatting, in one commit

```sh
cargo fmt --all
git commit -am "cargo fmt (no behaviour change)"
```

One commit, nothing else in it. Every later diff is then behaviour rather than
whitespace, and `git blame` survives because reviewers can skip this one commit
by hash.

If the repo has no `rustfmt.toml`, add an empty one — it pins the defaults
against future rustfmt changes.

## Phase 3 — Build the safety net

**Before touching any logic.** Follow `safety-net-and-testing.md`:
characterization tests for the top five churn files, then confirm they can fail
by breaking the implementation on purpose.

`find_untested_modules.py` says which files nothing tests.
`cargo llvm-cov` says which lines never execute. The first is free; run the
second if the suite is fast enough.

If the answer is "there are no tests at all", this phase is the project. Say so
plainly rather than proceeding.

## Phase 4 — Fix the bugs the detectors found

Now, and in this order:

1. `find_concurrency_issues.py` — a guard across an `.await` and a blocking call
   on the executor are production incidents.
2. `find_error_handling.py` — `unwrap` in fallible functions, swallowed errors,
   `panic!` in library code.
3. `find_unsafe_issues.py` — `static mut`, `transmute`, undocumented `unsafe`.
4. `find_type_issues.py` — narrowing `as` casts.
5. `find_security_issues.py` — shell strings, SQL interpolation, disabled TLS.

One class per pull request. A PR that fixes forty unwraps is reviewable; a PR
that fixes forty unwraps and reorganises three modules is not.

## Phase 5 — Modernise, then tighten

```sh
cargo fix --edition            # then bump the edition in Cargo.toml
cargo fix --edition-idioms
cargo clippy --fix --allow-dirty
```

Review each diff — `cargo fix` is mechanical, not infallible. Then the
signature-level work: `&String` → `&str`, `&Vec<T>` → `&[T]`, `get_` prefixes,
missing `Debug` derives. These are individually trivial and collectively make
the crate feel like Rust.

## Phase 6 — Design work, only in files that churn

Traits with one implementor, god structs, data clumps, over-engineering. This is
the expensive phase and it pays only where the code changes. **Do not refactor
cold code.** A smell in a file untouched since 2021 costs nothing; the same
smell in a file edited weekly costs every week.

## Phase 7 — Ratchet

Every cleared class of finding becomes a rule, or it comes back. Put the rules
in the manifest so `cargo clippy` and every IDE agree:

```toml
[lints.rust]
unsafe_code = "forbid"          # or "warn", if the crate has legitimate unsafe
missing_docs = "warn"
unused_must_use = "deny"

[lints.clippy]
unwrap_used = "warn"
expect_used = "warn"
panic = "warn"
needless_pass_by_value = "warn"
```

Then in CI:

```yaml
- run: cargo fmt --all -- --check
- run: cargo clippy --all-targets -- -D warnings
- run: cargo test --all-targets
- run: cargo audit
```

`-D warnings` is what makes the ratchet real. Introduce it after the existing
warnings are cleared, not before, or the first contributor turns it off.

## What to tell the user at each phase

Say which phase you are in and what the exit condition is. "Phase 3: no tests
exist for the four highest-churn modules; I am writing characterization tests
before touching the `unwrap`s" is a status. "Cleaning up the code" is not, and
it is how a two-week cleanup arrives as one unreviewable diff.
