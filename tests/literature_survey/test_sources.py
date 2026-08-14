"""Four indexes, four different ways of being awkward, one corpus.

The parsers are pure — bytes in, Candidate list out — so identity bookkeeping can
be held still over captured payloads. That is where a survey's corpus actually
goes wrong: two records of one paper inflate every count downstream, and two
papers merged into one silently deletes a result nobody will notice is missing.
"""

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "literature-survey" / "scripts"


@pytest.fixture
def sources(load_module):
    return load_module(SCRIPTS, "sources")


ARXIV_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v3</id>
    <title>Transactive memory
      systems in engineering teams</title>
    <summary>We study how teams remember.</summary>
    <published>2024-01-02T00:00:00Z</published>
    <author><name>A. Researcher</name></author>
    <author><name>B. Coauthor</name></author>
    <arxiv:doi>10.1145/3411764</arxiv:doi>
    <link rel="alternate" href="http://arxiv.org/abs/2401.00001v3"/>
    <link type="application/pdf" href="http://arxiv.org/pdf/2401.00001v3"/>
  </entry>
</feed>
"""


def test_the_arxiv_version_suffix_is_stripped_from_the_id(sources):
    """v3 and v1 are one paper; keeping the suffix makes them two rows and two downloads."""
    [candidate] = sources.parse_arxiv(ARXIV_FEED)

    assert candidate.external_ids["arxiv"] == "2401.00001"


def test_an_arxiv_title_wrapped_across_lines_is_rejoined(sources):
    [candidate] = sources.parse_arxiv(ARXIV_FEED)

    assert candidate.title == "Transactive memory systems in engineering teams"


def test_arxiv_pdf_and_landing_links_are_told_apart(sources):
    [candidate] = sources.parse_arxiv(ARXIV_FEED)

    assert candidate.pdf_url.endswith("/pdf/2401.00001v3")
    assert candidate.landing_url.endswith("/abs/2401.00001v3")
    assert candidate.year == 2024
    assert candidate.authors == ["A. Researcher", "B. Coauthor"]


def test_a_broken_arxiv_payload_raises_rather_than_returning_nothing(sources):
    """Silently zero results is indistinguishable from an empty literature."""
    with pytest.raises(sources.ParseError, match="did not parse as XML"):
        sources.parse_arxiv(b"<feed><entry>")


def test_semantic_scholar_nulls_become_empty_strings(sources):
    """S2 sends JSON null where a string belongs; `None` propagates into filenames."""
    payload = (b'{"data": [{"paperId": "abc", "title": "Shared mental models",'
               b' "abstract": null, "venue": null, "year": 2019, "citationCount": 12,'
               b' "externalIds": {"DOI": "10.1/x", "ArXiv": null},'
               b' "authors": [{"name": "C. Author"}], "openAccessPdf": null}]}')

    [candidate] = sources.parse_semantic_scholar(payload)

    assert candidate.abstract == "" and candidate.venue == "" and candidate.pdf_url == ""
    assert candidate.external_ids == {"doi": "10.1/x", "s2": "abc"}, "a null ArXiv id is not an id"


def test_an_openalex_inverted_abstract_is_put_back_in_order(sources):
    payload = (b'{"results": [{"id": "https://openalex.org/W123", "title": "T",'
               b' "doi": "https://doi.org/10.1/x", "publication_year": 2011,'
               b' "cited_by_count": 40, "authorships": [{"author": {"display_name": "D"}}],'
               b' "primary_location": {"source": {"display_name": "CSCW"},'
               b' "pdf_url": "https://x/p.pdf", "landing_page_url": "https://x/p"},'
               b' "abstract_inverted_index": {"we": [0], "find": [1], "no": [2], "effect": [3]}}]}')

    [candidate] = sources.parse_openalex(payload)

    assert candidate.abstract == "we find no effect"
    assert candidate.external_ids == {"doi": "10.1/x", "openalex": "W123"}, "the DOI url prefix is not part of the DOI"
    assert candidate.venue == "CSCW"


def test_crossref_list_valued_titles_and_venues_are_unwrapped(sources):
    payload = (b'{"message": {"items": [{"DOI": "10.1/y", "title": ["Team cognition"],'
               b' "container-title": ["Journal of Applied Psychology"],'
               b' "issued": {"date-parts": [[2001, 4]]},'
               b' "author": [{"given": "E.", "family": "Salas"}],'
               b' "is-referenced-by-count": 900, "URL": "https://doi.org/10.1/y"}]}}')

    [candidate] = sources.parse_crossref(payload)

    assert candidate.title == "Team cognition"
    assert candidate.venue == "Journal of Applied Psychology"
    assert candidate.year == 2001
    assert candidate.authors == ["E. Salas"]


def test_a_crossref_record_with_no_issue_date_has_no_year(sources):
    payload = b'{"message": {"items": [{"DOI": "10.1/z", "title": ["Untitled"]}]}}'

    [candidate] = sources.parse_crossref(payload)

    assert candidate.year is None


@pytest.mark.parametrize("parse,source", [
    ("parse_semantic_scholar", "Semantic Scholar"),
    ("parse_openalex", "OpenAlex"),
    ("parse_crossref", "Crossref"),
])
def test_a_broken_json_payload_raises_rather_than_returning_nothing(sources, parse, source):
    with pytest.raises(sources.ParseError, match=source):
        getattr(sources, parse)(b"{not json")


def test_every_source_has_a_query_builder_and_a_parser(sources):
    assert set(sources.PARSERS) == {"arxiv", "semantic_scholar", "openalex", "crossref"}
    for build_url, parse in sources.PARSERS.values():
        assert callable(build_url) and callable(parse)


@pytest.mark.parametrize("name", ["arxiv", "semantic_scholar", "openalex", "crossref"])
def test_a_query_with_spaces_and_symbols_is_url_encoded(sources, name):
    build_url, _ = sources.PARSERS[name]
    url = build_url("shared mental models & teams", limit=10)

    assert " " not in url and "&teams" not in url
    assert url.startswith("http")


# --- dedupe: the part that decides how big the corpus is -----------------

def test_records_sharing_a_doi_collapse(sources):
    merged = sources.dedupe([
        sources.Candidate(title="Team cognition", year=2001, external_ids={"doi": "10.1/y"},
                          sources=["crossref"]),
        sources.Candidate(title="Team Cognition", year=2001, external_ids={"doi": "10.1/Y"},
                          sources=["openalex"]),
    ])

    assert len(merged) == 1
    assert merged[0].sources == ["crossref", "openalex"], "the merged record remembers both indexes"


def test_a_bridging_record_joins_a_doi_only_and_an_arxiv_only_row(sources):
    """Single-pass keying misses this, and four disagreeing indexes produce it constantly."""
    merged = sources.dedupe([
        sources.Candidate(title="T", year=2024, external_ids={"doi": "10.1/x"}, sources=["crossref"]),
        sources.Candidate(title="T", year=2024, external_ids={"arxiv": "2401.1"}, sources=["arxiv"]),
        sources.Candidate(title="T", year=2024, external_ids={"doi": "10.1/x", "arxiv": "2401.1"},
                          sources=["semantic_scholar"]),
    ])

    assert len(merged) == 1
    assert merged[0].external_ids == {"doi": "10.1/x", "arxiv": "2401.1"}


def test_records_sharing_an_openalex_id_collapse_even_with_no_doi_and_no_year(sources):
    """`artifact_id` treats a work id as an identity, so dedupe has to as well.

    OpenAlex's `doi` and `publication_year` are both nullable. When they are null,
    keying on DOI and title+year alone leaves two rows pointing at one file on
    disk — and every snowball round rediscovers the same work, so the corpus can
    never saturate.
    """
    merged = sources.dedupe([
        sources.Candidate(title="Seed", external_ids={"openalex": "W1"}, sources=["openalex"]),
        sources.Candidate(title="Seed", external_ids={"openalex": "W1"}, sources=["openalex"]),
    ])

    assert len(merged) == 1


def test_an_openalex_id_bridges_to_a_doi_only_record(sources):
    merged = sources.dedupe([
        sources.Candidate(title="T", external_ids={"openalex": "W1"}),
        sources.Candidate(title="T", external_ids={"doi": "10.1/x"}),
        sources.Candidate(title="T", external_ids={"openalex": "W1", "doi": "10.1/x"}),
    ])

    assert len(merged) == 1


def test_two_different_papers_sharing_a_title_are_not_merged(sources):
    """A shared title is common; merging on it deletes a paper no later stage can restore."""
    merged = sources.dedupe([
        sources.Candidate(title="Attention", year=2017, sources=["arxiv"]),
        sources.Candidate(title="Attention", year=1999, sources=["crossref"]),
    ])

    assert len(merged) == 2


def test_untitled_undated_records_are_never_collapsed_into_one(sources):
    merged = sources.dedupe([sources.Candidate(title="Note"), sources.Candidate(title="Note")])

    assert len(merged) == 2, "with no id and no year there is no evidence these are one work"


def test_a_merge_keeps_the_more_informative_field_not_the_earlier_one(sources):
    merged = sources.dedupe([
        sources.Candidate(title="T", year=2020, external_ids={"doi": "10.1/x"}, abstract="short",
                          citation_count=3, authors=["A"]),
        sources.Candidate(title="T", year=2020, external_ids={"doi": "10.1/x"},
                          abstract="a considerably longer abstract", citation_count=41,
                          authors=["A", "B"], pdf_url="https://x/p.pdf"),
    ])

    assert merged[0].abstract == "a considerably longer abstract"
    assert merged[0].citation_count == 41
    assert merged[0].authors == ["A", "B"]
    assert merged[0].pdf_url == "https://x/p.pdf"


def test_a_triage_decision_survives_a_merge(sources):
    """Re-running search must not reset the triage the user already did."""
    merged = sources.dedupe([
        sources.Candidate(title="T", year=2020, external_ids={"doi": "10.1/x"}, status="dropped",
                          drop_reason="wrong field"),
        sources.Candidate(title="T", year=2020, external_ids={"doi": "10.1/x"}),
    ])

    assert merged[0].status == "dropped"
    assert merged[0].drop_reason == "wrong field"


def test_dedupe_preserves_first_seen_order(sources):
    """search_sources.py slices `merged[known_count:]` to find what is new; order is load-bearing."""
    merged = sources.dedupe([
        sources.Candidate(title="first", year=2001, external_ids={"doi": "10.1/a"}),
        sources.Candidate(title="second", year=2002, external_ids={"doi": "10.1/b"}),
        sources.Candidate(title="first", year=2001, external_ids={"doi": "10.1/a"}),
        sources.Candidate(title="third", year=2003, external_ids={"doi": "10.1/c"}),
    ])

    assert [c.title for c in merged] == ["first", "second", "third"]
