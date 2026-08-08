# Reading code critically, without a parser

The detectors have already found what pattern-matching can find: merge
markers, oversized files, TODO debt, committed secrets. This is the pass that
needs a reading brain — the part no regular expression can do, in any language.

## Per-function questions

Ask in this order. The first few find bugs and dead weight; the rest find cost.

1. **Can it be deleted?** The fastest simplification is removal. Is this
   function called? Is this parameter ever a non-default value? Is this branch
   reachable? Is this abstraction used more than once? Check with the tools
   available — grep, the project's own search, coverage if it has any — before
   deleting; then delete. Git remembers.
2. **Does it do one thing?** If you can't name it without "and", it's doing too
   much. If reading it requires holding more than a handful of things in your
   head at once, it's too big.
3. **Is the simplest version this complicated?** Could a lookup table replace
   this chain of conditionals? Could the language's own standard library
   replace this hand-rolled machinery? A reviewer who doesn't know the
   language's idioms can still ask this question of the shape of the code.
4. **Does every abstraction pay rent?** Each layer, base class, interface,
   factory, and indirection must earn its complexity by removing more than it
   adds. One implementation behind an interface is not an abstraction, it's
   overhead.
5. **Is the duplication real?** Before recommending an extraction, ask whether
   the two copies are the same *knowledge* or just coincidentally similar
   *text*. Code that looks alike but changes for different reasons must stay
   separate. Wait for a third occurrence before generalizing — and remember
   that without a parser, "looks alike" is itself a guess this skill cannot
   fully verify (see `unknown-language-review.md`).
6. **Do the names tell the truth?** A name should say what a thing is and why
   it exists. If a comment is needed to explain *what* the code does, the name
   is the problem. A comment explaining *why* is worth keeping.
7. **Will it fail loudly?** Does a failure become an error the caller can see,
   or does it disappear into a swallowed exception, a default value, or a
   silently-ignored return code? Invalid states should be unrepresentable, not
   patched over with defensive checks scattered everywhere.

## Per-module questions

- **What does it expose, and who uses each part?** Something exported but never
  referenced anywhere this scan can see is either dead or reached in a way the
  raw layer cannot follow (see the reference-graph caveats in
  `unknown-language-review.md`) — say which you believe and why.
- **Does it have one reason to change?** Two groups of code that never
  reference each other are two modules wearing one name.
- **Does loading it run anything?** A module with side effects at import/load
  time executes in whatever order the surrounding system chooses to load
  modules — that ordering is rarely something the author controlled.

## Triage rubric

Rank every finding and candidate so the list is ordered, not just full:

- **Severity** — correctness/bug risk > maintainability pain > cosmetic.
- **Effort** — trivial / small / medium / large.
- **Blast radius** — how much could break, and is it under test?
- **Churn** — how often this code changes (`git log --name-only`, workflow
  step 4 in `SKILL.md`).

Priority buckets:

- **P0** — a correctness bug found during review. Fix now.
- **Quick win** — cheap and low-risk (a merge marker, a committed secret to
  revoke, a stray `.env`). Batch these into one pass early.
- **High value** — hot *and* complex code: the file that changes every week and
  is also the hardest one to change safely.
- **Low** — cosmetic issues in cold code. Maybe never.

## The four things a finding needs

1. **Location** — `file:line`.
2. **What is wrong**, in one sentence, stated as a consequence, not a taste.
3. **The concrete fix.**
4. **Why it is worth doing** — the input that breaks it, the bug it caused, or
   the change it will make expensive.

If you cannot write the fourth, it is a preference, not a finding — say that
too. A review that separates "this is a defect" from "this looks odd to me"
gets acted on; one that mixes them gets skimmed. This is exactly the
finding/candidate split the schema enforces on the detectors themselves: a
finding you cannot justify with (4) has no business carrying a suggested fix.

## What not to raise

- Formatting a formatter already owns. Run the project's own formatter; don't
  review whitespace by eye.
- Style the project has consciously chosen and applied consistently — the goal
  is convergence with the codebase's own conventions, not with yours.
- Complexity genuinely forced by an external API, a framework contract, or a
  real present requirement. A cast or workaround at a boundary you do not
  control, with the reason written beside it, is correct as written.
- Anything in code that is being deleted.
