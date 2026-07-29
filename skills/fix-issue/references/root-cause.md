# Reproducing, and finding the actual defect

## Why reproduction comes first

An unreproduced bug has no verification. You can write a fix, write a test, watch
the test go green, and have changed nothing about the user's problem — because
nothing ever connected your test to their symptom. The reproduction is that
connection, and skipping it makes the rest theatre.

It is also the cheapest way to discover the report is wrong, which happens often
enough to be worth the ten minutes.

## Building the reproduction

Work down this list; stop at the first thing that fails the way the issue says.

1. **The exact command or input from the issue.** `fetch_issue.py` pulls the code
   blocks out for you.
2. **The stack trace's top frame.** It names the file and line where the failure
   surfaced — start reading there and work outward.
3. **The paths mentioned in the thread**, which the script also collects. Reporters
   who name a file are usually right about it.
4. **A test in the repo's own harness.** Once it reproduces there, it is the
   failing test from step 4 of the workflow. This is the goal — a shell repro is a
   stepping stone to it.

Match their environment where the issue depends on it: version, OS, locale,
timezone, data. Note anything you couldn't match; it may be the whole bug.

## When it won't reproduce

Do not fix it anyway. A speculative fix for an unconfirmed bug adds untested code
and closes the issue without solving it — the reporter comes back in a month and
the trail is now colder.

Work out which of these it is, and say so:

| Signal | What it means | What to do |
|---|---|---|
| Works on your machine, fails on theirs | Environment: version, OS, locale, timezone, filesystem, terminal | Ask for the specific facts you're missing. `gh version`, OS, locale, the exact input |
| Passes now, failed then | Already fixed, or intermittent | Check the log since the report: `git log --oneline --since=<issue date>`. If fixed, say which commit and ask whether to close |
| Fails sometimes | Race, ordering, or state dependence | Run it in a loop; run the suite in a different order. Intermittency is a finding, not an obstacle |
| The steps are incomplete | The reporter omitted context they thought obvious | Ask. Precisely — "what does `config.yml` look like?" beats "please provide more info" |
| It behaves as designed | Not a bug, or a documentation gap | Say so with the reasoning. Frequently the real fix is docs or an error message |

Asking is a real answer here. A question that names the one missing fact gets
answered; "cannot reproduce, please advise" does not.

## Symptom versus cause

The report describes where the problem *surfaced*. The defect is usually upstream.

> **Symptom:** "`gh run list` shows a run as queued while its jobs are running."
> **Cause candidates:** the API returns a stale status; the client reads the run's
> status instead of deriving it from its jobs; a cache isn't invalidated; the
> status enum lost a case in a refactor.

Only one of those is fixed by changing the display code, and four different
"fixes" would all make the reported symptom go away. That is what makes symptom
fixing so easy to do by accident — it works, briefly.

The test that you have the cause: **you can state the mechanism in one sentence
and predict a second symptom from it.** "The client reads `run.status` rather than
deriving from jobs, so `gh run view` will show the same staleness" — then go check
`gh run view`. If the prediction holds, you have the cause. If you cannot make one,
you have a correlation.

### Tools that answer "when did this start"

```bash
git log -S'functionName' --oneline        # commits that added/removed that string
git log -L 40,60:path/to/file.py          # the history of just those lines
git bisect start && git bisect bad && git bisect good <tag>
git blame -w -C path/to/file.py           # ignoring whitespace and moved code
```

A bug that used to work has a commit that broke it, and that commit usually
explains the intent you're about to violate. Read its message and its diff before
undoing it — the "fix" may reintroduce the bug that commit was fixing.

### Signs you are patching a symptom

- The fix is in display, formatting, or serialization code, but the complaint is
  about a *value* being wrong.
- The fix is a special case for the reported input, and you cannot say what the
  general rule is.
- You added a null check, retry, or `try/except` without knowing why the null,
  failure, or exception occurred.
- The fix makes the test pass but you cannot explain why it was failing.
- You needed to change the test's expectation to make it pass.

Any of these means stop and go back to isolating. A patched symptom is worse than
an open issue: the issue is now closed and the defect is still there, with a layer
of misleading code on top of it.

## Bugs whose real fix is out of scope

Sometimes the cause is architectural and the honest fix is a week of work. Say so
rather than quietly shipping a workaround.

State: what the actual defect is, what fixing it properly would take, and what a
contained mitigation would look like. Then let the user choose. If they take the
mitigation, say in the PR and the code that it *is* one, and what the real fix is —
an undocumented workaround becomes permanent, and the next person to read it will
assume it was the intended design.
