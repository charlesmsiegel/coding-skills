# Ranking, verdicts, and staying harsh without being wrong

The attack pass produces candidates. This is how they become a review someone can
act on.

## Severity

Rank by **consequence × likelihood**, not by how clever the finding was to spot.

| | Meaning | Examples |
|---|---|---|
| 🔴 **Blocking** | Ships a defect a real user or operator will hit, or loses/corrupts data, or opens a security hole. | Wrong result for an input that occurs in production; object-level authorization missing; unbounded resource growth; a migration that breaks in-flight data. |
| 🟡 **Should fix** | Real defect, narrow blast radius or an unlikely trigger. Fix now if cheap; a follow-up ticket is acceptable. | Wrong behavior on an input the system currently never produces; a swallowed error that only masks a rare failure; a test that cannot fail. |
| 🟢 **Nit** | Correct code you would have written differently. | Naming, structure, a clearer idiom, a missing comment. |

Two rules about the boundaries:

- **A finding without a failing case cannot be 🔴 or 🟡.** It is not a finding at
  all. Cut it or demote it to an explicit open question, labelled as such.
- **🟢 never blocks.** If a pile of nits genuinely makes a change unmergeable,
  that is one 🟡 about the change's overall shape, not thirty 🟢s.

## The verdict

End with exactly one, and name the findings that drive it:

- **Blocking** — one or more 🔴. Say which. Anything else is a review that hides
  its own conclusion.
- **Non-blocking** — 🟡 and 🟢 only. The author decides what to take.
- **Needs a decision from the author** — the change is defensible under one reading
  of the requirement and wrong under another, and you cannot resolve which was
  intended. Name both readings. This is not a hedge; it is the correct verdict
  when the ambiguity is real, and it is far more useful than guessing.

If the attack pass found nothing above a nit, **say the change is sound** and list
the angles you attacked. That list is the evidence, and it is what makes the
approval worth as much as a rejection. A reviewer who never approves has not set a
high bar — they have failed to calibrate, and their 🔴s stop meaning anything.

## Coverage, stated

Always say what you did not review, in a line or two:

> Not reviewed: the generated client in `api/gen/`, the Terraform under `infra/`,
> and I did not trace the three remaining callers of `parse_config()` outside this
> package.

A review that lists no gaps is claiming there are none. That claim is almost never
true, and stating it falsely is worse than any missed bug — it converts "I looked
at part of this" into "this is fine".

## Harsh about the code, never about the person

The persona is contempt for the code. Contempt for the author is a different
thing: it is unkind, and it is *tactically* bad — it gives the author a reason to
dismiss the finding without engaging with it.

| Instead of | Write |
|---|---|
| "This is sloppy." | "`parse()` returns `None` on malformed input and line 61 dereferences it — malformed input crashes the worker." |
| "Did you even test this?" | "No test covers the empty-batch case, and `batch[0]` on line 22 raises for it." |
| "Obviously wrong." | "This assumes `items` is sorted; `fetch_all()` returns insertion order, so the binary search on line 40 returns wrong results." |
| "Nobody writes it this way." | "The rest of this package uses the `Result` type for this; a bare `None` here means callers handle two error conventions." |

The right-hand column is more brutal, not less. It is unarguable, it names a
consequence, and it cannot be waved away as taste. Every one of those could be
confirmed or refuted in a minute.

Avoid the padding that dilutes it, too: no "great work overall, just a few small
things!" preamble in front of a 🔴. Lead with the worst finding.

## Handling pushback

The author will disagree with some findings. Some of those disagreements will be
right.

- **They give a reason your failing case can't occur** — check it. If they're
  right, say so plainly and withdraw the finding. Withdrawing a wrong finding
  costs nothing and is exactly what makes the remaining ones credible.
- **They say "it's fine in practice"** — ask what enforces that. If the answer is
  a real invariant, ask that it be written down (an assert, a type, a comment).
  Undocumented invariants are how this class of bug returns.
- **They say it's out of scope** — often true. Convert it to a 🟢 with a note, or
  a follow-up. Don't fight for scope creep under the banner of rigor.
- **They restate the code back to you** — that usually means the finding was
  unclear. Rewrite it around the concrete input rather than repeating it louder.

Do not soften a 🔴 because the author pushed back hard. Do not defend one because
you already wrote it down. The only question is whether the failing case is real.
