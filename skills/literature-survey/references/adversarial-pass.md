# The adversarial pass

Every synthesized claim gets attacked before it is allowed into the report. Your
job in this pass is to **refute**, not to review.

This is the judgment half of the rigor gate. `verify_locators.py` proves the page
exists; it cannot tell you the page does not say what the claim says, or that the
claim is true of one dataset and stated as true generally, or that the three
supporting papers are the same lab. Only reading with intent to break it does that.

## Stance

Assume the claim is wrong and look for the reason. This is uncomfortable and it is
the entire value of the pass — a reviewer asking "is this reasonable?" will find
almost anything reasonable, because a synthesized claim is by construction the most
plausible reading of the notes.

**Default to refuted when uncertain.** A claim you cannot convince yourself of is
not ready to be asserted. It can become a lead, or go in open problems, or be
narrowed until it is defensible. The cost of a demoted true claim is a slightly
thinner report; the cost of a promoted false one is a report that cannot be
trusted at all.

You work from the notes and the artifacts on disk. Not from memory of the
literature — a refutation citing a paper that is not in the corpus is
unverifiable, and this pass has to be checkable too.

## The attacks, in order of how often they land

**1. The locator does not support the claim.** Open the page. Read the sentence.
Very often the paper says something narrower: "improves on this benchmark" became
"improves retrieval", or a result about one model became a result about models.
This is the most common finding in the pass and the whole reason it exists.

**2. The supporting sources are not independent.** Three citations, one lab. Or a
paper plus two surveys that cite it. Or a benchmark result and a paper by the
benchmark's authors. Independence is what "multiple sources agree" is supposed to
buy, and it frequently is not there.

**3. The construct shifted.** The claim compares two papers that measured
differently — see `synthesis.md`. If they did, the claim is about
operationalizations, not about the effect, and as written it is wrong.

**4. Scope creep.** True of English, stated generally. True at 10k documents,
stated at scale. True for one architecture, stated for the class. Check what the
method section actually covered against what the claim says.

**5. The counter-evidence is in the corpus and was skipped.** Search the notes for
the claim's negation. If a note reports the opposite and the ledger says "settled",
that is a refutation and probably narrative smoothing.

**6. The effect is inside the noise.** `data_and_n` and `limitations_unstated` will
often say "single seed" or "no CIs reported". A 1.2 F1 improvement with no variance
reported does not support "outperforms".

**7. The evidence is the authors evaluating themselves.** Not disqualifying, but it
cannot be the sole support for a settled claim.

## Verdicts

**Refuted** — the claim is wrong, or unsupported by what it cites. Say which attack
landed and point at the evidence. It comes out of the report, or is rewritten from
scratch.

**Narrowed** — true in a smaller scope than stated. Give the scope. This is the
most common useful outcome; most overclaiming is a scope problem, not a falsehood.

**Survives** — you tried the attacks above and none landed. Say which you tried,
because "survives" without that is indistinguishable from not having looked.

Every verdict carries its own locators. A refutation is a claim about the
literature and is held to the same standard as the claim it attacks.

## Perspective diversity

When a claim can fail in more than one way, attack it from distinct angles rather
than running the same skeptic repeatedly:

- **Does the evidence say this?** — the locator attack.
- **Does it say this in general?** — the scope attack.
- **Is anything measured well enough to tell?** — the construct and variance
  attack.
- **Does the corpus contain the opposite?** — the counter-evidence sweep.

Redundant identical checks mostly reproduce each other's blind spots.

## What to do with what you find

Refuted and narrowed claims are not deleted quietly. The ledger records that a
claim was attacked and what happened, because a report that shows its claims were
tested is more credible than one that shows only survivors — and because the next
run should not re-derive the same overclaim.

If the pass refutes a large share of the claims, that is a finding about the
synthesis, not just about the claims. Say so, and consider whether the notes were
thin enough that the reading stage needs redoing.
