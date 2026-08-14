# Source landscape

Which indexes to query for which field, and how each one lies to you.

`search_sources.py` queries arXiv, Semantic Scholar, OpenAlex and Crossref for
every topic and subtopic. That default is right for a computer-science question
and actively misleading for others. This guide is how to correct it.

## Measured facts about the four default sources

These were observed from real runs of this skill, not read off documentation.

| source | keyless | observed behaviour |
|---|---|---|
| **arXiv** | yes | Reliable and fast. Matches **vocabulary across fields** — see the transactional-memory case in `triage-and-selection.md`. Preprints only, so no journal-only work. Ids carry a version suffix (`v2`) that the parser strips. |
| **OpenAlex** | yes | The most generous of the four and the widest coverage. Abstracts arrive as an inverted index. DOIs arrive as URLs. **This is the primary citation-graph source** — `snowball.py` uses it for exactly this reason. |
| **Crossref** | yes | Best for journal articles, which makes it the one that matters outside CS. Titles and venues are lists. No abstracts for much of its corpus, and no full text. |
| **Semantic Scholar** | no | **Returns HTTP 429 reliably without an API key**, including after four backed-off attempts. Treat its contribution as a bonus, never a dependency. With a key it is excellent, especially for citation contexts. |

`search_sources.py` treats one failing source as a caveat and all four failing as
an error. A run reporting "3 of 4 sources" is normal and is usually Semantic
Scholar being throttled.

## Where to look, by field

**Computer science, ML, NLP** — arXiv first, then OpenAlex. **ACL Anthology**
(`aclanthology.org`) for NLP, which is open and complete and not in arXiv when a
paper skipped preprinting. **OpenReview** (`openreview.net`) for ICLR/NeurIPS,
where the *reviews* are often more informative than the paper about what the
community disputes.

**HCI, CSCW, CSCL** — the **ACM Digital Library** is the field, and it is largely
paywalled. Crossref and OpenAlex give metadata; full text often will not be
available. Authors' own institutional pages frequently host the PDF; that is a
legitimate open copy, not a paywall circumvention.

**Management, organizational science, psychology** — this is where the defaults are
weakest and where the motivating topic for this skill lives. Crossref and OpenAlex
for metadata. **SSRN** for working papers, **NBER** for economics. Canonical work
sits in *Organization Science*, *Academy of Management Journal*, *Journal of
Applied Psychology* — none of them open. **Expect metadata-only, and record the
missing full text as a gap rather than pretending the abstract was the paper.**
Skip arXiv for these topics or expect its hits to be off-field.

**Medicine, biology** — **PubMed** and PMC, which are open and excellent.

**Artifacts** — GitHub for implementations, Hugging Face for models and datasets,
Papers with Code for the benchmark-to-code mapping. A repository is evidence of a
different kind: it shows what was actually built, which frequently differs from
what the paper describes.

## Grey literature, which is not optional

For anything younger than about two years, the papers are a lagging indicator. The
current state of practice lives in:

- **Lab and company engineering blogs** — often the only description of a system
  that is in production.
- **Conference talks** — slides and recordings, where people say the thing the
  paper's related-work section could not.
- **Hacker News and Reddit threads** — for *practitioner experience at scale*,
  which no paper reports. A thread saying "we tried this and it fell over at 10M
  documents" is evidence, and the report should mark it as the kind of evidence it
  is.
- **GitHub issues** on the reference implementation — where the failure modes are.

Discover these with the agent's own web search, then hand the URLs to
`fetch_artifacts.py` so they land in the manifest like everything else. A tutorial
that is not in the manifest cannot be cited.

## Rate limits and etiquette

`common.Http` enforces one connection at a time per host, a real interval between
calls, exponential backoff, and `robots.txt` for open-web fetches. Do not raise
the concurrency to make a run finish sooner: these are other people's servers, and
arXiv and OpenAlex in particular are public goods that stay usable because clients
behave.

Paywalls are recorded as gaps and never circumvented. If a paper is not reachable,
the report says so — "the literature says" and "the reachable literature says" are
different claims, and conflating them is the more serious error.
