# Triaging review feedback

Every thread gets exactly one classification before any code changes. Triage the
whole set first — reviewers make the same point in several places, and you want one
fix to close all of them rather than three divergent ones.

## The four classes

### Must-fix

A real defect, a broken contract, a violated project convention, or a blocking
reviewer verdict (`CHANGES_REQUESTED` on a specific point).

**Action:** fix it. If it was a behavioral bug, add the test that would have caught
it — otherwise the same comment returns on the next PR, and the reviewer learns
their feedback doesn't stick.

### Nit

Correct code the reviewer would have written differently. Naming, ordering,
a clearer idiom, a comment.

**Action:** take it unless it makes the code worse. Nits are cheap and declining
them costs more goodwill than they're worth. Two exceptions: when the nit conflicts
with a convention used elsewhere in the file (say so and point at the neighbours),
and when several nits together would restructure the change (that's a design
discussion, not a nit — reclassify it).

### Question

The reviewer is asking, not requesting. "Why does this retry twice?" "Is this
called from the worker path too?"

**Action:** answer it. Then ask whether the answer belongs in the code — a question
usually means the code didn't explain itself, so the durable fix is a comment, a
better name, or an assertion that makes the invariant explicit. Answering in the
thread and leaving the code opaque means the next reader asks again.

A question with an implied request ("Shouldn't this be cached?") is a request.
Treat it as one, and answer it as one.

### Wrong suggestion

The reviewer is mistaken: their fix would break something, they misread the code,
or they're missing a constraint.

**Action:** do not implement it. Reply with the evidence and leave the code as it
is. See the standard below.

## The standard for pushing back

Pushing back is the right move often enough that it needs a real bar, and low
enough stakes that the bar shouldn't be heroic. Before you decline a suggestion,
have one of these:

- **A failing case.** "That would break for an empty batch — `items[0]` on line 22."
- **A constraint they can't see.** "This runs inside the request thread, so the
  lock would serialize every request."
- **A convention with a pointer.** "The other three handlers in this package return
  `Result`; switching this one to exceptions would give callers two conventions."
- **A measurement.** "I tried that; it's 40% slower on the 10k-row fixture."

What is *not* sufficient: "I prefer it this way", "that's how I've always done it",
or silence. If you have none of the four, you do not have a pushback — you have a
preference, and you should take the suggestion.

Then say it plainly and without hedging into mush:

> Not doing this one — `parse()` can return `None` here (malformed input hits it in
> the import path), so unwrapping directly would crash the worker rather than skip
> the row. Left the check in. Happy to change it if you'd rather it raise.

Note the shape: the decision, the evidence, and a door left open. That last clause
matters — you may still be the one who's wrong.

## When you genuinely cannot tell

Say so and ask. This is a real answer, not a cop-out, and it beats both guessing and
splitting the difference into something neither of you wanted.

> I don't think I have the context for this one — is the ordering guarantee here
> coming from the queue, or are we relying on the single-writer assumption? If it's
> the latter your change is right and I'll make it.

The one thing not to do: implement a compromise nobody asked for. A half-applied
suggestion satisfies no one and is harder to review than either option.

## Reviewer suggestion blocks

A ```` ```suggestion ```` block is a patch the reviewer wrote and GitHub can apply
verbatim. `fetch_pr_feedback.py` surfaces them separately because they're the
highest-signal feedback on the PR — someone cared enough to write the fix.

They still get triaged. A suggestion block is a proposal, not a merge command, and
reviewers write them fast: check that it compiles, that it handles the cases the
original did, and that it doesn't silently drop a branch. Apply it as-is when it's
right — don't rewrite a correct suggestion into your own phrasing, since the
reviewer will diff it and wonder what you changed.

## Outdated threads

A thread flagged outdated points at code that has since moved. It is not
automatically stale: the request may still stand against the new location. Read it,
find where the code went, and decide. If the change already addressed it, say so in
the reply rather than leaving the thread silently unresolved.

## Feedback that is out of scope

Reviewers notice adjacent problems. That's valuable and it is not this PR's job.

Acknowledge it, agree or disagree on the merits, and offer to file it:

> Agreed, that whole retry path is confusing. Out of scope here — want me to open
> an issue?

Do not fix it in this PR without the user's say-so. A PR that grows during review
never lands, and the reviewer now has to re-review the parts they'd already
approved.
