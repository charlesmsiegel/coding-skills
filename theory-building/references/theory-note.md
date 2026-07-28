# Writing the theory

Naur's claim, compressed: *a program is a theory held by the people who built it.* The
source text is a projection of that theory. Documentation is a second projection. Neither
recovers the original, which is why a codebase whose builders left behaves like a
codebase that was never designed — the text is intact and the theory is gone.

Generated code starts in that state. No one ever held the theory, so there is nothing to
lose. Writing it down is what creates it.

## Contents

- [What a theory answers](#what-a-theory-answers)
- [The prediction test](#the-prediction-test)
- [Worked examples](#worked-examples)
- [Failure modes](#failure-modes)
- [Where the note lives](#where-the-note-lives)

## What a theory answers

Three questions. Anything else is commentary.

1. **What does this model?** Which part of the world is being represented, and what
   simplification is being made about it. "Every event has exactly one owner" is a
   theory. "Handles events" is not.
2. **Why this shape?** Why these types, these boundaries, this split into functions.
   Especially: what alternative was rejected and why. The rejected alternative is the
   single most useful sentence for whoever extends the code next.
3. **Where does it stop?** The inputs the model is designed for, and the point past which
   it is not merely untested but *wrong*.

## The prediction test

A theory predicts; an ad-hoc answer only responds. The test:

> Pick an input nobody wrote a test for. Does the stated theory tell you what the code
> does with it?

If yes, the theory is real, and untested inputs have a good chance of working — which is
the actual reason a theory matters. If the answer is "run it and see," what exists is a
pile of cases that happen to pass, and every new requirement is a coin flip.

This is also the sharpest available check on generated code, because a pile of cases and
a designed model look identical when both are green.

## Worked examples

**Thin — a shape, not a theory**

> Parses config files and returns a dict. Handles YAML and JSON.

Restates the signature. Predicts nothing. What happens on a duplicate key, an empty file,
a key present but null? Unanswerable, so every one of those is a future bug report.

**Real**

> Models a config as a *layered* value: defaults, then file, then environment, then
> flags, with later layers overriding earlier ones key-by-key (not wholesale). A missing
> key and a null key are different — null is an explicit "unset this," which is why the
> internal type is `Option<Option<T>>`. Layering is fixed at load time; nothing re-reads
> the file, so config is immutable for a process lifetime. Deliberately does not model
> per-request overrides — that needs a different theory.

Now the untested cases have answers. Duplicate keys across layers: last wins. Null in the
env layer: unsets. Hot reload: not supported by construction, and the note says so, so
nobody spends a day discovering it.

**Real, in a smaller change**

> Treats retry eligibility as a property of the *error*, not of the call site — so every
> caller gets identical behavior and nothing needs a `retries=` parameter. Errors carry
> `retryable: bool` from the transport layer. Assumes the transport can classify; if a
> new transport can't, this theory breaks rather than degrades.

Two sentences. Names the model, the consequence, and the breaking condition.

## Failure modes

**Restating the code.** "Loops over users and calls the API for each" is a description of
the text, not the model behind it. If the sentence is derivable by reading the code, it
adds nothing.

**Theory written to fit code already generated.** Backfilling a rationale onto whatever
came out is rationalization, and it produces confident notes for incoherent code. When
the code resists a clean statement, that's a finding — the code is wrong, not the note.
Say "no single theory covers this; it's two concerns tangled," and untangle them.

**Vocabulary with no referent.** `Manager`, `Handler`, `Processor`, `Service`, `Context`,
`Data`, `Info`, `Util`. These are placeholders for concepts not yet found. A type whose
name could belong to any program in any domain is a sign the modeling step was skipped.

**Grandeur.** Three paragraphs of philosophy over a CSV reader. Proportionality applies
here too; a trivial thing gets one line.

## Where the note lives

In the response, so it's read now — and somewhere durable, so it survives:

- **Module docstring / header comment** for a theory that governs one file.
- **ADR or design doc** for one spanning several modules; link it from the code.
- **PR description** for the reasoning behind a change, including the rejected
  alternative.
- **The test file** for boundary conditions. A test named
  `test_null_is_explicit_unset_not_missing` documents the theory and enforces it at once,
  which is the only form of documentation that can't silently rot.
