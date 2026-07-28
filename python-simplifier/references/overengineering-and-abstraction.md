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

## Specific patterns to hunt

(For the design-pattern-shaped cases — hand-rolled singletons, builders that are
keyword arguments in disguise, strategy-class hierarchies that are functions in
costume — see the pattern→simpler map in `design-patterns.md` and run
`find_pattern_issues.py`.)

**Single-implementation interface / ABC / Protocol.** An abstract base with exactly
one concrete subclass is a future-proofing tax. Merge them. (Tests are not a second
implementation — a mock does not justify an interface; patch the concrete class or
inject a callable.)

**Factory that builds one type.** `def make_widget(): return Widget()` is just
`Widget()` with extra steps. Direct instantiation.

**Strategy with one strategy.** A strategy object/registry with a single entry is a
function. Call the function.

**Thin wrapper / middle man.** A class whose methods only forward to another object
adds a name and a hop and nothing else. Use the wrapped object directly. (If the
wrapper exists to *narrow* a wide interface to the three methods you use, that can
be legitimate — judge by whether it removes confusion.)

**Configuration that is never varied.** A parameter, setting, or hook that every
caller passes the same value for is not configuration, it's noise. Hardcode the
value and delete the parameter until a second value is real.

**Premature parameterization.** Functions sprout `**kwargs`, optional flags, and
"extensibility hooks" for callers that don't exist. Each one is a path you now have
to keep working and reason about. Remove parameters with one effective value.

**Deep inheritance.** Four-plus levels, or inheritance used to share helper code
rather than to model an is-a relationship, is a smell. Prefer composition: pass the
collaborator in. (See `patterns-and-consistency.md`.)

**Layers that only pass data through.** A "service" that calls a "repository" that
calls an "adapter" that calls the ORM, each adding nothing but a signature, is
ceremony. Collapse layers that don't transform, validate, or decide.

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
```
