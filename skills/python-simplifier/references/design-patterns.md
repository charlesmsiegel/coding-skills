# Design Patterns: When to Reach for One, When to Strip One Out

Patterns are recurring solutions to recurring *forces*, and a shared vocabulary for
talking about them. Both halves matter — but in review, the vocabulary half is
cheap and the solution half is expensive. This guide pushes in **both directions**:
toward a pattern when the forces are real, and away from one when it's machinery
without a force behind it.

Two facts frame everything here:

1. **Most GoF patterns are workarounds for 1990s static OO languages.** Their
   keystone principle — *program to an interface, not an implementation* — is
   satisfied natively by Python: functions, classes, and modules are first-class
   objects, and duck typing means "interface" rarely requires a class. Strategy
   collapses to a callable; Factory to passing the class itself; Singleton to a
   module; Iterator to `yield`. A pattern transplanted into Python at full Java
   ceremony is usually over-engineering wearing a respectable name.
2. **A pattern is a refactoring *target*, not a starting point.** Designs earn
   patterns by accumulating real forces (a third algorithm variant, a genuine
   second product family), and the catalog's own evolution path runs from simple
   to heavy (direct construction → factory function → Factory Method → Abstract
   Factory) — never start at the heavy end. "If all you have is a hammer,
   everything looks like a nail" is the canonical novice failure; the canonical
   veteran failure is the 400-line if/elif forest that needed a pattern three
   features ago.

**The decision rule:** before adding a pattern, name the force — out loud, in the
PR description. "We swap this algorithm at runtime in two places today." "Callers
must never pair a Win button with a Mac dialog." If the force is "we might need
it," that's YAGNI (see `overengineering-and-abstraction.md`). Before *removing*
pattern machinery, do the same in reverse: confirm the force it served is absent
(one implementation, one caller, no runtime variation), then strip it under tests.

`find_pattern_issues.py` catches the mechanical cases in both directions —
hand-rolled singletons, getter/setter pairs, builders that are keyword arguments
in disguise, string-typed state machines, stateless strategy-class hierarchies.
`find_overengineering.py` (single-impl ABCs, one-type factories, thin wrappers)
and `find_design_smells.py` (type switches → dispatch) cover the adjacent ground.
This file is for the judgment calls.

## The Python translation table (all 23 GoF patterns)

"Python-native form" is the default; reach for the full classed pattern only when
the listed force is present *today*.

### Creational

| Pattern | Python-native form | Full pattern earns its keep when… |
|---|---|---|
| Factory Method | Pass the class or a callable in (classes are first-class): `JSONDecoder(parse_float=Decimal)`, a class attribute like `response_class`, or just a `@classmethod` constructor | You ship a framework whose users override one creation step by subclassing — and even then, an `__init__` parameter is usually better |
| Abstract Factory | Pass a family of callables, or one object exposing them | ≥2 *real* product families whose members must never be mixed; one family = delete it |
| Builder | Keyword arguments with defaults; a dataclass with `__post_init__` validation | Construction is genuinely staged (partial states meaningful, e.g. building a deep tree), or one build process must yield multiple representations — rare |
| Prototype | `copy.deepcopy`; `functools.partial`; store `(cls, args)` — first-class classes make "clone a template" unnecessary | Practically never in pure Python |
| Singleton | **A module-level instance** (the Global Object pattern); modules are already singletons | Back-compat only: callers already call the class and you must pivot to one shared instance without changing call syntax |

### Structural

| Pattern | Python-native form | Full pattern earns its keep when… |
|---|---|---|
| Adapter | A small function, or duck typing (implement the few methods the consumer calls) | Integrating a third-party/legacy interface you can't change; if you own both sides, change the class instead |
| Bridge | Composition: pass the implementation object into the abstraction | Two dimensions genuinely vary independently (M×N subclass explosion looming); on a cohesive class it's pure cost |
| Composite | Duck typing — leaves and containers just share the methods used; no base class needed | The model is *actually* a tree and clients treat parts uniformly; if callers still need `isinstance`, the symmetry is forced — undo it |
| Decorator (GoF) | A `__getattr__`-forwarding wrapper class (a few lines, future-proof) | Adding behavior to objects you didn't construct and can't subclass; breaks under introspection (`__class__`, `dir()`) — it supports programming, not metaprogramming |
| Facade | A plain module with a few functions fronting the subsystem | A subsystem's complexity genuinely leaks into many callers; watch it — facades drift toward god objects (split into several small ones) |
| Flyweight | Built into the language (interned strings, small ints, `()`); a factory function with a cache / `weakref.WeakValueDictionary` | Profiling shows RAM exhaustion from masses of similar immutable objects — it is purely an optimization pattern; anywhere else it's over-engineering by definition |
| Proxy | A `__getattr__` wrapper; `functools.cached_property` for lazy-init cases | Access control, remote stand-in, caching layer — with the service's exact interface; if it adds behavior instead, you mean Decorator |

