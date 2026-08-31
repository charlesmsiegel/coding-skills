# Public API and module structure

## What is breaking, and what is not

This is the table that should drive the review of any crate that is published or
depended on internally, because the cheap moment to fix these is now.

| Change | Breaking? |
|---|---|
| Adding a variant to a public enum | **Yes**, unless `#[non_exhaustive]` |
| Adding a field to a public struct | **Yes**, if it has public fields (breaks struct literals) |
| Adding a required trait method | **Yes**, unless it has a default |
| Adding a method to an inherent `impl` | No |
| Adding a `#[derive]` | No |
| Narrowing a return type (`Box<dyn Error>` → concrete) | **Yes** |
| Widening a parameter (`&String` → `&str`) | No |
| Renaming a public item | **Yes** |
| Making a public field private | **Yes** |
| Marking something `#[must_use]` | No (it is a lint) |

The consequence: `#[non_exhaustive]` on public enums and private fields with
accessors are decisions to make *before* 1.0, not after.

## Visibility

Rust has more than two levels, and using them is free:

- `pub` — the crate's public API. Every item here is a promise.
- `pub(crate)` — usable anywhere in this crate, invisible outside. This is the
  right default for most "internal but shared" items, and the one people forget.
- `pub(super)` — visible to the parent module. Good for a helper the sibling
  modules need.
- private — the default, and correct more often than people write it.

A struct whose fields are all `pub` *and* which has an inherent `impl` is a
contradiction: anything those methods maintain can be broken by writing a field
directly. Either it is a plain data carrier (all-`pub`, no invariants, fine) or
it has invariants (private fields, accessors).

## Naming as a contract

The conversion prefixes are promises about cost, and callers rely on them:

| Prefix | Receiver | Cost | Example |
|---|---|---|---|
| `as_` | `&self` | free — a borrow or a view | `str::as_bytes` |
| `to_` | `&self` | allocates or computes | `str::to_uppercase` |
| `into_` | `self` | consumes the receiver | `String::into_bytes` |

An `as_` method that allocates will be called in a loop by someone who read the
name. An `into_` that takes `&self` is not a conversion at all.

Other conventions: no `get_` prefix on getters (`fn name()`, not `fn get_name()`)
— `get` is reserved for the fallible/indexed form the standard library uses. A
`bool`-returning method reads as a question (`is_`, `has_`, `can_`). Iterator
types are named for what they iterate (`Iter`, `IntoIter`, `Windows`).

## Documentation

`///` on a public item is the contract. Three sections carry real weight, and
clippy has a lint for each:

- **`# Errors`** on anything returning `Result` — which failures, and what they
  mean for the caller.
- **`# Panics`** on anything that can panic — the precondition, stated.
- **`# Safety`** on every `unsafe fn` — the obligation the caller takes on.

A doc example is a test: `cargo test` compiles and runs every ` ``` ` block in a
doc comment. That makes documented APIs *tested* APIs, which is the strongest
argument for writing the examples.

`#![warn(missing_docs)]` at the crate root is how the next undocumented item
gets noticed.

## Module layout

Cargo's own conventions do most of the work:

```
src/lib.rs        the crate root and its public facade
src/main.rs       a binary
src/bin/*.rs      additional binaries
tests/*.rs        integration tests — one crate each, public API only
benches/*.rs      benchmarks
examples/*.rs     compiled by `cargo test`, so they cannot rot silently
build.rs          build script
```

Inside `src/`, prefer the 2018 style — `foo.rs` beside a `foo/` directory —
over `foo/mod.rs`. Both work; only one leaves you with fourteen editor tabs
labelled `mod.rs`. Mixing the two in one crate is the thing to fix.

**The failure mode with no analogue elsewhere:** a `.rs` file under `src/` that
no `mod` declaration reaches is not compiled at all. It does not type-check, its
tests do not run, and it will not appear in coverage. People discover this after
fixing a bug in a file rustc never opened. `find_module_issues.py` reports it as
`file_never_compiled`.

## Re-exports

A crate root that re-exports the names users need is good: `pub use
crate::store::Store;` gives `mycrate::Store` instead of `mycrate::store::Store`,
and lets the internal layout change without breaking anyone.

`pub use foo::*` is not. A glob makes a name's origin unfindable — grepping for
the definition of a symbol it pulls in returns nothing at that file — and it
silently re-exports whatever gets added to `foo` later, including things you did
not mean to make public. List the names.

The one conventional exception is `use super::*;` inside a `#[cfg(test)] mod
tests`, where the scope is one file and the intent is obvious.

## Features

- Features must be **additive**. Enabling one must never remove or change an
  item, because Cargo unifies features across the whole dependency graph: if any
  crate enables `foo`, everyone gets it.
- A `no_std` feature is the common exception people get wrong — `default =
  ["std"]` with `#![cfg_attr(not(feature = "std"), no_std)]` is additive; a
  `no_std` feature that *removes* `std` is not.
- Test the combinations. `cargo hack check --feature-powerset` exists because
  the one nobody compiles is the one that breaks.
