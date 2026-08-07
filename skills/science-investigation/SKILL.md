---
name: science-investigation
description: 'Audit whether a system''s numbers can be believed — is the right thing measured, on enough real data, with sound statistics, and does the reported score mean what people think it means. Use when the user says "audit the eval", "is this actually measured", "can we trust this number", "does the benchmark mean anything", "check the metrics", "check the scoring", "is n too small", "our LLM-as-judge scores went up", "did that actually improve", or asks whether an experiment, A/B test, or benchmark supports the conclusion drawn from it. Sharpest on LLM systems — evals, LLM-as-judge, RAG, agents, prompt pipelines — and applies across retrieval, ranking, ML, risk and fraud, pricing, forecasting, and analytics. It audits and reports; fixing the measurement is a separate task. For "is this code correct", use brutal-review instead.'
---

# Measurement Investigation

**A system can be fully tested, ship daily, and still have measurement that means
nothing.** Tests check that the code does what its author intended. They never check
that the metric measures what the dashboard claims, that the number had inputs, or
that the difference between two runs is bigger than the noise. Those gaps are
invisible to unit tests, type checkers, and ordinary code review — which is why they
survive for years in codebases that look immaculate.

This skill asks one question, of every quality/accuracy/score number the system
reports or a human trusts:

> **What does this number require to compute, on how many real examples was it
> computed, and what does it NOT tell you?**

Let `SKILL=/path/to/this/skill` — the directory holding this SKILL.md. Commands run
from the repository under audit, not from the skill directory. Needs **Python
3.11+**; the scripts are stdlib-only and make no network calls.

## The discipline

Four rules. Everything below assumes them.

- **Candidates, not verdicts.** Every script and grep here produces *hypotheses*. A
  finding exists only after you read the code and the metric definition yourself.
  **Read the code that consumes the number, not just the code that computes it** — a
  score that never orders, gates, or charts against a baseline is a different and
  usually bigger finding than a wrong formula.
- **Derive every count from a script, never from memory.** "8 metrics", "3 of 30
  labeled", "n=30" — each will be quoted back and acted on. Re-derive each one
  before shipping the report rather than recalling it from earlier in the session.
- **Report N beside every aggregate.** "0.8 over 30" and "0.8 over 3" are different
  facts, and both differ from "never measured" — which must be reported as null,
  never as `0.0`.
- **Don't manufacture findings.** If the subsystem has no real measurement content,
  say so in one line and stop. A padded audit trains the reader to skim the next one.

## Workflow

### 1. Map where numbers are produced

Mechanical enumeration first, so attention goes to judgment rather than grepping.

```bash
python "$SKILL/scripts/find_metrics.py" .            # definitions, thresholds, composite weights
python "$SKILL/scripts/count_examples.py" .          # dataset sizes and per-field coverage
python "$SKILL/scripts/find_fail_soft.py" .          # default-off flags, swallowed errors, silent caps
python "$SKILL/scripts/trace_value.py" 0.7 .         # what one threshold means across the tree
```

Each prints a `headline` (its most likely finding), a `caveat` (what it cannot see),
and rows that are **candidates to confirm** — never findings. `--format json` gives
the same object for piping. Run them on the eval/metrics subtree if the repo is
large; run them on the whole tree if you don't yet know where measurement lives.

### 2. Confirm by reading

For each candidate, open the file. Then open the *consumer*: who reads this number,
and what changes because of it? A metric nobody reads is a finding. A metric that
gates a release is a finding with a blast radius.

Build the inventory as you go — one row per metric: **name, formula, inputs,
consumer, N it was last computed over.** The gaps in that table are most of the
report.

### 3. Audit metric-vs-data

For every metric, list its inputs and then **count the real examples that supply all
of them.** Reference metrics — accuracy, recall, correctness, calibration — need
ground-truth labels; count the labeled rows, don't assume the dataset is labeled. It
is routine to find 8 metrics defined and 3 examples labeled, so the reference metrics
run on a subset nobody realizes is a subset.

Then name what is **structurally unmeasurable** with today's data, so silence isn't
read as success: recall with no gold set, calibration with no outcomes, causal effect
with no control arm, ranking quality with no graded relevance.

`references/metric-and-stats-audit.md` has this pass in full, plus composite honesty,
comparability across time, and dark measurement.

### 4. Interrogate the judge and the statistics

