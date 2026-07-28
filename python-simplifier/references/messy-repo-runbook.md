# Messy-Repo Runbook

The rest of this skill assumes you're reviewing code that already builds, has some
tests, and is roughly formatted. A **poorly written but functional** repository
often satisfies none of those. This runbook is the campaign for walking into one
cold and getting it under control — the sequence that makes every other detector
and guide in this skill *safe to act on*.

The governing idea: **establish the conditions for safe change before you change
anything.** Each phase below leaves the repo in a better-defended state than it
found it. Do them roughly in order; do not skip ahead to clever refactors.

## Phase 0 — Get it running and observable

You cannot simplify what you cannot run. First, make the repo reproducible and
visible, touching no logic:

- **Reproduce the environment.** Find the Python version it actually needs; create
  a clean virtualenv; get install + import to succeed. Note every undocumented
  step — you'll fix the manifest in Phase 4 (`find_dependency_issues.py`).
- **Run whatever tests exist** and record the result, even if it's "collection
  error." That output is your starting baseline.
- **Measure coverage and find the hot files.** Combine coverage with churn
  (`SKILL.md` step 2). The map of *high-churn × low-coverage × high-complexity* is
  your work order for the entire campaign. Cold, well-covered code waits.

Deliverable: the repo runs, you can execute its tests, and you know where the
danger is. No code has changed.

## Phase 1 — Build the safety net

Behavior is sacred, and only a test enforces that. Before refactoring the hot
files, pin their behavior. This is the load-bearing phase — see
`references/safety-net-and-testing.md` for the how.

- If `find_untested_modules.py` reports `no_tests_in_repo`, standing up pytest and a
  first characterization test **is** the deliverable for this phase.
- For each hot module you're about to touch, add characterization or golden-master
  tests that would fail if its observable behavior changed.
- Audit existing tests with `find_test_smells.py` — a suite full of
  assertion-less tests is a net with no rope. Don't lean on it until you've checked.

Do not proceed to structural changes on any unit until its contract is pinned.

## Phase 2 — Mechanical normalization (one reviewable baseline)

Now make the diffs of every *later* phase legible by getting all the noise out of
the way first, in commits that change **zero behavior**:

- **Format and sort imports** repo-wide in a single commit: `ruff format` (or
  Black) + `ruff check --select I --fix`. One giant whitespace commit that touches
  everything, then never again — so subsequent diffs are pure intent.
  `scripts/run_external_tools.py --fix` runs the installed formatters (black, isort,
  ruff --fix) for you; if they aren't installed, it tells you so — ask the user
  before installing anything into their environment.
- **Apply only the safe autofixes**: `ruff check --fix` for the rules that are
  unambiguously behavior-preserving (unused imports `F401`, `pyupgrade`/`UP`,
  obvious `SIM`/`RET`). Review the diff; revert anything that smells semantic.
- **Normalize the mechanical junk**: line endings, tabs-vs-spaces, file encodings,
  trailing whitespace, a missing `.gitignore`.

Run the Phase 1 tests after this phase. They must still pass — that's the proof the
normalization was safe. Commit formatting **separately** from logic forever after;
never bury a behavior change inside a reformat.

## Phase 3 — Run the detectors and triage

Only now is the codebase clean enough that the analyzers' output is signal, not
noise. Run the full suite and work the deliverable artifact:

```bash
python scripts/analyze_all.py . --format json > report.json
python scripts/format_findings.py report.json --min-severity high
```

Triage in this priority order — **correctness before structure before style**:

1. **Real bugs** — `find_mutation_hazards`, `find_exception_issues`,
   `find_global_state`, `find_resource_leaks`, plus anything `find_security_issues`
   surfaces. These are not cleanups; they're defects. Fix under the safety net.
2. **Structural** — `find_import_cycles` (cycles, god modules), `find_coupling_issues`,
   `find_duplicates`, `find_overengineering`, `find_parameter_objects`. This is the
   high-leverage simplification; use the judgment guides.
3. **Idiom & readability** — `find_unpythonic`, `find_outdated_idioms`,
   `find_return_issues`, `find_loop_simplifications`, naming, comments, docstrings,
   type gaps. Mostly autofixable or quick.

One smell → one entry → one small, behavior-preserving PR. Never batch unrelated
changes; a reviewer must be able to see that each diff preserves behavior.

## Phase 4 — Dependency and packaging hygiene

A functional mess usually has a dishonest manifest. Run `find_dependency_issues.py`
and reconcile what the code imports with what's declared:

- **Missing deps** → add them, so a fresh install actually works.
- **Unused deps** → remove them (or document the non-import reason).
- **Unpinned deps / no manifest** → pin versions and consolidate on one
  `pyproject.toml` so installs are reproducible. Add a lockfile.
- Check for known-vulnerable / abandoned packages and a sane Python-version floor.

## Phase 5 — Ratchet: make the cleanup irreversible

A cleanup with no enforcement decays back to the mean within months. The instant a
whole class of problem is cleared, turn on the gate that keeps it gone — this is the
single highest-return habit in the whole campaign.

- **Adopt one config and a CI gate.** A `pyproject.toml` with Ruff (lint + format),
  a type checker, and pytest+coverage. A CI workflow that runs all three on every PR.
  Use `scripts/run_external_tools.py` to see which of these the environment already
  has and to fold their output into the same findings format during the cleanup.
- **Baseline, don't boil the ocean.** You can't fix every violation at once and you
  shouldn't block all work to try. Ratchet instead:
  - Coverage: `--cov-fail-under=<current>` — it can only climb.
  - Lint/types: enable the rules you've cleared as *errors*; record the rest as a
    grandfathered baseline (e.g. a Ruff per-file-ignore list, or mypy's
    `--no-error-summary` baseline) that may only shrink. New code is clean; old
    code is paid down as it's touched.
  - Wire the unique detectors here (`find_mutation_hazards`, `find_import_cycles`,
    `find_security_issues`, `find_test_smells`) into CI so the bugs you fixed can't
    silently return.
- **Pre-commit hooks** for format + the fast lint rules, so the baseline never
  regresses locally.

The test of this phase: could the exact mess you just cleaned reappear and merge
without a red build? If yes, you haven't ratcheted — you've only mopped.

## What to resist the whole way through

- **Rewrites.** "This is so bad I'll just rewrite it" throws away the only thing the
  repo has going for it — it *works*. Strangle it incrementally under tests instead.
- **Behavior changes smuggled into cleanups.** Fixing a bug you find is good — in
  its own labeled commit, never inside a "refactor" or "reformat."
- **Boiling the ocean.** You will not fix everything. Fix what churns, ratchet it
  shut, move on. Cold code that blocks nothing stays as-is (`SKILL.md`: when NOT to
  simplify).
- **Polishing before the net exists.** Every phase here is downstream of Phase 1.
  No tests, no refactor.
