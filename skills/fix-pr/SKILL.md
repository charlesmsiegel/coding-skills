---
name: fix-pr
description: Work through the review feedback on a pull request — collect every inline thread, decide what each one actually asks for, fix what should be fixed, and answer what shouldn't. Use when the user says "fix the PR comments", "address the review feedback", "handle the review on PR 42", "respond to the reviewer", "my PR has changes requested", or "what's blocking this PR". Pulls resolved state and CI status too, so settled threads are not re-litigated and a red build is not mistaken for a review problem. A reviewer suggestion that is wrong gets a reasoned reply, not a silent implementation — complying with bad feedback is a failure mode, not politeness. Requires the gh CLI.
---

# Fix PR Review Feedback

Turn a pile of review comments into a set of committed fixes plus a set of honest
replies. The deliverable is both — a PR where every thread got an answer, not one
where every thread got a code change.

Let `SKILL=/path/to/this/skill` — the directory holding this SKILL.md. Commands run
from the repository being worked on, not from the skill directory. Needs **Python
3.11+**, `git`, and an authenticated **`gh`** with network access; `fetch_pr_feedback.py`
queries the GraphQL API through `gh` and can do nothing without it. If `gh auth status`
fails, say so — never reconstruct review threads from memory or from the diff.

## Collect everything first

```bash
python "$SKILL/scripts/fetch_pr_feedback.py"            # the current branch's PR
python "$SKILL/scripts/fetch_pr_feedback.py" 42         # a specific PR
python "$SKILL/scripts/fetch_pr_feedback.py" 42 --repo owner/name
python "$SKILL/scripts/fetch_pr_feedback.py" --format json   # for programmatic triage
python "$SKILL/scripts/fetch_pr_feedback.py" --all      # include already-resolved threads
```

`gh pr view --comments` shows review bodies but **not the inline comments**, and the
inline comments are where the actual requests live. This script pulls both, plus:

- **resolved / outdated state per thread** — so settled discussions stay settled and
  threads pointing at moved code are flagged rather than acted on blindly;
- **`suggestion` blocks** — reviewer-authored patches that can be applied verbatim;
- **CI status** — because a red build is a different problem from a review comment,
  and fixing it first often resolves several comments at once.

Threads are sorted so unresolved ones on current code come first.

## Workflow

1. **Read the failing checks before the comments.** A broken build is usually
   cheaper to fix and frequently the root of several review remarks. Fixing it
   first can make part of the review moot.

2. **Read the diff you are being reviewed on.** `gh pr diff 42`. Feedback is not
   interpretable without it, and a comment on line 61 means nothing until you know
   what line 61 became.

3. **Triage every thread before changing anything.** Classify each as *must-fix*,
   *nit*, *question*, or *wrong suggestion* — `references/triaging-feedback.md`
   has the rubric and, more importantly, the standard for pushing back. Triage the
   whole set first; reviewers routinely make the same point in three places, and
   one fix should close all three.

4. **Fix in coherent commits, not one commit per comment.** Group by the change
   being made. A reviewer re-reading the PR wants to see "extract the validation"
   once, not five commits each moving one line.

5. **Verify.** Run the repo's tests and linters. If a comment pointed at a real
   bug, add the test that would have caught it — a fix with no test invites the
   same comment on the next PR.

6. **Answer every thread**, including the ones you did not act on.
   `references/replying.md` covers what a good reply contains and how to post it.
   An unanswered thread reads as ignored, and it is the fastest way to make a
   reviewer repeat themselves.

7. **Summarize for the author-facing turn**: what changed, what you pushed back on
   and why, and what still needs their decision.

## Do not comply with feedback that is wrong

This is the discipline the whole skill hangs on. A reviewer is a person with
partial context, working fast, sometimes wrong. Silently implementing a mistaken
suggestion is not deference — it puts a defect in the codebase *and* denies the
reviewer the correction. Both of you end up worse off.

So: when a suggestion would break something, contradicts a constraint the reviewer
can't see, or rests on a misreading of the code, **say so with the evidence** and
leave the code alone. When you genuinely can't tell whether they're right, say
that too and ask — do not guess and do not quietly split the difference.

The inverse also holds: do not argue to protect your own work. The question is
never who is right, only what the code should be.

## Boundaries

- **Never resolve a thread you did not satisfy.** Resolving is the author's signal
  that the point was addressed; using it to clear a queue destroys its meaning.
- **Never force-push over review history** unless the user asks. Reviewers navigate
  by commit; rewriting the branch orphans their comments.
- **Never post replies without the user's go-ahead.** Replies are public, attributed
  to them, and hard to take back — draft them, show them, then post.
- **Stay in scope.** Reviewers often mention adjacent problems. Note them; fix them
  only if the user says so. A PR that grows during review never lands.

## Reference index (load on demand)

| Load this when… | File |
|---|---|
| Classifying feedback and deciding whether to comply, push back, or ask — with the standard of evidence each needs | `references/triaging-feedback.md` |
| Writing and posting the replies, and closing out the review | `references/replying.md` |
