# The design-smell catalog, in Rust

The classic smells, what each looks like in Rust specifically, and how to triage
the detector's candidates for them.

## Long function / deep nesting

Rust gives you more early-exit forms than most languages, and deep nesting
usually means none of them were used.

```rust
// before
fn handle(req: Request) -> Response {
    if let Some(user) = auth(&req) {
        if user.active {
            if let Ok(body) = parse(&req) { … }
            else { Response::bad_request() }
        } else { Response::forbidden() }
    } else { Response::unauthorized() }
}

// after
fn handle(req: Request) -> Response {
    let Some(user) = auth(&req) else { return Response::unauthorized() };
    if !user.active { return Response::forbidden() }
    let Ok(body) = parse(&req) else { return Response::bad_request() };
    …
}
```

`let … else`, `?`, and an early `return` flatten most bodies to one level. When
they do not, the function is doing two things.

## Flag parameter

```rust
// before — what is `true`?
render(&doc, true, false);

// after
enum Numbering { On, Off }
enum Wrapping { Hard, Soft }
render(&doc, Numbering::On, Wrapping::Soft);
```

Two-variant enums are free at runtime and the call site becomes readable. When
the flag selects *behaviour* rather than a mode, split the function instead.

## Data clump

Three or more parameters that keep travelling together are a type that has not
been named. In Rust the struct costs nothing:

```rust
fn connect(host: &str, port: u16, timeout: Duration, retries: u32)
fn probe(host: &str, port: u16, timeout: Duration, retries: u32)
// →
struct Endpoint { host: String, port: u16, timeout: Duration, retries: u32 }
```

## Primitive obsession

Adjacent parameters of the same primitive type can be swapped at a call site and
still compile. Newtypes make the swap a type error:

```rust
fn transfer(from: u64, to: u64, cents: u64)      // three ways to get this wrong
struct AccountId(u64);
struct Cents(u64);
fn transfer(from: AccountId, to: AccountId, amount: Cents)
```

## Type switch / `if` ladder

An `else if` chain comparing one value against constants is a `match`, and on an
enum the compiler then proves the cases are covered:

```rust
if kind == "json" { … } else if kind == "yaml" { … } else if kind == "toml" { … }
// →
enum Format { Json, Yaml, Toml }
match format { Format::Json => …, Format::Yaml => …, Format::Toml => … }
```

The payoff is the day someone adds `Format::Csv` and the compiler lists every
place that has to handle it. A `_` arm gives that away — keep `_` for genuinely
open sets (an integer, an `#[non_exhaustive]` enum from another crate).

## Feature envy

A method that reaches into another type more than into `self` belongs on that
type. Rust makes this easy even when the type is not yours: an extension trait.

```rust
trait DurationExt { fn as_human(&self) -> String; }
impl DurationExt for Duration { fn as_human(&self) -> String { … } }
```

## God struct / god impl

A struct with fifteen fields usually has three structs inside it, and the
subsets that change together are the seam. The nesting also enables *partial
borrows* — the compiler lets you hold `&mut a.x` and `&a.y` at once — which a
flat struct of that width fights you on constantly.

An inherent `impl` with twenty methods is the same problem seen from the
behaviour side.

## Temporary field

A field only one method ever reads is a local variable that outlives its use,
and every constructor has to initialise it. Pass it as a parameter.

In Rust the stronger version is a field that is `Option` only because it is
absent during construction. That is the type-state pattern asking to exist: a
`Builder` that produces a `Config` where the field is not optional at all.

## Refused bequest

A trait impl whose methods mostly `todo!()` or do nothing: the type does not
want this trait. Split the trait so the type can implement the part it means, or
drop the impl. A `dyn Trait` that panics on half its methods is a runtime error
waiting for the right caller.

## Message chain

`a.b().c().d().e()` walking someone else's object graph is a coupling to a shape
you do not own. Ask the first object for what you actually want.

Iterator adaptor chains are *not* this smell — `xs.iter().filter(..).map(..).
collect()` is one operation described in stages, and each stage names itself.

## Shotgun surgery

One conceptual change requiring edits in eight files. In Rust this is usually a
missing enum: the eight files each have their own `match` on a string or an
integer, and the change is adding a case to all of them. One enum makes the
eight `match`es exhaustive and the compiler finds them for you.

## Speculative generality

A trait with no implementor, a type parameter with one instantiation, an
`Option` field never set, a `mode` argument every caller passes the same value
for. Delete it. Git remembers, and the version that arrives when the need is
real will be shaped by the need rather than by the guess.

---

## Triaging a candidate

The detectors report shapes. Before reporting one as a finding:

1. **Read the code.** A "data clump" of three parameters may be three unrelated
   things that happen to appear together twice.
2. **Check the churn.** `git log --follow` on the file. A smell in code nobody
   touches costs nothing; the same smell in a file edited weekly costs every
   week.
3. **Price the fix.** A newtype through thirty call sites is a day. A `let …
   else` is a minute. Say which you are proposing.
4. **Check for a test.** If the code is untested, the finding is "this needs a
   characterization test", and the refactor comes after.
