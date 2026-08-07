# Promises: what the compiler will not tell you

TypeScript checks that a value is a `Promise<T>`. It does not check that anybody
ever looked at it. Every bug in this file compiles.

## Floating promises

A promise nobody awaits, returns, or `.catch`es is *floating*. Three things go
wrong, in increasing order of how long they take to diagnose:

1. **Ordering.** The next line runs before the work finishes. Tests pass locally
   because the machine is fast.
2. **Errors vanish.** A rejection with no handler becomes an unhandled rejection.
   Node has terminated the process on that since v15.
3. **Shutdown races.** The process (or the component) goes away mid-flight, and
   the write lands somewhere that no longer exists.

The fix is to say what you meant:

```ts
await save(x);                        // I need this done before continuing
return save(x);                       // my caller owns it
void save(x).catch(reportError);      // deliberately fire-and-forget, errors handled
```

`void save(x)` **alone** is not a fix — it silences the linter and keeps the bug.
The `.catch` is the part that matters.

**The case the syntactic detector cannot see:** an async function handed to
something that discards its return — `onClick={handleSave}`, `array.forEach(fn)`,
an Express handler, an event emitter. Whether that is a bug depends on the
callee's type, which is exactly what `@typescript-eslint/no-misused-promises`
reads. If the project does not run it, this is the highest-value rule to turn on.

## Array methods and `async`

Only `map` does something useful with a promise-returning callback.

| Call | What actually happens |
|---|---|
| `xs.forEach(async x => …)` | Fires all of them, waits for none, loses every rejection |
| `xs.map(async x => …)` | Returns `Promise<T>[]` — correct, but you must `await Promise.all(...)` |
| `xs.filter(async x => …)` | The predicate returns a `Promise`, which is **always truthy** — nothing is filtered |
| `xs.find/some/every(async …)` | Same: the promise is truthy, so the answer is wrong, not late |
| `xs.sort(async …)` | The comparator returns an object; the sort order is meaningless |

```ts
// Sequential, because each step depends on the last (or the API rate-limits).
for (const x of xs) await handle(x);

// Parallel, because they are independent.
await Promise.all(xs.map(handle));

// An async predicate: resolve first, then filter on the results.
const keep = await Promise.all(xs.map(isReady));
const ready = xs.filter((_, i) => keep[i]);
```

## `await` in a loop

Sequential when it need not be. Every iteration pays a full round trip, so 100
items at 50ms is five seconds instead of fifty milliseconds.

It is *correct* when:
- each iteration's input depends on the previous one's output;
- the remote end rate-limits, or you are deliberately throttling;
- ordering of side effects matters;
- memory matters — `Promise.all` over 100k items opens 100k connections.

When it is deliberate, write a one-line comment saying which of those it is. That
comment is what stops the next person "optimising" it into a thundering herd. When
concurrency needs a ceiling, use a bounded pool rather than all-or-one.

## `Promise.all` vs `allSettled`

`Promise.all` rejects on the first failure and **abandons** the rest — they keep
running, and their rejections are now unhandled. That is right when one failure
makes the whole operation pointless. When you want every result regardless, use
`allSettled` and handle each outcome. Do not use `all` and wrap it in a try/catch
hoping for partial results; you will get one error and no data.

## The Promise constructor

`new Promise(...)` is for wrapping something that is **not** already promise-based
— a callback API, an event, a timer. If the body contains `.then` or `await`, the
wrapper is redundant and lossy.

`new Promise(async (resolve, reject) => …)` is always a bug: the async function's
own promise is discarded, so a `throw` after the first `await` reaches nothing and
`reject` is never called. The promise hangs forever.

```ts
// Legitimate: adapting a callback API.
const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));
```

## Cancellation

Nothing cancels itself. An in-flight `fetch` after unmount, a `setTimeout` after
teardown, a subscription after the last consumer left — each is a leak, a race, or
a write to something that is gone.

`AbortController` is the standard mechanism and `fetch` honours it:

```ts
useEffect(() => {
  const controller = new AbortController();
  load(id, { signal: controller.signal }).then(setData).catch(ifNotAborted);
  return () => controller.abort();
}, [id]);
```

Without it, two quick `id` changes can land out of order and the *older* response
wins. That bug is intermittent, environment-dependent, and will be blamed on the
server.

## Error propagation

- `catch { }` — the error is gone and the function reports success. This is the
  worst outcome available: a wrong answer instead of a failure.
- `catch (e) { console.log(e) }` — the same, with a line in a log nobody reads.
- `catch (e) { throw new Error("save failed") }` — the original stack and message
  are gone. Use `{ cause: e }`; every modern runtime and reporter reads it.
- `try { … } finally { return x }` — the `return` discards an exception in flight.
- Rejecting or throwing a non-`Error` — no stack, and `instanceof Error` checks
  downstream do not match.

The decision at every `catch` is one of three: **handle** it (do something that
makes the failure not matter), **translate** it (add context, re-throw with
`cause`), or **let it through** (do not catch). "Log and continue" is not one of
them unless the operation is genuinely optional — and then say so in a comment.

## Reviewing async code

1. Every promise-returning call: awaited, returned, or `.catch`ed?
2. Every `await` in a loop: sequential on purpose, or accidentally?
3. Every `Promise.all`: is abandoning the rest on first failure right?
4. Every async operation with a lifetime (component, request, job): can it be
   cancelled, and is it?
5. Every `catch`: handle, translate, or let through — which?
6. Every async callback: does the thing receiving it await it?