If an LLM produces, grades, or summarizes anything that gets scored, load
`references/llm-measurement.md` **before** forming conclusions. The traps there — the
judge confound, prompt and model-version drift, silent fail-soft, non-determinism,
contamination, trajectory-blind agent evals, RAG's two failure surfaces — are the ones
that most often make an otherwise careful number meaningless.

For the statistics: can n resolve the effect the ship rule cares about — computed
from the spread of the deltas, not assumed from n alone? Is the comparison paired? Is
the resampling seeded? Were N metrics × K arms tested with no multiple-comparisons
correction? (It almost always is absent — say so.)

### 5. Reconcile and rank

Rank by **likelihood × blast radius on the decisions the number drives**, not by how
clever the finding is. Credit what is done well; a report that only flags is a report
that gets argued with. `references/reporting.md` has the finding template, the
ranking rule, and the "healthy measurement culture" signals worth crediting.

### 6. Verify counts before shipping

Re-run the scripts and check every number in your draft against fresh output.

- Does every aggregate in the report carry its N?
- Is every `file:line` one you personally opened?
- Is every count re-derived, not recalled?
- Does anything unmeasured appear as `0.0` rather than "not measured"?

A measurement audit that ships a wrong count has failed on its own terms.

## Red flags

Any of these, on sight, is worth a candidate row.

| Signal | What it usually means |
|---|---|
| A metric returns `0.0` on the error path | "Never ran" and "ran badly" are now the same number |
| `except:` around a scoring or judging call | Timeouts, refusals, and rate limits score as failures of the system under test |
| A judge prompt built inline from an f-string | Nothing pins the wording; last month's scores are not comparable |
| A model id with no version or date suffix | The provider can move the baseline under you silently |
| `temperature` unset or > 0 with a single run | The headline is one draw from a distribution reported as a fact |
| A composite that averages `[v for v in vals if v is not None]` | Skipped metrics vanish; survivors' mean is reported as the whole |
| Hand-written weights (`0.4 / 0.2 / 0.4`) with no derivation | A magic number decides the headline |
| A ship rule of "+3 points" with n in the tens | The rule may be finer than the instrument — compute the interval, don't assume it |
| `[:100]` or `sample(` inside the eval loop | "We measured everything" is false and unlabelled |
| A flag defaulting to `False` around the better method | The good result was never actually measured in production |
| A scoring change that only ever moved scores up | Progress on the dashboard, no change in the world |
| Two dashboards, same prefix, different questions | Two systems, one name — see `references/metric-and-stats-audit.md` |

## Common mistakes

| Mistake | Do this instead |
|---|---|
| Reporting a script row as a finding | Open the file. The script found a candidate; you find the finding. |
| Auditing the formula, not the consumer | Trace who reads the number and what changes. That's the blast radius. |
| Quoting a count from earlier in the session | Re-derive it. Counts drift as you learn what to include. |
| Writing "accuracy is 0.82" | Write "accuracy 0.82 over 30 labeled of 412 total". |
| Treating "never measured" as zero | Null, with the reason. Zero is a measurement claim. |
| Blaming the system for judge failures | The score is joint. Say which half you can't separate. |
| Listing every statistical imperfection | Rank by the decision at stake. Multiple-comparisons noise on an unread dashboard is a footnote. |
| Padding a thin subsystem into a full report | "No real measurement content here" in one line, then stop. |
| Fixing what you find | This skill audits. Fixing is a separate task with its own review. |

## Boundaries

- **Audit, don't fix.** Report the finding; let the owner decide the remedy. A
  fix pass changes the numbers you were sent to check.
- **Don't re-run the experiment** to see if the number reproduces unless asked —
  that's expensive, and often the finding is that nobody could.
- **Say what you did not audit.** Subsystems skipped, dashboards you couldn't
  reach, data you had no access to. A silent gap reads as a clean bill of health.
- **Distinguish "wrong" from "unmeasurable with today's data."** Both belong in the
  report, labelled differently; only the first is a defect.

## Reference index (load on demand)

| Load this when… | File |
|---|---|
| Anything is graded, generated, retrieved, or judged by an LLM — judge confound, version drift, fail-soft, non-determinism, contamination, agent trajectories, RAG | `references/llm-measurement.md` |
| Doing the metric-vs-data inventory, composites, sample size and statistics, comparability across time, dark measurement | `references/metric-and-stats-audit.md` |
| Writing the report — ranking rule, finding template, what to credit, how to say "nothing here" | `references/reporting.md` |
