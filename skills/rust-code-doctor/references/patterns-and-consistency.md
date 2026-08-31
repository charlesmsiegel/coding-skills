# Rust-shaped patterns, and using one of them everywhere

Two failures cost more than any individual pattern choice: importing a pattern
that has a better Rust spelling, and using three spellings of the same thing in
one codebase.

## The GoF patterns in Rust

| Pattern | The Rust form |
|---|---|
| Strategy | A closure parameter, or a generic `T: Trait`. Rarely a `dyn` object. |
| State | An enum, or type-state (a distinct type per state, transitions consume `self`) |
| Visitor | A `match` on an enum. Exhaustiveness is what the pattern was simulating. |
| Builder | A real pattern here — `Foo::builder().a(1).b(2).build()` — but only for many optional fields |
| Factory | A plain function, or `impl From<X> for Y` |
| Singleton | `OnceLock` / `LazyLock`. Never `static mut`. |
| Observer | A channel, or a `Vec<Box<dyn Fn(&Event)>>` |
| Decorator | A wrapper struct implementing the same trait |
| Adapter | `impl From`, or a newtype |
| Iterator | `impl Iterator for T` — the standard library's whole design |
| Null object | `Option<T>`, which the compiler forces you to handle |
| Template method | A trait with default methods calling required ones |
| RAII / dispose | `Drop`. This is Rust's native pattern, not a workaround. |

The ones worth dwelling on:

**Type-state** replaces a whole class of runtime checks. Instead of
`Connection { socket: Option<TcpStream> }` with an `unwrap` in every method,
have `Disconnected` and `Connected` be different types where `connect(self) ->
io::Result<Connected>` consumes one and produces the other. Calling `send` on a
disconnected connection is then a compile error, not a panic.

**Newtype** is the Rust pattern with no direct equivalent elsewhere: zero
runtime cost, turns argument swaps into type errors, gives a place to hang
methods and trait impls, and is the only way around the orphan rule.

**Drop** is what `try/finally` and `with` are for elsewhere, except it cannot be
forgotten. A guard type whose `Drop` releases the resource is more reliable than
any convention.

## Consistency matters more than the choice

Pick one and apply it everywhere:

- **Error strategy.** `thiserror` enums in libraries, `anyhow` in binaries. Not
  both in one crate, and never a third spelling (`Box<dyn Error>`,
  `Result<_, String>`) in the corners.
- **Construction.** `new()`, `Default`, `From`, or a builder — per type, chosen
  for a reason, not four styles across four types.
- **Async runtime.** One. Mixing tokio and async-std in one binary produces
  deadlocks that are extremely hard to diagnose.
- **Logging.** `tracing` or `log`, not both, and never `println!` in a library.
- **Serialisation.** One `serde` convention for field renaming, one for
  optional fields.
- **Module layout.** `foo.rs` + `foo/`, or `foo/mod.rs`. Not both.
- **Test placement.** Inline `#[cfg(test)] mod tests` for unit tests,
  `tests/` for integration. Say which in CONTRIBUTING.

## Finding the inconsistency

```sh
# Which error strategies are in play?
rg 'Box<dyn (std::)?error::Error|anyhow::|thiserror|Result<.*, String>' --stats

# Which logging?
rg '\b(println!|eprintln!|log::|tracing::)' -o --no-filename | sort | uniq -c

# Which construction conventions?
rg 'fn new\(|impl Default for|fn builder\(' -c
```

When there are two, the finding is not "pattern X is wrong" — it is "there are
two, here is which one to converge on, here is the migration". A codebase with
one merely-adequate convention is easier to work in than one with two good ones.

## The `impl` block convention

Group by purpose and keep the order predictable:

```rust
impl Store {
    // constructors
    pub fn new(path: &Path) -> io::Result<Self> { … }
    pub fn in_memory() -> Self { … }

    // accessors
    pub fn len(&self) -> usize { … }
    pub fn is_empty(&self) -> bool { … }

    // operations
    pub fn insert(&mut self, k: Key, v: Value) -> Option<Value> { … }

    // private helpers
    fn rebalance(&mut self) { … }
}

// trait impls in their own blocks, one per trait
impl Default for Store { … }
impl fmt::Debug for Store { … }
```

Note `is_empty` alongside `len`: clippy requires it, and callers expect it.
