# Writing the PR

The description exists so a reviewer can check your work without re-deriving it.
Its job is to supply what the diff cannot show: why this change, why this shape of
change, and what you are least sure about.

## Check for a template first

```bash
ls .github/PULL_REQUEST_TEMPLATE.md .github/pull_request_template.md 2>/dev/null
ls .github/PULL_REQUEST_TEMPLATE/ 2>/dev/null
```

If the repo has one, use it. It encodes what these maintainers want to know, and
substituting your own structure is a small act of not reading the room.

## Structure, absent a template

```markdown
Fixes #42

## What was wrong
`runList` read `run.status` directly, but a run stays `queued` until every job
has been dispatched — so a run with jobs already executing reported `queued`.

## The fix
Derive the displayed status from the job statuses (`deriveStatus`, `list.go:88`)
rather than reading the run's own field. `gh run view` had the same bug via the
same field and is fixed by the same change.

## Verification
- New test `TestRunList_QueuedWithRunningJobs` fails before, passes after.
- Full suite green (`go test ./...`).
- Reproduced the original report by hand and confirmed it now shows `in_progress`.

## Notes for the reviewer
- I did not change the API client's `Status` field itself — other callers depend
  on the raw value, and narrowing it looked like a separate change.
- Least sure about: whether `cancelled` jobs should count toward `in_progress`.
  Current behavior treats them as terminal.
```

The last section carries the most weight and is the one usually left out. A
reviewer's attention is finite; pointing it at the part you are least sure of is
worth more than any other paragraph, and it is the thing only you know.

## Linking the issue

Put a closing keyword in the **PR description** (not just a commit message) so the
merge closes the issue:

`Fixes #42` · `Closes #42` · `Resolves #42`

Cross-repo: `Fixes owner/repo#42`. Multiple issues need the keyword each time —
"Fixes #42, #43" only closes #42.

Use a closing keyword only when the PR fully resolves the issue. For a partial
fix, write `Part of #42` or `Refs #42` — auto-closing a half-fixed issue loses the
remaining work.

**Never close the issue by hand.** Link it and let the merge do it, so the
maintainer keeps the judgment about whether it is actually resolved.

## Commits

- Present tense, imperative, explaining *why* where the what is obvious:
  `derive run status from jobs so queued runs with active jobs display correctly`.
- Keep the test and the fix in the same commit, or the test first — never the fix
  first, since that leaves a commit where the bug is silently gone with nothing
  pinning it.
- Don't reference the issue in every commit message; once in the PR body is what
  the tooling reads.

## Creating it

```bash
gh pr create --draft --fill            # draft until CI is green
gh pr create --title "..." --body-file pr.md
gh pr ready                            # once green
```

Open as a **draft** when CI hasn't run. Requesting review on a red build spends a
reviewer's pass on something you already know needs another commit.

Before marking ready:

- [ ] The new test fails without the fix (you watched it fail)
- [ ] The full suite passes
- [ ] Lint and format match the repo's tooling
- [ ] The original reproduction now behaves correctly
- [ ] The description says what you're unsure about
- [ ] No unrelated changes in the diff — `git diff main...HEAD --stat` and read it

That last one catches more review friction than any other: a stray formatting
sweep or a debug print turns a two-minute review into a twenty-minute one.
