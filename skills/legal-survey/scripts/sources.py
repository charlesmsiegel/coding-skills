#!/usr/bin/env python3
"""Query builders and response parsers for the legal sources.

Every function here is pure: bytes in, Authority list out. That is deliberate.
Identity bookkeeping -- which CourtListener cluster, which reporter citation,
which of these three records are the same case -- is where a legal corpus goes
wrong, and pure functions over captured payloads are the only cheap way to hold
it still.

Each API is awkward differently, and the awkwardness is load-bearing:
  CourtListener     one cluster carries several opinions and several parallel
                    citations; the id that names the document is not the id the
                    citation graph filters on
  Federal Register  a "document" is a notice about law, not the law
  eCFR              search returns hierarchy fragments, not text; the text lives
                    behind a *dated* versioner endpoint, and the date is the point
  OpenAlex          abstracts are an inverted index; DOIs are URLs
  Crossref          titles and venues are lists
  legislation.gov.uk  Atom, and the id is a URI

Deliberately absent: the Caselaw Access Project API, which was retired in 2024.
Its corpus now lives in CourtListener, and bulk volumes at static.case.law. A
source that no longer answers is worse than one that was never offered, because
its failure reads as "no law on this question".
"""

import json
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from common import Authority, citation_key

ATOM = "{http://www.w3.org/2005/Atom}"
LEG_UK = "{http://www.legislation.gov.uk/namespaces/legislation}"

COURTLISTENER_SEARCH = "https://www.courtlistener.com/api/rest/v4/search/"
COURTLISTENER_ROOT = "https://www.courtlistener.com"
FEDERAL_REGISTER_API = "https://www.federalregister.gov/api/v1/documents.json"
ECFR_SEARCH_API = "https://www.ecfr.gov/api/search/v1/results"
ECFR_VERSIONER = "https://www.ecfr.gov/api/versioner/v1/full"
OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"
LEGISLATION_UK_FEED = "https://www.legislation.gov.uk/all/data.feed"


class ParseError(Exception):
    """A payload that did not parse. Never silently zero results."""


def _text(node, default: str = "") -> str:
    if node is None or node.text is None:
        return default
    return " ".join(node.text.split())


def _load_json(payload: bytes, source: str):
    try:
        return json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseError(source + " response did not parse as JSON: " + str(exc)) from exc


# --- CourtListener: US case law -------------------------------------------

def courtlistener_query_url(query: str, limit: int = 50, page: int = 1) -> str:
    """Opinion search. `type=o` is the opinion index; the others are dockets."""
    params = urlencode({
        "q": query,
        "type": "o",
        "order_by": "score desc",
        "page": page,
        "page_size": min(limit, 100),
    })
    return COURTLISTENER_SEARCH + "?" + params


def _best_citation(cites) -> str:
    """The most citable of a cluster's parallel citations.

    A Supreme Court case carries U.S., S. Ct. and L. Ed. 2d in some order. The
    official reporter is the one a brief uses and the one another database is
    most likely to agree on, so the preference order is not cosmetic -- it
    decides whether two records of one case dedupe into one artifact.
    """
    if not cites:
        return ""
    values = [c for c in cites if isinstance(c, str) and c.strip()]
    if not values:
        return ""
    for preferred in ("U.S.", "S. Ct.", "F.4th", "F.3d", "F.2d", "F. Supp."):
        for value in values:
            if preferred in value:
                return value
    return values[0]


