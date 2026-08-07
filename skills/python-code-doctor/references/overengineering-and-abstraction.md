# Over-Engineering and Abstraction

The single biggest source of accidental complexity is abstraction that was added
before it was needed, or that never paid for itself. The deterministic detector
`find_overengineering.py` catches the obvious structural cases (single-impl
interfaces, one-type factories, thin wrappers). This file is for the judgment
calls: deciding whether a given abstraction should exist at all.

## The cost model

Every abstraction has a price: a reader must now understand the abstraction *and*
the thing it abstracts, hold an extra layer in their head, and jump between files
to follow a single thought. An abstraction is only worth it when it removes more
complexity than it adds. The bar is not "could this ever be useful" — it's "does
this earn its keep *today*."

**Default verdict: an abstraction with one caller, one implementation, or one
reason to exist is overhead. Inline it.**

## Interrogate every abstraction

For each class, base class, interface (ABC/Protocol), factory, manager, handler,
strategy, wrapper, mixin, decorator, and layer, ask:

- **How many implementations / callers does it actually have?** One? Then it's not
  abstracting over anything. Delete the indirection and use the concrete thing.
- **What would I lose if I deleted it and inlined the body?** If the honest answer
  is "a layer of indirection," delete it. If it's "nothing," definitely delete it.
- **Was it built for a requirement that exists, or one that might?** "We might need
  to swap the database / support other formats / make it pluggable" is speculative
  generality. Build the second implementation when the second implementation
  arrives, not before. YAGNI.
- **Does the name describe behavior or just architecture?** `*Manager`, `*Handler`,
  `*Helper`, `*Processor`, `*Service`, `*Util`, `*Base`, `*Impl` are warning signs:
  they often name a place to put code rather than a real concept. What does it
  actually *do*? If you can't say, it may not deserve to be a thing.

## The named anti-patterns live elsewhere

The pattern-by-pattern list is deliberately not repeated here. The structural
cases — single-impl interface, one-type factory, one-entry strategy, thin
wrapper, never-varied config, speculative generality, deep inheritance — are
`SKILL.md`'s "Over-engineering anti-patterns" table, each with its fix. The
design-pattern-shaped cases (hand-rolled singletons, fluent builders,
strategy-class hierarchies) are the pattern→simpler map in `design-patterns.md`.
And the classic-catalog treatment of the overlapping smells — lazy class,
speculative generality, middle man — is in `refactoring-catalog.md`, with the
spot/diagnostic/fix/ignore-when protocol for each.

Three judgment nuances those lists don't carry:

- **A mock is not a second implementation.** Tests never justify keeping a
  single-impl interface — patch the concrete class or inject a callable instead.
- **A wrapper that narrows can be legitimate.** A wrapper that only forwards is a
  middle man; one that exists to narrow a wide interface down to the few methods
  callers actually use may earn its keep. Judge by whether it removes confusion.
- **Layers that only pass data through.** A "service" that calls a "repository"
  that calls an "adapter" that calls the ORM, each adding nothing but a
  signature, is ceremony no single-class detector flags. Collapse layers that
  don't transform, validate, or decide.

## DRY, correctly: duplication vs the wrong abstraction

DRY is about not duplicating *knowledge* — one authoritative place for each
business rule or fact. It is **not** "no two pieces of code may look alike."

Before extracting shared code, ask: **do these two places change for the same
reason, or do they merely look similar right now?** If a future requirement would
change one copy but not the other, they are different knowledge that happens to
share syntax. Merging them creates an abstraction that the next change will have to
fight — usually by adding a flag, then another, until the "shared" function is a
maze of conditionals serving callers that no longer have anything in common.

Guidance:
- **Rule of three.** Two similar fragments: leave them. The third occurrence tells
  you the shape of the real abstraction; extract then.
- **A little duplication is cheaper than the wrong abstraction.** Wrong abstractions
  are expensive to detect and expensive to unwind. Tolerable duplication is a local,
  honest cost.
- **If a "shared" helper has grown boolean/mode parameters that select wildly
  different behavior, that is the wrong abstraction.** Inline it back into its
  callers and let them diverge.

## The simplification reflex

When something looks complex, reach (in order) for: **delete it** → use the standard
library (`itertools`, `collections`, `functools`, `pathlib`, `dataclasses`,
`enum`) → a plain function → a small dataclass. Reach for a new class hierarchy,
metaclass, decorator framework, or plugin system **last**, and only with a concrete,
present reason. The burden of proof is on the abstraction, not on its removal.