### Behavioral

| Pattern | Python-native form | Full pattern earns its keep when… |
|---|---|---|
| Strategy | **A plain function** passed as an argument (or `functools.partial` to bind config); a dispatch dict to select | Strategies carry real configuration/state of their own — then a small class per strategy (or one class + partial) is honest |
| State | An `Enum` for the states + dispatch on it | Many states, state-specific behavior in several methods, transitions that change often — then one class per state pays |
| Command | A callable (+ args via `partial`/closure); functions are already "requests as objects" | You need undo/redo history, queuing, or serialization of operations — the *object* part matters then (pair with Memento) |
| Iterator | **Built into the syntax**: `for`, comprehensions, unpacking; write a generator (`yield`) | A custom container should be a first-class citizen of `for` — implement `__iter__`, usually *as* a generator; a hand-rolled `__next__` class is almost always a longer generator |
| Template Method | A function taking the variable steps as callable parameters | A framework base class where subclasses fill in hooks; inherits every liability of inheritance — prefer the callable-parameters form |
| Observer | A list of callbacks: `subscribers.append(fn)`, call them on change | Many event types/subscriber lifecycles — then a small event-emitter class or library; notification order is never guaranteed |
| Chain of Responsibility | A list of handler functions tried in order (`for h in handlers: if h(req) is not None: …`) | Handler set/order must change at runtime or be assembled from plugins |
| Mediator | Often just "extract a coordinating function/class" | Components are so entangled that pairwise refs must go; watch for the mediator becoming the new god object |
| Memento | `copy.deepcopy` of state, or an immutable snapshot (frozen dataclass/tuple) | Undo stacks where the object must snapshot *itself* to preserve encapsulation |
| Visitor | `functools.singledispatch`, or `match` on type — both give per-type behavior without touching the classes | Many operations over a *stable* heterogeneous tree (compilers/ASTs); brittle when element types still churn |
| Interpreter | Python *is* one — `ast` for Python-like grammars; for tiny DSLs a parser + dict/match evaluator | A real external grammar; reach for a parsing library before hand-building the class-per-rule version |

## The high-traffic judgment calls

**Strategy is the flagship in both directions.** Push *away*: a hierarchy of
stateless single-method classes is functions wearing costumes — replace each with
a plain function and select via a dispatch dict (`stateless_strategy_classes`
finds the mechanical case; even refactoring.guru concedes lambdas/first-class
functions do this "without bloating your code with extra classes"). Push
*toward*: a massive conditional choosing between algorithm variants, duplicated
across call sites, wants *some* strategy shape — but in Python that shape is a
dict of callables, not an ABC with five subclasses. Choose the class form only
when strategies have their own configuration or state.

**Singleton is global state with a title.** Its known costs: hides coupling,
breaks test isolation, drags in threading concerns. The Python answer is the
Global Object pattern — build the one instance at module level and import it —
and even that is a last resort: prefer passing the object in (see *Dependency
injection* in `patterns-and-consistency.md`). Borg/monostate (`self.__dict__ =
_shared_state`) is the same global state with weirder identity semantics. When
you find a hand-rolled `__new__` cache (`handrolled_singleton`), the fix is
usually *not* a better singleton — it's asking why the dependency isn't a
parameter.

**Factories: a verb is enough.** Python needs factory *functions* sometimes
(when construction involves a decision), and `@classmethod` alternate
constructors often (`datetime.fromtimestamp`). It almost never needs factory
*classes*. A `WidgetFactory` whose `create()` returns one type is `Widget()`
with extra steps (`find_overengineering.py` flags it); needing to vary what gets
created means accepting the class or callable as a parameter, not building a
parallel creator hierarchy.

