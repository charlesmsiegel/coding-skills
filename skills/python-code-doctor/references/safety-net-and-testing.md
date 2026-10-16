# Safety Net and Testing

This skill's first rule is **behavior is sacred** — and the only thing that makes
that rule real is a test that fails when behavior changes. Before you refactor a
poorly-tested codebase you have to build the net you'll be working over. This guide
is about doing that quickly and honestly: measuring what's covered, pinning what
isn't, and refusing to trust tests that don't actually test anything.

The deterministic helpers `find_untested_modules.py` (which source modules no test
references) and `find_test_smells.py` (tests that assert nothing, over-mock, or
hide logic) point at the gaps. This file is the judgment that goes with them.
"References" is read generously: an import, an attribute chain off an imported
package (`store.attempts.forget(...)`), `importlib`/`runpy` by name or by path, or —
for a module of web routes — a test string requesting one of its paths. A route
module none of those reach is only a candidate, since a test client can drive it
through the app with URLs no reading of the source can see; confirm with coverage.

## The order of operations is not negotiable

You do not get to refactor first and test later. The sequence is always:

1. **Measure** what is covered. You cannot refactor safely what you cannot observe.
2. **Pin** current behavior with characterization tests where coverage is thin and
   the code is about to change.
3. **Refactor** in small steps, running the tests after each one.
4. **Then** improve the tests — only once the structure is stable.

Skipping step 1 or 2 means you are refactoring blind, and "I'm pretty sure this is
equivalent" is exactly how silent behavior changes ship.

## Measure first: coverage as a map, not a grade

Run the existing suite under coverage and read it as a **map of where you can move
safely**, not as a number to maximize:

```bash
python -m pytest --cov=<pkg> --cov-report=term-missing   # if pytest-cov is present
# or, tool-agnostic:
python -m coverage run -m pytest && python -m coverage report -m
python -m coverage html        # browse uncovered lines line-by-line
```

Read it against churn (`SKILL.md` step 2). The dangerous quadrant is **high-churn ×
low-coverage**: code that changes often and would change silently. That is where
characterization tests go first. Don't chase 100% — chase coverage of the lines you
are about to touch. A module you will never edit does not need a test today.

If there is no suite at all, `find_untested_modules.py` will emit
`no_tests_in_repo`. That is the whole task changing shape: **establishing the
ability to test is the first deliverable**, ahead of any simplification.

## Characterization tests: pin behavior you don't understand

A characterization test does not assert what the code *should* do — it records what
it *does* do, bugs and all, so that a refactor that changes the output gets caught.
The point is a tripwire, not a specification.

The mechanical recipe:

1. Call the code with a representative input.
2. Assert against a placeholder.
3. Run it; let it fail; copy the **actual** output into the assertion.
4. Now it's a tripwire. If a refactor changes the output, you'll know — then you
   decide whether the change was intended.

```python
def test_characterize_invoice_total():
    # Not "the right answer" — the CURRENT answer. Pin it, then refactor under it.
    result = compute_invoice({"items": [...], "discount": "0.1", "region": "EU"})
    assert result == {"total": "118.80", "tax": "18.80", "currency": "EUR"}
```

Bias toward inputs that exercise the **branches you're about to refactor** and the
edge cases the code clearly cares about (empty, zero, negative, None, the weird
special-case `if`). One test through each branch beats fifty through the happy path.

## Golden master: pin behavior too wide to enumerate

When a function's output is large or its input space is huge — a report generator,
a serializer, a formatter, a tangle you can't even read yet — pin the **whole
output** against a stored snapshot instead of writing assertions by hand.

1. Capture outputs for a batch of real (or realistic) inputs.
2. Store them as the golden master (a file committed to the repo).
3. The test re-runs the inputs and diffs against the master; any difference fails.

