# Cleaning up a working-but-messy TypeScript repo, from cold

The situation: it builds (or nearly), it ships, nobody enjoys touching it, and
there is no obvious place to start. This is the order that works. Do not skip
ahead — every later phase depends on the earlier ones being true.

## Phase 0 — make the ground stable (hours)

You cannot refactor against a moving baseline.

1. **Install and build.** `npm ci` (not `install` — you want the lockfile's
   answer). Then the build. Record every step that is not in the README, and put
   it in the README.
2. **`tsc --noEmit`.** If it does not pass, that is the whole of Phase 0. Errors
   in the compiler are not a style issue; they mean nobody knows what the types
   are. Fix them, or find the config that was silently excluding half the tree.
3. **Run the tests.** Count passes, failures and skips. A suite with 40 skipped
   tests has 40 unverified claims.
4. **Audit the config.** `find_tsconfig_issues.py`. If `strict` is off, note it —
   it changes how you read everything else, because half the types are lying about
   `null`.
5. **Commit nothing yet.**

## Phase 1 — build the net (a day, sometimes more)

`find_untested_modules.py` and `find_test_smells.py`. You are not trying to reach
a coverage number; you are trying to be able to detect a behaviour change in the
code you are about to touch.

- If there are **no tests at all**, stop and write characterization tests for the
  three highest-churn modules. Nothing below is safe without them.
- If there are tests but they are hollow (no assertions, everything mocked, `.only`
  committed), fix those first. A test that cannot fail is worse than no test,
  because it will be cited as coverage.

See `references/safety-net-and-testing.md`.

## Phase 2 — normalize, in one behaviour-free commit (an hour)

Format the whole repo with Prettier or Biome, apply the safe ESLint autofixes, and
commit **that and nothing else**, with a message saying so. Add the SHA to
`.git-blame-ignore-revs`.

Doing this first means every subsequent diff is real. Doing it *later* means every
subsequent diff is 400 lines of whitespace with three real changes hidden in it.

Do not "fix a few things while you are in there". That is what makes this commit
unreviewable.

## Phase 3 — triage (an hour)

```bash
python "$SKILL/scripts/analyze_all.py" . --format json > report.json
python "$SKILL/scripts/run_external_tools.py" . --format json > tools.json
git log --since="1 year ago" --name-only --pretty=format: \
  | grep -E '\.(ts|tsx)$' | sort | uniq -c | sort -rn | head -30 > churn.txt
```

Cross the two: **high churn × high severity is the work.** Everything else is a
backlog, and a lot of it should stay there. Cold code that nobody touches and
nothing blocks is not worth the risk of changing it, however ugly.

Order within the work:
1. Correctness bugs (floating promises, swallowed errors, shared mutation,
   resource leaks) — these are defects, not debt.
2. Security leads.
3. Structural blockers in the hot files — the cycles and god modules that make
   every change expensive.
4. Type-safety holes at the boundaries of hot modules.
5. Everything else.

## Phase 4 — fix, in small commits (weeks, alongside feature work)

One smell class per PR, smallest first. "Remove all `var`" is reviewable in ten
minutes; "clean up the orders module" is not reviewable at all.

Sequence that avoids re-work:
1. Delete dead code. Everything after this is cheaper for not having to consider
   it.
2. Break the import cycles. They make every other refactor's blast radius
   unpredictable.
3. Fix the correctness bugs.
4. Then the structural work in the hot files.

`tsc --noEmit` and the tests after every commit. Ship the small ones continuously
rather than accumulating a branch — a two-week refactor branch will lose to
`main` and be abandoned.

## Phase 5 — ratchet (an afternoon, once)

Every class you cleared, you now prevent:

- Turn on the tsconfig flag. This is the strongest ratchet available, because it
  is checked on every build by every developer.
- Turn the ESLint rule to `error`.
- Add the relevant detector to CI with a zero-findings threshold for its category.
- Record the current count where a rule cannot be turned on wholesale, and fail
  the build when it goes up.

Without this phase the repo returns to its previous state in about six months,
and the next person concludes that cleanup does not work.

## Phase 6 — write it down (an hour)

A short `docs/conventions.md`: the decisions you made (error handling, module
style, validation library, test location), one example each, and one line on why.
Undocumented conventions are re-litigated in every code review by people who
were not there.

## What to resist

- **Rewriting.** The messy code works and encodes years of edge cases you will
  discover one production incident at a time.
- **Upgrading everything at once.** Frameworks, TypeScript, the bundler and the
  test runner in one PR means you cannot tell which one broke it.
- **A "clean up everything" branch.** It will not merge.
- **Fixing the low-severity findings first** because there are more of them and
  they are easy. That is motion, not progress.
