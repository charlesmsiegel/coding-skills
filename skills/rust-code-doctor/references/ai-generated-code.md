# Reviewing AI-generated Rust

## The starting condition

It compiles and the tests pass. That is where the review begins, not evidence
that it is finished — and in Rust it is a weaker signal than it looks, because
the compiler's approval covers memory safety and types and says nothing about
whether the code does the right thing.

Apply the same bar as any other change. The difference is *where* to look: at
the parts the compiler could not check.

## The tells

**`.unwrap()` everywhere.** The most reliable one. Generated Rust reaches for
`unwrap` because it makes the types line up, and the result is a function that
compiles and panics. Check every one against `error-handling.md`'s table — in
particular, an `unwrap` inside a function that already returns `Result` is never
right, and it is the most common single defect in generated Rust.

**`.clone()` everywhere.** The borrow checker said no and the clone made it say
yes. Each one is an argument that stopped early. Some are correct; the question
for each is which of the two good reasons applies (see
`ownership-and-borrowing.md`).

**`todo!()` / `unimplemented!()` satisfying a trait.** The impl compiles, the
trait is "implemented", and the first caller to reach that method panics. Also
watch for the quieter version: a method that returns `Default::default()` or an
empty `Vec` with no comment.

**An ignored parameter.** A `config`, `options` or `ctx` parameter the body
never reads. Every caller's setting is silently discarded. An underscore prefix
silences the warning without answering the question.

**Plausible constants.** `const TIMEOUT_SECS: u64 = 30;`, `MAX_RETRIES = 3`,
a buffer size of 8192. Each is *a* reasonable number and none of them came from
a measurement or a spec. Ask where each came from; the answer is often that it
should be configurable, or that the right value is very different.

**`Box<dyn Error>` in a library signature.** Generated code reaches for it
because it always works. It also erases exactly what a caller needs to branch
on, and narrowing it later is a breaking change.

**A trait with one implementor.** Generated from the habit of interface-first
design in other languages. In Rust the trait is not needed for testing (see
`overengineering-and-abstraction.md`).

**Hallucinated APIs.** Less common in Rust than elsewhere, because the compiler
rejects them — but *plausible* API use that compiles and is wrong does happen:
`sort_unstable` where stability mattered, `HashMap` where iteration order is
relied on later, `String::len` used as a character count (it is bytes),
`chars().count()` in a loop that is now O(n²).

**Prose that describes code that was not written.** "In a real implementation
you would…", "This is a simplified version…", "For now, we…". Each marks a place
where the model knew the real thing was harder.

**Tests that mirror the implementation.** A test that computes the expected
value the same way the code does passes whatever the code does. Look for tests
whose expected values are literals someone thought about.

## The review procedure

1. **`cargo check --all-targets`** over the whole project, not just the changed
   files. A change can compile in isolation and break a caller.
2. **`cargo clippy --all-targets`.** With type information it catches the
   `needless_clone`, the `redundant_closure`, the `manual_map` — the whole
   category this skill's syntactic detectors can only guess at.
3. **`analyze_diff.py`** for findings on the changed lines, plus
   **`find_ai_scaffolding.py`** over the changed files.
4. **Read the tests hard.** For each, ask what would have to break for it to
   fail. Then break the implementation on purpose and confirm it does.
5. **Check every claim in the PR description against the diff.** "Handles the
   empty case" — does it? Find the line.
6. **Check the error paths.** Generated code is systematically weaker there,
   because the happy path is what the prompt described.

## What not to do

Do not reject the code for being generated. Do not rewrite it to match your
style. The bar is the same as for any change: does it do what it claims, does it
fail safely, and can the next person maintain it.

The one thing to insist on: **an author who can answer questions about it.** If
nobody in the review can explain why a particular `.clone()` is there or where a
constant came from, that is the finding — not the code, the fact that it landed
without anyone knowing.
