---
name: theory-building
description: Guard against the "black box" failure mode in generated code — code that compiles, passes its tests, and is still stupid. Use whenever writing or finishing a non-trivial piece of code (a new module, class, service, schema, or any change beyond a couple of functions), whenever modifying code whose design you do not already understand, whenever handing code back to someone who will not read every line, whenever tests pass and the work feels done, and whenever asked "is this good code", "does this look right", or "just make it work". Applies to every language. Enforces four gates — state the theory, reuse before inventing, abstraction over repetition-with-variants, tests as a floor not a bar — then produces a short theory note so a human can review in minutes instead of skipping review. Distinct from python-code-doctor, which critiques code the user brings; this one governs code being produced right now.
---

# Theory Building

Code is not the deliverable. The **theory** the code embodies is the deliverable; the
code is a lossy serialization of it. Peter Naur's argument in *Programming as Theory
Building* is that a program's real content lives in the builders' mental model of how
the world maps onto the code — and that when the model is lost, the surviving text
degrades no matter how correct it was.

Naur's evidence for this is worth holding onto, because it is a finding rather than a
metaphor. A team inheriting a compiler was given full documentation, annotated program
texts, extensive written design discussion, *and* direct access to the original authors.
They still proposed extensions that "effectively destroyed its power and simplicity,"
while the authors "were able to spot these cases instantly" and offered solutions framed
entirely within the existing structure. The text was complete. The theory was not in it.

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

Naur's test for whether someone *has* a theory is behavioral, not textual. Three
capabilities, and the third is the one nobody checks:

1. **Map it to the world.** For each part of the code, name the aspect of the problem it
   matches — and conversely, for each aspect of the problem, say where it lives in the
   code. Include where the model *stops*: the inputs it is designed for, as opposed to
   the ones a test happened to cover.
2. **Justify it.** Say why each part is what it is, and what alternative was rejected.
   Naur is blunt that this bottoms out in judgment rather than derivable rules — which is
   exactly why it has to be recorded rather than re-derived later by someone with less
   context.
3. **Absorb a change.** Take a plausible next requirement and say how the theory
   accommodates it. "In a certain sense there can be no question of a theory
   modification, only of a program modification" — someone who holds the theory is
   *already* prepared for the demands that will arrive.

In three sentences or fewer, before or immediately after writing. If the theory can't be
stated, there isn't one. What exists instead is an ad-hoc answer shaped like code, and it
will not survive contact with the next requirement. Write it down where it lives with the
code — module docstring, header comment, ADR, PR description — because it is precisely
the artifact that gets lost when only the diff is delivered.

Two cheap checks, and they fail differently:

- **Prediction.** Given an input nobody wrote a test for, does the theory tell you what
  the code does? If the honest answer is "run it and see," the code is a black box that
  happens to be readable.
- **Modification rehearsal.** Name the next feature someone will plausibly ask for. Does
  it land as a natural extension, or does it need a special case bolted to the side? A
  theory that only accounts for what was already built is a description, not a theory.

One more thing the request itself cannot supply: **which reading you chose.** A prompt or
ticket almost always underdetermines the model — it admits several coherent theories, and
the code silently implements one of them. The reader cannot recover the discarded readings
from the text, so say which one you took and which you rejected.

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

When a library covers most of the need but not all, wrap it and note the gap rather than
reimplement: the missing piece goes on the note's **Assumes** or **Watch** line so nobody
mistakes the wrapper for full coverage. The full search order and synonym tactics are in
`references/abstraction.md`.

### Gate 3 — Abstraction, not repetition-with-variants

Duplication in generated code is rarely laziness. It's a **signal that a concept is
missing** — the model reached for the nearest pattern three times instead of naming the
thing all three have in common.

Factorization is not abstraction. Hoisting three similar blocks into one function with
five boolean parameters removes characters and adds nothing; the concept is still
missing, now with a worse call site. A real abstraction passes four checks:

- **It has a name in the domain.** Not `handleDataV2` or `processItems` — a noun or verb
  a domain expert would recognize. If naming it is hard, the concept isn't found yet.
