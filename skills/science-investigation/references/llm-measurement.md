# LLM Measurement Traps

Load this whenever an LLM **produces, retrieves, summarizes, or grades** anything that
later becomes a number. These are the failure modes that most often make an otherwise
careful measurement meaningless, and none of them is visible to a passing test suite.

Each section says what breaks, how to check it in the tree, and what a confirmed
finding looks like. Every check produces a candidate — confirm by reading.

---

## 1. The judge confound

When an LLM grades output (LLM-as-judge), or synthesizes an answer that is then
scored, the score is a **joint property of the system under test and the judge**. A
regression in either one moves the headline identically, and nothing in the number
says which moved.

This is the single most common way an LLM eval becomes uninterpretable, and it is
almost never acknowledged in the report the number appears in.

**Check:**

```bash
grep -rniE "judge|grader|rubric|as_a_judge|llm_eval|score_with|critic" --include='*.py' --include='*.ts' .
```

Then read the judging call site and answer three questions:

- **Is the judge's own reliability measured?** Agreement with human labels on a
  held-out slice (κ, % agreement), or self-consistency across repeated grades of the
  same output. Almost always the answer is no — and then the judge is an unvalidated
  instrument being used to certify everything else.
- **Is the judge the same model family as the system under test?** Self-preference is
  real; a model grading its own family's output is a conflict of interest, not a
  neutral measurement.
- **Does the judge see the reference answer?** A judge grading without ground truth is
  measuring plausibility, not correctness — which is a fine thing to measure and a
  terrible thing to label "accuracy".

**Finding shape:** "`eval/judge.py:44` grades answers with `claude-…`; no
human-agreement or self-consistency check exists anywhere in the tree. The reported
`quality` at `dash/panel.py:70` is therefore joint over (system, judge), and a judge
prompt change is indistinguishable from a product regression."

## 2. Prompt and model-version drift

A wording change in a judge prompt ("be strict", a hedging instruction, an added
example) moves scores. So does a provider silently upgrading behind an unversioned
model alias. Either one breaks comparability across time while the trend chart keeps
drawing a single line.

**Check:**

```bash
grep -rniE "gpt-|claude-|gemini-|llama|mistral|model *= *['\"]|model_name|deployment" --include='*.py' --include='*.ts' --include='*.yaml' --include='*.yml' --include='*.json' .
git log --oneline -- <prompts-dir> <judge-file>          # how often does the instrument change?
```

Ask:

- Is the model id **pinned to a version or date**, or is it a floating alias?
- Are the judge prompt, system prompt, and model id **recorded in each run's output**,
  not just in the code that produced it? A run record without its instrument settings
  cannot be compared to anything later.
- Do prompt files live in git with a version marker, or are they assembled inline from
  f-strings and config at call time?
- Does the run history show prompt edits interleaved with the score history? Overlay
  the two — `git log` on the prompt directory against the dates on the trend chart.

**Finding shape:** "Judge prompt changed at `prompts/judge.md` (commit `a1b2c3d`,
March 4); the quality trend at `reports/trend.json` spans Feb–May with no version
marker, so points before and after March 4 are not comparable."

## 3. Silent fail-soft

A timeout, rate limit, refusal, empty string, content filter, or IAM denial that gets
recorded as `0.0` — or as `""` that then scores 0 — is indistinguishable from "the
system answered badly". A batch of them looks exactly like a regression, or worse,
like a feature that works poorly rather than one that **isn't running at all**.

**Check:**

```bash
python "$SKILL/scripts/find_fail_soft.py" .
```

Then, per site, read for:

- A per-example **error field** that distinguishes "scored 0" from "never scored".
- Whether the aggregate **excludes errored examples** or averages them in as zeros.
- Whether an empty or refused completion takes the same path as a wrong one.
- Retries that exhaust silently and fall through to the default.

The tell is a metric whose *denominator* never changes while its numerator drops.

**Finding shape:** "`runner.py:88` catches every exception around the model call and
appends `score=0.0`; no error field is written. In `runs/2024-06-11.jsonl`, 41 of 300
rows carry `latency_ms > 30000`, which is the timeout — the reported mean of 0.61 is
over 300 rows of which 41 measured nothing."

## 4. Non-determinism unaccounted for

With temperature > 0, sampling, or tool-call ordering, the metric is a **random
variable**. Reporting one draw as a fact means the first "improvement" you chase may
be resampling noise.

**Check:**