def parse_courtlistener(payload: bytes) -> list[Authority]:
    data = _load_json(payload, "CourtListener")
    out = []
    for item in data.get("results") or []:
        ids = {}
        cluster = item.get("cluster_id") or item.get("id")
        if cluster:
            ids["cl"] = str(cluster)
        # The nested opinion ids are what the citation graph filters on, and they
        # are not the cluster id. Keeping only the cluster meant snowball and
        # check_treatment had nothing to query with and reported every case as
        # unresolvable -- a corpus-wide blind spot produced by one missing field.
        opinion_ids = [str(o.get("id")) for o in item.get("opinions") or [] if o.get("id")]
        if opinion_ids:
            ids["cl_opinion"] = ",".join(opinion_ids)
        absolute = item.get("absolute_url") or ""
        landing = (COURTLISTENER_ROOT + absolute) if absolute.startswith("/") else absolute
        date_filed = item.get("dateFiled") or ""
        out.append(Authority(
            title=item.get("caseName") or "",
            authority_type="case",
            external_ids=ids,
            citation=_best_citation(item.get("citation")),
            court=item.get("court_id") or "",
            jurisdiction=item.get("court_id") or "",
            decided=date_filed[:10],
            precedential_status=item.get("status") or "",
            citation_count=item.get("citeCount"),
            summary=item.get("snippet") or "",
            landing_url=landing,
            sources=["courtlistener"],
        ))
    return out


# --- Federal Register: notices, proposed and final rules ------------------

def federal_register_query_url(query: str, limit: int = 50, page: int = 1) -> str:
    params = [
        ("conditions[term]", query),
        ("per_page", str(min(limit, 1000))),
        ("page", str(page)),
        ("order", "relevance"),
    ]
    for field_name in ("document_number", "title", "publication_date", "type",
                       "abstract", "html_url", "pdf_url", "agencies", "citation"):
        params.append(("fields[]", field_name))
    return FEDERAL_REGISTER_API + "?" + urlencode(params)


def parse_federal_register(payload: bytes) -> list[Authority]:
    data = _load_json(payload, "Federal Register")
    out = []
    for item in data.get("results") or []:
        number = item.get("document_number") or ""
        published = item.get("publication_date") or ""
        agencies = [a.get("name", "") for a in item.get("agencies") or [] if a.get("name")]
        # A Federal Register document is an agency's *notice about* law -- a
        # proposed rule, a final rule's preamble, a notice of availability. It is
        # regulatory material and dated by publication, so `as_of` is the
        # publication date and not today: the FR is an archive, not a code.
        out.append(Authority(
            title=item.get("title") or "",
            authority_type="regulation",
            external_ids={"fr": number} if number else {},
            citation=item.get("citation") or "",
            court=", ".join(agencies),
            jurisdiction="us",
            decided=published,
            as_of=published,
            precedential_status=item.get("type") or "",
            summary=item.get("abstract") or "",
            pdf_url=item.get("pdf_url") or "",
            landing_url=item.get("html_url") or "",
            sources=["federal_register"],
        ))
    return out


# --- eCFR: the codified regulations ---------------------------------------

def ecfr_query_url(query: str, limit: int = 50, page: int = 1, as_of: str = "") -> str:
    params = {"query": query, "per_page": min(limit, 100), "page": page}
    if as_of:
        params["date"] = as_of
    return ECFR_SEARCH_API + "?" + urlencode(params)


def ecfr_full_text_url(as_of: str, title: str, section: str = "") -> str:
    """The dated text of one CFR section.

    The date is in the path, not an option. eCFR will hand you the code as it
    stood on any day it has, and a claim about a regulation is a claim about a
    version -- so the fetch stage records the date it asked for and the manifest
    refuses an archived regulation that has none.
    """
    params = {"section": section} if section else {}
    query = ("?" + urlencode(params)) if params else ""
    return ECFR_VERSIONER + "/" + as_of + "/title-" + str(title) + ".xml" + query


