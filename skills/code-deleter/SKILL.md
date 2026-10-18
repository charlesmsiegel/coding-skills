---
name: code-deleter
description: Use when the user wants less code, safe deletions, or behavior-preserving simplification with no net increase in lines of code. Applies to redundant defensive checks, dead paths, duplicate logic, unnecessary wrappers, speculative abstractions, and over-engineering in any language. Also triggers on "code deleter", "delete unnecessary code", "simplify without adding code", and "make this smaller". For feature additions or behavior changes, use a different workflow.
---

# Code Deleter

**Every run must end with fewer or the same code lines as it started with.
Net growth is forbidden. A verified no-op is a successful result.** Preserve
observable behavior; a smaller broken program fails this skill.

## The budget

Measure against the **worktree at invocation**, including staged, unstaged, and
untracked code, never against HEAD or a merge base. Preserve the user's existing
edits. The budget covers every code change this run leaves behind, including
tests, helpers, scripts, and code outside the initially targeted module.

- Require both production code alone and all code including tests to have a
  delta <= 0. Necessary new tests are allowed only within actual code savings.
- Count physical and nonblank lines after the project's normal formatting.
  Neither measure may grow. Review the diff for actual code reduction too:
  deleting comments, blank lines, documentation, fixtures, or generated/vendor
  content cannot fund new implementation. Do not remove tests or weaken checks
  to make the budget pass.
- No minification, semicolon packing, unreadable expressions, moving logic into
  strings/config, new dependencies to hide implementation, or counting moves
  and renames as deletions. Keep the accounting scope and test classification
  fixed throughout the run. No unrelated cleanup to manufacture deletion credit.
- If verification or the budget fails, shrink the proposed change or undo only
  this run's edits. Never leave net-positive code as a partial result. If a safe
  refactor needs more lines, report it as a separate recommendation.

## Workflow

1. **Establish the baseline before editing.** Read local instructions and the
   relevant entry points, callers, tests, and contracts. Record the starting Git
   status; save original contents for files you may edit so rollback preserves
   dirty work. Keep scratch probes and the budget snapshot outside the project.
   Use the shipped gate below. If a complete baseline cannot be measured, make
   no code changes and report the limitation.

2. **Find candidates, then prove them.** Search for repeated logic, unused private
   code, pass-through layers, duplicated validation, and abstractions with no
   current purpose. Use [references/safe-deletions.md](references/safe-deletions.md)
   for the evidence required. Search absence, type annotations, and green tests
   alone do not establish safety. For each edit, state what becomes unnecessary
   and which enforced invariant or caller evidence proves it.

3. **Make the smallest justified simplification.** Prefer an existing primitive
   or direct flow to a new abstraction. Preserve results, exception types and
   messages, ordering, mutation, side effects, cleanup, public interfaces, and
   supported compatibility. Do not silently fix bugs or change requirements.
   In review-only requests, report candidates and leave the code unchanged.

4. **Verify behavior and size.** Establish relevant behavior before editing;
   reuse existing tests and add focused characterization cases only where needed
   and affordable. Exercise the removed branch's boundary and failure paths.
   Run the applicable tests, type/build checks, and formatter, then inspect the
   final diff and rerun the gate. Missing tools or unresolved baseline failures
   are limitations, never a passing verification. Undo edits whose safety cannot
   be established. Recheck after any further edit, including test changes.

5. **Deliver the measured result.** Give before/after/net code and production
   counts, the safety evidence for each retained simplification, checks run and
   limitations, and valuable candidates skipped. With no safe reduction, report
   zero change. Do not create a report file or commit unless requested.

## Mechanical gate

Needs Git and Python 3.11+. Let `SKILL` be this skill's installed directory and
`BASELINE` a fresh path in a temporary directory **outside** the project. Commands
run from the project's Git root. Adapt variable syntax to the current shell.

Inventory the project's authored code first. Repeat `--include` for **every**
source type/path (including templates and executable scripts) and `--tests` for
every test convention. These are case-sensitive, repository-relative globs;
`*.py` includes nested files because `*` also matches `/`. The following is a
Python example, not a default scope for other languages:

```bash
python "$SKILL/scripts/line_budget.py" snapshot --root . --baseline "$BASELINE" --include '*.py' --tests 'tests/*' --tests 'test_*.py' --tests '*/test_*.py'
# Perform the justified refactor, tests, and normal formatting.
python "$SKILL/scripts/line_budget.py" check --root . --baseline "$BASELINE"
```

The snapshot records current tracked and nonignored untracked source. The check
includes newly created matching files and remembers original paths even if they
become ignored. Exit **0** means all four budgets pass, **1** means growth,
**2** means measurement failed; only 0 permits retaining a verified refactor.
Never overwrite/recreate the starting snapshot, narrow its patterns, reclassify
production as tests, change ignore rules, edit excluded code, or introduce an
unmeasured source type. The gate refuses directory boundaries that hide nested
Git repositories or submodules. Work wholly inside a measurable worktree or
leave code unchanged; do not exclude a boundary to force a passing measurement.
An aggregate gate cannot prove behavioral safety or detect comment-only credit;
the contract and diff review remain mandatory.
