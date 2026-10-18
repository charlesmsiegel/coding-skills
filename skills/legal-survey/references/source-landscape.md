# Source landscape

Which repositories to query for which question, what each one will not tell you,
and where the law you need is behind a wall.

`search_sources.py` queries CourtListener, the Federal Register, eCFR, OpenAlex
and Crossref by default. That set is right for a US federal question and thin
almost everywhere else. This guide is how to correct it.

## The open sources, and how each one lies to you

| source | keyless | what it is, and what it is not |
|---|---|---|
| **CourtListener** | yes, better with a token | The largest open US case-law corpus, and since 2024 the home of the Caselaw Access Project's data. Also the citation graph this skill's treatment check and snowball both run on. Anonymous access is throttled hard and caps the result set. **Coverage is uneven below the federal appellate level** — state trial courts are patchy and some state intermediate courts start late. |
| **Federal Register** | yes | Excellent, complete back to 1994, and full text. But an FR document is an agency's *notice about* law — a proposed rule, a final rule and its preamble, a notice. The preamble is often the best available explanation of what a regulation means; it is not the regulation. |
| **eCFR** | yes | The codified regulations, and — the part that matters — a *versioner* that will hand you the code as it stood on any given day. Its search returns hierarchy fragments, not text; the text comes from a second, dated call. |
| **OpenAlex** | yes | The widest index of legal scholarship, and the citation graph for the secondary half of the corpus. Abstracts arrive as an inverted index. Law reviews are indexed unevenly because many carry no DOI. |
| **Crossref** | yes | Best for journal-published legal scholarship. Titles and venues are lists. No full text, and no abstracts for much of its corpus. |
| **legislation.gov.uk** | yes | UK primary and secondary legislation, as an Atom feed. Off by default. It serves *revised* text whose currency varies by instrument — the feed timestamp is when the record was published, not a promise that every amendment is folded in. |

`search_sources.py` treats one failing source as a caveat and all sources failing
as an error. A run reporting "4 of 5 sources" is normal.

### The CourtListener token

Anonymous CourtListener works and covers less. A free token raises the rate limit
and lifts the result cap, and case law is usually the half of the corpus the
answer turns on.

`search_sources.py` emits a caveat when `COURTLISTENER_TOKEN` is absent. **When
that caveat appears and the question turns on case law, ask the user for a
token** — they can get one from a free courtlistener.com account. If they decline,
that is a perfectly good answer; record it in the method tab so the report's
thinness is attributable.

## What is not here, and why

**Westlaw, Lexis and Bloomberg Law are out of reach and stay that way.** They hold
the headnotes, the citators, the annotated codes and much of the state-court
corpus. This skill does not scrape them, does not use borrowed credentials, and
does not pretend the open web is a substitute. Where the answer needs them, the
report says so in the gaps tab. That is not a formality: an open-source research
run that presents itself as equivalent to a Westlaw run is making a claim about
completeness it cannot support.

**The Caselaw Access Project API was retired in 2024.** Its corpus moved into
CourtListener; bulk volumes remain at `static.case.law` for offline work. Do not
add a source that no longer answers — its failure reads as "no law on this".

**Google Scholar** has excellent case coverage and no API, and blocks automated
access. Fine for a human to check by hand; not a source this pipeline can use.

**HeinOnline** is where the older law reviews are. Paywalled. OpenAlex and
Crossref will give you the metadata, and the author's own SSRN posting is very
often the open copy — that is a legitimate open version, not a circumvention.

## Where to look, by question

**US federal case law** — CourtListener, then the court's own site for anything
recent. Slip opinions appear on `uscourts.gov` and on the Supreme Court's own site
before they reach any database, and for a question about a decision from the last
few weeks that is the only place the text exists.

**US state law** — CourtListener first, then **the state's own judiciary site**,
which is authoritative and usually free. State *codes* are the harder half:
several states publish only through a vendor, and the free versions lag. Check
the code's own currency statement and record it as `as_of`; a state code with no
stated currency date is a gap, not a text.

**Federal statutes** — the **Office of the Law Revision Counsel** (`uscode.house.gov`)
publishes the US Code with an explicit currency statement, and **govinfo** has the
authenticated PDFs. Neither is in the default source set; hand their URLs to
`fetch_authorities.py --url`.

**Federal legislative history** — **Congress.gov** for bills, reports and the
Congressional Record. Its API needs a key and has no useful keyword search over
full text, so the practical route is the web interface plus `--url`. Committee
reports are the part that matters; floor statements are the part courts discount.

**Regulations and agency practice** — eCFR for the text, the Federal Register for
the preamble and the rulemaking history, and **regulations.gov** for the comment
docket. Then the agency's own guidance, manuals and enforcement actions, which is
where the operative interpretation usually lives and which no case-law database
indexes. Guidance is not binding; it is evidence of what the agency will do.

**UK** — `legislation.gov.uk` for statute (enable it with `--source legislation_uk`),
**BAILII** and the **National Archives' Find Case Law** service for judgments.

**EU** — **EUR-Lex** for everything, with a SPARQL endpoint that is powerful and
awkward; in practice, search its web interface and fetch by CELEX id with `--url`.

**ECHR** — **HUDOC** has a JSON API and full judgments.

**Canada** — **CanLII** has an API requiring a key granted on request.

**International** — the **UN Treaty Collection**, **WIPO Lex** for IP law by
country, and the **ICJ** and **ITLOS** sites for their own decisions.

## Grey legal literature, which is not optional

For anything younger than a couple of years, and for anything about how a rule is
actually enforced, the published law is a lagging indicator:

- **Agency guidance, FAQs, manuals and no-action letters** — often the only
  statement of the operative interpretation.
- **Enforcement actions and consent decrees** — what the agency does, as opposed
  to what it says.
- **Amicus briefs** in a pending case — the best available survey of the
  arguments, written by people paid to find them.
- **Practitioner blogs and firm client alerts** — fast, well-informed, and
  advocacy. Treat as a pointer to authority, never as authority.
- **Bar association ethics opinions** — non-binding and heavily relied on.

Discover these with the agent's own web search, then hand the URLs to
`fetch_authorities.py --url` so they land in the manifest like everything else.
**An authority that is not in the manifest cannot be cited**, and the point of the
flag is that this rule does not silently narrow the corpus to whatever has an API.

## Rate limits and etiquette

`common.Http` enforces one connection at a time per host, a real interval between
calls, exponential backoff, and `robots.txt` for open-web fetches. Do not raise
the concurrency to make a run finish sooner. CourtListener is run by a small
non-profit and the Federal Register and eCFR are public infrastructure; they stay
usable because clients behave.

The `COURTLISTENER_TOKEN` is sent only to CourtListener. A blanket Authorization
header would hand it to every publisher the fetch stage touches.

Paywalls are recorded as gaps and never circumvented. If an opinion is not
reachable, the report says so — "the law says" and "the law we could reach says"
are different claims, and in a legal memo conflating them is the more serious
error by a wide margin.
