# Replying and closing out the review

Every thread gets an answer, including the ones you did not act on. A thread with
code changed but no reply makes the reviewer diff the branch to find out what you
did; a thread with neither makes them repeat themselves.

## What a reply contains

Three things, in about two sentences:

1. **What you did** — fixed, didn't fix, or need input.
2. **Where** — the commit or the file:line, when there was a change.
3. **Why** — only when it isn't obvious. A fixed nit needs no rationale; a decline
   always does.

| Case | Reply |
|---|---|
| Fixed as asked | "Done in `a1b2c3d`." |
| Fixed differently | "Fixed in `a1b2c3d`, though I hoisted the guard into `validate()` instead of inlining it — three callers needed the same check." |
| Declined | "Not doing this one — `parse()` returns `None` for malformed input (hit via the import path), so unwrapping would crash the worker. Left the check in. Happy to change it if you'd rather it raise." |
| Question answered | "It retries twice because the upstream 502s on cold start. Added a comment at line 34 so the next reader doesn't have to ask." |
| Need their input | "Not sure which you want here — is the ordering guarantee from the queue, or the single-writer assumption? If the latter, your change is right and I'll make it." |
| Out of scope | "Agreed that path is confusing. Out of scope for this PR — want me to file an issue?" |

Skip the padding. "Great catch, thanks so much!" on fourteen threads is noise a
reviewer scrolls past, and it buries the two replies that actually needed reading.
One "good catch" on the one they genuinely caught means something.

## Posting

**Show the drafts to the user before posting anything.** Replies are public and
attributed to them.

```bash
# Reply in a specific inline thread (in_reply_to is the review comment id,
# which is the numeric tail of the thread URL: .../#discussion_r2263783218)
gh api repos/{owner}/{repo}/pulls/{number}/comments \
  -f body="Done in a1b2c3d." -F in_reply_to=2263783218

# One summary comment on the PR instead of per-thread replies
gh pr comment 42 --body-file reply.md
```

For a review with many small threads, a single summary comment is often kinder
than fourteen notifications — group the replies by file, and reserve per-thread
replies for the ones where the location matters. For a review with two substantive
threads, reply in place.

## Resolving

**Only the ones you actually satisfied.** Resolving is the signal that a point was
addressed; using it to clear a queue destroys the signal for everyone who relies
on it.

Leave open: anything you declined, anything awaiting the reviewer's answer, and
anything you fixed differently than asked — those need the reviewer's eyes, and
resolving them hides the divergence.

Some teams reserve resolving for the reviewer entirely. Check the repo's
`CONTRIBUTING.md` before resolving anything, and when in doubt, leave it and say
"addressed in a1b2c3d, leaving this open for you to close."

## Re-requesting review

Once the threads are answered and CI is green:

```bash
gh pr ready 42                      # if it was a draft
gh pr review 42 --request-changes   # not this — that's for reviewing others
gh pr edit 42 --add-reviewer <login>  # re-request after changes
```

Pair it with a short summary comment so the reviewer knows what to re-read:

> Pushed three commits: extracted the validation (`a1b2c3d`), fixed the empty-batch
> crash you found plus a test (`e4f5g6h`), and took the naming nits (`i7j8k9l`).
> Left one thread open — the ordering question on `worker.py:88` needs your call.
> CI is green.

## What not to do

- **Don't force-push over review history** unless asked. Reviewers navigate by
  commit; rewriting the branch orphans their comments and loses the thread
  anchors.
- **Don't resolve to make the page look clean.**
- **Don't mark ready for review with red CI.** It wastes a reviewer's pass.
- **Don't answer only the easy threads.** The hard one is the reason the PR is
  still open, and skipping it is more visible than you'd think.
