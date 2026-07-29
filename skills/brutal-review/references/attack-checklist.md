# The attack pass

Reading a diff top-to-bottom finds style. Attacking it by failure mode finds bugs.
Work these angles deliberately; for each one, the question is always the same:
**what input, sequence, or state makes this produce the wrong answer?**

Not every angle applies to every change. Skipping an irrelevant one is judgment;
skipping a relevant one silently is a gap you owe the author a sentence about.

---

## 1. The seam the diff hides

A diff shows changed lines. It does not show the code that *calls* them, which is
where most real breakage lives. This angle is first because it is the one a
diff-shaped review structurally cannot see.

- **Every caller of every changed signature.** Grep for the symbol. A new
  parameter with a default is source-compatible and can still be wrong at every
  existing call site, because those sites now silently take the default.
- **Return-shape changes.** A function that returned `None` on failure and now
  raises, or returned a list and now returns a generator — every consumer's error
  path just changed. Who catches it? Who iterates twice?
- **Widened or narrowed types.** A field that was non-null and is now optional
  breaks every reader that didn't check.
- **Removed behavior.** A deleted branch, a dropped side effect, a log line
  something greps for. Who depended on it?
- **Serialized and persisted shapes.** A changed dict key, column, or wire field
  meets data written by the *old* code. Is there data in flight? A migration?
- **Concurrent deployment.** For a period, old and new code run at once against
  the same data. Does that work?

## 2. Boundaries and degenerate inputs

The cases pattern-completion does not produce, in rough order of how often they
actually bite:

- **Empty** — empty list, empty string, empty file, zero rows, no matches.
- **One** — the off-by-one's favorite home; also the case where "join with commas"
  and "pluralize" go wrong.
- **All / none** — every item matches, no item matches.
- **Duplicates** — the same key twice, the same item twice. Does a dict comprehension
  silently drop one?
- **Boundary values** — `0`, `-1`, `MAX`, exactly-at-the-limit, one past it.
- **Absent vs empty vs null** — three distinct states routinely collapsed into one.
  Which does the code mean? Which does the caller send?
- **Unicode, width, and normalization** — non-ASCII names, combining characters,
  emoji in something length-limited, RTL text in something concatenated.
- **Very large** — the input 1000× bigger than the test fixture. Where does it go
  quadratic, allocate the whole thing, or hit a limit?

## 3. Error and failure paths

The happy path is what the author tested. The failure path is what ships broken.

- **Every `try`: what is actually caught, and is it caught too broadly?** A broad
  catch around a wide block hides failures nobody anticipated.
- **Swallowed errors.** An `except`/`catch` that logs and continues leaves the
  program running on state it doesn't understand. What is the state afterward?
- **Partial failure.** The operation half-succeeded: three of five records
  written, the file created but not filled, the row inserted but the cache not
  invalidated. Is that state recoverable? Is it detectable?
- **Cleanup on the error path.** Does the file/socket/lock/transaction close when
  the middle throws? Is it a context manager / `defer` / `finally`, or hope?
- **Retries.** Is the retried operation idempotent? Is there a bound? Does the
  backoff have jitter, or will every client retry in lockstep?
- **Error messages.** Does the message name the value that failed, or just the
  type? Does it leak a secret, a token, or a full internal path?

## 4. Time, ordering, and concurrency

Only attack this when the code is genuinely concurrent — but when it is, this is
where the expensive bugs are.

- **Check-then-act.** `if not exists: create` is a race unless the create is
  atomic. Same for read-modify-write on a shared counter or file.
- **Shared mutable state.** Module globals, class attributes, caches, connection
  pools. What happens with two requests at once?
- **Ordering assumptions.** Does the code assume events arrive in order, that a
  callback fires before something else, that a dict preserves insertion order
  *across processes*?
- **Timeouts.** Is there one? What happens when it fires mid-operation?
- **Clock.** Timezone-naive datetimes, `now()` called twice and assumed equal, DST
  transitions, monotonic vs wall clock for durations.
- **Reentrancy.** Can this be called again while it's still running?

## 5. Resources and lifecycle

- Files, sockets, locks, cursors, subprocesses, temp files — opened where, closed
  where, and closed on the exception path too?
- Unbounded growth: a cache with no eviction, a list that only appends, a log that
  never rotates.
- Anything acquired in a loop and released outside it.

## 6. Security, when the change touches a boundary

Only when input crosses a trust boundary — but then, thoroughly.

- **Injection** — SQL/shell/template/path built by concatenation from anything a
  user influences.
- **Authorization at the object level.** The endpoint checks *that* you're logged
  in; does it check that this record is *yours*? This is the single most common
  real vulnerability in application code.
- **Secrets** — hardcoded, logged, in an error message, in a URL, committed.
- **Deserialization** of untrusted input; `eval`/`exec` on anything derived from input.
- **Path traversal** — a user-supplied name reaching the filesystem without
  normalization.
- **Timing** — secret comparison with `==` rather than a constant-time compare.

## 7. The tests

The tests are part of the change and get attacked like the rest of it.

- **Does the test fail if the feature is broken?** Mentally revert the
  implementation. If the test still passes, it tests nothing. This is the single
  highest-value question on this list.
- **Assertions that cannot fail** — `assert result is not None`, `assert True`,
  asserting on a mock's return that the test itself configured.
- **Over-mocking** — so much mocked that only the mocks are exercised.
- **The untested case is the interesting case.** Which of §2's degenerate inputs
  has no test?
- **Shared state between tests** — passes alone, fails in suite, or passes only in
  a given order.

## 8. The theory, not the code

The deepest angle, and the one worth reaching for when the change is large or
architectural. Everything above asks what input breaks a line. This asks what
input breaks the *model*.

- What does this change believe the world is? State it in one sentence.
- What real-world case does that sentence not cover? (One user per account — until
  a merge. One currency — until it isn't. Names have two parts — they don't.)
- Which coherent alternative reading of the requirement did the author *not*
  implement, and would a reasonable person have expected that one?
- The next obvious feature request: does it extend this naturally, or does it need
  a special case bolted on? If the latter, the abstraction is already wrong and
  every line built on it is future rework.

A finding here outranks anything in §1–§7, because the other sections describe
code that can be fixed and this one describes code that has to be replaced. It
also demands the most evidence — name the requirement and the case, or it is
architecture astronomy.
