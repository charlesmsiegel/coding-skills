# Synthesis

You have 15–30 notes and no papers. Turn them into a ledger, then into an
argument.

The ledger is the audit trail: every row is settled, contested, or unsupported,
and every settled row carries locators that `verify_locators.py` will re-resolve.
The argument is what the reader actually reads. Both come from the notes and
nothing else.

## The three buckets

**Settled** — multiple independent artifacts agree, and you can cite each. Two
papers by the same group are not independent. A paper and a survey summarising that
paper are not independent. A claim in one place is not settled, however confident
the authors were.

**Contested** — the corpus disagrees. This is a *finding*, and the most valuable
kind, because it is what a reader cannot get from any single paper. State both
positions at full strength, with locators for each, and say what would settle it.

**Unsupported** — appears in the corpus but nothing establishes it. Common in
introductions, where "it is well known that X" cites a paper that cites a paper
that measured something else. Record these as `Lead`s with `also_explained_by`;
they are frequently the most interesting open problems.

The temptation is to resolve contested into settled by weighing. Resist it. If two
credible papers disagree, the honest report says the field disagrees.

## Same word, different construct

**This is the failure that makes a synthesis confidently meaningless, and it is the
main reason this guide exists.**

Two papers both measure "shared mental model". One operationalizes it as
convergence between team members' concept maps; the other as the mean of a
five-item self-report Likert scale. They report opposite effects on team
performance.

There is no contradiction to resolve. **They measured two different things.**
Averaging them, or reporting "evidence is mixed", destroys the actual finding —
which is that the construct is not measured consistently, so the literature's
disagreement is partly an artifact of instrumentation.

The rule: **before comparing two results, check that the two papers measured the
same thing.** When they did not, the ledger row is about the operationalizations,
not about the effect:

> **Contested — and partly an artifact of measurement.** Concept-map convergence
> predicts performance (Smith 2019, p.7); self-report shared-understanding scales
> do not (Jones 2021, p.12). The two are weakly correlated with each other
> (Lee 2023, p.4), so these are plausibly different constructs sharing a name
> rather than conflicting findings about one.

Places this bites hardest: psychology constructs measured by scale versus by
behaviour; "accuracy" on benchmarks with different preprocessing; "hallucination
rate" under incompatible definitions; anything an LLM judge scored, where the judge
*is* the operationalization.

## Narrative smoothing

A good report has a thesis. The danger is that a thesis makes contradicting
evidence feel like noise.

Symptoms to watch for in your own draft:

- A paper appears in the ledger but not in the narrative, and it is the one that
  disagrees.
- Hedges accumulate around inconvenient results ("although some work suggests")
  while convenient ones are stated flat.
- The timeline reads as progress toward the current consensus, with the branches
  that were abandoned for non-technical reasons omitted.
- Every finding points the same way. Real literatures are messier than that; if
  yours is not, you have probably smoothed it.

The corrective is mechanical: after drafting, walk the ledger and confirm every
contested row appears in the narrative *as contested*. Then run the adversarial
pass, which exists precisely to catch this.

## Promoting and dropping leads

A `Lead` becomes a `Claim` only when you can point at the evidence — at which
point it needs locators and the `also_explained_by` list goes away. A lead that
survives the whole synthesis without evidence has two honest homes: the open
problems tab, or nowhere. It does not get promoted because it fits the thesis.

## Weighting evidence

Not all agreement is equal:

| stronger | weaker |
|---|---|
| independent replication | one group, several papers |
| pre-registered or held-out evaluation | benchmark chosen after seeing results |
| effect larger than reported variance | no variance reported at all |
| result survives an obvious baseline | no baseline, or a weak one |
| adversarial or third-party evaluation | authors evaluating their own system |
| practitioner reports at scale | a demo on a toy dataset |

A single well-controlled study beats three uncontrolled ones pointing the same way.
Say which kind of evidence a settled claim rests on; "three papers agree" means
little if all three ran the same flawed benchmark.

## What the reader actually needs

Order the argument by what changes a decision, not by what the field finds
interesting. For a survey: what is settled enough to build on, what is contested
enough to hedge against, what is unsolved. For a question: the answer, the
strongest case against it, and what would change it.

And say what the corpus could not tell you. A gap the report names is a limit; a
gap it does not name is a hole the reader will fall into.
