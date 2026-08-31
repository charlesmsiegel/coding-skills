# Does this abstraction earn its keep?

## The burden of proof

An abstraction is a claim that two things are the same. The claim costs a name,
a file, an indirection, and a decision the next reader has to reconstruct. It
pays when the sameness is real and stable. It does not pay when the two things
merely have the same shape today.

Default to the concrete version. Add the abstraction on the **third** occurrence,
when you can see which part actually varies — at two, you are guessing, and the
guess is what produces the parameter that means "which caller am I".

## The Rust-specific version

The single biggest source of over-abstraction in Rust codebases is a habit
imported from languages where interfaces are needed for testing:

> "The trait is there so I can mock it."

**In Rust you do not need a trait to substitute an implementation.** A generic
parameter accepts any type that fits; a `#[cfg(test)]` struct can be the fake
with no trait at all; a function parameter can be a closure. The trait buys you
dynamic dispatch and a named contract — if you need neither, it is a file, a
`Box`, and a vtable in exchange for nothing.

## The catalog

| Shape | Why it is usually wrong | Fix |
|---|---|---|
| Trait with one implementor | Abstraction over one thing | Use the concrete type |
| Trait with no implementor | Written for code that never arrived | Delete, or document it as an extension point |
| Fieldless struct + associated fns | A module with a `struct` keyword | Make it a module; export the functions |
| Newtype that only forwards | A name and a maintenance cost | Give it an invariant, or delete it |
| Builder for a two-field struct | Ceremony around `Foo::new(a, b)` | `new`, or `..Default::default()` |
| `Box<dyn Trait>` with one impl | A vtable for a known type | The concrete type |
| Type parameter used once | A concrete type with a letter for a name | Name the type, or `impl Trait` |
| Deep supertrait chain | Every implementor satisfies the whole chain | Flatten; a bound can list several traits |
| Module that only re-exports | Indirection between caller and definition | Import from where the item lives |
| `Rc<RefCell<T>>` for a tree | Runtime borrow checking for a static shape | Indices into a `Vec`, or ownership |
| Generic over a lifetime that could be elided | Noise | Elide it |

## The three questions

For each candidate, in order:

1. **How many concrete things does it abstract over, today, in this repo?** One
   is not an abstraction. Zero is speculative.
2. **What would deleting it cost?** If the answer is "a `use` line changes and
   two files merge", delete it. If it is "four downstream crates break", it is
   load-bearing.
3. **Would a new reader find the real code faster with or without it?** This is
   the one that decides most cases. An abstraction that makes the reader open
   three files to find one function has negative value however elegant it is.

## When the abstraction is right

- **Two real implementors exist now.** Not planned — present.
- **A published extension point.** Downstream crates implement it. Document that
  intent in the trait's docs, or someone will delete it as a single-impl trait.
- **A `dyn` collection.** `Vec<Box<dyn Handler>>` genuinely needs the trait.
- **The orphan rule.** A newtype is the only way to implement a foreign trait
  for a foreign type. This is the case where a pure-wrapper newtype is correct.
- **A real invariant.** A newtype whose constructor validates and whose field is
  private is not a pass-through — it is the only place the invariant can be
  broken, which is the entire point.
- **A boundary you actually stub.** An HTTP client, a clock, a filesystem — if
  the fake exists and the tests use it, the seam is earning its keep.

## DRY versus the wrong abstraction

Duplication is cheaper than the wrong abstraction, and the asymmetry is large:
duplication is visible and local, while a wrong abstraction is a shape every
future change has to be threaded through.

Before merging two similar blocks, ask **why** they are similar:

- Same *idea*, one reason to change → extract.
- Same *shape*, different reasons to change → leave them. They will diverge, and
  the merged version will grow a `mode` parameter that means "which caller am
  I" — the signature that tells you the abstraction was wrong.

The tell that you got it wrong: a boolean or enum parameter added purely so one
caller can get slightly different behaviour out of the shared function. When
that appears, split it back apart.

## Zero-cost is not zero-complexity

Rust makes many abstractions free at runtime: newtypes, enums, iterator chains,
generics, `#[repr(transparent)]`. That is a genuine and important property — and
it is an argument for using them where they clarify, not an argument that they
are always free. They still cost compile time, error-message quality, and the
reader's attention. "Zero-cost" is about the machine code, not about the person.
