# Traits, generics, and when each is the wrong tool

## The three ways to be polymorphic

| Form | Dispatch | Use when |
|---|---|---|
| `fn f<T: Trait>(x: T)` | static, monomorphised | the type is known at the call site (almost always) |
| `fn f(x: impl Trait)` | static, same thing | you do not need to name `T` (argument position) |
| `fn f(x: &dyn Trait)` | dynamic, vtable | heterogeneous collections, plugin boundaries, avoiding code bloat |

Default to generics. Reach for `dyn` when you genuinely need values of different
types in one collection (`Vec<Box<dyn Handler>>`), when the set of types is
open at runtime, or when monomorphisation is measurably bloating the binary.

`impl Trait` in *return* position is a different thing again: it names an
anonymous concrete type. Remember that the auto traits leak — a
`-> impl Iterator<Item = T>` that happens to capture an `Rc` becomes non-`Send`,
and every caller that spawns it breaks on an unrelated change inside the body.
Write `-> impl Iterator<Item = T> + Send` for anything public.

## Does this trait earn its keep?

A trait with one implementor is the single most common over-abstraction in Rust
codebases, imported from languages where interfaces are needed for testing.
**They are not needed for testing in Rust.** A generic parameter accepts any type
that fits, and the fake can be a `#[cfg(test)]` struct with no trait involved:

```rust
// The trait exists only so tests can substitute a fake:
trait Clock { fn now(&self) -> Instant; }
struct SystemClock;
impl Clock for SystemClock { … }

// It was not needed. This is enough:
fn schedule<C: Fn() -> Instant>(now: C) { … }
// or, if the seam is bigger, a #[cfg(test)] type and a generic parameter.
```

Keep the trait when:

- **Two implementors exist now** (not "will exist").
- It is a **published extension point** — downstream crates implement it. Say so
  in the docs, or the next reviewer will delete it.
- Values of different types must live in one collection or behind one pointer.
- It is a **marker or a capability** the compiler checks (`Send`, `Sealed`).

Delete it when there is one implementor and the crate is the only consumer. The
concrete type is shorter, the errors are better, and the indirection was buying
a substitution nobody performs.

## Trait design

- **Few methods.** A trait with fifteen methods forces every implementor to
  write fifteen, and the "refused bequest" shape — half of them `todo!()` —
  follows. Split it.
- **Default methods** for anything derivable from the required ones. That is how
  `Iterator` gets away with sixty methods and one required.
- **Associated types when there is exactly one right choice per implementor**
  (`Iterator::Item`); **generic parameters when an implementor may want
  several** (`From<T>`).
- **Object safety** is a constraint, not a goal. A trait with generic methods or
  `Self` in return position cannot be a `dyn` object — fine, unless you needed
  one. Check before designing around it.
- **Sealing** (`trait Foo: private::Sealed`) lets you add methods later without
  a breaking change, for traits you do not want implemented downstream.

## Blanket impls

```rust
impl<T: Display> MyTrait for T { … }
```

Powerful and hard to undo: it forecloses every future `impl MyTrait for
SomeSpecificType`, because that would overlap. Coherence has no specialisation
on stable Rust. Write blanket impls deliberately, and prefer a narrower bound.

## The orphan rule, and living with it

You may implement a trait for a type only if you own the trait or the type. When
you own neither, the newtype is the answer:

```rust
struct Wrapper(ExternalType);
impl ExternalTrait for Wrapper { … }
```

This is the one place a pure pass-through newtype is right. Add `Deref` if the
wrapper is meant to be transparent — but not on a newtype that enforces an
invariant, where `Deref` would let callers bypass it.

## Generics that are not pulling their weight

- **A type parameter used once in the signature.** `fn f<T: AsRef<str>>(x: T)`
  where `x` is only read is fine (it accepts more callers). `fn f<T>(x: T) ->
  Vec<T>` with a single call site is a concrete type with a letter for a name,
  and it costs a monomorphised copy per instantiation.
- **A `where` clause repeating what the bound already says.**
- **Generic over a lifetime that could be elided.** `fn f<'a>(x: &'a str) ->
  &'a str` is `fn f(x: &str) -> &str`.
- **Deep supertrait chains.** Each level is another thing every implementor must
  satisfy and another file the reader has to open. Traits compose by being
  implemented side by side; a bound can list several (`T: Read + Seek`).

## Derives worth having on every public type

`#[derive(Debug)]` is not optional in practice. Without it the type cannot
appear in a panic message, a test failure, or `dbg!` — and neither can anything
that contains it, so the omission propagates outward through the crate.

Then, in rough order of value: `Clone` where copying is meaningful, `PartialEq`
+ `Eq` where equality is, `Hash` if it will be a map key, `Default` if there is
an obvious empty value, `Copy` for fieldless enums and small POD structs,
`PartialOrd`/`Ord` only if there is a real ordering.