def parse_ecfr(payload: bytes, as_of: str = "") -> list[Authority]:
    data = _load_json(payload, "eCFR")
    out = []
    for item in data.get("results") or []:
        hierarchy = item.get("hierarchy") or {}
        title = str(hierarchy.get("title") or "")
        section = str(hierarchy.get("section") or "")
        part = str(hierarchy.get("part") or "")
        citation = ""
        if title and section:
            citation = title + " CFR " + section
        elif title and part:
            citation = title + " CFR part " + part
        headings = item.get("headings") or {}
        name = headings.get("section") or headings.get("part") or item.get("label") or citation
        starts_on = item.get("starts_on") or ""
        ident = "-".join(part for part in (title, part, section) if part)
        out.append(Authority(
            title=name or citation or "untitled CFR fragment",
            authority_type="regulation",
            external_ids={"ecfr": ident} if ident else {},
            citation=citation,
            jurisdiction="us",
            # The version this fragment belongs to. `starts_on` is when this text
            # took effect; the caller's --as-of is the date the code was read at.
            decided=starts_on,
            as_of=as_of or starts_on,
            summary=item.get("full_text_excerpt") or "",
            landing_url=ecfr_full_text_url(as_of or starts_on, title, section)
            if (as_of or starts_on) and title else "",
            sources=["ecfr"],
        ))
    return out


# --- Secondary literature: law reviews, working papers --------------------

def openalex_query_url(query: str, limit: int = 50, page: int = 1) -> str:
    params = urlencode({"search": query, "per-page": min(limit, 200), "page": page})
    return OPENALEX_API + "?" + params


def crossref_query_url(query: str, limit: int = 50, offset: int = 0) -> str:
    params = urlencode({"query": query, "rows": min(limit, 1000), "offset": offset})
    return CROSSREF_API + "?" + params


def _deinvert_abstract(index: dict) -> str:
    """OpenAlex stores abstracts as word -> positions. Put it back in order."""
    if not index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, spots in index.items():
        for spot in spots:
            positions.append((spot, word))
    positions.sort()
    return " ".join(word for _spot, word in positions)


def parse_openalex(payload: bytes) -> list[Authority]:
    data = _load_json(payload, "OpenAlex")
    out = []
    for item in data.get("results") or []:
        ids = {}
        doi = item.get("doi") or ""
        if doi:
            ids["doi"] = doi.replace("https://doi.org/", "")
        oa_id = (item.get("id") or "").rsplit("/", 1)[-1]
        if oa_id:
            ids["openalex"] = oa_id
        location = item.get("primary_location") or {}
        source = location.get("source") or {}
        year = item.get("publication_year")
        out.append(Authority(
            title=item.get("title") or "",
            authority_type="secondary",
            external_ids=ids,
            authors=[(a.get("author") or {}).get("display_name", "")
                     for a in item.get("authorships") or []],
            decided=str(year) if year else "",
            venue=source.get("display_name") or "",
            citation_count=item.get("cited_by_count"),
            summary=_deinvert_abstract(item.get("abstract_inverted_index") or {}),
            pdf_url=location.get("pdf_url") or "",
            landing_url=location.get("landing_page_url") or "",
            sources=["openalex"],
        ))
    return out


def parse_crossref(payload: bytes) -> list[Authority]:
    data = _load_json(payload, "Crossref")
    out = []
    for item in (data.get("message") or {}).get("items") or []:
        titles = item.get("title") or []
        venues = item.get("container-title") or []
        parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
        authors = []
        for person in item.get("author") or []:
            name = (person.get("given", "") + " " + person.get("family", "")).strip()
            if name:
                authors.append(name)
        ids = {}
        if item.get("DOI"):
            ids["doi"] = item["DOI"]
        year = parts[0] if parts and isinstance(parts[0], int) else None
        out.append(Authority(
            title=titles[0] if titles else "",
            authority_type="secondary",
            external_ids=ids,
            authors=authors,
            decided=str(year) if year else "",
            venue=venues[0] if venues else "",
            citation_count=item.get("is-referenced-by-count"),
            landing_url=item.get("URL") or "",
            sources=["crossref"],
        ))
    return out


# --- legislation.gov.uk: the pluggable non-US source ----------------------

def legislation_uk_query_url(query: str, limit: int = 50, page: int = 1) -> str:
    params = urlencode({"title": query, "results-count": min(limit, 200), "page": page})
    return LEGISLATION_UK_FEED + "?" + params