```bash
grep -rniE "temperature|top_p|top_k|seed|random_state|n_samples|num_return" --include='*.py' --include='*.ts' .
```

Ask: is the eval run once? Repeated with variance reported? Seeded? Is the *judge*
also sampling at temperature > 0, so the grader is noisy on top of a noisy system?

A quick sanity check that costs nothing: if any run was ever repeated on identical
inputs, diff the two result files. The spread between them is the noise floor, and
every claimed effect smaller than it is not an effect.

**Finding shape:** "System and judge both run at `temperature=0.7`
(`config/eval.yaml:12,31`), single pass, no seed. The two repeats stored at
`runs/rerun-a.json` and `runs/rerun-b.json` differ by 2.4 points on the same inputs;
the ship rule at `docs/process.md:18` is +3 points."

## 5. Contamination and leakage

If the eval set is in the model's training data, or leaks through few-shot examples,
retrieved context, or a shared prompt, the number measures memorization. **A number
inflated by leakage is worse than no number** — it is confidently wrong in the
direction everyone wants.

**Check:**

- Where did the eval set come from? Public benchmark (assume contamination unless the
  cutoff says otherwise), scraped production data, or hand-written after the model's
  cutoff?
- Do the few-shot examples in the prompt overlap the eval set? Compare the two files
  directly — exact-match on inputs is a cheap first pass.
- For RAG: does the retrieval corpus contain the gold answers verbatim, such that
  retrieval is doing lookup rather than the reasoning being claimed?
- Was any of the eval set used for prompt tuning or few-shot selection? That's a
  train/test split violation with no train/test split in sight.

**Finding shape:** "`data/eval.jsonl` is the public HotpotQA dev split; the model card
lists a training cutoff after its publication. 12 of 200 eval questions also appear in
`prompts/fewshot.json`."

## 6. Agent and trajectory evaluation

For multi-step agents, scoring only the final answer leaves the trajectory unmeasured:
tool misuse, wasted steps, destructive or unsafe actions, loops that happened to
terminate on the right answer.

**Ask:**

- Is **partial credit** defined, or is a run that got 9 of 10 steps right a flat zero?
  Flat-zero scoring compresses everything into a coarse signal that can't guide work.
- Are **unsafe or irreversible actions** measured at all, or only whether the answer
  was right? An agent that deletes the wrong file and then reports the correct answer
  scores 1.0.
- Is **cost** — steps, tokens, wall time, tool calls — recorded alongside quality? An
  agent that doubles latency for +1 point is a regression the quality metric calls a
  win.
- Is a **stuck run** (hit max steps) distinguished from a wrong answer?

**Finding shape:** "`agent_eval.py:120` scores `final_answer == expected` only. Step
counts are logged but never aggregated; `runs/*.jsonl` shows a mean of 14 steps
against a cap of 15, so runs are terminating at the cap and scoring as wrong answers."

## 7. RAG's two failure surfaces

Retrieval quality and generation quality are separate, and a single end-to-end score
hides which one regressed — so the fix lands on the wrong half.

The four numbers a RAG eval needs, and which of them exist here:

| Measure | Question it answers | Needs |
|---|---|---|
| Retrieval recall / hit rate | Was the right context fetched at all? | Gold passage labels |
| Retrieval precision / nDCG | Was it ranked usefully? | Graded relevance |
| Faithfulness / groundedness | Did the answer stick to the retrieved context? | The context, per example |
| Answer correctness | Was the answer right? | Ground truth |

**Faithfulness is not correctness.** An answer can be perfectly grounded in retrieved
context that is itself wrong (scores well, is wrong), or correct from parametric
knowledge with no grounding (scores badly, is right). A system reporting only one of
them is reporting half the story — say which half.

Check whether retrieved context is even *stored* per example. If it isn't, faithfulness
cannot be computed after the fact, and that is a structural limit worth naming.

---

## What good looks like here

Credit these explicitly where you find them; they are rarer than the failures.

- Judge agreement with human labels reported alongside the scores it produces.
- Model id and prompt hash written into every run record.
- A per-example `error` field, with errored examples excluded from aggregates and
  their count reported.
- Repeated runs with variance, or a seeded deterministic path.
- Eval data authored after the model cutoff, or held out and never used for tuning.
- Retrieval and generation scored separately.
- **Shadow mode** — an LLM verdict logged and discarded before it changes behavior,
  so its reliability is measured before anyone depends on it.
