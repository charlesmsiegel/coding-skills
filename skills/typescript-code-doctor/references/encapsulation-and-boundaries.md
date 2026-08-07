# Who is allowed to change what

Encapsulation is not about classes. It is about being able to answer "if I change
this, what else can break?" in bounded time. Every leak makes that answer larger.

## The three boundaries

TypeScript has three, and they are enforced very differently:

| Boundary | Enforced by | Escapable at runtime |
|---|---|---|
| `private` / `protected` | The compiler only | Yes — `(obj as any).secret`, or any JS caller |
| `#name` (ECMAScript private) | The runtime | No |
| Module (not exported) | Bundler + runtime | No |

**The module boundary is the strong one.** A non-exported binding is genuinely
unreachable — no cast gets to it. When you need real encapsulation, the answer is
usually "do not export it", not "add `private`".

Between `private` and `#name`: `private` is the default choice (better tooling,
no downlevel cost, and the compiler is the audience). Use `#name` when the class
is consumed from JavaScript, or when the field must be inaccessible even to a
determined caller.

## Class fields

Default to `private readonly`, and widen only when something needs it.

```ts
class Order {
  public items: Item[] = [];          // anyone can push, splice, or replace it
  private readonly items: Item[] = []; // the class owns its contents
}
```

A public mutable field is an invariant nobody can defend: every assignment
anywhere becomes part of the class's contract, and "who sets this?" becomes a
codebase-wide grep. `readonly` costs nothing at runtime and turns the accidental
later write into a compile error.

**Parameter properties** make the good default terse:

```ts
constructor(private readonly db: Db, private readonly clock: Clock) {}
```

**Getter/setter pairs that only forward** are a public field written the long way.
They defend nothing and cost a call. Make it a field; reintroduce accessors when
there is real logic — validation, laziness, a computed view.

## Returning your internals

```ts
getItems(): Item[] { return this.items; }   // the caller now holds your array
```

The caller can `push`, `sort` or `splice` it and your invariants are gone, in a
line of code that does not mention your class. Three fixes, in order of
preference:

1. **Do not return it.** Expose the operation the caller actually wanted
   (`totalPrice()`, `hasItem(id)`).
2. **Return a read-only view.** `readonly Item[]` / `ReadonlyMap<K, V>` — free at
   runtime, and the compiler rejects mutation at every call site.
3. **Return a copy.** `[...this.items]`. Real cost, real safety, and the caller
   may mutate freely.

The same applies to accepting one: if you store an array a caller handed you, you
are sharing state with them. Copy on the way in, or document that you do not.

## Module-level state

```ts
export let currentUser: User | null = null;   // a global with import syntax
let cache = new Map<string, Row>();           // a global with better manners
```

An **exported `let`** is the worse one: importers see a live binding that changes
under them, at a moment they cannot observe. Export a function instead, so the
module keeps control of when the value changes and callers can see that they are
asking.

A **module-level `let` mutated from inside functions** is a global. It makes tests
order-dependent (module state persists across test files in the same worker), it
cannot be scoped per request in a server, and two consumers cannot have different
values. Sometimes it is genuinely right — a process-wide cache, a memoised
connection — and then it should be behind a function that owns its lifetime and
can be reset in tests.

Writing to `window` / `globalThis` is the same thing without even the module as a
scope.

## Immutability at the boundary

Mutating something you do not own is the highest-severity finding in this area,
because the effect appears in a caller that never mentioned your function.

```ts
function normalise(user: User) { user.name = user.name.trim(); return user; }  // surprise
function normalise(user: User): User { return { ...user, name: user.name.trim() }; }
```

The same for arrays: `items.sort()` sorts *in place*. `[...items].sort()` does
not. `toSorted`, `toReversed`, `toSpliced` and `with` (ES2023) do the copy for
you where available.

Mutating an **imported** object is worse again: the change is global and invisible
to every other importer.

When mutation is the point, say so in the name — `sortInPlace`, `applyPatch` —
and take the mutable thing explicitly.

## Reaching in

`other._private` and `other.#x` from outside the owner are the same smell as a
public field, seen from the other end. Either the behaviour belongs on that object
(move the method there — see feature envy) or the member is really part of its
API and should be named as such.

Long chains — `order.customer.address.city.name` — are the module-level version.
The caller now depends on every type along the path, and any of them changing
breaks it. Ask the first object for what you want, or destructure once at a
boundary you control. The exception is data you own end to end: walking a plain
JSON shape you just parsed is not a Demeter violation.

## What good looks like

- The module exports the smallest set of names that lets consumers do their job.
- Every field is `private` or `readonly` unless something demonstrably needs
  otherwise.
- Collections leave the class as read-only views or copies.
- No exported mutable bindings; shared state lives behind a function that owns
  its lifetime and can be reset.
- Functions do not modify their arguments, or say in the name that they do.
- "If I change this field, who breaks?" is answerable by reading one file.
