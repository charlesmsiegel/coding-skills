# Executing a refactor safely

Behaviour is sacred. Every technique here is a sequence of steps where the code
compiles and the tests pass between each one, because a refactor that cannot be
stopped halfway is a rewrite.

Rust helps more than most languages here — the compiler finds every call site —
but it also has failure modes that only appear after the change, so the order
matters.

## Before anything

1. `cargo test` — green. If there are no tests, see
   `safety-net-and-testing.md`; a characterization test comes first.
2. `git commit` — a clean base to return to.
3. `cargo fmt` in its own commit, so the refactor diff is behaviour and not
   whitespace.

## Extract function

1. Copy the block into a new `fn` below, with parameters for everything it
   reads and a return type for everything it produces.
2. Compile. The errors are your parameter list; the compiler is doing the
   analysis for you.
3. Replace the original block with a call.
4. Test.

If step 2 produces borrow errors, the block was reading and writing the same
data. That is information: either pass `&mut`, or return the new values and let
the caller assign — the second is usually clearer.

## Introduce newtype

The safest sequence, because the compiler enumerates the work:

1. `struct UserId(pub u64);` — `pub` field for now.
2. Change **one** signature to take `UserId`.
3. Compile. Fix every error the compiler lists. This is the whole migration and
   it is mechanical.
4. Once the call sites are converted, make the field private and add the
   accessor and constructor you actually want.
5. Add `#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]` as needed.

## Replace `bool` parameter with an enum

1. Define the enum next to the function.
2. Add a new function taking the enum; make the old one a one-line wrapper that
   converts and calls it.
3. Migrate call sites, compiling as you go.
4. Delete the wrapper. The compiler finds anything left.

## Extract struct from a data clump

1. Define the struct with public fields.
2. Add a new function taking it; keep the old signature as a wrapper that builds
   the struct and delegates.
3. Migrate callers.
4. Delete the wrapper, then tighten the fields and add a constructor.

## Replace `unwrap` with `?`

1. Change the return type to `Result<T, E>` — start with `anyhow::Result<T>` in
   an application, or the crate's existing error enum.
2. Replace `unwrap()` with `?` one at a time; add `#[from]` variants as the
   compiler asks for them.
3. Compile. Every caller is now an error, and each is a decision: propagate
   (add `?`), handle, or — at the top of the call tree — report.
4. Test, including the failure path. A `Result` nobody ever returns `Err` from
   in a test is a code path that has never run.

## Collapse a single-implementor trait

1. Find the one `impl`. Confirm there is genuinely one (`find_overengineering.py`
   reports it, but check for downstream crates).
2. Change the consumers from `&dyn Trait` / `T: Trait` to the concrete type.
3. Compile; fix.
4. Delete the trait.
5. Test — especially anything that was using a test double through the trait,
   which now needs the generic parameter or a `#[cfg(test)]` type.

## Split a god struct

1. Identify the field subset that changes together.
2. Define the new struct; add it as a field of the old one, keeping the old
   fields temporarily.
3. Move one method at a time to the new struct; update its callers.
4. Delete the old fields.
5. Test between each move. This one is genuinely incremental and should never
   be done in one commit.

## Change `&Vec<T>` to `&[T]`

The easiest win in the catalog:

1. Change the parameter type.
2. Compile. Usually nothing breaks — deref coercion means `&vec` still works at
   every call site.
3. Done. Callers with arrays and slices now compile too.

## What to run after every refactor

```sh
cargo fmt --check
cargo check --all-targets      # every target, not just the lib
cargo clippy --all-targets
cargo test
cargo test --no-default-features    # if the crate has features
cargo doc                            # doc examples are tests; broken links are not
```

`--all-targets` matters: a change that breaks only the benches or the examples
is invisible to a plain `cargo build`, and `cargo test` alone does not compile
benches.

## Rust-specific hazards

- **`impl Trait` in return position leaks auto traits.** Extracting a helper can
  make a returned iterator non-`Send` and break a caller that spawns it. Name
  the bounds you promise.
- **Adding a variant to a public enum is breaking** unless it is
  `#[non_exhaustive]`. Check before you add.
- **Making a private field public is easy; the reverse is breaking.** Start
  private.
- **A `#[derive]` you remove is a breaking change** for anyone who relied on it.
- **Feature combinations.** A refactor that compiles with default features may
  not with `--no-default-features`. `cargo hack check --feature-powerset` if the
  crate has more than a couple.
