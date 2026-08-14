#!/usr/bin/env python3
"""Query builders and response parsers for the four scholarly sources.

Every function here is pure: bytes in, Candidate list out. That is deliberate.
Identity bookkeeping — which arXiv id, which DOI, which of these three records
are the same paper — is where a survey's corpus goes wrong, and pure functions
over captured payloads are the only cheap way to hold it still.

Each API is awkward differently, and the awkwardness is load-bearing:
  arXiv     ids carry a version suffix; titles wrap across lines
  S2        sends JSON null where a string is expected
  OpenAlex  abstracts are an inverted index; DOIs are URLs
  Crossref  titles and venues are lists
"""

import json
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from common import Candidate

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

ARXIV_API = "http://export.arxiv.org/api/query"
S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"

S2_FIELDS = "paperId,title,abstract,year,citationCount,venue,externalIds,authors,openAccessPdf"


class ParseError(Exception):
    """A payload that did not parse. Never silently zero results."""


def _text(node, default: str = "") -> str:
    if node is None or node.text is None:
        return default
    return " ".join(node.text.split())


def arxiv_query_url(query: str, limit: int = 50, start: int = 0) -> str:
    params = urlencode({
        "search_query": "all:" + query,
        "start": start,
        "max_results": limit,
        "sortBy": "relevance",
    })
    return ARXIV_API + "?" + params


def semantic_scholar_query_url(query: str, limit: int = 50, offset: int = 0) -> str:
    params = urlencode({"query": query, "limit": limit, "offset": offset, "fields": S2_FIELDS})
    return S2_API + "?" + params


def openalex_query_url(query: str, limit: int = 50, page: int = 1) -> str:
    params = urlencode({"search": query, "per-page": limit, "page": page})
    return OPENALEX_API + "?" + params


def crossref_query_url(query: str, limit: int = 50, offset: int = 0) -> str:
    params = urlencode({"query": query, "rows": limit, "offset": offset})
    return CROSSREF_API + "?" + params


def parse_arxiv(payload: bytes) -> list[Candidate]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ParseError("arXiv response did not parse as XML: " + str(exc)) from exc
    out = []
    for entry in root.findall(ATOM + "entry"):
        raw_id = _text(entry.find(ATOM + "id"))
        bare = raw_id.rsplit("/", 1)[-1]
        arxiv_id = bare.split("v")[0] if "v" in bare else bare
        ids = {"arxiv": arxiv_id}
        doi = _text(entry.find(ARXIV_NS + "doi"))
        if doi:
            ids["doi"] = doi
        pdf_url = ""
        landing_url = ""
        for link in entry.findall(ATOM + "link"):
            if link.get("type") == "application/pdf":
                pdf_url = link.get("href", "")
            elif link.get("rel") == "alternate":
                landing_url = link.get("href", "")
        published = _text(entry.find(ATOM + "published"))
        year = int(published[:4]) if published[:4].isdigit() else None
        out.append(Candidate(
            title=_text(entry.find(ATOM + "title")),
            external_ids=ids,
            authors=[_text(a.find(ATOM + "name")) for a in entry.findall(ATOM + "author")],
            year=year,
            abstract=_text(entry.find(ATOM + "summary")),
            pdf_url=pdf_url,
            landing_url=landing_url,
            sources=["arxiv"],
        ))
    return out


def _load_json(payload: bytes, source: str):
    try:
        return json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseError(source + " response did not parse as JSON: " + str(exc)) from exc


def parse_semantic_scholar(payload: bytes) -> list[Candidate]:
    data = _load_json(payload, "Semantic Scholar")
    out = []
    for item in data.get("data") or []:
        raw_ids = item.get("externalIds") or {}
        ids = {}
        if raw_ids.get("ArXiv"):
            ids["arxiv"] = raw_ids["ArXiv"]
        if raw_ids.get("DOI"):
            ids["doi"] = raw_ids["DOI"]
        if item.get("paperId"):
            ids["s2"] = item["paperId"]
        pdf = item.get("openAccessPdf") or {}
        out.append(Candidate(
            title=item.get("title") or "",
            external_ids=ids,
            authors=[a.get("name", "") for a in item.get("authors") or []],
            year=item.get("year"),
            venue=item.get("venue") or "",
            citation_count=item.get("citationCount"),
            abstract=item.get("abstract") or "",
            pdf_url=pdf.get("url") or "",
            sources=["semantic_scholar"],
        ))
    return out


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


