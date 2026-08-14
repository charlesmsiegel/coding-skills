# Triage and selection

You have 100–300 candidates and can read 15–30. This guide is about which ones,
and about the four ways that choice goes wrong.

Everything here is judgment. `search_sources.py` cannot do it, and no mechanical
check downstream will catch a mistake made at this stage — a wrongly selected
paper is parsed correctly, hashed correctly, cited by the correct page, and about
the wrong subject.

## 1. Field drift, which is the one that ruins corpora silently

**Search engines match vocabulary, not fields.** This is the first thing to guard
against because it is invisible to every other check in the skill.

A real run of this skill searched **"transactive memory systems"** — a construct
from organizational psychology, about how teams distribute who-knows-what — and
arXiv's top hits were:

- *Opacity of Memory Management in Software Transactional Memory*
- *Unidirectional Error Correcting Codes for Memory Systems: A Comparative Study*

Both are about computer architecture. Neither has anything to do with teams. Both
matched on "transactional memory" and "memory systems". A survey built on them
would have been fluent, well-cited, hash-verified, and worthless.

**So: name the target discipline for every subtopic before triaging it.** Write it
down explicitly, in the form "organizational psychology and management science —
*not* computer architecture". Then:

> Drop every off-field candidate with the reason `wrong field: matched on
> vocabulary, not subject`.

Recording the reason matters as much as the drop. It puts the failure in
`candidates.json` where a reader can see the search went astray, instead of
leaving a corpus that looks deliberate.

Signals that a hit is off-field:

| signal | example |
|---|---|
| venue belongs to another discipline | *IEEE Trans. on Computers* for a teams question |
| the shared term is a compound in the other field | "transactional memory", "memory systems" |
| author affiliations are all one unrelated department | every author in an EE department |
| the abstract's nouns are from the wrong ontology | latency, cache lines, throughput — not teams, tasks, expertise |

**A cross-disciplinary topic is different from a drifted one.** Some questions
genuinely span fields: knowledge sharing between LLM agents really does borrow
from team-cognition theory. When that is the case, say so, and keep both fields
with the connection stated. The test is whether the papers are *about the same
phenomenon*, not whether they share a word.

## 2. Citation count, which is a popularity measure with a time lag

Raw citation counts cannot rank recent work against old work. A 2011 paper with
812 citations and a 2026 paper with 3 may be equally important; the difference is
that one has had fifteen years to accumulate.

- **Never rank a mixed-year candidate list by citations.** Rank within year bands,
  or use citations-per-year, and even then treat it as a weak signal.
- **The tail is where the news is.** A survey that selects the top-N by citations
  systematically reproduces the last decade's consensus and misses whatever is
  currently overturning it — which is usually the thing the reader asked about.
- **High citations can mean "widely criticised".** A paper everybody cites to
  disagree with looks identical, in the metadata, to one everybody builds on. You
  find out by reading, or by reading its citers' framing.
- **Zero citations on a 2026 preprint is not evidence of anything.**

Select deliberately across the year range: enough recent work to see the current
state, enough older work to know what the current state is a reaction to.

## 3. Surveys: shortcut or trap

A recent survey of the field is the highest-value single artifact you can read —
and the easiest way to produce a survey-of-a-survey that adds nothing.

**Use a survey to:** learn the field's own taxonomy and vocabulary, find the
canonical papers, and discover which questions the field considers open.

**Do not use a survey to:** support a claim about a primary result. If the survey
says "Zhang et al. found X", the claim's locator belongs in Zhang et al., not in
the survey. Citing the survey for X means nobody in your pipeline has read the
evidence for X.

**Two or more surveys disagreeing about the field's shape is a finding**, not a
nuisance to average away. It usually means the field has not settled its own
boundaries, which is worth telling the reader.

## 4. Preprint and published are one work

The same paper appears as an arXiv preprint and a journal article, often with a
different title and a year or two apart. `sources.dedupe()` collapses them when
the identifiers overlap, but it cannot when a title was rewritten for publication.

Watch for: same authors, same year ±2, substantially the same abstract. Merge
them, keep the published venue and the preprint's open PDF, and cite the version
you actually read.

**Preprint-only is not a disqualification.** In fast-moving ML the preprint *is*
the literature. But an unreviewed preprint's claims carry less weight than a
reviewed one's, and the report should say which is which rather than flattening
them.

## How many, and what "selected" means

Select 15–30 for full reading. Fewer than about 15 and the synthesis is one or two
papers' opinions; more than about 30 and the reading is shallow enough that the
notes stop being worth having.

Set `status` on **every** candidate:

- `selected` — will be fetched and read in full.
- `dropped` — with a reason. Required by the dataclass, and the reason is what
  makes the survey auditable.
- `new` — not yet judged. **No candidate should still be `new` when fetching
  begins**; an unjudged candidate is one nobody decided about.

Good drop reasons are specific and re-readable a month later:

```
wrong field: matched on vocabulary, not subject
superseded by 2026 version (same authors, extended)
survey; used for orientation but not cited for primary results
no retrievable full text and abstract is insufficient to judge
duplicate of 2510.20345 under the pre-publication title
off-question: about X, and the question is Y
```

Bad drop reasons, all of which mean "I did not decide":

```
not relevant
low quality
too old
```
