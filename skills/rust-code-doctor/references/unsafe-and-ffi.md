# Reviewing `unsafe`

`unsafe` does not turn checking off. It moves the obligation from the compiler
to a human, and the only artifact of that human's reasoning is a comment. So the
review starts there.

## The three questions

1. **Is the safety argument written down?** A `// SAFETY:` comment naming the
   invariant, and why it holds *here*. Without one, nothing can be reviewed and
   nothing can be re-checked after the next edit.
2. **Is the argument true?** Read it against the code, not just for its
   existence. A stale SAFETY comment is worse than none — it stops the next
   reader looking.
3. **Is the block as small as it can be?** Every safe line inside an `unsafe`
   block is a line whose invariants the argument has to cover. Narrow it to the
   operations that need it.

```rust
// SAFETY: `idx` was bounds-checked against `self.len` on the line above, and
// `self.buf` is never reallocated while `&self` is held.
let value = unsafe { self.buf.get_unchecked(idx) };
```

## `unsafe fn` versus `unsafe {}`

They mean different things and are reviewed differently.

- **`unsafe {}`** — *this code* discharges an obligation. The argument is local.
- **`unsafe fn`** — *the caller* must discharge an obligation. That obligation
  is part of the public contract and belongs in the docs under a `# Safety`
  heading. An `unsafe fn` with no `# Safety` section is an unstated contract;
  `clippy::missing_safety_doc` catches it.

If a function is `unsafe` only because its body contains an `unsafe` block whose
invariant the function itself guarantees, it should not be `unsafe fn` — it
should be a safe function wrapping an unsafe block. That is the whole point of
the encapsulation.

## `unsafe impl Send` / `Sync`

This is the highest-stakes `unsafe` in most codebases, because it is a promise
about every use of the type, forever, made once at a single line.

`unsafe impl Send for T` claims T can be moved to another thread.
`unsafe impl Sync for T` claims `&T` can be shared between threads.

Both need a SAFETY comment naming **which field** made the auto-derive fail
(almost always a raw pointer or a `Cell`) and **why** sharing it is sound — a
lock, an atomic, an ownership discipline the type enforces. "It seemed fine" is
how a data race gets shipped, and a data race in Rust is undefined behaviour,
not a flaky test.

## The specific operations

| Operation | The invariant | The safe alternative |
|---|---|---|
| `get_unchecked(i)` | `i < len` | `get(i)` — the bounds check is one predictable branch |
| `unwrap_unchecked()` | the value is `Some`/`Ok` | `unwrap()` |
| `from_utf8_unchecked` | the bytes are valid UTF-8 | `from_utf8` |
| `set_len(n)` | every element below `n` is initialised | `resize`, `extend`, `truncate` |
| `assume_init()` | the value has been written | keep it in `MaybeUninit` until it has |
| `transmute::<A, B>` | the layouts are identical *and* every bit pattern is valid for B | `as`, `to_le_bytes`, `bytemuck` |
| `mem::uninitialized()` | nothing — it is instant UB for most types | `MaybeUninit` |
| `mem::forget` | you meant to leak | `ManuallyDrop`, which says so in the type |
| `static mut` | no two accesses overlap, ever | `AtomicU64`, `OnceLock`, `Mutex` |

`static mut` deserves a note: Rust 2024 makes references to it a hard error, so
it is also an edition-migration blocker. Nearly every use is an atomic or a
`OnceLock`.

`transmute` deserves another: it is the one where "it works" and "it is sound"
diverge most often. `transmute::<&[u8], &[u32]>` compiles and violates alignment.
Check the *specific* pair of types, not the general shape.

## FFI

The boundary is where `unsafe` genuinely earns its place. What to check:

- **Every pointer from C**: is it non-null, aligned, valid for the claimed
  length, and alive for the claimed lifetime? Nothing in the signature says so.
- **Ownership**: who frees it? A `*mut c_char` returned from C is usually not
  yours; a `CString::into_raw` you hand to C must come back through
  `from_raw` or it leaks.
- **`repr(C)`** on every struct that crosses. A `#[repr(Rust)]` struct has no
  guaranteed layout at all.
- **Panics must not cross.** Unwinding into C is undefined behaviour. Wrap the
  Rust side in `catch_unwind`, or declare the function `extern "C-unwind"` only
  if you have actually thought about it.
- **Strings**: C strings are NUL-terminated bytes, Rust strings are UTF-8 with a
  length. `CStr::from_ptr` is unsafe for both reasons.

The shape that keeps this manageable: one thin, entirely `unsafe` module that
does nothing but translate, and a safe API over it that the rest of the crate
uses. Then the review surface is that one module.

## When `unsafe` is the wrong answer

- To avoid a bounds check that has not been profiled. `get_unchecked` in a loop
  the optimiser could already prove safe buys nothing and costs the audit.
- To implement a self-referential struct. Use `Pin`, an arena with indices, or
  `ouroboros` — hand-rolled self-reference is where the hardest UB lives.
- To get a global mutable. `OnceLock`, `LazyLock`, atomics.
- Because the borrow checker said no. That is a design signal, not an obstacle.