def parse_openalex(payload: bytes) -> list[Candidate]:
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
        out.append(Candidate(
            title=item.get("title") or "",
            external_ids=ids,
            authors=[(a.get("author") or {}).get("display_name", "")
                     for a in item.get("authorships") or []],
            year=item.get("publication_year"),
            venue=source.get("display_name") or "",
            citation_count=item.get("cited_by_count"),
            abstract=_deinvert_abstract(item.get("abstract_inverted_index") or {}),
            pdf_url=location.get("pdf_url") or "",
            landing_url=location.get("landing_page_url") or "",
            sources=["openalex"],
        ))
    return out


def parse_crossref(payload: bytes) -> list[Candidate]:
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
        out.append(Candidate(
            title=titles[0] if titles else "",
            external_ids=ids,
            authors=authors,
            year=parts[0] if parts and isinstance(parts[0], int) else None,
            venue=venues[0] if venues else "",
            citation_count=item.get("is-referenced-by-count"),
            landing_url=item.get("URL") or "",
            sources=["crossref"],
        ))
    return out


PARSERS = {
    "arxiv": (arxiv_query_url, parse_arxiv),
    "semantic_scholar": (semantic_scholar_query_url, parse_semantic_scholar),
    "openalex": (openalex_query_url, parse_openalex),
    "crossref": (crossref_query_url, parse_crossref),
}


def _normalized_title(title: str) -> str:
    letters = [ch.lower() for ch in title if ch.isalnum() or ch.isspace()]
    return " ".join("".join(letters).split())


def _keys(candidate: Candidate) -> list[str]:
    """Every identity this record claims, most authoritative first.

    Title is included only with a year, because two different papers genuinely do
    share a title and merging them is an error no later stage can detect.

    The OpenAlex id counts as an identity for the same reason the other two do —
    and because `artifact_id` already treats it as one. Two records that resolve
    to one artifact_id but survive as two candidates would name one file on disk
    from two rows, which inflates the candidate count and, worse, makes every
    snowball round rediscover them: OpenAlex's own `publication_year` and `doi`
    are both nullable, so a work known only by its work id would never saturate.
    """
    keys = []
    doi = candidate.external_ids.get("doi")
    if doi:
        keys.append("doi:" + doi.lower())
    arxiv = candidate.external_ids.get("arxiv")
    if arxiv:
        keys.append("arxiv:" + arxiv.lower())
    openalex = candidate.external_ids.get("openalex")
    if openalex:
        keys.append("openalex:" + openalex.lower())
    if candidate.year is not None:
        keys.append("title:" + _normalized_title(candidate.title) + ":" + str(candidate.year))
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


def _merge(a: Candidate, b: Candidate) -> Candidate:
    ids = dict(a.external_ids)
    ids.update({k: v for k, v in b.external_ids.items() if v})
    return Candidate(
        title=_richer(a.title, b.title),
        external_ids=ids,
        authors=_richer(a.authors, b.authors),
        year=a.year if a.year is not None else b.year,
        venue=_richer(a.venue, b.venue),
        citation_count=_richer(a.citation_count, b.citation_count),
        abstract=_richer(a.abstract, b.abstract),
        pdf_url=_richer(a.pdf_url, b.pdf_url),
        landing_url=_richer(a.landing_url, b.landing_url),
        sources=sorted(set(a.sources) | set(b.sources)),
        status=a.status if a.status != "new" else b.status,
        drop_reason=a.drop_reason or b.drop_reason,
    )


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """Collapse records that describe the same work.

    Union-find over identity keys, so a record carrying arXiv+DOI bridges a
    DOI-only record to an arXiv-only one — which single-pass keying misses, and
    which is the common case when four indexes disagree about what they know.
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
    for candidate in candidates:
        keys = _keys(candidate) or ["obj:" + str(id(candidate))]
        for key in keys[1:]:
            union(keys[0], key)
        keyed.append((keys[0], candidate))

    groups: dict[str, Candidate] = {}
    order: list[str] = []
    for key, candidate in keyed:
        root = find(key)
        if root in groups:
            groups[root] = _merge(groups[root], candidate)
        else:
            groups[root] = candidate
            order.append(root)
    return [groups[root] for root in order]