def parse_legislation_uk(payload: bytes) -> list[Authority]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ParseError("legislation.gov.uk response did not parse as XML: " + str(exc)) from exc
    out = []
    for entry in root.findall(ATOM + "entry"):
        identifier = _text(entry.find(ATOM + "id"))
        link = entry.find(ATOM + "link")
        landing = link.get("href", "") if link is not None else identifier
        updated = _text(entry.find(ATOM + "updated"))
        year_node = entry.find(LEG_UK + "year")
        year = year_node.get("Value", "") if year_node is not None else ""
        number_node = entry.find(LEG_UK + "number")
        number = number_node.get("Value", "") if number_node is not None else ""
        citation = " ".join(part for part in (year, "c." + number if number else "") if part)
        out.append(Authority(
            title=_text(entry.find(ATOM + "title")),
            authority_type="statute",
            external_ids={"uk": identifier} if identifier else {},
            citation=citation,
            jurisdiction="uk",
            decided=year or updated[:10],
            # legislation.gov.uk serves the *revised* text, whose currency varies
            # by instrument. Recording the feed's own timestamp is the honest
            # answer to "as of when": it is when this record was published, not a
            # promise that every amendment is folded in.
            as_of=updated[:10],
            summary=_text(entry.find(ATOM + "summary")),
            landing_url=landing,
            sources=["legislation_uk"],
        ))
    return out


PARSERS = {
    "courtlistener": (courtlistener_query_url, parse_courtlistener),
    "federal_register": (federal_register_query_url, parse_federal_register),
    "ecfr": (ecfr_query_url, parse_ecfr),
    "openalex": (openalex_query_url, parse_openalex),
    "crossref": (crossref_query_url, parse_crossref),
    "legislation_uk": (legislation_uk_query_url, parse_legislation_uk),
}

# US-first, as the skill's scope says. The non-US sources are real and tested but
# off by default, because running them on a US question buys nothing and spends
# somebody else's rate limit.
DEFAULT_SOURCES = ("courtlistener", "federal_register", "ecfr", "openalex", "crossref")
OPTIONAL_SOURCES = tuple(name for name in PARSERS if name not in DEFAULT_SOURCES)


def _normalized_title(title: str) -> str:
    letters = [ch.lower() for ch in title if ch.isalnum() or ch.isspace()]
    return " ".join("".join(letters).split())


def _keys(authority: Authority) -> list[str]:
    """Every identity this record claims, most authoritative first.

    The reporter citation leads because it is the identity two legal databases
    are most likely to share, normalized so "410 U.S. 113" and "410 US 113" are
    one key rather than two.

    Title is included only with a date, and only for non-cases. Two different
    cases genuinely do share a name -- "United States v. Johnson" names hundreds
    of unrelated prosecutions -- and merging them is an error no later stage can
    detect, because the merged record parses, hashes and cites perfectly well
    while being about the wrong defendant.
    """
    keys = []
    # The same function artifact_id uses. If the two disagree, two rows that never
    # merge can still name one file on disk -- so one authority is fetched twice,
    # read twice and counted twice, or worse, two authorities share a note.
    normalized = citation_key(authority.citation, authority.authority_type)
    if normalized:
        keys.append("cite:" + normalized)
    ids = authority.external_ids
    for name in ("cl", "fr", "ecfr", "usc", "uk", "doi", "openalex"):
        value = ids.get(name)
        if value:
            keys.append(name + ":" + value.lower())
    if authority.authority_type != "case" and authority.decided:
        keys.append("title:" + _normalized_title(authority.title) + ":" + authority.decided)
    return keys


def _richer(a, b):
    """Field-wise merge that keeps the more informative value, not the earlier one."""
    if a in (None, "", [], {}):
        return b
    if b in (None, "", [], {}):
        return a
    if isinstance(a, str) and isinstance(b, str):
        return a if len(a) >= len(b) else b
    if isinstance(a, int) and isinstance(b, int):
        return max(a, b)
    if isinstance(a, list) and isinstance(b, list):
        return a if len(a) >= len(b) else b
    return a


