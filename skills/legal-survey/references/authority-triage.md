# Authority triage and selection

You have 100–300 candidates and can read 15–30. This guide is about which ones,
and about the five ways that choice goes wrong in law specifically.

Everything here is judgment. `search_sources.py` cannot do it, and no mechanical
check downstream will catch a mistake made at this stage — a wrongly selected
case is fetched correctly, hashed correctly, validated correctly, cited to the
correct page, and about the wrong body of law.

## 1. Jurisdiction drift, which ruins a corpus silently

**Search engines match words, not jurisdictions or doctrines.** This is the first
thing to guard against because it is invisible to every other check in the skill.

Three shapes of it:

**The same term, a different doctrine.** "Consideration" in contract is not
consideration in a planning appeal. "Battery" in tort is not battery in criminal
law and neither is a battery in a products case. "Conversion" is a tort, a
corporate-finance event, and a religious status. "Standing" in federal court is a
constitutional test with a body of law that does not transfer to a state court
that has no Article III.

**The same name, a different statute.** Nearly every state has a consumer
protection act, a wage act, a public records act. A search for "public records act
exemption deliberative" returns California, Washington and Florida cases about
three different statutes with three different exemption structures. They are not
authority for each other, and the opinions look identical in a result list.

**The same words, a different country.** "Judicial review" in the UK is an
administrative-law procedure; in the US it is Marbury. A search will happily mix
them.

So: **name the jurisdiction and the doctrine for every issue before triaging it.**
Write it down explicitly, in the form "federal FOIA exemption 5, deliberative
process privilege — *not* state public-records acts, and *not* the civil discovery
deliberative privilege". Then:

> Drop every off-jurisdiction candidate with the reason `wrong jurisdiction:
> matched on vocabulary, not on the governing law`.

Recording the reason matters as much as the drop. It puts the failure in
`candidates.json` where a reader can see the search went astray, instead of
leaving a corpus that looks deliberate.

Signals that a hit is off-jurisdiction or off-doctrine:

| signal | example |
|---|---|
| court belongs to another system | a state supreme court for a federal-question issue |
| the statute cited is a state analogue | "Public Records Act" where the question is FOIA |
| the shared term is a term of art elsewhere | "consideration", "conversion", "standing" |
| the opinion's own citations are all from another line | it cites no case your other candidates cite |

**A genuinely cross-jurisdictional question is different from a drifted one.**
Where there is no binding authority, persuasive authority from other circuits or
states is exactly what you want — that is what a court will be reading too. When
that is the case, say so, keep it, and mark it `persuasive` with the reason.

## 2. Weight: the field that makes this a legal survey

Every selected authority carries a `weight` and a `weight_reason`, and the
dataclass will not let you skip it. Weight is **relative to the forum you named**,
and the reason has to say on what basis.

- **binding** — this court must follow it. Same jurisdiction, superior (or the
  same) court, on point, still good law. All four, not three.
- **persuasive** — this court may follow it. Another circuit, another state, a
  lower court, a well-reasoned opinion from anywhere, a treatise, a restatement.
- **secondary** — commentary. Law reviews, treatises, practice guides, blogs. It
  can be extremely useful and it binds nobody.

Good weight reasons name the forum and the basis:

```
binding: 9th Cir. published opinion; forum is N.D. Cal.
binding: controlling Supreme Court authority on the federal question
persuasive: 2d Cir.; no 9th Cir. authority on this sub-issue
persuasive: unpublished 9th Cir. disposition; not precedential under Circuit Rule 36-3
secondary: law review note; used for the taxonomy, not cited for the rule
```

Bad ones, all of which mean "I did not decide":

```
important case
leading authority
relevant
```

**Three traps in assigning weight:**

*Published versus unpublished.* An unpublished disposition is usually not
precedential, and the rule varies by court. CourtListener's `status` field carries
this; do not discard it. An unpublished case that says exactly what you want is
the most tempting thing in the corpus and the least useful.

*A case is binding only for its holding.* A binding case's dicta is not binding.
See `reading-an-authority.md` — the two go in different fields for this reason.