- **It makes unwritten cases expressible.** A good abstraction covers cases nobody
  implemented, because it captures the rule rather than the instances.
- **It reduces the number of things to hold in mind**, not just the line count.
- **Ideally, it makes illegal states unrepresentable** — the strongest form, where the
  type itself refuses the combinations that shouldn't exist.

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

Concrete prompts for attacking the theory are in `references/abstraction.md`.

## Changing code whose theory you don't have

Most work is not new code. It's a modification to code whose builders are gone, or were
never human — and that puts you permanently in the position of Naur's second team: fluent
in the text, without the theory behind it. Naur is uncomfortably direct about this;
rebuilding a theory from the documentation alone is "strictly impossible," and doing it
from the text is "a difficult, frustrating, and time consuming activity."

The decision this changes is sharper than it looks. A requested modification can usually
be implemented **many different ways, all correct**. Some extend the existing theory
naturally; others are, in Naur's phrase, "unintegrated patches on the main part of the
program." Both pass the tests. Only someone holding the theory can tell them apart — which
is why a diff that looks locally reasonable is the normal way good structure dies. A
large real-time system he describes accumulated a decade of such changes until "the
original powerful structure was still visible, but made entirely ineffective by amorphous
additions."

So before changing code you didn't write:

- **Recover the theory first, and write the one sentence.** What does this module think
  the world is? Read for the model, not the syntax. Check the sentence against the code
  rather than against what the code should have been.
- **Prefer the change the existing structure already anticipates.** If a facility is
  there, use it. The instinct to add a parallel path beside it is exactly the failure
  Naur's inheritors kept making with full documentation in hand.
- **When you can't find the theory, say so and shrink the change.** An honest "I could not
  determine why this retries twice, so I left it and added the new case alongside" is
  worth more than a confident refactor built on a guessed model.
- **Don't restructure what you don't understand.** Tidying code whose theory you haven't
  recovered is how power and simplicity get destroyed by someone being helpful.

Naur's own conclusion is more radical than most teams will accept — that discarding the
text and re-solving the problem often beats reviving it, "at no higher, and possibly
lower, cost." That is rarely your call to make unilaterally. Surface it as an option with
its reasoning; don't act on it. See `references/inherited-code.md`.

## Write so the reviewer can check you

A theory that only you can verify hasn't been handed over. This is measurable: in a study
of 120 students prompting a code model, participants judged the generated code correct
61.8% of the time — and roughly one in eleven (9%) of those judgments was wrong. Readers
of generated code are not a reliable check, and unfamiliar constructs make it worse: five
participants could not validate *or* repair otherwise-working code because it used
features they didn't know, and whether a feature counted as familiar tracked nothing more
principled than whether their school had taught it (the same construct read as familiar
to 37.5% of students at one institution and 60.6% at another that covered it).

Cleverness therefore has a specific cost: it converts a reviewer into a spectator.

- **Write in the dialect of the surrounding code**, not the most elegant one available.
- **If an unusual construct genuinely earns its place, say why in one clause** — the note
  is what keeps the reader able to check it.
- **When a second attempt produces the same wrong output, stop rewording and restate the
  model.** In that study, repeated identical wrong generations preceded a fifth of all
  abandonments. Re-prompting a wrong theory just re-renders it.

## The theory note

Black-box review is what happens when reading everything is the only way to review
anything: it costs too much, so it gets skipped. So make review cheap. End any
non-trivial change with a short note — under ten lines, in the response, plus a durable
copy in the code or PR:

```
Theory:      what this models, in a sentence or two
Instead of:  the other coherent reading of the request, and why not it
Reused:      existing library/module used instead of writing new code
New concept: any abstraction introduced, and its name in the domain
Assumes:     conditions relied on that no test enforces
Cost:        complexity / allocations / round trips that matter
Watch:       the part most likely to be wrong, and why
```

There is one criterion for what belongs in it, from Alistair Cockburn's commentary on
Naur: write **that which helps the next reader build an adequate theory of the program.**
Not what the code does — they can read that. What they could not have reconstructed. The
liberating half of the same rule is that the note "cannot — and so need not — say
everything"; completeness was never the goal.

