# Error handling: what to return and when to panic

## The decision

| Situation | Return |
|---|---|
| The caller can reasonably do something about it | `Result<T, E>` with a concrete `E` |
| Absence is normal and needs no explanation | `Option<T>` |
| A precondition the caller was told about was violated | panic (document it under `# Panics`) |
| An invariant *this code* is responsible for is broken | panic — the bug is here |
| Out of memory, or a state the type system should have prevented | panic |

The line is who could have prevented it. A missing file is the world's problem
and gets a `Result`. An index the caller was told to keep in range is the
caller's problem and may panic. A `None` this function just proved was `Some` is
this function's problem, and the fix is to restructure so the proof is in the
type.

## Library versus application

This is the distinction that decides most of the argument.

**A library** returns a concrete error type — an enum, usually via `thiserror`.
Downstream code must be able to `match` on the variant to decide whether to
retry, fall back, or give up. `Box<dyn Error>` and `anyhow::Error` erase exactly
the information a caller needs, and once published, narrowing them is a breaking
change.

**An application** uses `anyhow` (or `eyre`). Nothing downstream branches on the
variant; what matters is the chain of context that reaches the log:

```rust
let raw = fs::read_to_string(&path)
    .with_context(|| format!("reading {}", path.display()))?;
let cfg: Config = toml::from_str(&raw)
    .with_context(|| format!("parsing {}", path.display()))?;
```

`.context()` at each layer is the difference between "No such file or directory"
and "reading /etc/app/config.toml: No such file or directory".

## `?` and what it does for you

`?` is not just an early return. It applies `From` to the error, which is why a
function returning `AppError` can `?` an `io::Error`, a `ParseIntError` and a
`reqwest::Error` in three consecutive lines — provided `AppError` has a `#[from]`
for each. That is the whole reason to define an error enum rather than stringify
at each site.

Two consequences worth knowing:

- `?` in a function returning `Option<T>` short-circuits on `None`. It composes
  the same way.
- `?` on a `Result` inside a closure returns from the *closure*. This is the
  usual reason a `.map(|x| f(x)?)` does not compile the way people expect;
  `.collect::<Result<Vec<_>, _>>()` is normally what was meant.

```rust
// Turning an iterator of Results into a Result of a collection:
let parsed: Result<Vec<u32>, _> = lines.iter().map(|l| l.parse()).collect();
let parsed = parsed?;   // one error, the first, short-circuits the whole thing
```

## The `unwrap` question

`.unwrap()` is not always wrong. It is wrong when it is *load-bearing in
production* and the invariant is not visible.

| Context | Verdict |
|---|---|
| A test | Fine. It *is* the assertion. |
| `build.rs`, an example, a one-off binary | Fine. |
| A `static` regex compiled from a literal | Fine — prefer `.expect("literal regex")`. |
| Inside a function that returns `Result` | Never. `?` was one character away. |
| A library entry point | Never. You are panicking in someone else's process. |

When the value genuinely cannot be absent, `.expect("…")` with the *reason* is
strictly better than `.unwrap()`: the message reaches the person reading the
panic, who is not you.

## Errors that get swallowed

Four shapes, in descending order of how much they hurt:

```rust
Err(_) => {}                       // the failure is now invisible
let _ = fallible();                // same, with a note that you meant it
fallible().ok();                   // converts to Option and drops it
fallible().unwrap_or_default();    // a failure and an empty success now look identical
.map_err(|_| MyError::Whatever)    // the cause — the only useful part — is gone
```

The last one is the most common in otherwise careful code, and the most
expensive: `Unavailable` with no source tells an on-call engineer nothing.

## Panics in a library, concretely

A `panic!` in a library aborts the *caller's* program. If the caller is a web
server, that is a request thread dying; if they set `panic = "abort"`, it is the
process. There are three legitimate uses:

1. A documented precondition (`slice[i]`, `Vec::remove`). Say so under
   `# Panics` in the doc comment.
2. An invariant your own code maintains and has just violated. The bug is
   yours, and continuing would produce wrong data.
3. `unreachable!()` where the type system cannot express the proof — and if it
   *can*, restructure instead.

`todo!()` and `unimplemented!()` are neither. They are notes to yourself that
compile.

## Making a class of error impossible

The best error handling is a type that cannot reach the error state.

```rust
// before: every caller must remember to check
struct Connection { socket: Option<TcpStream> }
impl Connection { fn send(&self) { self.socket.as_ref().unwrap().write(…) } }

// after: an unconnected connection has no `send`
struct Disconnected;
struct Connected { socket: TcpStream }
impl Disconnected { fn connect(self) -> io::Result<Connected> { … } }
impl Connected    { fn send(&self) -> io::Result<()> { … } }
```

That is the Rust answer to a whole category of runtime checks, and it is why
"replace the `unwrap`" is often the wrong fix — the right one is upstream.