**State: the ladder is real, the pattern is sized.** A string field compared and
reassigned across methods (`string_state_machine`) is a state machine the type
system can't see. The fix escalates with the forces: (1) always — make the states
an `Enum`; (2) behavior branches on state in several methods — a dispatch dict
keyed by state, or `match`; (3) many states, frequent changes, per-state data —
one class per state (full State pattern). Stopping at (1) or (2) is correct for
most code; "may be excessive if a state machine has only a few states or rarely
changes" is the catalog's own caveat.

**Builder vs keyword arguments.** The telescoping-constructor problem the
Builder pattern solves does not exist in a language with keyword arguments,
defaults, and dataclasses. A class of chained `set_x()`-returning-`self` methods
plus `.build()` (`fluent_builder`) should be one call with kwargs and a
`__post_init__` for validation. The convenience-constructor *spirit* of Builder —
simple calls assembling a complex hidden structure, like pyplot — is thriving,
idiomatic Python; it just doesn't look like the GoF diagram.

**Decorator pattern ≠ `@decorator` syntax.** The `@` syntax wraps a function or
class *at definition time, for every use*; the GoF pattern wraps an *individual
object at runtime*. Both are legitimate; don't let the name confuse a review.
For per-object wrapping, the `__getattr__`-forwarding wrapper is the idiomatic
implementation — and remember any wrapper is unmasked by introspection.

**Observer/callbacks: start with a list.** `self._on_change: list[Callable]`
plus a loop is the whole pattern for most needs. Reach for an event class or
library when there are many event types, unsubscription, or async delivery.
The smell pushing *toward* it is real: module A polling B's attributes, or B
hard-coding calls to A, C, and D on every change.

## Patterns the language already ships

Recognize these as patterns *being used correctly*, not as missing patterns:
the iterator protocol and generators (Iterator); context managers (deterministic
acquire/release — what GoF code approximates with try/finally and finalizers;
`try_finally_close` and `finalizer_del` find the unconverted cases); first-class
functions and `functools.partial` (Strategy, Command, Prototype, callbacks);
modules (Singleton, Facade, namespacing); `functools.singledispatch` and `match`
(Visitor, type dispatch); descriptors and `@property` (Proxy for attribute
access — also why `get_x()`/`set_x()` pairs are never needed: a public attribute
can become computed later with zero caller changes); `__getattr__` (dynamic
Decorator/Proxy); `__init_subclass__` (subclass registries without metaclasses);
`abc`/`Protocol` (interfaces — Protocol when you only need structural typing);
`functools.lru_cache`/`cached_property` (memoization, lazy init, Flyweight-ish
caching); `copy.deepcopy` (Prototype, Memento).

## Python-specific patterns worth recommending by name

- **Global Object.** Constants and shared instances live at module level. Keep
  module-level objects *immutable* where possible, and never do I/O at import
  time — import failures fire before logging exists, and "because your code has
  been imported" does not mean "it will be used." Lazy: a `@functools.cache`
  module function.
- **Prebound Methods.** A module-level instance with its bound methods exported
  as module functions (`random.random`, `random.seed` are methods of a hidden
  `Random()`). The right shape when most users want a casual function interface
  but independent instances must stay possible. Only when construction is cheap
  and side-effect-free.
- **Sentinel Object.** `_MISSING = object()` checked with `is` — for when `None`
  is a legitimate value and you must distinguish "not passed." Don't invent
  in-band sentinel *values* (`-1`, `""`) in new code; raise or return an explicit
  marker object.
- **Null Object.** A real, well-behaved stand-in (`NO_MANAGER = Person("no acting
  manager")`) that lets call sites drop their `if x is None` branches. Worth it
  when the None-checks outnumber the object's methods.
- **Registry via `__init_subclass__`.** Plugin tables that fill themselves when a
  subclass is defined — no metaclass, no explicit registration calls
  (`registry_metaclass` flags the metaclass version).
- **Dependency injection without a framework.** Passing a callable or object in
  *is* DI in Python. Constructor injection (`__init__(self, clock=time.monotonic)`)
  covers nearly every case; a DI container in Python code is almost always
  imported ceremony.

