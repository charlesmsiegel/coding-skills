# Names, comments, and function shape

## Names that carry a contract

Rust's conventions are not decoration. Three of them are promises callers rely
on, and breaking them misleads people who read the name and did not read the
body.

**The conversion prefixes** (`as_` free, `to_` allocates, `into_` consumes). An
`as_` method that allocates gets called in a loop. An `into_` taking `&self` is
not a conversion.

**No `get_` on getters.** `fn name(&self) -> &str`, not `fn get_name`. `get` in
the standard library means the fallible indexed form (`HashMap::get`,
`slice::get`) that returns an `Option`, and using it for a plain accessor makes
that distinction unreadable.

**Booleans read as questions.** `is_valid`, `has_children`, `can_retry`. At a
call site `if x.valid()` and `if x.validate()` look identical and only one of
them is a query.

Then the mechanical ones, which rustc mostly warns about: `snake_case` for
functions, variables, modules and crates; `UpperCamelCase` for types, traits and
enum variants; `SCREAMING_SNAKE_CASE` for constants and statics.

## Names that mislead

- **Module stutter.** `parser::ParserConfig` — the path already says `parser`.
  `parser::Config`, and `use parser::Config as ParserConfig` where the short
  name would be ambiguous.
- **Single letters** outside a coordinate, an index, or a closure parameter with
  one obvious meaning. `let t = ...` in a forty-line function costs every
  reader.
- **`data`, `info`, `manager`, `handler`, `helper`, `util`.** Each is a name
  that means "I did not decide what this is". A `Utils` struct is a module.
- **Type names that describe the implementation, not the meaning.**
  `StringMap` versus `Headers`.

## Shadowing

Rebinding is idiomatic and good:

```rust
let config = read_file(path)?;      // String
let config = parse(&config)?;       // Config
```

Each step is a real transformation and the name stays true. Three or more
shadows of one name in one body is where it stops helping — a debugger
breakpoint no longer tells you which `config` you are looking at. Give the
stages different names at that point.

## Comments

Delete comments that restate the code:

```rust
// increment the counter
counter += 1;

/// Returns the name.
pub fn name(&self) -> &str { &self.name }
```

The second is worse than the first, because it makes the item look documented
and stops anyone writing the real documentation.

Keep comments that say **why**:

```rust
// The upstream API rejects batches over 500 with a 413 rather than a 400,
// which our retry logic would treat as retryable. Chunk below the limit.
const BATCH_SIZE: usize = 400;

// SAFETY: `idx` was bounds-checked above, and `buf` is not reallocated
// while `&self` is held.
let value = unsafe { self.buf.get_unchecked(idx) };
```

The three that carry real weight, each with a clippy lint behind it:

- `# Errors` — which failures a caller can expect, on anything returning
  `Result`.
- `# Panics` — the precondition, on anything that can panic.
- `# Safety` — the obligation, on every `unsafe fn`.

And doc examples are tests. `cargo test` compiles and runs every ` ``` ` block
in a doc comment, which makes a documented API a tested one and means the
examples cannot silently rot.

## Commented-out code

Delete it. Git has every version it ever had. A commented block is the one kind
of code nobody dares remove, because nobody knows whether it still matters —
which is precisely the state that makes it worthless.

## TODO and FIXME

A comment cannot be scheduled, assigned, or closed. Move it to the tracker with
an owner, or delete it. If it must stay, say what has to be true before it can
be resolved — "TODO: fix this" is a note that will outlive everyone who
understood it.

## Function shape

- **One level of abstraction per function.** A body that opens a socket, parses
  a header, and formats an error message is three functions.
- **Flat is readable.** `let … else`, `?`, and early `return` remove nesting;
  three levels usually means two of them were not used.
- **Name the branches.** A condition worth a comment is worth a `let is_expired
  = …;` instead.
- **Return early, allocate late.**

## Module documentation

`//!` at the top of a module says what it is for. It is the first thing a reader
of an unfamiliar file sees and the cheapest orientation you can give them:

```rust
//! Wire format for the v2 protocol.
//!
//! Encoding is little-endian throughout. The v1 decoder lives in `super::v1`
//! and is kept only for reading archived files.
```
