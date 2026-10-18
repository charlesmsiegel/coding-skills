# Synthesis

You have 15–30 notes, a treatment record, and no documents. Turn them into a
ledger, then into an argument.

The ledger is the audit trail: every row is settled, contested, or unsupported,
and every settled row carries locators that `verify_authorities.py` will
re-resolve. The argument is what the reader actually reads. Both come from the
notes and nothing else.

## The three buckets

**Settled** — binding authority in the forum says so, and it is still good law.
Note the asymmetry with a scholarly survey: **one controlling case settles a
question that a hundred persuasive ones do not.** Counting authorities is the
wrong instinct here. A single on-point holding from the forum's own appellate
court outweighs any amount of agreement elsewhere, and the ledger row should say
which it is.

**Contested** — the authorities disagree. This is a *finding*, and usually the
most valuable one, because it is what a reader cannot get from any single
opinion. State both positions at full strength, with locators for each, name the
courts, and say what would resolve it. A circuit split is not a nuisance to
average away; it is the answer to "how much risk is there".

**Unsupported** — a proposition that appears in the corpus but nothing
establishes. Endemic in briefs, secondary sources and introductions, where "it is
well established that X" cites a case that cites a case that held something
narrower. Record these as `dicta` with `why_not_binding`; they are frequently the
most interesting open questions.

The temptation is to resolve contested into settled by weighing. Resist it. If
two circuits disagree, the honest report says the circuits disagree — and then
says which way *your* forum is likely to go and why, which is a different and
clearly-labelled claim.

## Hierarchy is not a tiebreaker, it is the structure

Before you can weigh two authorities against each other you have to know they are
in the same conversation:

1. **Constitution**, then **statute**, then **regulation**. A case interpreting a
   statute that has since been amended is not authority about the current statute,
   however good the reasoning.
2. **Binding beats persuasive**, always, on the point it actually holds.
3. **Later beats earlier from the same court** — but only where the later case
   addresses the point. A more recent case that does not reach the issue does not
   displace an older one that does.
4. **Holding beats dicta**, including a superior court's dicta. A circuit's
   holding controls over the Supreme Court's aside, which is counterintuitive and
   correct.
5. **En banc beats panel. Published beats unpublished.**

## Same words, different rule

**This is the failure that makes a synthesis confidently meaningless, and it is
the main reason this guide exists.** It is the legal analogue of measuring two
different things and averaging them.

Two courts both apply "the deliberative process privilege". One asks whether the
document was predecisional and deliberative. The other asks whether disclosure
would chill deliberation on these facts. They reach opposite results on similar
documents.

There is no split to resolve. **They are applying different tests.** Reporting
"the courts are divided on whether draft memoranda are protected" destroys the
actual finding — which is that the doctrine is stated in two incompatible ways, so
the disagreement is partly about the test and not about the documents.

The rule: **before comparing two outcomes, check the two courts applied the same
test to the same posture.** When they did not, the ledger row is about the tests:

> **Contested — and partly a difference in the test.** The Ninth Circuit applies
> the two-part predecisional-and-deliberative inquiry (*Smith*, p. 7); the Second
> Circuit asks whether disclosure would chill deliberation on the facts
> (*Jones*, p. 12). A district court in the Ninth Circuit reads them as
> consistent (*Doe*, p. 4). These are plausibly two formulations diverging in
> application rather than a clean split, which matters because a court persuaded
> the tests are the same will not treat the out-of-circuit authority as contrary.

Where this bites hardest: standards of review stated differently; multi-factor
tests where courts weight the factors differently; "substantial evidence" and
"clearly erroneous" across systems; anything where one court states a rule and
another states an exception to it.

## Posture is half of what a case holds

A holding is a proposition *about a procedural question*. Two opinions on the same
doctrine from the same court are not comparable if one reviewed a motion to
dismiss and the other a bench trial.

- A reversal of dismissal holds that the complaint *states a claim*, not that the
  plaintiff is right.
- A denial of summary judgment holds a *dispute of fact* exists.
- An affirmance under abuse-of-discretion review holds the trial court was *within
  its range*, which is compatible with the opposite ruling also being affirmed.

Flattening these produces a ledger that says the law is settled when what is
settled is a much smaller thing.

## Weighting authority

| stronger | weaker |
|---|---|
| binding, on point, good law | persuasive, or analogous, or unvalidated |
| holding | dicta, however emphatic |
| en banc or supreme court | panel |
| published | unpublished or non-precedential |
| recent, in a moving doctrine | old, in a moving doctrine |
| old and repeatedly followed, in a settled one | recent and uncited |
| the statute's text | a case's paraphrase of the statute's text |
| the agency's regulation | the agency's guidance |
| a court's characterisation of the record | a brief's characterisation |

A single controlling case beats three persuasive ones pointing the same way. Say
which kind of authority a settled claim rests on; "the weight of authority
supports X" means little if none of it binds the forum.

## Narrative smoothing

A good memo has a thesis. The danger is that a thesis makes contrary authority
feel like noise — and in a legal document that is not merely sloppy, it is the
thing opposing counsel is paid to find.

Symptoms in your own draft:

- An authority appears in the ledger but not in the narrative, and it is the one
  that cuts the other way.
- Hedges accumulate around inconvenient cases ("although one court has suggested")
  while convenient ones are stated flat.
- The doctrinal history reads as progress toward the current rule, with the
  abandoned line omitted.
- Every authority points the same way. Real doctrines are messier; if yours is
  not, you have probably smoothed it, or the question was not contested and the
  report should say *that* instead.

The corrective is mechanical: after drafting, walk the ledger and confirm every
contested row appears in the narrative *as contested*. Then run the adversarial
pass, which exists precisely to catch this.

## Promoting and dropping dicta

A `Dictum` becomes a `Claim` only when you can point at a holding — at which
point it needs locators and `why_not_binding` goes away. A dictum that survives
the whole synthesis without becoming a holding has two honest homes: the open
questions tab, or nowhere. It does not get promoted because it fits the thesis.

Dicta from a court that binds the forum is worth flagging even so: it tells you
where that court is likely to go, and "the Ninth Circuit has signalled" is a
legitimate and clearly-labelled claim that is not "the Ninth Circuit has held".

## What the reader actually needs

Order the argument by what changes a decision, not by what the doctrine finds
interesting. For a survey of an area: what is settled enough to rely on, what is
contested enough to plan around, what is unresolved. For a specific question: the
answer, the strongest case against it, how confident you are, and what would
change it.

And say what the corpus could not tell you. A gap the report names is a limit; a
gap it does not name is a hole the reader will fall into — in front of a judge, on
the record.
