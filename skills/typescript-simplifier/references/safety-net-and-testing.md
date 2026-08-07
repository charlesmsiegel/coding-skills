# The safety net you need before refactoring

Refactoring means changing structure without changing behaviour. Without a way to
detect a behaviour change, you are not refactoring — you are rewriting and hoping.

## What counts as a net

In rough order of what they catch:

1. **`tsc --noEmit` passing, under `strict`.** In TypeScript this is a real part
   of the net, not a formality: rename, extract, move method and change-signature
   are all verified by the compiler. It catches shape changes; it catches nothing
   about values.
2. **Tests that assert values**, not that a function ran.
3. **Characterization tests** around the specific code you are about to change.
4. **Coverage data** — not as a target, but to answer "does this line ever run".

## Building a characterization test

You are not writing the test the code deserves. You are writing a test that
records what it does **today**, including the parts that look wrong. If the
behaviour is a bug, that is a separate change, made deliberately, after.

```ts
// Not "the right answer" — the current answer.
it("characterizes formatOrder for the legacy discount path", () => {
  expect(formatOrder(legacyOrder)).toEqual({
    total: "12.30",          // note: string, not number, and rounds half-down
    discount: undefined,     // note: absent, not 0
  });
});
```

The technique when you cannot predict the output: assert something wrong, run the
test, and paste the actual value in — after reading it and confirming it is what
the code really does now. Snapshot tests are the automated form of this and are
fine *for this purpose*, provided somebody reads the first snapshot.

Where to point it:
- The public entry points of the module you are changing, not its internals — you
  are about to change the internals.
- The edge cases the code visibly handles: the early returns, the `catch`, the
  `?? default`. Those branches are the behaviour that gets lost.

## Signs a test suite is not a net

The detector finds these mechanically; here is what to do about each.

**No assertion.** Passes as long as nothing throws. That is a smoke test wearing a
unit test's name. (Testing Library's `getBy*`/`findBy*` queries *do* assert, since
they throw when nothing matches — those are fine.)

**Only `toBeDefined` / `toBeTruthy`.** Passes for almost any value. Assert the
value.

**`expect(...).rejects` or `.resolves` without `await`.** The assertion never
runs. The test passes unconditionally, forever. This is the worst one in the list
because it looks the most correct.

**An `async` test that never awaits.** Finishes before the thing it tests.

**`.only` committed.** The rest of the file is silently skipped and CI is green.

**Everything mocked.** A test where every collaborator is a mock asserts the
wiring, not the behaviour: it fails on every refactor (because the wiring changed)
and passes through real breakage (because the mocks still return what they always
did). Mock what is slow, non-deterministic, or external; use the real thing for
the rest.

**`if` inside a test.** One path asserts nothing and you cannot tell from a green
run which ran. Two tests.

**Assertions on mock call counts only.** `expect(save).toHaveBeenCalledTimes(1)`
tells you a function ran. It does not tell you the system did the right thing.

## Coverage, used correctly

Coverage is a **map of the untested**, not a score. Two questions it answers well:

- Which lines of the module I am about to change never execute? Those are the ones
  where a refactor can silently break something.
- Did my new characterization tests actually reach the branch I care about?

It answers "is this code correct" not at all: a suite of assertion-less tests hits
100%.

`find_untested_modules.py` answers a cheaper question — does *any* test import
this module — which is enough to find the gaps before you start. Coverage
(`run_external_tools.py --run-coverage`) answers the finer one.

## The order of operations

1. Confirm `tsc --noEmit` and the tests are green **before** touching anything. A
   red baseline makes every later signal useless.
2. Run `find_untested_modules.py` and `find_test_smells.py` over the area you plan
   to change.
3. Write characterization tests for the entry points you are about to disturb.
   Watch them pass, then break something deliberately and watch them fail — a
   test you have never seen fail is not evidence.
4. Refactor in small steps, green after each.
5. Delete the characterization tests that have been superseded by better ones. The
   scaffolding is not the building.

## Ratcheting

When a class of problem is cleared, make it impossible to come back:

- A tsconfig flag (the strongest — it is checked on every build).
- An ESLint rule with `error` severity.
- One of these detectors in CI with a zero-findings threshold for its category.
- A coverage floor for a specific directory — the whole-repo number is a
  meaningless average.

The rule that makes a ratchet work: **the number only ever goes down.**
