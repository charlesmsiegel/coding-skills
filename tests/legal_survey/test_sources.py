"""Parsing and identity, over captured payload shapes.

The parsers are pure, so these are cheap. The dedupe tests are the ones that
matter: two records for one authority mean two files on disk, two notes, and two
rows in a corpus whose counts the masthead presents as checkable — and one record
for two authorities means a claim cited to the wrong case.
"""

import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "legal-survey" / "scripts"


@pytest.fixture
def sources(load_module):
    return load_module(SCRIPTS, "sources")


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS, "common")


def payload(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


# --- CourtListener ------------------------------------------------------

CL_RESULT = {
    "results": [{
        "cluster_id": 108713,
        "caseName": "Doe v. Agency",
        "court_id": "ca9",
        "dateFiled": "2019-04-02T00:00:00Z",
        "status": "Published",
        "citeCount": 41,
        "citation": ["901 F.3d 1099", "2018 WL 12345"],
        "absolute_url": "/opinion/108713/doe-v-agency/",
        "snippet": "the deliberative process privilege",
        "opinions": [{"id": 900001}, {"id": 900002}],
    }]
}


def test_courtlistener_yields_a_case_with_its_court_and_date(sources):
    (authority,) = sources.parse_courtlistener(payload(CL_RESULT))

    assert authority.authority_type == "case"
    assert authority.title == "Doe v. Agency"
    assert authority.court == "ca9"
    assert authority.decided == "2019-04-02"
    assert authority.precedential_status == "Published"


def test_courtlistener_keeps_the_opinion_ids_the_graph_filters_on(sources):
    """The cluster id names the document; only the opinion ids query the graph."""
    (authority,) = sources.parse_courtlistener(payload(CL_RESULT))

    assert authority.external_ids["cl"] == "108713"
    assert authority.external_ids["cl_opinion"] == "900001,900002"


def test_courtlistener_prefers_the_official_reporter_over_a_vendor_cite(sources):
    (authority,) = sources.parse_courtlistener(payload(CL_RESULT))

    assert authority.citation == "901 F.3d 1099"


def test_the_supreme_court_reporter_wins_over_its_parallels(sources):
    body = payload({"results": [{"cluster_id": 1, "caseName": "X v. Y",
                                 "citation": ["93 S. Ct. 705", "410 U.S. 113"],
                                 "absolute_url": "/opinion/1/x/"}]})

    (authority,) = sources.parse_courtlistener(body)

    assert authority.citation == "410 U.S. 113"


def test_courtlistener_builds_an_absolute_landing_url(sources):
    (authority,) = sources.parse_courtlistener(payload(CL_RESULT))

    assert authority.landing_url == "https://www.courtlistener.com/opinion/108713/doe-v-agency/"


def test_a_malformed_payload_raises_rather_than_returning_nothing(sources):
    """Zero results and an unparseable response must never look the same."""
    with pytest.raises(sources.ParseError):
        sources.parse_courtlistener(b"<html>502 Bad Gateway</html>")


# --- Federal Register ---------------------------------------------------

def test_the_federal_register_dates_a_document_by_its_publication(sources):
    body = payload({"results": [{
        "document_number": "2021-11234",
        "title": "Final Rule on Disclosure",
        "publication_date": "2021-06-01",
        "type": "Rule",
        "abstract": "The agency finalizes...",
        "html_url": "https://www.federalregister.gov/d/2021-11234",
        "pdf_url": "https://www.govinfo.gov/x.pdf",
        "agencies": [{"name": "Environmental Protection Agency"}],
    }]})

    (authority,) = sources.parse_federal_register(body)

    assert authority.authority_type == "regulation"
    assert authority.as_of == "2021-06-01", "an FR document is an archive, not current law"
    assert authority.external_ids["fr"] == "2021-11234"
    assert "Environmental Protection Agency" in authority.court


# --- eCFR ---------------------------------------------------------------

def test_ecfr_builds_a_cfr_citation_from_the_hierarchy(sources):
    body = payload({"results": [{
        "hierarchy": {"title": "40", "part": "60", "section": "60.1"},
        "headings": {"section": "Applicability"},
        "starts_on": "2024-01-01",
        "full_text_excerpt": "The provisions of this part apply...",
    }]})

    (authority,) = sources.parse_ecfr(body, as_of="2026-09-04")

    assert authority.citation == "40 CFR 60.1"
    assert authority.as_of == "2026-09-04", "the as-of the caller asked for wins"


def test_the_ecfr_text_url_carries_the_date_in_its_path(sources):
    url = sources.ecfr_full_text_url("2019-07-01", "40", "60.1")

    assert "/2019-07-01/title-40.xml" in url
    assert "section=60.1" in url


# --- secondary literature -----------------------------------------------

def test_openalex_abstracts_are_reinverted(sources):
    body = payload({"results": [{
        "id": "https://openalex.org/W123",
        "title": "The Deliberative Process Privilege",
        "publication_year": 2022,
        "abstract_inverted_index": {"Exemption": [0], "5": [1], "is": [2], "narrow": [3]},
        "authorships": [{"author": {"display_name": "A. Scholar"}}],
        "primary_location": {"source": {"display_name": "Yale L.J."}},
    }]})

    (authority,) = sources.parse_openalex(body)

    assert authority.summary == "Exemption 5 is narrow"
    assert authority.authority_type == "secondary"
    assert authority.venue == "Yale L.J."


def test_crossref_titles_and_venues_arrive_as_lists(sources):
    body = payload({"message": {"items": [{
        "title": ["Rethinking Exemption 5"],
        "container-title": ["Harvard Law Review"],
        "issued": {"date-parts": [[2023, 4]]},
        "DOI": "10.2307/1073947",
        "author": [{"given": "B.", "family": "Author"}],
        "URL": "https://doi.org/10.2307/1073947",
    }]}})

    (authority,) = sources.parse_crossref(body)

    assert authority.title == "Rethinking Exemption 5"
    assert authority.venue == "Harvard Law Review"
    assert authority.decided == "2023"


# --- legislation.gov.uk -------------------------------------------------

UK_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:leg="http://www.legislation.gov.uk/namespaces/legislation">
  <entry>
    <id>http://www.legislation.gov.uk/id/ukpga/2000/36</id>
    <title>Freedom of Information Act 2000</title>
    <updated>2024-02-01T00:00:00Z</updated>
    <link href="http://www.legislation.gov.uk/ukpga/2000/36"/>
    <summary>An Act to make provision for the disclosure of information.</summary>
    <leg:year Value="2000"/>
    <leg:number Value="36"/>
  </entry>
</feed>"""


def test_the_uk_feed_yields_a_statute_with_a_chapter_citation(sources):
    (authority,) = sources.parse_legislation_uk(UK_FEED.encode("utf-8"))

    assert authority.authority_type == "statute"
    assert authority.citation == "2000 c.36"
    assert authority.jurisdiction == "uk"
    assert authority.as_of == "2024-02-01", "the feed timestamp is what we can honestly claim"


def test_the_uk_source_is_off_by_default(sources):
    """US-first is the skill's declared scope; the others are opt-in."""
    assert "legislation_uk" in sources.PARSERS
    assert "legislation_uk" not in sources.DEFAULT_SOURCES
    assert "legislation_uk" in sources.OPTIONAL_SOURCES


# --- identity and dedupe ------------------------------------------------

def test_two_renderings_of_one_citation_collapse(common, sources):
    a = common.Authority(title="Roe v. Wade", citation="410 U.S. 113")
    b = common.Authority(title="Roe v. Wade", citation="410 US 113", external_ids={"cl": "9"})

    (merged,) = sources.dedupe([a, b])

    assert merged.external_ids["cl"] == "9"


def test_a_bridging_record_joins_two_that_share_nothing(common, sources):
    """Union-find: a record carrying both ids links a cite-only row to a cl-only one."""
    cite_only = common.Authority(title="Doe v. Agency", citation="901 F.3d 1099")
    both = common.Authority(title="Doe v. Agency", citation="901 F.3d 1099",
                            external_ids={"cl": "108713"})
    cl_only = common.Authority(title="Doe v. Agency (slip op.)", external_ids={"cl": "108713"})

    assert len(sources.dedupe([cite_only, both, cl_only])) == 1


def test_two_cases_sharing_a_name_are_not_merged(common, sources):
    """'United States v. Johnson' names hundreds of unrelated prosecutions."""
    a = common.Authority(title="United States v. Johnson", decided="2019",
                         external_ids={"cl": "1"})
    b = common.Authority(title="United States v. Johnson", decided="2019",
                         external_ids={"cl": "2"})

    assert len(sources.dedupe([a, b])) == 2


def test_two_secondary_works_sharing_a_title_and_year_do_merge(common, sources):
    """A law review article is identified by its title in a way a case is not."""
    a = common.Authority(title="Rethinking Exemption 5", authority_type="secondary",
                         decided="2023")
    b = common.Authority(title="Rethinking Exemption 5", authority_type="secondary",
                         decided="2023", external_ids={"doi": "10.2307/1"})

    assert len(sources.dedupe([a, b])) == 1


def test_a_case_merged_with_a_commentary_record_stays_a_case(common, sources):
    commentary = common.Authority(title="Doe v. Agency", authority_type="secondary",
                                  citation="901 F.3d 1099")
    opinion = common.Authority(title="Doe v. Agency", authority_type="case",
                               citation="901 F.3d 1099")

    (merged,) = sources.dedupe([commentary, opinion])

    assert merged.authority_type == "case"


def test_selected_beats_dropped_when_two_rows_disagree(common, sources):
    """Triage is hand-edited, so opposite verdicts on one authority are routine."""
    dropped = common.Authority(title="Doe v. Agency", citation="901 F.3d 1099",
                               status="dropped", drop_reason="duplicate of the F.3d opinion")
    selected = common.Authority(title="Doe v. Agency", citation="901 F.3d 1099",
                                status="selected", weight="binding",
                                weight_reason="9th Cir.; forum is N.D. Cal.")

    (merged,) = sources.dedupe([dropped, selected])

    assert merged.status == "selected"
    assert merged.drop_reason == "", "a selected authority carrying a drop_reason cannot exist"


def test_a_merge_keeps_a_weight_with_its_own_reason(common, sources):
    """Weight and weight_reason must travel together or the invariant rejects the merge."""
    weighed = common.Authority(title="Doe", citation="901 F.3d 1099", status="selected",
                               weight="persuasive", weight_reason="2d Cir.; no 9th Cir. law")
    bare = common.Authority(title="Doe v. Agency", citation="901 F.3d 1099")

    (merged,) = sources.dedupe([bare, weighed])

    assert merged.weight == "persuasive"
    assert merged.weight_reason == "2d Cir.; no 9th Cir. law"


def test_dedupe_keeps_the_richer_field_not_the_earlier_one(common, sources):
    short = common.Authority(title="Doe", citation="901 F.3d 1099")
    long = common.Authority(title="Doe v. Agency of Something", citation="901 F.3d 1099")

    (merged,) = sources.dedupe([short, long])

    assert merged.title == "Doe v. Agency of Something"


def test_records_with_no_identity_at_all_stay_separate(common, sources):
    a = common.Authority(title="", authority_type="secondary")
    b = common.Authority(title="", authority_type="secondary")

    assert len(sources.dedupe([a, b])) == 2


def test_every_default_source_has_a_parser(sources):
    for name in sources.DEFAULT_SOURCES:
        assert name in sources.PARSERS


def test_the_retired_caselaw_access_project_api_is_not_a_source(sources):
    """Its API shut down in 2024; a source that cannot answer reads as 'no law here'."""
    assert not any("case.law" in name or name == "cap" for name in sources.PARSERS)
