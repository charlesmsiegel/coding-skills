# Turning strictness on in a repo that has been running without it

`strict: true` in a 200-file codebase produces thousands of errors, a branch
nobody merges, and a team that concludes strictness is impractical. The point of
this guide is that it is a **ratchet**, not a migration: strictness is turned on
for new code immediately and for old code file by file, and it never goes back.

## What each flag actually buys

`strict` is nine flags. They are not equally valuable, and the error counts differ
by an order of magnitude.

| Flag | What it catches | Typical cost |
|---|---|---|
| `strictNullChecks` | Every `undefined is not an object` — the biggest single class of runtime error in JS | Highest error count, highest value |
| `noImplicitAny` | Parameters and variables that were silently unchecked | High count, mechanical to fix |
| `strictFunctionTypes` | Unsound callback parameter assignment | Low count |
| `strictPropertyInitialization` | A non-optional class field never assigned | Low count (needs `strictNullChecks`) |
| `noImplicitThis` | `this` typed `any` in unbound functions | Very low |
| `useUnknownInCatchVariables` | `catch (e)` typed `any` | Low, mechanical |
| `strictBindCallApply`, `alwaysStrict`, `noImplicitOverride`* | Small, real | Negligible |

\* `noImplicitOverride` is not part of `strict`; it is worth adding.

Beyond `strict`, three more are worth the argument:

- **`noUncheckedIndexedAccess`** — `arr[0]` becomes `T | undefined`. This is the
  one that finds the empty-array and missing-key bugs `strictNullChecks` misses.
  It is also the noisiest, because every indexed read now needs a check. Turn it
  on *after* `strict` has settled.
- **`exactOptionalPropertyTypes`** — separates "absent" from "present and
  `undefined`". Only worth it if your code distinguishes them (patch/PATCH
  semantics, `JSON.stringify` behaviour).
- **`verbatimModuleSyntax`** — forces explicit `import type`. Cheap, and prevents
  a class of bundler and decorator surprise.

## The order that works

**1. Stop the bleeding first.** Before fixing anything, make sure the pile stops
growing. Add the strict config as a *second* tsconfig that CI runs over an
allowlist, or use a lint rule so new files must be strict.

**2. `noImplicitAny` before `strictNullChecks`.** It is mostly mechanical (write
the parameter types) and it makes the `strictNullChecks` pass easier, because the
types are then real.

**3. Leaf modules before hubs.** A shared type fixed in a hub creates errors in
every importer at once. Utilities, constants and pure functions first; the module
graph in reverse topological order. `find_module_issues.py` gives you that graph.

**4. Fix the errors, do not silence them.** The whole point is the errors. A
migration that lands with 400 new `!` assertions and `@ts-expect-error` comments
has moved the problem into a place where nothing will ever revisit it.

**5. Flip the global flag and delete the scaffolding** once the allowlist covers
everything. Leaving both configs alive forever is the failure mode: nobody knows
which one is authoritative.

## Per-file strictness without a fork

TypeScript has no per-file `strict`, so the workable options are:

- **A second tsconfig with `include`.** `tsconfig.strict.json` extends the base,
  sets `strict: true`, and lists the migrated files. CI runs both. Adding a file
  to the list is a one-line PR, which is the right size.
- **`// @ts-strict-ignore` with a plugin** (`typescript-strict-plugin`) — the
  inverse: strict everywhere, opt out per file. Better ergonomics if you are
  willing to add the dependency, because the default is right.
- **Project references** for a monorepo, where the boundary is a package.

Whichever you pick, the list only ever shrinks. Wire that into CI.

## Fixing the errors: the common shapes

**`Object is possibly 'undefined'`** — this is the finding, not the obstacle.

```ts
const user = users.find(u => u.id === id);
user.name;              // error, and correct: find returns T | undefined
if (!user) throw new NotFound(id);
user.name;              // narrowed
```

Reach for `!` only after deciding it is not case 1 or 2 in
`references/type-system.md`.

**`Parameter implicitly has an 'any' type`** — write the type. If it is genuinely
polymorphic, a type parameter; if genuinely unknown, `unknown`. Resist the urge to
sweep these with `any`: that is the state you are migrating out of.

**`Property has no initializer`** — either give it one, mark it optional (and
handle the absence), or use the definite-assignment `!:` *only* when a framework
assigns it (dependency injection, an ORM). That is the one legitimate use of `!:`.

**A field that is only set after `init()`** — this is not a strictness problem, it
is a modelling problem. Two states, one type. Split it (see the discriminated
union section of `references/type-system.md`).

## Measuring progress

Count errors, not files, and put the number in CI output:

```bash
npx tsc --noEmit -p tsconfig.strict.json 2>&1 | grep -c "error TS"
```

A ratchet test that fails when the count *increases* is worth more than a plan.

## The argument you will have

> "This is a lot of churn for no features."

The honest answer: it is not zero-risk and it is not free. What it buys is that a
whole category of production incident stops being possible, and that every future
refactor gets cheaper because the compiler can verify it. Point at the last three
`undefined` incidents in the tracker; if there are none, the case is genuinely
weaker and `noImplicitAny` alone may be the right stopping point.

What is *not* defensible is `strict: true` in the config plus `any` throughout the
code. That is the cost with none of the benefit, and it is the state a rushed
migration produces.
