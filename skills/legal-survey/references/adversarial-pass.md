# The adversarial pass

Every synthesized claim gets attacked before it is allowed into the report. Your
job in this pass is to **refute**, not to review.

This is the judgment half of the rigor gate. `verify_authorities.py` proves the
page exists, the quote is on it, the file is the case it claims to be, and no
citing opinion announced an overruling. It cannot tell you the page does not say
what the claim says, that the holding is narrower than stated, that the case is
distinguishable on its posture, or that the contrary authority in the corpus was
quietly skipped. Only reading with intent to break it does that.

The stance is the one opposing counsel will take, and doing it now is much
cheaper than having it done to you later.

## Stance

Assume the claim is wrong and look for the reason. This is uncomfortable and it is
the entire value of the pass — a reviewer asking "is this reasonable?" will find
almost anything reasonable, because a synthesized claim is by construction the
most plausible reading of the notes.

**Default to refuted when uncertain.** A claim you cannot convince yourself of is
not ready to be asserted. It can become an open question, or be narrowed until it
is defensible. The cost of a demoted true claim is a slightly weaker memo; the
cost of a promoted false one is a memo that cannot be relied on at all.

You work from the notes, the treatment records and the documents on disk. Not from
memory of the law — a refutation citing a case that is not in the corpus is
unverifiable, and this pass has to be checkable too. If you remember a case that
would break the claim, that is a lead: add it to candidates, fetch it, read it,
and then use it.

## The attacks, in order of how often they land

**1. The pincite does not support the claim.** Open the page. Read the sentence in
its paragraph. Very often the court said something narrower: "we need not decide
whether X" became "X"; "on this record" became a general rule; a statement of the
parties' contentions became the court's holding. This is the most common finding
in the pass and the whole reason it exists.

**2. It is dicta.** Ask the deletion test again, adversarially: if this sentence
were removed, would the judgment change? A claim built on an aside from a court
that binds the forum is the most seductive error in the corpus, because
everything about it looks right.

**3. The posture does not travel.** The case reversed a dismissal and the claim
states the merits. The case reviewed for abuse of discretion and the claim states
what the law requires. See `synthesis.md` — this attack lands more often than
anyone expects, because posture is invisible in a quotation.

**4. The authority does not bind the forum.** Check the `weight_reason` against
the forum actually named in `candidates.json`. A persuasive case stated as
controlling is a claim about a court that court has not made.

**5. The text has changed.** The case construes a statute that was amended, or a
regulation that was rewritten, or a version of the code other than the one the
question's as-of date points at. The opinion is real, good law and beside the
point. Cross-check every statutory claim against the amendment history — this
does not show up in the treatment record.

**6. The treatment was not really clean.** `good` here means no negative language
was found in the citing opinions that were read. Check: was the check truncated by
the cap? Was the case `unchecked` because nothing has cited it? Is there an
"overruled on other grounds" that somebody waved past? For any claim the answer
turns on, read the recent citing opinions yourself.

**7. The contrary authority is in the corpus and was skipped.** Search the notes
for the claim's negation. If a note reports a court going the other way and the
ledger says "settled", that is a refutation and probably narrative smoothing.

**8. The supporting authorities are not independent.** Four cases, three of which
cite the first and add nothing. A line of district court decisions all following
one unreviewed opinion. A treatise and the case it summarises. Independence is
what "the weight of authority" is supposed to buy, and frequently it is not there.

**9. The facts are distinguishable.** Compare `facts_material` in the note against
the situation the report is addressing. The rule may be right and inapplicable,
which is the distinction that decides most actual disputes.

## Verdicts

**Refuted** — the claim is wrong, or unsupported by what it cites. Say which
attack landed and point at the evidence. It comes out of the report, or is
rewritten from scratch.

**Narrowed** — true in a smaller scope than stated. Give the scope: which court,
which posture, which version of the text, which facts. This is the most common
useful outcome; most overclaiming in law is a scope problem, not a falsehood.

**Survives** — you tried the attacks above and none landed. Say which you tried,
because "survives" without that is indistinguishable from not having looked.

Every verdict carries its own locators. A refutation is a claim about the law and
is held to the same standard as the claim it attacks.

## Perspective diversity

When a claim can fail in more than one way, attack it from distinct angles rather
than running the same skeptic repeatedly:

- **Does the text say this?** — the pincite attack.
- **Does it say it as a holding, for this forum?** — the dicta and weight attacks.
- **Does it still say it?** — the treatment and amendment attacks.
- **Does the corpus contain the opposite?** — the contrary-authority sweep.

Redundant identical checks mostly reproduce each other's blind spots.

## What to do with what you find

Refuted and narrowed claims are not deleted quietly. The ledger records that a
claim was attacked and what happened, because a report that shows its claims were
tested is more credible than one that shows only survivors — and because the next
run should not re-derive the same overclaim.

If the pass refutes a large share of the claims, that is a finding about the
synthesis, not just about the claims. Say so, and consider whether the notes were
thin enough that the reading stage needs redoing.