```python
def test_golden_master_report(snapshot_dir):
    for case in load_fixtures("cases/"):
        out = render_report(case.input)
        golden = snapshot_dir / f"{case.name}.txt"
        if not golden.exists():            # first run records the master
            golden.write_text(out)
        assert out == golden.read_text(), f"output changed for {case.name}"
```

This is the highest-leverage move on legacy code you don't yet understand: it lets
you refactor aggressively under a net that covers behavior you never had to
articulate. The risk is pinning a bug as "correct" — that's acceptable. The job
right now is *no change in behavior*, not *correct behavior*. Fix bugs in a
separate, clearly-labeled step, after the structure is sound.

## Don't trust tests that don't test (run find_test_smells.py)

A green suite over hollow tests is worse than no suite: it gives false confidence.
Before you lean on existing tests, audit them. `find_test_smells.py` catches the
mechanical cases; confirm them by reading.

- **Asserts nothing** (`test_without_assertion`). A test that runs nothing that can
  fail (`pass`, an assignment) is reported high: add real assertions or delete it —
  a test that can't fail is noise that makes the bar look met. A test that calls code
  and never asserts only proves *it didn't raise*, and is reported low: sometimes
  that is the whole contract ("an empty store must not raise"), but even then a
  postcondition — nothing written, state unchanged — lets it catch more than a
  crash; otherwise assert the result. An assertion one step away
  counts — in a helper the test calls (`_assert_same`, `check_reply`, a same-module
  function that asserts), in a nested function it runs or installs as a tripwire, in
  an `except` clause, or spelled `pytest.fail(...)` / `raise AssertionError(...)`.
- **Tests the mocks, not the code** (`overmocking`). When a test mocks every
  collaborator, it asserts that the code calls the mocks the way the test said —
  i.e. it pins the implementation, not the behavior, and survives even when the
  real integration is broken. Prefer real objects, fakes, or in-memory doubles;
  reserve mocks for true boundaries (network, clock, filesystem, randomness).
- **Hides logic** (`logic_in_test`). `if`/`for`/`while` in a test means the test
  has branches that may not run, and a bug in the test masquerades as a pass.
  Make tests linear and obvious; use parametrization for multiple cases. Two shapes
  have no branch that can be skipped and are not reported: a `for` over a fixed,
  non-empty table written in the source (`for mode in ("soft", "hard"):`,
  `range(3)`, a module constant bound once) with no `break`/`continue`/`return` in
  it — parametrize still reports each case separately, which is worth having but is
  style — and a guard that ends the test loudly, `if not shutil.which("git"):
  pytest.skip(...)` or `if got != want: pytest.fail(diff)`. A loop over a computed
  collection is reported: it passes vacuously when the collection is empty.
- **Skipped without a reason** (`skipped_without_reason`). A silent `@skip` is dead
  coverage pretending to be alive. Give it a reason and a ticket, or delete it.
- **Asserts on incidental detail.** A test that pins log strings, dict ordering, or
  whitespace breaks on harmless refactors and trains people to ignore failures.
  Assert on the contract, not the coincidence.

The test of a test: **could it ever fail?** If you can't construct the change that
breaks it, it isn't protecting anything.

## What "tested enough to refactor" actually means

You are ready to refactor a unit when:

- Its **observable contract** — return values and side effects, for the inputs that
  matter — is pinned by a test that would fail if you broke it.
- The **branches you intend to touch** each execute under some test.
- You can run that test in **seconds**, so you actually run it after every step.

That is the bar. It is lower than "comprehensive test suite" and higher than "it
imports without error." Meet it for the code in front of you, then move.

## Then, and only then: ratchet coverage

Once a hot module is refactored under its net, lock the net in. Add a coverage floor
in CI (`--cov-fail-under`) set to *current* coverage, not an aspirational number, so
it can only go up. Wire `find_test_smells.py` into CI so new assertion-free tests
can't merge. Coverage that isn't enforced silently rots back down — see
`references/messy-repo-runbook.md` for the ratchet pattern in full.
