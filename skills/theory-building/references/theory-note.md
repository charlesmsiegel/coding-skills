# Writing the theory

Naur's claim, compressed: *a program is a theory held by the people who built it.* The
source text is a projection of that theory. Documentation is a second projection. Neither
recovers the original, which is why a codebase whose builders left behaves like a
codebase that was never designed — the text is intact and the theory is gone.

Generated code starts in that state. No one ever held the theory, so there is nothing to
lose. Writing it down is what creates it.

## Contents

- [What a theory answers](#what-a-theory-answers)
- [The two tests](#the-two-tests)
- [Say which reading you took](#say-which-reading-you-took)
- [Worked examples](#worked-examples)
- [Failure modes](#failure-modes)
- [Where the note lives](#where-the-note-lives)

## What a theory answers

Four questions. Anything else is commentary. The first three are Naur's own test for
whether a person *has* the theory; the fourth is where his first question bottoms out.

1. **What does this model?** Which part of the world is being represented, and what
   simplification is being made about it. "Every event has exactly one owner" is a
   theory. "Handles events" is not.
2. **Why this shape?** Why these types, these boundaries, this split into functions.
   Especially: what alternative was rejected and why. The rejected alternative is the
   single most useful sentence for whoever extends the code next.
3. **What change does it absorb?** Given the next requirement someone will plausibly
   ask for, does the model already have a place for it? Naur treats this as the
   defining capability — "a person having the theory must already be prepared to respond
   to the kinds of questions and demands that may give rise to program modifications."
4. **Where does it stop?** The inputs the model is designed for, and the point past which
   it is not merely untested but *wrong*.

## The two tests

A theory predicts; an ad-hoc answer only responds. Two checks, failing differently:

> **Prediction.** Pick an input nobody wrote a test for. Does the stated theory tell you
> what the code does with it?

If yes, the theory is real, and untested inputs have a good chance of working — which is
the actual reason a theory matters. If the answer is "run it and see," what exists is a
pile of cases that happen to pass, and every new requirement is a coin flip.

> **Modification rehearsal.** Name the next feature someone will ask for. Does it land as
> an extension, or does it need a special case welded to the side?

The second catches what the first misses: a model can cover every input and still have no
room to grow, which is the state a codebase is in right before its structure starts
dying. A theory that accounts only for what was already built is a description.

Both are the sharpest available checks on generated code, because a pile of cases and a
designed model look identical when both are green.

## Say which reading you took

A request almost never determines a single theory. "Cache the user lookups" admits at
least a per-request cache, a process-wide cache with a TTL, and a write-through cache with
invalidation — all faithful to the words, with completely different failure modes. The
code implements exactly one and gives no sign that the others were ever live.

This gap is the normal case rather than an edge case; a study of beginners prompting a
code model found both sides systematically misreading each other, with participants
convinced they had described the problem "the best way to say it" while the model built
something else. The person reading your diff has the same problem in reverse: they cannot
recover the readings you discarded.

So state the one you took and the one you rejected. One clause each is enough — *"process
wide with a 60s TTL, not per-request, because the hit rate across requests is the whole
point"* — and it is the sentence that stops someone re-litigating the design six months
later without knowing it was ever litigated.

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

One test decides what goes in, from Cockburn's commentary on Naur: include **that which
helps the next reader build an adequate theory of the program.** Not what the code does —
they can read that, and a note that restates it burns the attention you were trying to
save. What they could not have reconstructed. The same rule sets you free at the other
end: documentation "cannot — and so need not — say everything."

In the response, so it's read now — and somewhere durable, so it survives:

- **Module docstring / header comment** for a theory that governs one file.
- **ADR or design doc** for one spanning several modules; link it from the code.
- **PR description** for the reasoning behind a change, including the rejected
  alternative.
- **The test file** for boundary conditions. A test named
  `test_null_is_explicit_unset_not_missing` documents the theory and enforces it at once,
  which is the only form of documentation that can't silently rot.