## Smell → pattern map (when to push toward)

The forces, with their deterministic tripwires:

| Smell (detector) | Reach for |
|---|---|
| if/elif ladder on a type tag or kind string (`find_design_smells.py: type_switch`) | Dispatch dict → `match` → polymorphism, in that order of escalation |
| Same conditional on object state in several methods (`string_state_machine`) | Enum, then state-keyed dispatch, then State classes (see above) |
| Massive conditional choosing algorithm variants; near-duplicate functions differing in one step | Callable parameter / dispatch dict (Strategy-as-functions); Template-as-callable-params |
| Telescoping constructor, long parameter list (`find_code_smells.py: long_parameter_list`, `find_parameter_objects.py`) | Keyword args + dataclass (parameter object); not a Builder class |
| Subclass explosion across two independent axes | Bridge-shape: composition, pass the implementation in |
| Hard-coded notification calls / cross-module polling | Callback list (Observer) |
| Undo/redo, audit log, or task queue requirements | Command-as-callables + Memento snapshots |
| Sequential validation blob that keeps reordering | List of handler functions (Chain of Responsibility) |
| Per-type operations scattered over a stable class tree | `singledispatch` / `match` (Visitor-shape) |
| try/finally pairs, `__del__` cleanup (`try_finally_close`, `finalizer_del`) | Context manager |
| "Argument omitted" vs "argument is None" bugs | Sentinel Object |
| `if x is None` branches at every call site | Null Object |

## Pattern → simpler form map (when to push away)

| Found in code (detector) | Replace with |
|---|---|
| `__new__`/accessor/metaclass instance caching (`handrolled_singleton`), Borg (`borg_shared_state`) | Module-level instance; better, pass it in |
| Factory class building one type (`find_overengineering.py`) | Direct call; `@classmethod` constructor if a second entry point helps |
| Stateless one-method class hierarchy (`stateless_strategy_classes`) | Functions + dispatch dict |
| Fluent builder (`fluent_builder`) | Keyword arguments / dataclass |
| `__iter__`/`__next__` class (`iterator_class`) | Generator function |
| get/set accessor pairs (`getter_setter_pair`) | Public attribute; `@property` when logic actually arrives |
| None-check lazy property (`handrolled_lazy_property`) | `functools.cached_property` |
| Module-dict memoization (`handrolled_memoize`) | `functools.lru_cache` |
| Registration metaclass (`registry_metaclass`) | `__init_subclass__` |
| Single-impl ABC/Protocol, thin forwarding wrapper (`find_overengineering.py`) | The concrete class; the wrapped object |
| Visitor over a churning hierarchy | Methods on the elements, or `singledispatch` |
| Flyweight without a measured RAM problem | Plain objects (then profile) |

## Patterns that are easy to confuse (review vocabulary)

- **Adapter vs Proxy vs Decorator vs Facade:** Adapter *changes* an interface to
  fit; Proxy keeps the *same* interface and controls access; Decorator keeps the
  interface and *adds behavior* (stackable, client-composed); Facade invents a
  *new, smaller* interface over a whole subsystem.
- **Strategy vs State vs Command vs Template Method:** Strategy = interchangeable
  ways to do the *same* task, mutually unaware; State = behavior varies by mode
  and states know/trigger transitions; Command = *any* operation reified so it
  can be queued/undone; Template Method = the inheritance-flavored Strategy
  (fixed skeleton, overridable steps) — prefer the composition flavor.
- **Mediator vs Observer vs Facade:** Mediator is a hub components knowingly talk
  through (two-way); Observer is dynamic one-way subscriptions; Facade simplifies
  access without adding behavior, and the subsystem doesn't know it exists.
- **`@decorator` vs Decorator pattern:** definition-time, all instances vs
  runtime, per object (see above).

## Review checklist

For each pattern-shaped structure found: **what force does it serve, today?**
One implementation/family/state/strategy → strip it to the simple form. For each
smell the smell-map names: **which is the *lightest* shape that resolves it?**
Enum before State classes, dict before hierarchy, function before class, module
before Singleton, kwargs before Builder. And in both directions: behavior is
sacred — characterization tests first (`safety-net-and-testing.md`), one
pattern-step per commit (`refactoring-techniques.md` for the mechanics).
