# Working in code whose theory you don't have

## Contents

- [The position you are always in](#the-position-you-are-always-in)
- [Recovering a theory from the text](#recovering-a-theory-from-the-text)
- [Patch vs. natural extension](#patch-vs-natural-extension)
- [How structure actually dies](#how-structure-actually-dies)
- [When rebuilding beats reviving](#when-rebuilding-beats-reviving)
- [Parallel work fragments the theory](#parallel-work-fragments-the-theory)

## The position you are always in

Naur reports a team that inherited a working compiler and was given every artifact a
handover is supposed to consist of: full documentation, annotated program texts, extensive
written design discussion, and access to the original authors for advice. They were
competent and motivated. They still repeatedly proposed extensions that ignored facilities
already in the structure — facilities "discussed at length in its documentation" — and
that would have been added as patches which "effectively destroyed its power and
simplicity." The original authors "were able to spot these cases instantly."

Nothing was missing from the documents. The theory simply wasn't in them.

This is the ordinary condition of an agent editing a codebase: fluent in the text, without
the model behind it, and — worse — with no felt sense of the difference, because the text
reads perfectly well either way. Naur calls reconstructing a theory from documentation
alone "strictly impossible," and from the text "a difficult, frustrating, and time
consuming activity." Assume you are doing the difficult version, badly, and act
accordingly.

He also names the trap that follows: the new programmer "is likely to feel torn between
loyalty to the existing program text, with whatever obscurities and weaknesses it may
contain, and the new theory that he or she has to build up." The resolution is not to pick
a side silently. It is to notice which one you are serving in a given edit, and say so.

## Recovering a theory from the text

Read for the model, not the syntax. In rough order of yield:

1. **The type and data definitions.** They are the ontology — what the program believes
   exists. A field that is nullable in one place and defaulted in another is a seam
   between two theories.
2. **The boundary functions.** Where data enters and leaves, the assumptions get made
   explicit because they have to be.
3. **The tests, read as claims about the world.** A test named
   `test_null_is_explicit_unset` states a theory. A test named `test_case_3` states that
   someone was fixing a bug.
4. **The history of one hot file.** `git log -p` on the most-changed file shows what kept
   being wrong, which is the negative image of the theory.
5. **Comments that explain *why*.** Rare and disproportionately valuable. Comments that
   restate the code are noise; a comment naming a rejected alternative is a fragment of
   the original theory that survived.

Then write the one sentence: *this module thinks the world is ___.* Check it against the
code, not against what the code ought to have been. If two sentences are needed and they
don't compose, you've found a module with two theories in it — which is a finding worth
reporting, and usually the real cause of whatever bug you were sent to fix.

If the sentence won't come, say that plainly. "I could not determine why this path retries
twice; I preserved it and added the new case beside it" is an honest handoff. A confident
refactor built on a guessed model is not.

## Patch vs. natural extension

The load-bearing insight for modification work:

> A given desired modification may usually be realized in many different ways, all correct
> … some of them perhaps conforming to that theory or extending it in a natural way, while
> others may be wholly inconsistent with that theory, perhaps having the character of
> unintegrated patches on the main part of the program.

All correct. All green. Only one keeps the program alive. And the criterion cannot be
reduced to a rule — Naur's whole argument is that recognizing the relevant similarity
between the new demand and the existing facilities is a judgment available to someone with
the theory and to no checklist.

What can be done mechanically is to force the choice into the open:

- **Enumerate two implementations** before writing one. If only one comes to mind, that's
  a sign of pattern-completion rather than design.
- **Ask which existing facility this new demand resembles.** If the answer is "none," ask
  it again — Naur's inheritors kept adding parallel machinery beside facilities that
  already existed.
- **Prefer the edit that leaves fewer concepts** in the module than a version that leaves
  more. "Simplicity and good structure," Naur notes, "can only be understood in terms of
  the theory of the program" — so this judgment is only available *after* the recovery
  step, never before it.
- **Name it when you patch anyway.** Deadlines are real. "This is a patch against the
  module's theory: it special-cases legacy tenants instead of extending the tenant policy,
  because the policy change needs a migration we can't ship today" is a debt someone can
  find and repay. The same patch unlabeled is indistinguishable from design.

## How structure actually dies

Not by one bad commit. Naur followed a compiler through roughly ten years of maintenance
by people without its theory and found that "the original powerful structure was still
visible, but made entirely ineffective by amorphous additions of many different kinds."

Every one of those additions passed review. Each was locally reasonable — that is the
point, and the reason "does this diff look fine?" is the wrong question. The right one is
whether the change is grounded in the theory of the program, because, as Naur puts it, for
a program to retain its quality "it is mandatory that each modification is firmly grounded
in the theory of it."

The practical form for a reviewer, and for the note you hand them: *which existing concept
does this change extend, and does the module have more or fewer concepts afterward?*

## When rebuilding beats reviving

Naur's most-ignored conclusion is that revival is often the worse deal — that the existing
text should sometimes be discarded and the problem solved afresh, "at no higher, and
possibly lower, cost" than reconstructing a theory to fit code nobody understands.

Take it seriously as an *option to surface*, never as a unilateral action. Rewrites destroy
undocumented behavior that real users depend on, and the second system will be built by
someone who also lacks the original theory. Say what a rewrite would cost and what it would
buy; the choice belongs to the people who own the consequences.

The honest middle path, when a module's theory is unrecoverable and the change is large:
rebuild that module behind its current interface, with characterization tests pinning the
behavior you're preserving — and state in the note which behaviors you deliberately did
not preserve.

## Parallel work fragments the theory

Cockburn's gloss on Naur, and directly relevant when several agents work at once:

> Imagine 10 programmers working as fast as they can, in parallel, each making design
> decisions and adding classes as she goes. Each will necessarily develop her own theory
> as she goes. As each adds code, the theory that binds their work becomes less and less
> coherent, more and more complicated. … The design easily becomes a "kludge."

Parallelism multiplies output and divides the theory. The mitigation is a shared model
stated *before* the fan-out, concrete enough to constrain choices — the metaphor, the
ontology, the naming of the central concepts — so that independent work converges instead
of drifting. Cockburn's test for a good shared model is that it lets one person "guess
accurately where someone else on the team just added code, and how to fit her new piece in
with it."

If work has already fanned out without one, expect the seams at the boundaries: two names
for one concept, two error conventions, two ideas about who owns validation. Reconciling
those is theory work, and it does not happen by merging cleanly.