# Selected beats dropped beats new. Triage is hand-edited by design, so two rows
# for one authority can carry opposite verdicts -- dropping one as "duplicate of
# the Ninth Circuit opinion" is the natural thing to write while triaging, and
# the merge is the act of recognising that duplicate. Taking one side's status
# with the other's reason produces selected-with-a-drop_reason, which the
# Authority invariant rejects: the whole search stage dies on a dataclass field,
# naming neither of the two rows that disagreed.
_STATUS_RANK = {"selected": 2, "dropped": 1, "new": 0}


def _resolve_status(a: Authority, b: Authority) -> tuple[str, str]:
    winner = a if _STATUS_RANK[a.status] >= _STATUS_RANK[b.status] else b
    if winner.status != "dropped":
        return winner.status, ""
    return "dropped", winner.drop_reason or a.drop_reason or b.drop_reason


def _resolve_weight(a: Authority, b: Authority) -> tuple[str, str]:
    """A weighed row wins over an unweighed one, and its reason travels with it.

    Weight and weight_reason must move together or the invariant rejects the
    merge -- taking the richer reason from one row and the weight from another
    produces a record asserting 'binding' on somebody else's grounds.
    """
    for first, second in ((a, b), (b, a)):
        if first.weight:
            return first.weight, first.weight_reason
        if second.weight:
            return second.weight, second.weight_reason
    return "", ""


def _merge(a: Authority, b: Authority) -> Authority:
    ids = dict(a.external_ids)
    ids.update({k: v for k, v in b.external_ids.items() if v})
    status, drop_reason = _resolve_status(a, b)
    weight, weight_reason = _resolve_weight(a, b)
    return Authority(
        title=_richer(a.title, b.title),
        # A case merged with a secondary record describing it is still a case.
        # The more specific type wins over "secondary", which is what an index of
        # commentary will always call everything it holds.
        authority_type=a.authority_type if a.authority_type != "secondary" else b.authority_type,
        external_ids=ids,
        citation=_richer(a.citation, b.citation),
        court=_richer(a.court, b.court),
        jurisdiction=_richer(a.jurisdiction, b.jurisdiction),
        decided=_richer(a.decided, b.decided),
        precedential_status=_richer(a.precedential_status, b.precedential_status),
        authors=_richer(a.authors, b.authors),
        venue=_richer(a.venue, b.venue),
        citation_count=_richer(a.citation_count, b.citation_count),
        summary=_richer(a.summary, b.summary),
        as_of=_richer(a.as_of, b.as_of),
        pdf_url=_richer(a.pdf_url, b.pdf_url),
        landing_url=_richer(a.landing_url, b.landing_url),
        sources=sorted(set(a.sources) | set(b.sources)),
        status=status,
        drop_reason=drop_reason,
        weight=weight,
        weight_reason=weight_reason,
    )


def dedupe(authorities: list[Authority]) -> list[Authority]:
    """Collapse records that describe the same authority.

    Union-find over identity keys, so a record carrying both a reporter citation
    and a CourtListener id bridges a citation-only record to a cl-only one --
    which single-pass keying misses, and which is the common case when a case
    arrives once from the opinion index and once from a law review's reference
    list.
    """
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    keyed = []
    for authority in authorities:
        keys = _keys(authority) or ["obj:" + str(id(authority))]
        for key in keys[1:]:
            union(keys[0], key)
        keyed.append((keys[0], authority))

    groups: dict[str, Authority] = {}
    order: list[str] = []
    for key, authority in keyed:
        root = find(key)
        if root in groups:
            groups[root] = _merge(groups[root], authority)
        else:
            groups[root] = authority
            order.append(root)
    return [groups[root] for root in order]
