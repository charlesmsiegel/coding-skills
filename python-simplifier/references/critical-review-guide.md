# Critical Review Guide

This is the master guide for the judgment-based half of the skill. The scripts in
`scripts/` find what can be found mechanically. This file (and the others in
`references/`) is for everything that needs a reading brain: whether an
abstraction earns its keep, whether two pieces of "duplication" are really the
same thing, whether a pattern is the *right* pattern, whether a name tells the
truth.

## The stance: assume the code is guilty

Approach every file as if it is **too complicated until proven otherwise.** Most
code can be smaller, flatter, and more direct than it is. Your default question is
not "is this acceptable?" but "**why isn't this simpler?**" If you can't answer
that with a concrete reason (a real requirement, a measured constraint), the code
should change.

Be specific and unsparing in what you flag, but hold two hard limits:

1. **Behavior is sacred.** Simplification must never change what the code does.
   If a piece of code is untested, the *first* action is a characterization test
   that pins current behavior — then refactor under it. Never refactor blind.
2. **Simpler, not cleverer.** The goal is code a tired junior can read at 5pm, not
   a showcase of techniques. A clever one-liner that needs a comment lost.

## The workflow

1. **Run the scripts first.** Start with `python scripts/analyze_all.py <path>`.
   Triage the deterministic findings before reading anything by hand — never spend
   judgment on what a tool already caught.
2. **Find the hot files.** Refactoring effort should follow change frequency, not
   line count. Get the churn list and read the most-changed files first:
   ```bash
   git log --since="1 year ago" --name-only --pretty=format: \
     | grep '\.py$' | sort | uniq -c | sort -rn | head -30
   ```
   A file that is *both* high-churn and high-complexity is the top target. **Do not
   refactor cold code** — ugly code that never changes and blocks nothing is not a
   priority.
3. **Read with the judgment guides open.** For the hot files, walk the relevant
   reference:
   - `overengineering-and-abstraction.md` — does each abstraction justify itself?
   - `refactoring-catalog.md` — design smells (feature envy, divergent change,
     primitive obsession, ...) and the refactoring each one wants.
   - `patterns-and-consistency.md` — is this the right pattern, applied the *same*
     way as the rest of the codebase?
   - `naming-comments-readability.md` — do the names and comments tell the truth?
   - `python-idioms.md` — concrete before/after idiom swaps.
4. **Produce a findings artifact.** One smell, one entry, one small PR. Render any
   script's JSON into a list/cards/JSON file with `scripts/format_findings.py`. That
   artifact is the output — do not create tickets in a tracker on your own. If the
   user wants findings filed, ask which ticket software or MCP to use (Jira, Linear,
   GitHub Issues, Asana, a connected MCP, ...) and create them through that tool.

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

## The ratchet

Every time a *class* of problem is cleared, turn on the check that prevents its
return (a Ruff rule, a complexity gate, one of these scripts wired into CI). A
review that doesn't leave enforcement behind just resets the clock.
```