*Binding requires still-good-law.* An overruled Supreme Court case binds nobody.
Weight and treatment are separate fields precisely so a run cannot assert one
while never checking the other.

## 3. Currency, which no other research domain has this badly

A statute or regulation is a snapshot. The same citation resolved a year apart is
two different texts, and the reader is usually asking about conduct that happened
at a particular time.

- **Set `--as-of` from the facts, not from today.** Conduct in 2019 is governed by
  the 2019 text. Litigation filed today about 2019 conduct may involve both.
- **Amendments and effective dates are traps.** A statute amended in 2021 with an
  effective date in 2023 has three periods, not two.
- **A case interpreting a superseded version is not authority for the current
  one**, however squarely on point it looks. This is the single most common way an
  otherwise careful memo goes wrong, because the case is real, good law, on point,
  and about a text that no longer exists.
- Record the code's own currency statement. A code with no stated currency date
  is a gap, not a text.

## 4. Citation count, which is a popularity measure with a time lag

CourtListener's citation counts cannot rank a recent case against an old one. A
1978 case with 4,000 citing references and a 2025 case with two may be equally
important to your question; one has had forty-seven years.

- **Never rank a mixed-year candidate list by citation count.** Rank within
  periods, or read the recent tail deliberately.
- **The recent tail is where the doctrine moved.** A corpus selected by citation
  count reproduces the settled view and misses the split that is currently
  forming — which is usually what the reader asked about.
- **A heavily-cited case may be heavily criticised.** A case everyone cites to
  distinguish looks identical, in the metadata, to one everyone follows. You find
  out from `check_treatment.py` and from reading the citing opinions.
- **Zero citing references on a case from last month is not evidence of anything**
  — but it does mean the treatment check has nothing to work with, so the case is
  `unchecked` rather than good.

## 5. Secondary sources: shortcut or trap

A recent law review article surveying your exact question is the highest-value
single artifact you can read — and the easiest way to produce a memo that is a
paraphrase of somebody's footnotes.

**Use a secondary source to:** learn the doctrine's own vocabulary and structure,
find the leading cases, find the circuit split, and find the argument nobody in
the case law has made yet.

**Do not use one to:** support a statement of what the law is. If the article says
"the Ninth Circuit held X in *Smith*", the claim's locator belongs in *Smith*, not
in the article. Citing the article for X means nobody in your pipeline has read
the case — and law review footnotes contain errors at a rate that will surprise
you.

A **restatement** or a leading **treatise** is a special case: courts cite them,
so they carry real persuasive weight. They are still secondary, and the weight
field still says `secondary`.

## How many, and what "selected" means

Select 15–30 for full reading. Fewer than about 15 and the answer is one or two
opinions' view of the doctrine; more than about 30 and the reading is shallow
enough that the notes stop being worth having.

Bias the selection deliberately:

- the controlling authority, whatever it is
- the leading case in the forum, and the leading case against
- the most recent authority in the forum, which is what the court will have read
- the best-reasoned contrary authority you can find — the adversarial pass will
  ask for it, and finding it now is cheaper
- the operative statutory or regulatory text itself, always. A doctrine survey
  with no statute in it is a survey of what courts said about a text nobody read.

Set `status` on **every** authority:

- `selected` — will be fetched, validated and read in full. Requires `weight` and
  `weight_reason`.
- `dropped` — with a reason. Required by the dataclass, and the reason is what
  makes the survey auditable.
- `new` — not yet judged. **No authority should still be `new` when fetching
  begins**; an unjudged authority is one nobody decided about.

Good drop reasons are specific and re-readable a month later:

```
wrong jurisdiction: matched on vocabulary, not on the governing law
state analogue statute; the question is the federal act
superseded: interprets the pre-2021 text and the question is post-amendment
unpublished; not precedential in the forum and adds nothing the published line lacks
duplicate of the F.3d opinion under the slip-opinion caption
secondary; used for the taxonomy, not cited for any rule
on the procedural question, and the question is the merits
```

Bad drop reasons, all of which mean "I did not decide":

```
not relevant
old
weaker case
```