The **Cost** line is where performance lives, because performance is a theory choice: an
O(n²) shape becomes O(n log n) by choosing a different model, not by tuning the text.
State what the theory implies — complexity, allocations, round trips, lock scope — and
when a real constraint conflicts, redesign rather than patch; patching a wrong theory to
hit a number is how the spaghetti starts.

The **Watch** line matters most. It aims a reviewer's limited attention at the place it
pays, which is the honest alternative to both "read all 800 lines" and "don't look."

A filled-in note, the hollow note that mechanical use produces, and how this template
relates to Gate 1's three sentences are all in `references/theory-note.md`.

## Proportionality

These gates scale with stakes, and applying them to a five-line throwaway script is its
own kind of failure. Three tiers:

- **Throwaway** — a one-off analysis, a scratch file: a one-line theory note at most.
- **Ordinary shared code** — anything another person or agent will touch: Gate 2's reuse
  check always, because it is cheap and catches the most expensive failure, plus Gate 1's
  one-sentence theory.
- **Load-bearing** — modules others extend, persistence formats, anything that runs
  unattended: all four gates and the full note.

If a task is being executed on autopilot, that's the signal to slow down, not to speed
up. Anything that promises freedom from thinking should be treated as probably stupid,
including this skill: a checklist run mechanically produces theory notes that are as
hollow as the code they describe. The gates are prompts for judgment, not a substitute.

Naur would go further, and he is worth taking seriously against his own admirers. A
theory, he argues, "has no inherent division into parts and no inherent ordering," so
"for the primary activity of the programming there can be no right method" — no sequence
of steps that mechanically yields good solutions. What methods are actually good for, on
his account, is the *education* of the people using them. Read these gates that way:
scaffolding that teaches what to attend to, discarded once attending is automatic. Four
gates in a fixed order is already a small lie about how theories get built.

## An honest limit

A model auditing its own abstractions is partly circular — the same weakness that
produced repetition-with-variants also evaluates whether the repetition was warranted.
This skill narrows the gap; it doesn't close it. The theory note exists precisely because
a human still needs to look, and its job is to make looking cheap enough to actually
happen.

And the human looking is not a reliable backstop either — the 9% of confident-and-wrong
correctness judgments above is the measured version of that. Two unreliable readers do
not make a reliable review. What they can do is fail *differently*, which is the entire
value on offer here: the note says where to look, so the human's attention lands where
the model's self-assessment is weakest.

## Sources

- Peter Naur, "Programming as Theory Building" (1985), reprinted in *Computing: A Human
  Activity* (1992) and in Alistair Cockburn's *Agile Software Development*, whose
  commentary supplies the documentation criterion used above.
  <https://pages.cs.wisc.edu/~remzi/Naur.pdf>
- Nguyen, Babe, Zi, Guha, Anderson & Feldman, "How Beginning Programmers and Code LLMs
  (Mis)read Each Other" (2024). <https://arxiv.org/abs/2401.15232> — source of the
  review-reliability and unfamiliar-construct figures. Scope worth keeping in view: 120
  students who had taken one CS course, working on small problems with a 2022-era model.
  It measures novices, not senior reviewers, and the numbers are cited here as evidence
  that reading generated code is harder than it feels — not as a constant.

## Reference index (load on demand)

Keep `SKILL.md` lean; pull in depth only when the change needs it.

| Load this when… | File |
|---|---|
| Writing the theory or the note — a filled-in note beside a hollow one, the four questions a theory answers, worked prose theories, failure modes, where the note lives | `references/theory-note.md` |
| Judging an abstraction or running the reuse search — factorization vs. abstraction, the four checks in full, the search order with synonym tactics, test prompts that attack the theory | `references/abstraction.md` |
| Modifying code whose theory you don't hold — the recovery reading order, patch vs. natural extension, rebuilding behind the interface, parallel work fragmenting a theory | `references/inherited-code.md` |
