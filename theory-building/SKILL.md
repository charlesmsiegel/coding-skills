---
name: theory-building
description: Guard against the "black box" failure mode in generated code — code that compiles, passes its tests, and is still stupid. Use whenever writing or finishing a non-trivial piece of code (a new module, class, service, schema, or any change spanning more than a couple of functions), whenever handing code back to someone who will not read every line, whenever tests pass and the work feels done, whenever introducing a helper or abstraction, and whenever asked "is this good code", "does this look right", "ship it", or "just make it work". Applies to every language. Enforces four gates — state the theory, reuse before inventing, abstraction over repetition-with-variants, and tests as a floor not a bar — then produces a short theory note so a human can review in minutes instead of skipping review entirely. Distinct from python-simplifier / django-simplifier, which critique code the user brings; this one governs code being produced right now.
---

# Theory Building

Code is not the deliverable. The **theory** the code embodies is the deliverable; the
code is a lossy serialization of it. Peter Naur's argument in *Programming as Theory
Building* is that a program's real content lives in the builders' mental model of how
the world maps onto the code — and that when the model is lost, the surviving text
degrades no matter how correct it was.

This matters more, not less, when a model writes the code. Neural networks are strong at
reproducing patterns with variants and weak at forming a *new* abstraction that did not
exist in training. The result passes the compiler, passes the tests, and still encodes
no theory — the software equivalent of a forty-line `isEven` built from a lookup table.
"It works" was already a low bar. **"The tests pass" is lower**, because when the same
model writes the code and the tests, both inherit the same flawed premise and fail
together. Green tests are then evidence of consistency, not of correctness.

The purpose of this skill is not to slow down coding. It's to make the difference
between the two kinds of output visible *before* handoff, and to give a reviewer
something cheaper to check than every line.

## The four gates

Run these on any non-trivial change. Most take seconds. Gate 2 is the one most often
skipped and most often expensive.

### Gate 1 — State the theory

In three sentences or fewer, before or immediately after writing: what mental model does
this code embody? What real-world thing does each type and each function correspond to?
Where is the boundary of the model — the inputs it is *designed* to handle, as opposed
to the ones a test happened to cover?

If the theory can't be stated, there isn't one. What exists instead is an ad-hoc answer
shaped like code, and it will not survive contact with the next requirement. Write the
theory down where it lives with the code — module docstring, header comment, ADR, PR
description — because it is precisely the artifact that gets lost when only the diff is
delivered.

A theory predicts. Test it: *given an input nobody wrote a test for, does the theory tell
you what the code will do?* If the honest answer is "run it and see," the code is a
black box that happens to be readable.

### Gate 2 — Reuse before inventing

The `isEven` failure is not a knowledge failure. It's a *retrieval* failure: generating
plausible code from patterns is easy, and checking whether the thing already exists takes
a deliberate step. Take the step, every time, before writing a helper.

Look, in this order, for the abstraction already existing in:

1. **The standard library** of the language in use.
2. **Dependencies already in the manifest** — read the actual manifest
   (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `pom.xml`), not memory.
3. **The repository itself.** Grep for the concept and its synonyms before adding a
   function. A second `parse_duration` in a codebase is worse than no `parse_duration`.

Never rely on recollection of a library's API surface — recollection is exactly the
faculty that produces confident, plausible, non-existent methods. Open the manifest, read
the installed source, run `--help`. If a reimplementation turns up, delete it and use the
original; that deletion is usually the highest-value edit in the change.

### Gate 3 — Abstraction, not repetition-with-variants

Duplication in generated code is rarely laziness. It's a **signal that a concept is
missing** — the model reached for the nearest pattern three times instead of naming the
thing all three have in common.

Factorization is not abstraction. Hoisting three similar blocks into one function with
five boolean parameters removes characters and adds nothing; the concept is still
missing, now with a worse call site. A real abstraction passes these:

- **It has a name in the domain.** Not `handleDataV2` or `processItems` — a noun or verb
  a domain expert would recognize. If naming it is hard, the concept isn't found yet.
- **It makes unwritten cases expressible.** A good abstraction covers cases nobody
  implemented, because it captures the rule rather than the instances.
- **It reduces the number of things to hold in mind**, not just the line count.

When the concept won't come, say so explicitly rather than papering over it with a
parameterized wrapper. Named, honest duplication is repairable later; a false
abstraction hardens and has to be dismantled first. See `references/abstraction.md`.

### Gate 4 — Tests are a floor, not a bar

Passing tests means no *anticipated* failure occurred. It says nothing about the cases
nobody thought of, and when the tests were generated alongside the code, "nobody thought
of" covers the same blind spots twice.

So don't stop at green. Do this instead:

- **Name the assumptions no test pins down.** Encoding, timezone, ordering, null vs
  absent, concurrency, size limits, clock and locale. Write them into the theory note.
- **Attack the theory, not the code.** Ask what input would make the *model of the
  problem* wrong — not what input would make this line throw. Those are different
  questions, and only the first finds the interesting bugs.
- **Add the cases that pattern-completion would not produce**: the empty case, the
  duplicate case, the adversarial case, the "two of these arrive at once" case.
- **Note deliberately untested territory.** "Retries under partial network failure are
  untested" is worth more to a reviewer than another passing assertion.

## Performance is a theory choice

Quality constraints usually cannot be reached by tuning generated code, because they are
properties of the theory rather than of the text. An O(n²) shape does not become O(n log n)
through micro-optimization; it becomes so by choosing a different model of the problem.

State the characteristics the theory implies — complexity, allocations, round trips,
lock scope — and when a real constraint conflicts with them, redesign rather than patch.
Patching a wrong theory to hit a number is how the spaghetti starts.

## The theory note

Black-box review is what happens when reading everything is the only way to review
anything: it costs too much, so it gets skipped. So make review cheap. End any
non-trivial change with a short note — under ten lines, in the response, plus a durable
copy in the code or PR:

```
Theory:      what this models, in a sentence or two
Reused:      existing library/module used instead of writing new code
New concept: any abstraction introduced, and its name in the domain
Assumes:     conditions relied on that no test enforces
Cost:        complexity / allocations / round trips that matter
Watch:       the part most likely to be wrong, and why
```

The **Watch** line matters most. It aims a reviewer's limited attention at the place it
pays, which is the honest alternative to both "read all 800 lines" and "don't look."

## Proportionality

These gates scale with stakes, and applying them to a five-line throwaway script is its
own kind of failure. A one-off analysis or a scratch file needs a one-line theory note at
most. A shared module, anything with a persistence format, anything others will extend,
anything that will run unattended — full gates.

If a task is being executed on autopilot, that's the signal to slow down, not to speed
up. Anything that promises freedom from thinking should be treated as probably stupid,
including this skill: a checklist run mechanically produces theory notes that are as
hollow as the code they describe. The gates are prompts for judgment, not a substitute.

## An honest limit

A model auditing its own abstractions is partly circular — the same weakness that
produced repetition-with-variants also evaluates whether the repetition was warranted.
This skill narrows the gap; it doesn't close it. The theory note exists precisely because
a human still needs to look, and its job is to make looking cheap enough to actually
happen.

## References

- `references/theory-note.md` — writing the theory, worked examples, common failure modes
- `references/abstraction.md` — factorization vs. abstraction, the reuse search, tests
  that attack the theory
