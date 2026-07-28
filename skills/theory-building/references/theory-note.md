# Writing the theory

Naur's claim, compressed: *a program is a theory held by the people who built it.* The
source text is a projection of that theory. Documentation is a second projection. Neither
recovers the original, which is why a codebase whose builders left behaves like a
codebase that was never designed — the text is intact and the theory is gone.

Generated code starts in that state. No one ever held the theory, so there is nothing to
lose. Writing it down is what creates it.

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

Check the result with SKILL.md's two Gate 1 tests — prediction and modification
rehearsal. They matter most here, on generated code, because a pile of cases and a
designed model look identical when both are green.

## Say which reading you took

Gate 1 requires naming the reading you chose; here is what that looks like. "Cache the
user lookups" admits at least a per-request cache, a process-wide cache with a TTL, and a
write-through cache with invalidation — all faithful to the words, with completely
different failure modes. The code implements exactly one and gives no sign the others
were ever live; a study of beginners prompting a code model found exactly this gap, each
side convinced it had understood the other. One clause per side is enough — *"process-wide
with a 60s TTL, not per-request, because the hit rate across requests is the whole
point"* — the sentence that stops the design being re-litigated blind six months later.

## Worked examples: the prose theory

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

## Worked example: the full note

The change: add client-side rate limiting to a service's outbound client for a
third-party API, after the provider started returning 429s.

```
Theory:      Rate is a property of the API key, not of the caller — one shared
             token bucket per key, process-wide, so N workers can't multiply the
             budget. Callers block with a deadline rather than error, because our
             callers are batch jobs that prefer late to failed.
Instead of:  A limiter per call site (simpler, but N workers × limit = a ban), or
             erroring when out of budget (right for interactive callers; ours aren't).
Reused:      `limits` (already in pyproject.toml) for the bucket math; new code is
             ~30 lines of wiring around it.
New concept: RateBudget — the per-key budget a caller acquires from before sending.
Assumes:     Single process. The budget is not shared across hosts; a second
             instance silently halves the headroom, and nothing detects that.
Cost:        One lock acquisition per request — negligible next to the network call.
Watch:       X-RateLimit-Remaining headers are ignored; we trust the configured
             limit. If the provider lowers limits dynamically, this breaks first.
```

Every line carries something the diff cannot: the per-key-not-per-caller decision, the
rejected error-on-exhaustion design, the multi-instance trap, the header deliberately
not read. A reviewer who reads only this knows where to look.

## The hollow version

The same change, the same template, filled in mechanically:

```
Theory:      Adds a rate limiter so we don't exceed the API's rate limits.
Instead of:  Not rate limiting, which causes 429 errors.
Reused:      Standard library.
New concept: RateLimiter class for rate limiting.
Assumes:     The API enforces rate limits.
Cost:        Minimal overhead.
Watch:       Nothing in particular; tests pass.
```

The test that separates the two: cover the diff and ask what the note tells you that the
code could not. The real note answers "what happens when a second instance starts" and
"why block instead of error"; the hollow one answers nothing, because every line is
derivable from the diff or the ticket in seconds. A hollow note is worse than none — it
spends the reviewer's trust while aiming their attention nowhere.

## The three sentences and the seven fields

Gate 1's theory statement — three sentences or fewer — and this template are not two
artifacts. The three sentences *are* the **Theory** line; the note starts by pasting
them. The other six fields carry what a theory statement, and a diff, cannot: the
rejected reading (**Instead of**), the search that was run (**Reused**), the concept that
now exists (**New concept**), and the boundaries no test enforces (**Assumes**, **Cost**,
**Watch**). If writing the note feels like starting over, Gate 1 was skipped; done
honestly, it is one minute of transcription plus six honest lines.

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

The criterion for what goes in — Cockburn's "that which helps the next reader build an
adequate theory of the program" — is in SKILL.md. What follows is where the note goes:
in the response, so it's read now, and somewhere durable, so it survives.

- **Module docstring / header comment** for a theory that governs one file.
- **ADR or design doc** for one spanning several modules; link it from the code.
- **PR description** for the reasoning behind a change, including the rejected
  alternative.
- **The test file** for boundary conditions. A test named
  `test_null_is_explicit_unset_not_missing` documents the theory and enforces it at once,
  which is the only form of documentation that can't silently rot.

---

Sources: Peter Naur, "Programming as Theory Building" (1985); Nguyen et al., "How
Beginning Programmers and Code LLMs (Mis)read Each Other" (2024, arXiv:2401.15232) —
120 novices on small problems, cited as evidence that readings diverge, not as a constant.
