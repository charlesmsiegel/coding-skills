# Using the type system instead of working around it

The detectors find where checking was switched off. This is how to decide what
should have been there instead.

## The one question

**Where does untrusted data become a trusted type?** Every TypeScript codebase
has a boundary — an HTTP response, `localStorage`, a config file, `postMessage`,
a form. Inside the boundary, types are facts the compiler enforces. Outside, they
are assumptions. `as` is how people pretend the boundary does not exist.

A well-typed codebase has few casts and they are all in the same few files. A
badly-typed one has casts scattered everywhere, because the boundary was never
drawn and every consumer has to re-assert what it hopes is true.

```ts
// The boundary, done wrong: one assertion, unlimited blast radius.
const user = (await res.json()) as User;

// Done right: the assertion is a check, and the type is earned.
function isUser(value: unknown): value is User {
  return typeof value === "object" && value !== null
    && typeof (value as Record<string, unknown>).id === "string";
}
const raw: unknown = await res.json();
if (!isUser(raw)) throw new Error("unexpected /user payload");
const user = raw;  // User, and true
```

In a codebase with a schema library (zod, valibot, arktype, typebox), the parse
call *is* the boundary and the inferred type is the only declaration you need.
Suggest one when there are more than a handful of hand-written guards.

## `any` vs `unknown`

`any` is not "a value of unknown type". It is **"stop checking, and keep not
checking everything this touches"**. It is contagious: a property read off an
`any` is `any`, the return of a call on it is `any`, and it silently defeats
every check downstream, including in files nobody edited.

`unknown` is the honest version: you may hold it, pass it and store it, but you
must narrow before you use it. Replacing `any` with `unknown` produces a burst of
errors — every one of them is a place the code was already unchecked.

| Situation | Use |
|---|---|
| Genuinely unknown shape (parsed JSON, caught error) | `unknown`, then narrow |
| "Any object" | `Record<string, unknown>` or a named interface, not `object` or `{}` |
| "Any function" | The actual signature `(x: string) => void`, not `Function` |
| A generic container the caller parameterises | A type parameter `<T>` |
| A third-party type is wrong | Augment the module's types, or one cast in one adapter |
| You have not worked out the type yet | `unknown` and a TODO — `any` is a decision you will not revisit |

**`catch` is always `unknown`** (with `useUnknownInCatchVariables`, which `strict`
turns on). Anything can be thrown. `e instanceof Error ? e.message : String(e)`
is the honest read.

## When `as` is right, and when it is a lie

`as` never changes a value. It changes what the compiler is willing to believe
about it. It is legitimate when **you know something the compiler cannot** and
that knowledge is checked somewhere:

- Immediately after a runtime validation the compiler cannot follow.
- Narrowing a literal into a branded/opaque type after validating it.
- `as const` — the opposite of a cast: it *narrows*, and costs nothing.
- Test doubles, where the object is deliberately partial.

It is a lie when it is used to make an error go away:

- `as any` — always. If the type is wrong, fix the type or validate the value.
- `as unknown as T` — the compiler has told you the two types have nothing in
  common, and this is the syntax for overruling it. Somebody will be paged.
- Asserting a wider type down to a narrower one without a check
  (`as User` on a `Record<string, unknown>`).

### `satisfies` is usually what was wanted

`as` widens *and* silences. `satisfies` checks without widening, so the literal
keeps its narrow inferred type:

```ts
// `as` loses the literal types: routes.home is string.
const routes = { home: "/", user: "/user/:id" } as Record<string, string>;

// `satisfies` checks the shape AND keeps "/" and "/user/:id" as literals.
const routes = { home: "/", user: "/user/:id" } satisfies Record<string, string>;
```

Whenever you see `as SomeWiderType` on an object literal, `satisfies` is almost
certainly the fix.

## Non-null assertion (`!`)

`x!` says "this is never null here" with no evidence. Ask which of three cases it
actually is:

1. **The type is wrong.** `document.getElementById` returns `T | null` because it
   can. Handle the null, or throw with a message that names what was missing.
2. **The invariant is real but unexpressed.** A field set in `init()` and used
   everywhere after is genuinely non-null — but the type does not say so. Use a
   discriminated union of the "uninitialised" and "ready" states, or a
   constructor that cannot produce the bad state.
