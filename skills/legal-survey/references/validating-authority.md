# Validating authority — is it still good law?

`check_treatment.py` runs before reading and again after every snowball round. It
is the stage with no counterpart in a scholarly literature survey, and it exists
because a superseded paper is merely dated whereas a superseded case is wrong.

## What this stage actually is

It walks CourtListener's citation graph for every archived case, reads a window of
text around each mention of the case in each citing opinion, and matches
treatment language. It writes `treatment.json`, one record per authority.

**It is not a citator, and the report must never present it as one.** Shepard's
and KeyCite are editorial products: attorney-editors read citing opinions and
classify what each one did. Their signals mean something because a person stood
behind each one and a company is liable for it.

Say the difference out loud, in the method tab, in these terms or better:

> Treatment was checked against CourtListener's open citation graph by matching
> language in citing opinions. No commercial citator was consulted. "No negative
> signal" means none was found in the opinions read — it is weaker than a
> citator's clean flag, and any authority the conclusion turns on should be
> confirmed in Shepard's or KeyCite before it is relied on.

## The four statuses, and what each is worth

| status | means | what to do |
|---|---|---|
| `negative` | a citing opinion says the case is overruled, abrogated, reversed, vacated, superseded by statute, or no longer good law | do not cite it as good law without a `treatment_ack`; the gate blocks it |
| `caution` | a citing opinion distinguishes, limits, criticises or questions it, or cert. was granted | read the citing opinion. Often fine, sometimes fatal |
| `good` | citing opinions were read and none of the above appeared | proceed, and say what "good" means here |
| `unchecked` | nothing has cited it, or the graph could not be reached | **not clean.** Say so in the report |

`unchecked` is the status that does the most work, and the one most likely to be
misread. A case nobody has cited has not been approved by anything — it has been
ignored, which is a different fact and sometimes a more interesting one. A very
recent case is uncited because it is new. An old case with no citing references is
usually a signal that you have found something nobody relies on, and that is worth
knowing before you build an argument on it.

## What the check will miss

Know these, because the report has to say them:

**Implicit abrogation.** A later Supreme Court decision adopts a rule
inconsistent with your case without naming it. No citing opinion says
"overruled", so no pattern matches. This is the most common serious miss and the
one a citator's editors are specifically employed to catch.

**Supersession by statute with no case saying so.** Congress amends the statute
and the case interpreting the old text is simply beside the point. Nothing in the
citation graph announces it. **Cross-check every case against the statute's
amendment history** — this is a manual step and it is not optional.

**Reversal on appeal, when the corpus starts too late.** A district court opinion
reversed in an unreported disposition, or before CourtListener's coverage of that
court begins.

**"Overruled on other grounds."** The single most common false positive *and* a
real trap in both directions. A citing opinion saying *Smith* was "overruled on
other grounds by *Jones*" produces a `negative` signal here, and *Smith* may be
perfectly good for the proposition you want. It may equally be that the "other
grounds" are exactly your grounds. **You have to read the citing opinion.** The
right outcome is usually a claim carrying a `treatment_ack` that says which
holding survived and which did not.

**Depublication and vacatur as moot.** A case can stop being citable without any
court criticising it.

**Truncation.** The check reads at most `--citing-cap` citing opinions per case
(25 by default). A leading case has thousands, and the overruling is not
guaranteed to be in the first twenty-five even sorted by date. When the cap bites,
`check_treatment.py` says so and the report must repeat it: **the check is bounded
by budget, not by the graph.** For the two or three authorities the answer
actually turns on, raise the cap or read the citing line by hand.

## Using `treatment_ack`

`Claim.treatment_ack` is the escape hatch, and it is deliberately narrow. Citing an
authority that is not in good standing is sometimes exactly right:

- the rule as it stood at the time of the conduct, in a question with an as-of date
- a holding the overruling did not touch
- the case as the *subject* of the discussion — "the court overruled *Smith*" is a
  claim about *Smith* that requires citing it

Write the acknowledgement so a reader knows you knew:

```
"Overruled on other grounds by Jones, 602 U.S. 1 (2024); the Exemption 5
 holding at 118 was not disturbed and later cases still cite it for that."
```

Not:

```
"Still relevant."
```

An empty or vacuous `treatment_ack` satisfies the gate and defeats its purpose.
The gate can only check that you said something; the adversarial pass is where
somebody checks that what you said is true.

## The rule

**Never cite an authority whose treatment you have not seen.** Not "usually";
never. The check is mechanical precisely so this holds for every case in the
corpus rather than for the ones somebody remembered to worry about — and the
memorable ones are not the ones that get you.
