# Reading Rust critically

The per-function checklist, and how to decide whether a detector finding is
worth anyone's time.

## Before you read a line

1. **What edition?** `find_cargo_issues.py` answers this. On 2015 the compiler
   was never going to catch what 2018 catches; on 2021 it was. Every other
   judgment is calibrated by this.
2. **Does it compile?** `cargo check --all-targets`. A borrow error the author
   has not seen yet is worth more than everything in this guide, and half the
   "designs" in a non-compiling file are provisional.
3. **Is there a test suite?** `find_untested_modules.py`. If not, the review
   changes shape: the finding is "there is no safety net", and every proposed
   refactor is conditional on building one.
4. **What churns?** `git log --since="1 year ago" --name-only --pretty=format: |
   grep '\.rs$' | sort | uniq -c | sort -rn | head -30`. Effort goes where the
   code changes. A file nobody has touched in two years is not where your review
   pays back.

## Per function

Ask these in order. Stop at the first one that is a real problem — fixing it
usually dissolves the ones below.

1. **What can this return that the caller does not expect?** A `Result` whose
   error type is `String` or `Box<dyn Error>` tells the caller nothing they can
   act on. A function that panics is a function whose signature lies.
2. **Where does it panic?** Every `unwrap`, `expect`, `[i]`, `a - b` on
   unsigned, `a / b`, and `slice[a..b]`. For each: is the invariant that makes
   it safe *visible from this function*? If the answer is "the caller always…",
   the type is wrong.
3. **What does it own that it does not need to own?** `String` where `&str`
   works, `Vec<T>` where `&[T]` works, `self` where `&self` works. Each one
   pushes an allocation onto every caller.
4. **What does it clone, and why?** The honest answers are "the value outlives
   the borrow" and "profiling said this was fine". "The borrow checker
   complained" is an unfinished argument, not a reason — and it is the most
   common one.
5. **Can the type make this unrepresentable?** An `Option` that is always `Some`
   after construction, a `bool` pair where three of four combinations are
   invalid, a `String` that is always one of five values. Rust's enums cost
   nothing at runtime; a state that cannot be constructed needs no test.
6. **Is the nesting load-bearing?** Three levels usually means two early
   returns were not written. `let … else`, `?`, and an early `return` flatten
   most bodies to one level.
7. **Who else can reach this data?** `pub` fields, `Rc<RefCell<T>>`, a method
   handing out `&mut` to an internal collection. Rust's guarantees are about
   who can *write*, and each of these gives that away.

## Triaging a detector finding

For each finding, ask two questions in this order.

**Is it real?** Read the code. The detectors read syntax and are deliberately
conservative, but "conservative" is not "correct" — a `.clone()` inside a loop
may be on a value the loop consumes, and a trait with one implementor may be a
published extension point. A finding you cannot confirm from the code is a
finding you do not report.

**Does fixing it pay?** Rank on three axes:

| | High | Low |
|---|---|---|
| **Blast radius** | A panic on a code path users reach | A `low` naming nit in a private helper |
| **Churn** | The file changed 40 times this year | Untouched since 2021 |
| **Cost to fix** | One line, mechanical | A signature change through 30 call sites |

The order that follows from this: **correctness bugs that can panic or
deadlock** → **API shapes that are breaking to change later** (a missing
`Debug`, a `pub` field, an exhaustive public enum) → **allocation and ownership**
→ **idioms and naming**. Style findings in cold code are the last thing, and
often the right answer is to leave them.

## What not to report

- A `.clone()` in a constructor that runs once at startup.
- `unwrap()` in a test, a build script, or an example.
- A long `match` on an enum. Exhaustiveness *is* the design.
- `unsafe` in an FFI shim, where the alternative is not writing the binding.
- Anything in generated code (`build.rs` output, `prost`, `bindgen`).
- A finding you have not read the surrounding code for.

## The Rust-specific bias

Prefer making the compiler responsible. A newtype, an enum that replaces a
`bool` pair, a `#[non_exhaustive]`, a `[lints]` entry, or one `#![deny]` removes
a *class* of bug permanently — where a fix removes one instance. When a review
finds three instances of the same mistake, the deliverable is the rule that
makes the fourth impossible.
