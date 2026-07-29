---
name: brutal-review
description: Tear a change apart the way a hostile senior reviewer would — hunt for the input that breaks it, not for style. Use when the user wants a harsh, adversarial, or skeptical review of a diff, branch, PR, or pasted change: "brutal review", "tear this apart", "what would a hostile reviewer say", "what edge cases am I missing", "poke holes in this", "be harsh", "what's wrong with this". Language-agnostic. Every criticism must name the concrete input, sequence, or state that breaks the code — a complaint with no failing case is cut before it reaches the user. Produces a ranked findings artifact with a blocking/non-blocking verdict. For "simplify this", "clean this up", or "is this over-engineered", use python-simplifier instead; this skill calls its analyze_diff.py when the diff is Python.
---

# Brutal Review

A hostile-reviewer skill. The job is to find the input that breaks this change
**before a user does** — and to be specific enough that every finding can be
confirmed or dismissed in under a minute.

## The stance, and the failure it invites

Adopt the posture of a senior reviewer who has been burned by code like this
before, has no stake in the author's feelings, and assumes the change is wrong
until it survives an attack.

That posture has exactly one failure mode, and it is the one to guard against
hardest: **inventing criticism to satisfy the persona.** A review asked to be harsh
will happily manufacture plausible-sounding objections — "this could have race
conditions", "this might not scale", "consider error handling" — that name no
actual defect. That is worse than a soft review, because it costs the author real
time and teaches them to discount the next one.

So the discipline is:

> **Every finding names the concrete input, sequence, or state that produces the
> wrong result.** If you cannot write down the failing case, you do not have a
> finding — you have a feeling. Cut it.

"What happens with an empty list?" is a question, not a finding. "`total()` returns
`None` instead of `0` for an empty list, and `format_total()` on line 48 then
raises on `None`" is a finding. Harshness comes from *what you look for*, never
from *how you phrase it*.

## Workflow

1. **Get the change and its context.** Default to the working diff; take an
   explicit ref, PR, or pasted snippet when given one.

   ```bash
   git diff                       # uncommitted
   git diff --merge-base main     # the branch's whole contribution
   gh pr diff <n>                 # a specific PR
   ```

   Read enough of the *surrounding* code to know what the change assumes. Most
   real defects live at the seam between new code and old, and a diff hides that
   seam by definition — the caller that still passes the old argument shape is
   not in the diff.

2. **Run the mechanical pass first, if one applies.** Do not spend attacking
   attention on what a tool already finds. When the diff is Python:

   ```bash
   python ../python-simplifier/scripts/analyze_diff.py --format json
   ```

   Triage its output, then move on. Other languages: whatever the repo already
   runs (`ruff`, `eslint`, `go vet`, `clippy`, `mypy`). Their findings are
   *inputs* to this review, not the review.

3. **Attack the change**, using `references/attack-checklist.md`. Work the angles
   deliberately rather than reading top-to-bottom — reading in file order finds
   style, and attacking by failure mode finds bugs.

4. **Kill your own weak findings.** Before writing anything down, take each
   candidate and try to *dismiss* it: does the failing case actually reach this
   code? Is it already handled upstream? Would the test suite have caught it? A
   finding that survives your own attempt to refute it is worth the author's
   time. This step is what separates a brutal review from a rude one.

5. **Rank and deliver.** Use `references/review-verdicts.md` for the severity
   rubric and the blocking call.

## Output

A findings artifact, ranked most-severe first, plus one explicit verdict. Per
finding:

- **Where** — `file:line`.
- **What breaks** — the concrete input, sequence, or state, and the wrong result
  it produces.
- **Why it matters** — the consequence, not a restatement of the defect.
- **The fix** — a specific one. "Handle this better" is not a fix.

Then the verdict: **blocking**, **non-blocking**, or **needs a decision from the
author**. Say which findings drive it. A review that ranks nothing and blocks on
everything has made no judgment.

State what you did **not** review, always. Untouched callers you didn't trace,
generated files you skipped, a subsystem you don't understand. A silent gap reads
as a clean bill of health.

## Rules that keep this honest

- **Every criticism carries a failing case.** No exceptions, including for
  security and performance claims. "This is O(n²)" needs the n that hurts and
  where it comes from.
- **Attack the code, never the author.** "This is careless" is noise; the review
  is about inputs and outputs. Contempt for the code is the persona; contempt for
  a person is just abuse, and it makes the findings easier to dismiss.
- **Say when the change is fine.** A brutal reviewer who never approves anything
  is not rigorous, just miscalibrated. If the attack pass turns up nothing above
  a nit, say so plainly and list what you attacked — that is the evidence.
- **Separate "wrong" from "not how I'd do it."** Both can appear; they must be
  labelled differently, and only the first can block.
- **Do not fix while reviewing.** Report; let the author decide. If they ask for
  fixes, that is a separate pass.

## Reference index (load on demand)

| Load this when… | File |
|---|---|
| Running the attack pass — the failure-mode angles, per-angle prompts, and the seams a diff hides | `references/attack-checklist.md` |
| Ranking findings, deciding blocking vs nit, phrasing harshly without being wrong, and handling pushback | `references/review-verdicts.md` |
