# Cargo, the toolchain, and making the checks stick

## The tools, and what only each one can tell you

| Tool | Answers | Nothing else does |
|---|---|---|
| `cargo check` | does it compile — types, lifetimes, traits, exhaustiveness | this is *the* correctness check; no syntax scanner substitutes |
| `cargo clippy` | 700+ lints, with type information | `needless_clone`, `manual_map`, `redundant_closure` need types |
| `cargo fmt --check` | formatting drift | — |
| `cargo test` | behaviour, plus doc examples | — |
| `cargo audit` | published advisories against `Cargo.lock` | whether the version you have is vulnerable |
| `cargo deny check` | licences, duplicate versions, banned crates | licence compliance |
| `cargo llvm-cov` | which lines executed | "is this tested" versus "does a test import it" |
| `cargo +nightly udeps` | dependencies nothing uses, from the build graph | proves it, where syntax can only guess |
| `cargo tree --duplicates` | one crate at two versions | slow builds and two incompatible copies of a type |
| `cargo hack --feature-powerset` | which feature combination does not build | the combination nobody compiles is the one that breaks |
| `cargo mutants` | which mutations no test caught | whether the suite can actually fail |

`run_external_tools.py` drives the ones that are present and reports the rest
rather than installing them.

## Cargo.toml, reviewed

```toml
[package]
name = "thing"
version = "0.1.0"
edition = "2021"          # 2015 = no `dyn`, `extern crate` required, different paths
rust-version = "1.75"     # the MSRV. Without it a dependency bump silently raises it.
description = "…"         # crates.io refuses a publish without this
license = "MIT OR Apache-2.0"
repository = "…"

[dependencies]
serde = { version = "1", features = ["derive"] }
regex = "*"               # ← wildcard: accepts the next breaking release. Never.
weird = { git = "…" }     # ← no rev: reproducible only until someone pushes

[lints.rust]
unsafe_code = "warn"

[lints.clippy]
unwrap_used = "warn"
```

The `[lints]` table is the part most repos are missing and the part that matters
most for a cleanup, because it is where a cleared class of finding stays
cleared. Without it, "we agreed not to use `unwrap`" is a comment in a PR
thread; with it, it is a warning at every keystroke in every editor.

For a workspace, declare them once at the root and inherit:

```toml
# workspace Cargo.toml
[workspace.lints.clippy]
unwrap_used = "warn"

# each member
[lints]
workspace = true
```

## Editions

| Edition | What arrived |
|---|---|
| 2015 | the baseline: `extern crate`, `dyn` optional, `try!` |
| 2018 | module path changes, `dyn Trait` required, NLL, `async`/`await` |
| 2021 | disjoint closure captures, `IntoIterator` for arrays, consistent panic macros |
| 2024 | `static mut` references are an error, `gen` blocks, tightened `unsafe` rules |

Editions are per-crate and interoperate freely, so a bump is not a coordinated
migration. `cargo fix --edition` does the mechanical part; review the diff.

## Dependencies

- **Caret is the default and is right.** `serde = "1"` means `>=1.0.0, <2.0.0`.
  Pinning exactly (`=1.0.193`) in a library forces the version on every
  downstream user and causes unresolvable conflicts.
- **`Cargo.lock`**: commit it for binaries, and — since 2023 — for libraries too.
  It does not affect downstream resolution and it makes CI reproducible.
- **Features**: `default-features = false` plus an explicit list is how a heavy
  dependency gets light. Check what the defaults actually pull in.
- **Duplicate versions** (`cargo tree --duplicates`) mean two copies of a type
  that do not interconvert, and error messages that say `Foo is not Foo`.

## The CI that keeps it clean

```yaml
- run: cargo fmt --all -- --check
- run: cargo clippy --all-targets --all-features -- -D warnings
- run: cargo test --all-targets --all-features
- run: cargo test --doc
- run: cargo check --no-default-features
- run: cargo audit
- run: cargo +${{ env.MSRV }} check      # the declared MSRV is real
```

`--all-targets` catches breakage in benches and examples that a plain build
misses. `cargo test --doc` is separate because `--all-targets` skips doc tests —
a documented API's examples are tests, and they rot silently otherwise.

Add `-D warnings` **after** the existing warnings are cleared. Turning it on
over a warning backlog just teaches the next contributor to disable it.

## Profiles worth knowing

```toml
[profile.release]
lto = "thin"           # meaningful size and speed win, moderate build cost
codegen-units = 1      # slower build, faster binary
panic = "abort"        # smaller, faster — but no `catch_unwind`, and tests still need unwind
strip = "symbols"

[profile.dev]
opt-level = 1          # when debug builds are unusably slow
[profile.dev.package."*"]
opt-level = 3          # optimise dependencies, keep your own crate debuggable
```

The last pair is the one people do not know about, and it is often the single
biggest improvement to a slow debug build.
