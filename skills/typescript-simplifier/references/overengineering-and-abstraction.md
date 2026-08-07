# Does this abstraction earn its keep?

TypeScript makes abstraction unusually cheap to *write* and unusually cheap to
*do without*. Both facts matter here, and the second is the one people forget.

## The structural-typing argument

In a nominally-typed language (Java, C#), an interface is how two types become
compatible. In TypeScript they are compatible if their shapes match, whether or
not any interface exists. That removes most of the traditional reasons to declare
one:

- **"I need it to mock in tests."** No. Any object with the right shape satisfies
  the parameter. A `Partial<Service>` cast, a hand-written literal, or a fake
  class all work with no interface.
- **"I need it for dependency injection."** No. The parameter type can be the
  class itself, or an inline object type.
- **"Someone might implement it differently later."** That is YAGNI, and when the
  day comes the refactor is `Extract Interface` — one command in every IDE.

What is left, and is genuine:
- **Two or more real implementations exist now**, and callers must not care which.
- **A published boundary**: a plugin contract, a package's public API, an
  adapter port with implementations on both sides of a process boundary.
- **Declaration merging** — you need consumers to augment it.

The detector reports an interface implemented once. Check it against that list.

## The tests each abstraction has to pass

Ask all four. One "no" is usually enough.

1. **Does it exist more than once?** One implementation, one caller, one strategy,
   one config value — the abstraction is describing a single thing at a distance.
2. **Does it hide something a reader is better off not knowing?** Good abstraction
   removes detail. A wrapper that forwards adds a name and hides nothing.
3. **Can you name it without "Manager", "Helper", "Util", "Service", "Handler",
   "Wrapper" or "Base"?** Those names are what you write when the thing has no
   single responsibility to name.
4. **Does removing it make the code longer?** Try it in your head. If inlining the
   abstraction *shortens* the code, it was negative-value.

## The specific shapes, and what to do

**Single-implementation interface.** Delete it; use the class or an inline object
type. Keep it only for a published boundary.

**Abstract class with one subclass.** Collapse. An abstract base holds what
several subclasses share; with one, it is one class in two files with an
inheritance hop in every stack trace.

**All-static class.** A module already is a namespace. `export function` each
member and delete the class. (`@typescript-eslint/no-extraneous-class`.)

**`FooService` with one public method and no state.** A function. The class adds a
construction step, an object identity nothing uses, and a name at every call site.

**Factory that constructs one type.** `new Foo(...)`. A factory earns its place
when it *chooses* between types or hides non-trivial construction.

**Pass-through module.** `export const getUser = (id) => api.getUser(id)` × 12.
Import `api` directly. The layer has to be updated for every signature change and
hides where the work happens. Legitimate only when it is genuinely translating —
different types, different error model, an anti-corruption layer around something
you do not control.

**Single-use type parameter.** `function parse<T>(s: string): T` relates nothing;
the caller picks `T` and nothing checks it. Return `unknown`.

**Generic that could be concrete.** A `Repository<T>` with one `T` is a
`UserRepository` with extra syntax and worse error messages.

**Options bag with unused options.** Every option is a branch you must support
forever. `find_ai_scaffolding.py` flags the ones the body never reads; also look
for the ones every caller passes the same value for.

**Barrel file.** See `references/modules-and-dependencies.md` — it is
over-engineering with a build-performance cost attached.

## DRY, and where it goes wrong

The rule is not "no two pieces of code may look alike". It is **"every piece of
*knowledge* has one authoritative representation"**. Two functions with identical
shape encoding two different business rules are not a DRY violation, and merging
them is the classic wrong abstraction:

```ts
// Same shape, different rules. Merging these produces a function with a
// `kind` parameter and two branches — worse than the duplication.
function priceForRetail(o: Order) { return o.subtotal * 1.2; }
function priceForTrade(o: Order)  { return o.subtotal * 1.2; }
```

Signals it is real duplication (extract it):
- Fixing a bug in one copy means fixing it in the others.
- The copies change together in the history.
- The concept has a name.

Signals it is coincidence (leave it):
- The copies have diverged before.
- Merging needs a flag parameter or a `kind` discriminator.
- They belong to different domains, layers, or teams.

**Duplication is cheaper than the wrong abstraction.** A wrong abstraction couples
two schedules of change and gets harder to unpick with every caller. When unsure,
wait for the third occurrence.

The type-level version of this is worth stating too: two interfaces with the same
members are not necessarily one type. Structural typing already lets them
interoperate; merging them couples two things that may legitimately diverge.

## YAGNI, applied

Delete on sight: config that never varies, a parameter every caller passes the
same value for, `| undefined` on something that is always present, an extension
point with one extension, a plugin system with one plugin, a version field that
is always 1, and an abstraction whose commit message says "for future
flexibility".

The counter-argument — "it will be expensive to add later" — is usually false in
TypeScript specifically, because the refactors (extract interface, widen a type,
add a parameter) are mechanical and the compiler finds every call site.
