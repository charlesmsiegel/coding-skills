# Critical Review Guide

The reviewer stance, the workflow, and the output/ticketing rules live in
`SKILL.md`, along with the routing table for every file in `references/` — start
there. This file adds the two tools used during the read itself: the questions to
ask of every piece of code, and the rubric for ranking what they turn up.

## The critical-questions checklist

Apply these to every function, class, and module you read. Each "no" is a finding.

**Can it be deleted?** The fastest simplification is removal. Is this function
called? Is this parameter ever a non-default value? Is this branch reachable? Is
this abstraction used more than once? When in doubt, check with `grep` and
coverage, then delete — git remembers.

**Does it do one thing?** A function should do one thing at one level of
abstraction. If you can't name it without "and", it's doing too much. If reading it
requires holding more than a handful of things in your head, it's too big.

**Is the simplest version this complicated?** Could a dict replace this if/elif
ladder? Could a comprehension replace this loop? Could a guard clause replace this
nesting? Could a dataclass replace this bag of positional arguments? Could standard
library (`itertools`, `collections`, `pathlib`, `functools`) replace this hand-rolled
machinery?

**Does every abstraction pay rent?** Each layer, base class, interface, factory,
and indirection must earn its complexity by removing more than it adds. One
implementation behind an interface is not an abstraction, it's overhead. (See
`overengineering-and-abstraction.md`.)

**Is the duplication real?** Before extracting shared code, ask whether the two
copies are the same *knowledge* or just coincidentally similar *text*. Code that
looks alike but changes for different reasons must stay separate. A little
duplication is far cheaper than the wrong abstraction. Wait for the third
occurrence before generalizing.

**Is this the same way we do it elsewhere?** Consistency is a feature. If the rest
of the codebase models data with dataclasses, this module shouldn't pass dicts. If
errors are handled one way over there, handle them that way here. Pick the
canonical form and converge on it. (See `patterns-and-consistency.md`.)

**Do the names tell the truth?** A name should say what a thing is and why it
exists. If you need a comment to explain *what* the code does, the code (or its
names) is the problem. (See `naming-comments-readability.md`.)

**Will it fail loudly?** Errors should surface with context, not be swallowed.
Invalid states should be unrepresentable, not patched with defensive checks
scattered everywhere.

## Triage rubric

Rank every finding so the board is ordered, not just full:

- **Severity** — correctness/bug risk > maintainability pain > cosmetic.
- **Effort** — auto-fixable / small / medium / large.
- **Blast radius** — how much could break, and is it under test?
- **Churn** — how often this code changes (from the hotspot list).

Priority buckets: **P0** correctness bugs found during review → fix now;
**quick wins** auto-fixable and zero-risk → batch into one PR early, then turn on
the enforcing rule; **high value** hot + complex code, god classes, the
duplicated core logic; **low** cosmetic issues in cold code → maybe never.

When a class of problem is cleared, turn on the check that prevents its return
(`SKILL.md`, workflow step 5) — a review that doesn't leave enforcement behind
just resets the clock.
