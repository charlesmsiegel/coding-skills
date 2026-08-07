# Patterns, and making the codebase use one of them

Two failures live here. The first is a pattern applied where a language feature
would do. The second — more expensive and less often noticed — is three different
right answers to the same question in one codebase.

## The GoF patterns in TypeScript

Most of the classic patterns exist to work around limitations TypeScript does not
have: no first-class functions, no closures, no structural typing, no modules.

| Pattern | TypeScript-native form | When the full pattern is still right |
|---|---|---|
| Singleton | A module-level `const`. Modules are singletons. | Never in app code. Better: pass the instance in. |
| Strategy | A function parameter, or a record of functions | Several strategies with state and lifecycle |
| Factory | A function returning the type | Choosing between types, or non-trivial construction |
| Builder | An options object, or `satisfies` on a literal | Genuinely incremental construction with validation between steps |
| Observer | An `EventTarget`, a callback, or the framework's own | A real pub/sub with many subscribers and lifetimes |
| Command | A closure, or a discriminated union of actions | Undo/redo, queuing, serialisation |
| State | A discriminated union + `switch` | Behaviour (not just data) differing per state |
| Decorator | Function composition, or a wrapping function | Many composable layers over a stable interface |
| Adapter | A function that maps types | A real anti-corruption layer around a system you do not own |
| Template method | A function taking callbacks | Rarely — inheritance for reuse ages badly |
| Iterator | A generator (`function*`) or an iterable | Never hand-roll `next()`/`done` |
| Visitor | An exhaustive `switch` over a discriminated union | Traversing a genuinely open node hierarchy |

The recurring shape: **a class with one method and no state is a function.** If
its constructor takes the collaborators and its single method takes the data,
those are just parameters.

## Smell → the pattern that was missing

| Smell | What to reach for |
|---|---|
| `if/else if` on a string tag, repeated in several files | Discriminated union + exhaustive switch |
| A `status: string` field compared against literals across methods | A state union; the State pattern if behaviour differs |
| A big `switch` that constructs different types | A record of constructors keyed by the tag |
| Long parameter list built up over several calls | Options object; a builder only if steps must validate |
| The same `try/finally` cleanup in a dozen places | A `using`-style helper: `await withConnection(async c => …)` |
| Hand-rolled `next()`/`done` iterator | `function*` |
| Manual memo `Map` around a pure function | A small `memoize` helper, or the framework's cache |
| Callback pyramid | `async`/`await` |
| A class hierarchy for data with no behaviour | A union of plain types |

## Pattern → simpler

| You see | Consider |
|---|---|
| `getInstance()` | A module-level export, or injection |
| `AbstractFooFactory` | A function |
| `FooStrategyImpl` (one impl) | The function it wraps |
| `FooBuilder().withA().withB().build()` | `{ a, b }` |
| `IFooService` + `FooService` | `FooService` |
| `BaseFoo` → `AbstractFoo` → `Foo` | One class, or composition |
| A DI container in a 20-module app | Passing arguments |

## Consistency is worth more than the choice

A codebase that does one thing three ways costs more than a codebase that does the
second-best thing everywhere: every reader must learn all three, every reviewer
must decide which applies, and every new file starts an argument.

Questions worth deciding once and applying everywhere:

- **Errors:** thrown exceptions, or a `Result<T, E>` union? Mixing them means
  every caller must handle both.
- **Async:** `async`/`await` throughout, or promise chains? Pick `await`.
- **Modules:** named exports, or default exports? Named, usually — they are
  greppable, renameable, and auto-import correctly.
- **Types:** `interface` for object shapes and `type` for the rest, or `type`
  throughout. Either. Not both at random.
- **Nullability:** `undefined` or `null` for absence? Pick one; having both means
  every check is `== null` or a bug.
- **Naming:** file naming (`kebab-case.ts` vs `PascalCase.tsx`), test location
  (co-located vs `__tests__`), barrel policy.
- **Validation:** where the boundary is, and with what — one schema library, not
  three.
- **Immutability:** `readonly` on public surfaces, or not.

## How to converge without a big-bang PR

1. **Count.** `git grep -c` the two spellings. The majority is usually the
   decision, already made.
2. **Write it down.** One paragraph per decision, with the reason and one example.
   In `CONTRIBUTING.md` or a `docs/conventions.md`, not in someone's head.
3. **Enforce it mechanically** where possible — an ESLint rule, a tsconfig flag,
   a `find_*` script in CI. A convention nothing checks is a convention that
   decays.
4. **Convert opportunistically**, in the files you are already touching. A
   dedicated "consistency PR" touching 200 files is unreviewable and conflicts
   with everything.
5. **Only ratchet.** New code follows the rule from the day it is written; old
   code converges. The count only goes down.

## When inconsistency is fine

Two subsystems with genuinely different constraints may legitimately differ — a
performance-critical hot path, a generated client, a module that mirrors an
external API's shape. Say so in a comment where the difference starts, so the next
reader knows it is a decision and not a drift.
