# Metric-vs-Data and Statistics Audit

The domain-agnostic half. Every check below carries a concrete LLM instance, because
that is where these failures are densest — but they apply equally to a fraud model, a
pricing experiment, a forecast, or a product analytics dashboard.

---

## 1. Metric-vs-data: does the number even have inputs?

Start here. It is the cheapest pass and it finds the biggest holes.

Build one row per metric:

| Metric | Formula | Inputs it needs | Consumer | N it ran on |
|---|---|---|---|---|

Fill the last two columns by **counting, not assuming**:

```bash
python "$SKILL/scripts/find_metrics.py" .      # the metric and threshold candidates
python "$SKILL/scripts/count_examples.py" .    # rows per dataset, and per-field coverage
```

`count_examples.py` reports how many records carry each field. That is the number that
matters: a **reference metric** — accuracy, recall, correctness, calibration, F1 — needs
ground-truth labels, and its real N is the count of rows that have one, not the size of
the dataset.

The common shape: 8 metrics defined, 30 rows in the eval set, 3 of them labeled. The
five reference metrics quietly run on 3 examples, are reported next to the three
reference-free ones as if all eight were equally supported, and nobody has ever seen
the two numbers side by side.

Then trace the **consumer** for each metric. A number that no chart, no gate, no
ranking, and no alert reads is dead measurement — the cost is paid, the value is not
collected. That's a finding, and often a larger one than a wrong formula.

## 2. What is structurally unmeasurable

Name these explicitly, so silence isn't read as success. "We don't measure X" is a
finding; a report that lists only what *is* measured implies the rest is fine.

| Claim | Requires | Absent it, you have |
|---|---|---|
| Recall / coverage | A gold set of everything that should have been found | Precision on what you happened to look at |
| Calibration | Realized outcomes per prediction | Confidence scores nobody has checked |
| Causal effect ("the feature caused +4%") | A control arm | A before/after correlation |
| Ranking quality (nDCG, MAP) | Graded relevance, not binary clicks | A click-through proxy |
| Faithfulness (LLM/RAG) | Retrieved context stored per example | An answer with no audit trail |
| Regression detection | A stable baseline run | Two numbers from two different instruments |

**LLM instance:** a RAG system reporting "94% accuracy" with no gold passage set cannot
report recall at all — it can only tell you how often it was right about the questions
someone thought to ask. The questions it silently fails to retrieve for are absent by
construction.

## 3. Composite and aggregate honesty

Two ways a composite lies.

**Renormalization over survivors.** A composite that averages only the metrics that
produced a value reports the survivors' mean *as if it were the whole*. A run where
half the metrics skipped scores the same as a run where all of them passed. Look for:

```python
scores = [v for v in components.values() if v is not None]
return sum(scores) / len(scores)          # <- the whole finding
```

The honest form carries the denominator: report the composite **and** how many of the
components contributed. If a component is missing, either the composite is null or it
is labelled "3 of 8 components".

**Undocumented weights.** `0.4 * relevance + 0.2 * latency + 0.4 * quality` — where did
those come from? If nobody can say, they are a magic number deciding the headline, and
the ranking they produce is an opinion wearing a decimal point. Ask whether the weights
were fit to anything, chosen by a person in a meeting, or copied from an earlier system
with different components.

Check whether the composite is even **monotone in what people think it means**: does a
strictly better system always score higher? A latency term inside a "quality" composite
means a slower, better answer can lose to a faster, worse one, and the metric's name
does not say so.

## 4. Sample size vs. effect size

The instrument must be finer than the decision rule.

- What is the **ship rule**? "+3 points", "no regression", "beats baseline". Find it
  written down; if it isn't written down, that's the first finding.
- What is **n**? Not the dataset size — the number of examples that supplied every
  input to the metric in question.
- Can n resolve the effect? A bootstrap over 30 paired deltas has an interval wider
  than most effects teams argue about. Rough shape: a difference in a proportion around
  0.5 needs on the order of hundreds of paired examples to resolve a few points; tens
  of examples resolve tens of points.

If a confidence interval or bootstrap exists, read it for three things:

- **Pairing.** Comparing the same items across arms removes item-difficulty variance
  and is usually a large win. Unpaired comparison over a small heterogeneous eval set
  is mostly measuring which items landed in which arm.
- **Seeding.** Is the resampling seeded and reproducible? An unseeded bootstrap gives a
  different interval each run, which means the interval itself is being cherry-picked
  by whoever reruns it.
- **Multiple comparisons.** N metrics × K arms × however many weekly reruns is a lot of
  tests. Correction is almost always absent — note it, and note how many tests were
  actually run, because that's the number that sets the false-positive rate.

**LLM instance:** a prompt-tuning loop that tries 20 prompt variants against a 40-example
eval and ships the best one has selected on noise. The winner's score is biased upward
by the selection itself and will not reproduce on the next run. Look for the loop, not
just the final number.

## 5. Comparability across time

A trend chart assumes one instrument. Check that assumption.

- Did the **scoring methodology, judge prompt, model version, or eval set** change
  during the window the chart covers? `git log` on the scoring code and the data
  directory answers this in seconds.
- Is a **version marker embedded in each report**, so runs from two regimes can't be
  averaged into one line? A run record with no methodology version is uncomparable to
  anything by design.
- Were historical runs **re-scored** under the new method, or is the old data still
  under the old one? Mixed is the worst case and the most common.

**A methodology change that only ever moved scores up deserves special scrutiny.** It
looks exactly like progress on the dashboard and costs nothing to produce. Check the
direction of every scoring change in the history; a sequence of changes all favoring
the current system is a pattern, not a coincidence.

## 6. Dark and truncated measurement

Things that were never measured but read as measured.

- **Flag-off components.** The better method — adaptive cutoff, learned reranker,
  reflection step — sits behind a flag defaulting to off, with no recorded A/B. The
  benchmark that justified building it was run once, locally, and never again. Check
  for a recorded comparison; if there isn't one, the component's value is folklore.
- **Timeouts scored as complete.** A run that hit its wall-clock cap and returned
  partial results, counted in the denominator as a finished example.
- **Silent caps.** `[:100]`, `LIMIT 500`, `sample(n=50)`, `head -n` in the eval
  pipeline. A capped run that reports no cap reads as "we measured everything". The cap
  itself is fine; the silence is the finding.
- **Filtered-out failures.** A preprocessing step that drops malformed or empty rows
  before scoring. Those rows are usually the interesting ones, and dropping them
  inflates every metric downstream.

```bash
python "$SKILL/scripts/find_fail_soft.py" .    # flags, swallowed errors, caps, in one pass
```

## 7. Two systems, one name

Distinct measurement systems sharing a name or a directory prefix — a "quality eval"
and a "productivity A/B" both living under `evaluation/`, both reporting a
`quality_score`, answering different questions on different populations with different
methods.

They cannot stand in for each other, and conflating them is a finding in its own right:
someone will cite one to settle a question only the other could answer.

**Check:** for each metric name that appears in more than one place, confirm the
definition is the same in both.

```bash
python "$SKILL/scripts/trace_value.py" quality_score .
```

Different formula, different population, or different denominator behind one name means
the name is doing work it can't do. Say which is which, and which one the decision in
question actually needs.
