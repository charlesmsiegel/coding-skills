# Reviewing AI-generated TypeScript

The compiler catches the easy half of what a generator gets wrong. What survives
is the half that type-checks and is still wrong — and it looks more finished than
hand-written code does, which is precisely the problem.

## The stance

The code compiles. That is the *starting* condition, not evidence. Three
questions, in order:

1. **Does this API exist, with this signature, in this version?** The compiler
   answers this for typed packages — which is most of them now, so a hallucinated
   method usually fails to build. The gap is anything reached through `any`, a
   loose `.d.ts`, `fetch`, an ORM's dynamic surface, or a config object typed
   `Record<string, unknown>`. Those are unchecked, and that is where invented APIs
   land.
2. **Is the logic right for the actual requirement**, or right for a plausible
   generic version of it? The tell is code that handles the textbook case
   beautifully and does not mention the constraint in the ticket.
3. **Does the error path do anything?** Generated code is systematically optimistic
   on the failure path, because failure modes are project-specific and the happy
   path is not.

## The specific tells

**Type-shaped lies.** The type says one thing, the value is another. `as` and
`any` are the load-bearing parts:

```ts
const config = JSON.parse(raw) as AppConfig;    // nothing checked this
const rows = (await db.query(sql)) as User[];   // nor this
```

The type is now a claim with no evidence behind it, and every consumer trusts it.
Check each cast for a validation next to it.

**Ignored options.** A function takes `options` and the body never reads it.
Every caller that passes a setting is silently ignored, and the signature says
otherwise. `find_ai_scaffolding.py` finds these; they are high severity because
they look like working configuration.

**Stubs that satisfy the interface.** `throw new Error("Not implemented")` inside
a class that implements an interface: the build is green, the type is satisfied,
and it fails at the first call. In a large generated change, count the stubs
before reviewing anything else.

**Duplicate definitions.** The same function or type declared twice at module
scope. The later one wins at runtime; the earlier body is dead code that reads as
live. Common when a change was made in two passes.

**Fake robustness.** `try/catch` around everything, catching, logging and
continuing. It looks defensive and converts every failure into a wrong value
further downstream. Also: `?? []`, `?? {}` and `?? 0` on values that should never
be absent — each one hides the case where they are.

**Over-parameterised generics.** `<T>` where `T` appears once, `Partial<Record<K,
V>>` where an interface would do, conditional types for a two-case union. The
shape of expertise without its function.

**Tests that mirror the implementation.** Generated tests are written from the
code, not from the requirement, so they assert what it does — including the bugs —
and pass by construction. Look for: every collaborator mocked, assertions only on
call counts, and no case the implementation obviously does not handle.

**Comments that describe intent that is not in the code.**
`// Retry up to 3 times with backoff` above a single call. The comment is what was
asked for; the code is what was produced.

**Plausible constants.** A timeout of 5000, a page size of 100, a retry count of
3, none of which came from anywhere. Ask where each number is from; a generated
one has no answer.

## The review sequence for a generated change

1. **`analyze_diff.py`** — findings on the changed lines only.
2. **`tsc --noEmit`** on the whole project. A generated change often type-checks
   in isolation and breaks a consumer.
3. **`find_ai_scaffolding.py`** — stubs, ignored options, duplicate definitions,
   merge markers.
4. **`find_type_gaps.py` on the diff** — every new `any`, `as`, `!` and
   `@ts-ignore` is a place the compiler was told to stop. In new code there is
   rarely a legacy excuse.
5. **`find_async_issues.py`** — floating promises and `forEach(async …)` are
   over-represented in generated code, because they read correctly.
6. **Read the tests, hard.** Then break the implementation on purpose and check
   the tests fail. A generated suite that has never failed is not evidence.
7. **Check every claim in the PR description against the diff.** "Adds retry
   logic" and "handles the empty case" are the sentences most likely to be true of
   the description and not of the code.

## Questions worth asking out loud

- Which of these types is checked at runtime, and where?
- What happens when this request fails? When it times out? When it returns 200
  with an error body?
- Which of these branches has a test that fails without it?
- Where did this number come from?
- What did this replace, and was the replaced thing doing something this is not?

## What not to do

Do not reject generated code for being generated. The bar is the same as for any
other change: does it do the right thing, can you tell when it stops, and can the
next person change it. Apply that bar exactly, and apply it to the parts the
compiler could not.
