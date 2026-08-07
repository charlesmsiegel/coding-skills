# Writing the Measurement Report

The output is one document: ranked findings, each citing `file:line` the author
personally read, plus what was credited and what was not audited. It should read as
one system, not as four scripts' output stapled together.

---

## The ranking rule

Rank by **likelihood × blast radius on the decisions the number drives.**

*Likelihood* is your confidence that the defect is real after reading the code — not
how bad it would be if true. *Blast radius* is what the number changes: a metric that
gates releases outranks a metric on a dashboard nobody opens, even when the dashboard
metric is more obviously broken.

So the ordering questions are, in order:

1. What decision does this number drive? (Ship gate > ranking > chart > nothing.)
2. How wrong is the number, and in which direction?
3. Who would notice if it were wrong today? If the answer is nobody, that is itself
   near the top of the report.

A finding that a widely-cited number was **never measured at all** outranks a finding
that a carefully-measured number used the wrong denominator.

## Finding template

Per finding, four parts. No more.

- **What the number claims** — as the dashboard, report, or docstring states it, quoted.
- **What it actually measures** — with `file:line` for the definition, the consumer, and
  the data. Cite only lines you opened.
- **N** — the count of real examples that supplied every input, derived from a script
  this session. If the answer is zero, say "never measured", not `0.0`.
- **What decision this affects** — the gate, the chart, the ranking, the meeting. If
  none, say so; that's the finding.

Example:

> **`quality_score` (ship gate, `ci/gate.py:31`) is joint over system and judge, on 3
> labeled examples.**
> The runbook (`docs/eval.md:12`) describes it as "answer accuracy". It is computed at
> `eval/score.py:88` by asking `claude-…` to grade each answer; no human-agreement check
> exists in the tree. `count_examples.py` finds 412 rows in `data/eval.jsonl`, 3 of which
> carry `expected_answer` — the other 409 are graded reference-free, so the aggregate
> mixes two different measurements under one name.
> **Affects:** the release gate at 0.75, run on every merge to main.

## What to credit

A report that only flags gets argued with rather than acted on, and the good practices
below are rare enough that finding one is real information. Call them out by name:

- **`null`, not `0.0`,** for the unmeasurable — with N reported beside it.
- **Documented renormalization** — a composite that says how many components contributed.
- **Pre-registered kill-switch or abandon criteria**, written before the experiment ran.
  Rare, and the strongest evidence that the experiment was designed rather than
  narrated.
- **Published negative results** — a hypothesis ruled out in writing. The single
  strongest signal of a healthy measurement culture, because it is the one thing nobody
  is rewarded for producing.
- **Shadow mode** — a verdict logged and discarded before it changes behavior, so
  reliability is established before dependence.
- **Pinned prompt and model versions**, recorded per run.
- **Seeded, reproducible statistical code.**

## What to say when there is nothing

If the subsystem has no real measurement content — no metrics, no scoring, no
experiment, just code — say that in one line and stop:

> No measurement content in this subsystem: no metric definitions, no eval data, no
> thresholds beyond retry limits. Nothing to audit.

Do not synthesize a report out of the absence. Padding trains the reader to skim, and
the next audit will be skimmed too.

## Structure of the document

```
Summary          2–4 sentences. The single most consequential thing, and whether
                 the headline numbers can be believed as stated.
Findings         Ranked. The template above, most consequential first.
Credited         What is done well, by name. Short.
Not audited      Subsystems skipped, dashboards unreachable, data with no access,
                 counts that could not be derived and why.
```

**"Not audited" is not optional.** A measurement audit that silently skips a subsystem
has committed the exact error it exists to find: a gap that reads as a clean result.

## Before you ship it

Every count in the document must be re-derived from a script in this session, not
recalled from earlier in the conversation. Then check:

- [ ] Every aggregate carries its N.
- [ ] Every `file:line` is one you opened yourself.
- [ ] Nothing unmeasured is written as `0.0`.
- [ ] Every finding names the decision it affects, or says explicitly that there isn't
      one.
- [ ] "Wrong" and "unmeasurable with today's data" are labelled differently.
- [ ] Nothing in the report is a script row that was never confirmed by reading.
