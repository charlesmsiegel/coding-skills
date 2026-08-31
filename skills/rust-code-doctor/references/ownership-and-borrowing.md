# Ownership, borrowing, and the clones they leave behind

## The question to ask about every `.clone()`

There are exactly two good answers:

1. **The value has to outlive the borrow.** It goes into a struct, crosses a
   thread boundary, is stored in a collection that outlives the caller.
2. **A profiler said this is not hot, and the alternative is a lifetime
   parameter that infects six types.** This is a real and respectable answer.

"The borrow checker complained" is not an answer — it is a description of the
moment the argument stopped. It is also the most common reason a `.clone()`
exists, which is why they cluster in the code that was hardest to write.

## What the borrow checker is usually telling you

| Error | What it usually means | The fix that is not a clone |
|---|---|---|
| "cannot borrow as mutable more than once" | Two parts of one struct are being mutated together | Split the struct; borrow the fields separately (the compiler allows disjoint field borrows) |
| "borrowed value does not live long enough" | A reference is escaping the scope that owns the data | Move ownership outward, or return an owned value from that boundary only |
| "cannot move out of borrowed content" | A `&self` method wants to consume a field | `std::mem::take`, `Option::take`, or an `impl` on `self` instead of `&self` |
| "cannot borrow while also borrowed" in a loop | Reading and writing the same collection | Collect the changes, then apply them after the loop |

The disjoint-field one is worth internalising, because cloning around it is
almost reflexive:

```rust
// does not compile: two &mut self
fn tick(&mut self) { self.update(&self.config); }

// compiles, no clone: the fields are borrowed separately
fn tick(&mut self) {
    let Self { state, config } = self;
    state.update(config);
}
```

## Parameters: what to take

| Take | When |
|---|---|
| `&str` | You only read the text. Accepts `String`, `&String`, `&str`, `Cow`, a literal. |
| `&[T]` | You only read the sequence. Accepts `Vec`, arrays, slices, sub-slices. |
| `&T` | You only read it. |
| `&mut T` | You modify it and the caller keeps it. |
| `T` | You store it, consume it, or transform it into the return value. |
| `impl Into<String>` | Callers have a mix, and one allocation at the boundary is right. |
| `impl AsRef<Path>` | It is a path. This is the standard library's own convention. |

`&String`, `&Vec<T>`, `&PathBuf` and `&Box<T>` are never right in a public
signature: they accept a strict subset of what the borrowed form accepts and buy
nothing. This is the single most common Rust API mistake, and it is mechanical
to fix.

Taking `String` by value when the body only reads it is the mirror image: it
forces every caller to either clone or give up their copy.

## Returning

- Return owned values (`String`, `Vec<T>`) from anything the caller will keep.
- Return `&T` tied to `&self` for accessors — the lifetime is elided and the
  caller can clone if they need to.
- Return `impl Iterator<Item = T> + '_` instead of `Vec<T>` when the caller may
  not need all of it. Add `+ Send` if anyone might spawn it: without it, an
  unrelated change inside the body can silently make the return type non-`Send`
  and break callers.
- `Cow<'_, str>` when the common path returns the input unchanged and the rare
  path allocates. It is under-used and exactly right for normalisation.

## `Rc<RefCell<T>>` and `Arc<Mutex<T>>`

Both move a compile-time check to runtime. `RefCell` turns a borrow error into a
panic at the second borrow; `Mutex` turns it into a deadlock or a poisoned lock.
Sometimes that is the right trade — a genuine graph, a cache shared between
threads — and sometimes it is a data structure that has not been designed yet.

Ask: **who owns this, really?** In most cases where `Rc<RefCell<T>>` appears in
application code, one component owns the data and the others want to *read* it
or *ask it to change*. An index (`Vec<T>` plus `usize` handles) or a
message-passing boundary (a channel) removes the shared mutability entirely, and
both are easier to test.

When it stays, keep the critical section as small as possible and never let a
guard cross an `.await` (see `async-and-concurrency.md`).

## Cheap things people think are expensive

- Passing a struct by value. It is a `memcpy` of the fields, and the optimiser
  usually removes it. Wrapping small structs in `Box` "to avoid the copy" is
  almost always slower.
- A newtype (`struct UserId(u64)`). Zero runtime cost, and it turns argument
  swaps into compile errors.
- An enum with a small payload. It is a tag plus the largest variant.
- Iterator chains. They compile to the same loop.

## Expensive things people think are cheap

- `.clone()` on a `Vec`, `String`, `HashMap`, or anything containing them. It is
  a full deep copy, and inside a loop it is one per iteration.
- `.to_string()` on a `&str` in a hot path.
- `.collect::<Vec<_>>()` in the middle of a chain that is then iterated once.
- `format!` where `write!` into an existing buffer would do.
- An enum with one huge variant: every value is the size of the largest, so a
  `Result<(), BigError>` costs the size of `BigError` everywhere. `Box` the big
  variant.
