# Patterns and Consistency

Two failures show up constantly in messy Python: the *wrong* pattern for the job,
and the *right* pattern applied five different ways across the codebase. This guide
covers both — pick the right tool, then make the whole codebase use it the same
way. Consistency is itself a feature: a predictable "good enough" pattern used
everywhere beats a patchwork of locally clever ones.

## Part 1 — Choose the right pattern

This section covers the everyday Python idiom choices. For the classic design
patterns — when a GoF pattern is genuinely warranted, when it's ceremony, and the
Python-native form of each — load `design-patterns.md`.

**Guard clauses over nested conditionals.** Handle the edge cases up front and
return early; don't wrap the happy path in three levels of `if`. Flat reads better
than nested. (`analyze_complexity.py` flags the nesting.)

**Dispatch over long if/elif type-switches.** A ladder that branches on a type tag
or string (`if kind == "a": ... elif kind == "b": ...`) is a maintenance magnet.
Replace with a dict mapping key → function, or with polymorphism (a method each
subclass implements). Adding a case should mean adding an entry, not editing a
ladder.

**Polymorphism over `isinstance` chains.** If you're switching on the concrete type
of an object, the behavior probably belongs *on* those types. Let each type answer
for itself. (Reserve `isinstance` for genuine boundary checks.)

**Composition over inheritance.** Inherit only for a true is-a relationship with a
substitutable contract. To share helper code or assemble behavior, pass
collaborators in. Composition is easier to test, swap, and reason about than a deep
class tree. (See `refactoring-catalog.md`: refused bequest.)

**Dependency injection over globals and singletons.** Pass what a function needs
(the client, the clock, the config) as arguments rather than reaching for module
state. It makes behavior explicit and tests trivial. (`find_global_state.py` finds
the mutable-global anti-pattern this replaces.)

**Context managers for setup/teardown.** Anything with a paired acquire/release —
files, locks, connections, temporary state — belongs in a `with`. Don't hand-roll
try/finally cleanup; write or use a context manager so the cleanup can't be
skipped.

**Dataclasses and enums for data.** A bag of related values is a `dataclass`
(`frozen=True` when it shouldn't mutate), not a loose tuple or an untyped dict. A
fixed set of options is an `enum.Enum`, not magic strings. The type then carries
validation and meaning instead of scattering both. (See primitive obsession in
`refactoring-catalog.md`.)

**Pure functions where you can.** Separate computation (no side effects, easy to
test) from effects (I/O, mutation, network). A core of pure functions with a thin
effectful shell is simpler than logic and effects braided together.

**Exceptions over error codes / sentinels.** Raise specific exceptions and let
callers handle them; don't thread `-1`/`None`/`False` "error" returns through the
code and re-check at every level. (Pair with the exception hygiene in
`find_exception_issues.py`: chain with `from`, catch narrow, never swallow.)

## Part 2 — Apply it consistently

Once the right patterns are chosen, the codebase should use **one canonical form
per concern.** Inconsistency is a tax: every reader has to figure out whether a
difference is meaningful or just historical. When you find a concern handled two
ways, pick the better one as canonical and converge the rest on it (incrementally,
under tests).

Concerns that should each have exactly one house style:

- **Data modeling.** Dataclass vs `dict` vs `namedtuple` vs `TypedDict` vs Pydantic —
  pick the default for the codebase and use it. Don't model the same kind of entity
  as a dataclass here and a raw dict there.
- **Error handling.** One strategy for how errors propagate and where they're
  caught/logged. Not "exceptions in this module, error tuples in that one."
- **Configuration access.** One way to read settings (a typed config object, an env
  loader) — not `os.environ[...]` sprinkled in some files and a settings module in
  others.
- **Logging.** One logger setup and one level convention. Not `print()` here,
  `logging` there, f-strings eagerly formatted in some calls.
- **String formatting.** f-strings everywhere (the modern default); don't mix in
  `%` and `.str.format()` for no reason.
- **Paths and time.** `pathlib.Path` over `os.path` string-munging;
  timezone-aware `datetime` handled one consistent way.
- **Imports & module layout.** One import ordering/grouping, one convention for what
  goes where. (Let a formatter/isort enforce it.)
- **Naming vocabulary.** One word per concept across the whole codebase (see
  `naming-comments-readability.md`).

## How to drive consistency

1. **Identify the canonical form.** For each concern above, find how the *majority*
   (or the best) of the codebase does it. That's the target.
2. **Flag the deviations as findings.** Each "this module does X differently" is an
   issue: converge it to the canonical form.
3. **Convert incrementally, behavior-preserved.** Don't rewrite everything at once;
   migrate file by file under characterization tests.
4. **Ratchet it.** Once a concern is consistent, add the enforcement (a linter rule,
   a formatter, a CI check, one of these scripts) so drift can't creep back.

## The review prompt

For any piece of code, ask: **is this the right pattern for the job, and is it the
*same* way the rest of the codebase solves this?** If it's a worse pattern, replace
it. If it's a different-but-equal pattern, converge it to the canonical one. Either
way the codebase gets more predictable — and predictable is simpler.
```