3. **A narrowing the compiler lost.** `if (x.y) { use(x.y!) }` fails because `x.y`
   is a property access it cannot track across a call. Hoist it:
   `const y = x.y; if (y) use(y);`

A codebase full of `!` has usually never made the second case explicit.

## Discriminated unions: the highest-leverage move

Most "type switch" smells are a union that was never declared. Compare:

```ts
// Every consumer re-derives the rules, and nothing checks them.
interface Job { status: string; result?: string; error?: string; startedAt?: Date }

// The states are the type; illegal combinations cannot be constructed.
type Job =
  | { status: "queued" }
  | { status: "running"; startedAt: Date }
  | { status: "done"; result: string }
  | { status: "failed"; error: string };
```

The second version deletes every `if (job.result)` guard, makes
`job.result` unavailable unless `status === "done"`, and — with an exhaustive
switch — **fails the build** when someone adds a `"cancelled"` state:

```ts
function assertNever(value: never): never {
  throw new Error(`unhandled case: ${JSON.stringify(value)}`);
}

switch (job.status) {
  case "queued":  return "Waiting";
  case "running": return `Since ${job.startedAt.toISOString()}`;
  case "done":    return job.result;
  case "failed":  return job.error;
  default:        return assertNever(job);
}
```

This is the single change that most often removes a whole class of runtime bug.
When you see an interface where several fields are optional and only certain
combinations are legal, this is the finding.

## Enums

`enum` is one of the few TypeScript features that emits runtime code, which puts
it at odds with `isolatedModules`, `verbatimModuleSyntax`, `erasableSyntaxOnly`
and Node's type-stripping. Numeric enums are also unsound — any number assigns to
one. Prefer either:

```ts
type Status = "queued" | "running" | "done";        // usually enough

const Status = { Queued: "queued", Done: "done" } as const;  // when you need a value
type Status = (typeof Status)[keyof typeof Status];
```

Keep an existing enum if it is used widely and works. Do not introduce new ones,
and never introduce `const enum` in a library (it breaks consumers that transpile
per file).

## Generics

A type parameter exists to **relate** two things: an argument to a return, or one
argument to another. If `T` appears once in the signature, it relates nothing —
callers can instantiate it with anything and the body knows nothing about it. That
is `any` with more syntax.

```ts
function parse<T>(json: string): T          // a lie: `parse<User>(...)` checks nothing
function parse(json: string): unknown       // honest
function first<T>(items: T[]): T | undefined  // real: input relates to output
```

Constrain rather than widen: `<T extends { id: string }>` says what the body
needs. And when the signature has grown three parameters and a conditional type,
ask whether two ordinary overloads would say the same thing to a reader.

## Readonly and immutability

`readonly` on a property, `ReadonlyArray<T>` / `readonly T[]` on a collection, and
`as const` on a literal are free at runtime and catch a real class of bug: the
caller who sorts the array you handed back. Use them at API boundaries —
parameters you promise not to mutate, and returns you do not want mutated.

They are shallow. `readonly` on an object property does not freeze the object's
own properties. When deep immutability matters, say so at the boundary (`Object.freeze`,
or a library) rather than assuming the type conveys it.

## Interface vs type alias

Both are fine. Consistency matters more than the choice. The genuine differences:

- `interface` declarations merge; `type` aliases do not. Merging is what you want
  for module augmentation and what you do *not* want for local models — a second
  `interface User` silently extends the first instead of erroring.
- `type` can express unions, tuples, mapped and conditional types; `interface`
  cannot.

A workable rule: `interface` for object shapes that something implements or that
consumers extend, `type` for everything else, especially unions.

## Reading a type-gap finding

| Finding | The question to ask |
|---|---|
| `explicit_any` | Is the shape genuinely unknown (→ `unknown` + narrow), or just unwritten (→ write it)? |
| `as_any` | What error is this silencing? Fix that. |
| `double_assertion` | The two types are unrelated. Which one is wrong? |
| `type_assertion` | Is there a check next to it? If not, what makes this true? |
| `non_null_assertion` | Which of the three cases above is it? |
| `ts_ignore` | Convert to `@ts-expect-error` with a reason; it then expires on its own. |
| `all_optional_type` | Which combinations are actually legal? That is your union. |
| `unsafe_builtin_type` | What is the real signature/shape? |
| `untyped_parameter` | Under `noImplicitAny` this is a build error, not a style note. |
