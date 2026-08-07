# Refactoring Techniques

The classic technique catalog (Fowler / refactoring.guru): the *fixes* that
`refactoring-catalog.md` entries name, adapted to Python. Load this when actually
executing a refactoring, not just diagnosing.

## The discipline (read before touching anything)

Refactoring is a series of small changes, each leaving the program working.
Three rules, checked after every step:

1. **The code gets cleaner.** If it's just as messy afterwards, the hour was
   wasted — usually a sign you batched many refactorings into one big change.
   If the code is so rotten that no local improvement helps, stop refactoring
   and consider a rewrite of that part — *with* tests and budgeted time.
2. **No new functionality.** Don't mix refactoring with feature work; separate
   them at least at commit granularity. One technique → one commit is the ideal.
3. **All tests pass after every step.** If a test breaks: either you erred (fix
   it) or the test was pinned to private structure (fix the test — test behavior,
   not internals). No tests at all? Build the safety net first:
   `safety-net-and-testing.md`.

**When to refactor — the Rule of Three:** first time, just do it; second time,
wince but duplicate; third time, refactor. Beyond that: refactor when adding a
feature (clean first, then add — it's easier in clean code), when fixing a bug
(bugs live in the dirtiest corners), and during code review (the last chance
before the code ships).

**Choosing direction:** half the catalog comes in inverse pairs — Extract ↔
Inline (Method, Class, Variable), Hide Delegate ↔ Remove Middle Man, Pull Up ↔
Push Down, Inheritance ↔ Delegation. Neither direction is "more refactored";
move code toward where it's used and toward fewer moving parts. When a previous
abstraction stopped earning its keep, inlining it is progress.

## Composing methods

Streamlining method bodies — most refactoring starts here.

| Technique | Use it when | Python form |
|---|---|---|
| Extract Method | A fragment needs a comment or recurs | Function/method named after the comment |
| Inline Method | The body is as clear as the name | Replace calls with the body, delete |
| Extract Variable | An expression is hard to parse | Named intermediate; name = meaning |
| Inline Temp | A temp only aliases a simple expression | Use the expression directly |
| Replace Temp with Query | A temp caches a computable value across a long body | Small method/property; enables further extraction |
| Split Temporary Variable | One variable reused for unrelated things | One variable per meaning |
| Remove Assignments to Parameters | A parameter is reassigned mid-body | Copy to a local; parameters are inputs |
| Replace Method with Method Object | Extraction blocked by tangled locals | Small class holding the locals as fields — or, in Python, often a closure |
| Substitute Algorithm | A clearer algorithm does the same job | Replace wholesale, under tests (stdlib first) |

**Extract Method — mechanics.** (1) Create the new function, named for *what*,
not *how* (the comment you're deleting is the name). (2) Copy the fragment.
(3) Pass the locals it reads as parameters; return what it writes — if that means
more than ~2 return values, the fragment boundary is wrong or a dataclass is
hiding in those values. (4) Replace the fragment with a call. (5) Tests.

**Replace Temp with Query — mechanics.** Extract the temp's initializer into a
method/property; replace every read of the temp with a call; delete the temp.
Only for side-effect-free computations (separate query from modifier first).

## Moving features between objects

The heart of refactoring: putting behavior where the data lives.

| Technique | Use it when | Python form |
|---|---|---|
| Move Method | A method envies another class's data | Move it there; leave a delegating stub only during migration |
| Move Field | A field is used more by another class | Move it with its invariants |
| Extract Class | One class does the work of two | New class; the fields that travel together go first |
| Inline Class | A class no longer earns its keep | Fold members into the main user, delete |
| Hide Delegate | Callers navigate `a.b.c` | Method/property on `a` returning what callers need |
| Remove Middle Man | A class only forwards | Let callers talk to the delegate |
| Introduce Foreign Method | A read-only class lacks one helper | Plain module function taking the object as first arg |
| Introduce Local Extension | A read-only class lacks many helpers | Subclass or wrapper; callers use it instead — don't monkey-patch |

**Move Method — mechanics.** (1) Check what the method uses from its current
home; anything shared may need to move too or be passed in. (2) Declare it on the
target class; adjust `self`. (3) Make the old method delegate to the new one.
(4) Re-point callers one at a time, tests between batches. (5) Delete the old
shell.

**Extract Class — mechanics.** (1) Pick the seam: fields used together, methods
that only touch those fields. (2) Create the class; Move Field each field, tests
after each. (3) Move Method the behavior. (4) Decide how the original holds it
(usually plain composition). Resist making the new class bidirectionally aware of
the old one.

## Organizing data

Replacing primitives and loose data with types that carry their own rules. This
is where Java-era mechanics translate loosest — idiomatic Python first:

| Technique | Use it when | Python form |
|---|---|---|
| Replace Data Value with Object | A primitive carries meaning/rules | `@dataclass(frozen=True)` value type |
| Replace Array with Object | Positional tuple/dict-with-string-keys as a record | Dataclass (or `NamedTuple` at boundaries) |
| Replace Magic Number with Symbolic Constant | A bare number encodes meaning | Module-level constant or `Enum` |
| Replace Type Code with Class | Int/string constants encode a fixed set | `enum.Enum` / `StrEnum` |
| Replace Type Code with Subclasses | Behavior varies by the type code | Subclass per variant; then Replace Conditional with Polymorphism |
| Replace Type Code with State/Strategy | The "type" changes at runtime or can't subclass | Delegate to a swappable state/strategy object (often just a function) |
| Replace Subclass with Fields | Subclasses differ only in constant values | One class; the constants become fields |
| Encapsulate Field | Direct access breaks an invariant | `@property` — only when there's an invariant; bare attributes are idiomatic |
| Self Encapsulate Field | Subclasses must be able to override access | `@property`; rarely needed in Python |
| Encapsulate Collection | A getter hands out the mutable list | Return a copy/tuple/iterator; add `add_`/`remove_` methods |
| Change Value to Reference | Many copies of one conceptual object | One shared instance via a registry/factory |
| Change Reference to Value | Shared mutable object causes aliasing bugs | Frozen dataclass with `__eq__` by value |
| Change Unidirectional Association to Bidirectional | Both sides truly need each other | Use sparingly; one side owns the link |
| Change Bidirectional Association to Unidirectional | One direction is unused | Delete the back-pointer (it's aliasing + cycles) |
| Duplicate Observed Data | Domain data trapped in UI code | Separate domain object; observe via events/callbacks |

**Replace Type Code with Subclasses — mechanics.** (1) Make the type-code field
an `Enum` first (instant win even if you stop here). (2) Create a subclass per
variant; a factory `classmethod` picks the subclass from the code. (3) Move
variant-specific behavior down via Replace Conditional with Polymorphism, one
ladder at a time. (4) When no ladders remain, the enum may dissolve. Stop at the
dispatch-dict stage if the variants are data-shaped rather than behavior-shaped —
a hierarchy nobody needed is the next reviewer's finding.

## Simplifying conditional expressions

| Technique | Use it when | Python form |
|---|---|---|
| Decompose Conditional | The condition (or branches) need parsing | Extract predicate to a named function/variable |
| Consolidate Conditional Expression | Several conditions, same result | One predicate with a name (`any()`/`all()`) |
| Consolidate Duplicate Conditional Fragments | Same statement in every branch | Hoist it out (detector: `find_design_smells.py`) |
| Remove Control Flag | A bool variable steers the loop | `break`/`return`/`continue`; or make the condition real |
| Replace Nested Conditional with Guard Clauses | Edge cases wrap the happy path | Early returns; flat reads better than nested |
| Replace Conditional with Polymorphism | Branching on type, repeatedly | Method per variant — or a dispatch dict when variants are data |
| Introduce Null Object | `if x is None` checks proliferate for one collaborator | A do-nothing implementation; **sparingly** — one or two None-checks don't justify a class |
| Introduce Assertion | The code assumes an invariant silently | `assert` for impossible states (never for input validation — raise) |

**Replace Nested Conditional with Guard Clauses — mechanics.** For each edge
case: invert the condition, return/raise early, dedent the remainder. Repeat
until the happy path sits at indentation level one. Tests after each inversion.

## Simplifying method calls

Interfaces other code can read at a glance.

| Technique | Use it when | Python form |
|---|---|---|
| Rename Method | The name lies or mumbles | Rename everywhere (IDE/grep); names are the API |
| Add / Remove Parameter | The signature drifted from reality | Keyword-only params for clarity; defaults for migration |
| Separate Query from Modifier | One method both answers and mutates | Two methods; queries are side-effect-free |
| Parameterize Method | Several methods differ by a value | One method, one parameter |
| Replace Parameter with Explicit Methods | A param just selects a branch | One method per behavior (kills boolean flags) |
| Preserve Whole Object | Caller unpacks an object to pass its parts | Pass the object |
| Replace Parameter with Method Call | Callee could compute the argument itself | Let it; shorter signatures |
| Introduce Parameter Object | A clump recurs across signatures | Frozen dataclass |
| Remove Setting Method | A field must not change post-init | `frozen=True` dataclass / no setter |
| Hide Method | A method has no external callers | Prefix `_` |
| Replace Constructor with Factory Method | Construction needs logic or a name | `@classmethod` (`from_config`, `parse`) — not a Factory class |
| Replace Error Code with Exception | Callers must remember to check returns | Raise a specific exception; never `-1`/`None` error codes |
| Replace Exception with Test | Exceptions used for normal control flow | Check first (`if key in d`) or use `.get()`; exceptions are for the exceptional — but EAFP is idiomatic when the "failure" is rare and atomic |

## Dealing with generalization

Moving behavior up and down hierarchies — and knowing when to leave hierarchies
entirely. Python bias: composition first; hierarchies must prove substitutability
(a subclass you can't pass where the parent goes is a refused bequest).

| Technique | Use it when | Python form |
|---|---|---|
| Pull Up Field / Method | Subclasses duplicate a member | Move to the superclass |
| Pull Up Constructor Body | Subclass `__init__`s overlap | Shared part in `super().__init__()` |
| Push Down Field / Method | A member serves only one subclass | Move it down |
| Extract Subclass | Some instances need extra behavior | Subclass for the special case — if it's *data* that varies, a field is cheaper |
| Extract Superclass | Two classes share real behavior | Shared parent for the common part |
| Extract Interface | A client needs only a slice of the API | `typing.Protocol` (structural; no inheritance needed) |
| Collapse Hierarchy | Parent and child are nearly identical | Merge them |
| Form Template Method | Same algorithm shape, different steps | Base method calling overridable steps — or a function taking the varying steps as callables |
| Replace Inheritance with Delegation | Subclass uses a sliver of the parent / no true is-a | Hold the object, forward what's needed |
| Replace Delegation with Inheritance | You delegate *everything* to one collaborator | Inherit — only if true is-a and you'd forward the whole API |

## The agent protocol

How an agent (this skill) applies the catalog to a finding — including its own
detector output:

1. **Triage the candidate.** A detector hit names a smell; read the catalog
   entry's diagnostic question against the actual code. Reject false positives
   explicitly (say why) — don't "fix" idioms.
2. **Check the blast radius.** Who calls this? Is it tested? Untested → write a
   characterization test first (`safety-net-and-testing.md`). Cold and harmless →
   leave it; fix what churns.
3. **Pick the *smallest* named technique** that removes the smell. Prefer the
   function over the class, the dispatch dict over the hierarchy, the stdlib over
   machinery. The fix must not introduce next review's over-engineering finding.
4. **Execute in steps, tests green after each.** One technique per commit, the
   technique name in the commit message ("Extract Method: pull tax rules out of
   checkout()").
5. **Second pass.** After the mechanical candidates are cleared, re-read the hot
   files for the judgment-only smells (change preventers, duplicated intent,
   alternative classes) that detectors can't see.
6. **Ratchet.** A cleared smell class gets its guard: the matching detector (or
   Ruff rule) stays in CI so it can't creep back.
