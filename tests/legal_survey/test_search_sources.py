"""Discovery: what happens when the network, not the law, is empty.

The distinction this stage has to hold is between "four of five sources answered"
(a caveat) and "nothing answered" (an error). Getting it wrong renders a dead
network as a question with no law on it, which is the most expensive kind of
wrong a legal survey can be — "no authority is on point" is an answer somebody
will act on.
"""

import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "legal-survey" / "scripts"

CL_PAYLOAD = json.dumps({"results": [{
    "cluster_id": 1, "caseName": "Doe v. Agency", "court_id": "ca9",
    "dateFiled": "2019-04-02T00:00:00Z", "citation": ["901 F.3d 1099"],
    "absolute_url": "/opinion/1/doe/", "opinions": [{"id": 90}],
}]}).encode()

FR_PAYLOAD = json.dumps({"results": [{
    "document_number": "2021-1", "title": "Final Rule", "publication_date": "2021-06-01",
    "html_url": "https://www.federalregister.gov/d/2021-1",
}]}).encode()

ECFR_PAYLOAD = json.dumps({"results": [{
    "hierarchy": {"title": "40", "section": "60.1"}, "headings": {"section": "Applicability"},
    "starts_on": "2024-01-01",
}]}).encode()

OPENALEX_PAYLOAD = json.dumps({"results": [{
    "id": "https://openalex.org/W1", "title": "Exemption 5", "publication_year": 2022,
}]}).encode()

CROSSREF_PAYLOAD = json.dumps({"message": {"items": [{
    "DOI": "10.1/y", "title": ["Rethinking Exemption 5"], "issued": {"date-parts": [[2023]]},
}]}}).encode()

BY_HOST = {
    "www.courtlistener.com": CL_PAYLOAD,
    "www.federalregister.gov": FR_PAYLOAD,
    "www.ecfr.gov": ECFR_PAYLOAD,
    "api.openalex.org": OPENALEX_PAYLOAD,
    "api.crossref.org": CROSSREF_PAYLOAD,
}


@pytest.fixture
def search(load_module):
    return load_module(SCRIPTS, "search_sources")


class FakeHttp:
    """Answers by host, so a single source can be made to fail on its own."""

    def __init__(self, dead=(), flaky=()):
        self.dead = set(dead)
        self.flaky = set(flaky)
        self.urls: list[str] = []

    def get(self, url, accept="*/*"):
        self.urls.append(url)
        host = url.split("/")[2]
        if host in self.dead:
            raise OSError("connection refused")
        if host in self.flaky and len([u for u in self.urls if host in u]) > 1:
            raise OSError("timed out")
        return type("R", (), {"body": BY_HOST[host], "content_type": ""})()


def read(out) -> dict:
    return json.loads((out / "candidates.json").read_text(encoding="utf-8"))


def test_a_full_run_records_every_source_and_the_question(search, tmp_path):
    result = search.run("exemption 5", [], tmp_path, FakeHttp(), forum="Ninth Circuit",
                        as_of="2026-09-04")

    payload = read(tmp_path)
    assert result["searched"] == 5
    assert payload["forum"] == "Ninth Circuit"
    assert payload["as_of"] == "2026-09-04"
    assert payload["question"] == "exemption 5"


def test_ecfr_fetch_url_uses_the_requested_historical_date(search, tmp_path):
    http = FakeHttp()
    search.run("applicability", [], tmp_path, http, as_of="2019-01-01",
               source_names=["ecfr"])

    [authority] = read(tmp_path)["authorities"]
    assert authority["as_of"] == "2019-01-01"
    assert "/2019-01-01/" in authority["landing_url"]
    assert "date=2019-01-01" in http.urls[0]


def test_one_dead_source_is_a_caveat_not_a_crash(search, tmp_path):
    result = search.run("exemption 5", [], tmp_path, FakeHttp(dead={"api.crossref.org"}))

    assert result["searched"] == 4
    assert any("crossref failed on every query" in c for c in result["caveats"])
    assert read(tmp_path)["sources_failed"] == ["crossref"]


def test_every_source_failing_is_an_error(search, tmp_path):
    """An unreachable network must never render as an absence of law."""
    with pytest.raises(SystemExit, match="connectivity problem rather than an absence of law"):
        search.run("exemption 5", [], tmp_path, FakeHttp(dead=set(BY_HOST)))


def test_a_partly_answered_source_is_not_counted_as_complete(search, tmp_path):
    """'5 of 5 sources' over half-run queries is the coverage claim the report inherits."""
    result = search.run("exemption 5", ["adoption", "waiver"], tmp_path,
                        FakeHttp(flaky={"www.ecfr.gov"}))

    assert result["searched"] == 5
    assert result["complete"] == 4
    assert any("its coverage of this question is partial" in c for c in result["caveats"])
    assert read(tmp_path)["sources_partial"] == {"ecfr": 2}


def test_every_issue_is_searched_separately_in_every_source(search, tmp_path):
    http = FakeHttp()

    search.run("exemption 5", ["adoption", "waiver"], tmp_path, http)

    assert len(http.urls) == 15, "5 sources x 3 queries"


def test_a_rerun_preserves_triage_decisions(search, tmp_path):
    """Otherwise the wide-triage stage is redone on every search."""
    search.run("exemption 5", [], tmp_path, FakeHttp())
    payload = read(tmp_path)
    payload["authorities"][0]["status"] = "dropped"
    payload["authorities"][0]["drop_reason"] = "wrong jurisdiction"
    (tmp_path / "candidates.json").write_text(json.dumps(payload), encoding="utf-8")

    search.run("exemption 5", [], tmp_path, FakeHttp())

    after = read(tmp_path)
    assert after["authorities"][0]["status"] == "dropped"
    assert after["authorities"][0]["drop_reason"] == "wrong jurisdiction"


def test_a_rerun_does_not_duplicate_authorities(search, tmp_path):
    search.run("exemption 5", [], tmp_path, FakeHttp())
    first = len(read(tmp_path)["authorities"])

    search.run("exemption 5", [], tmp_path, FakeHttp())

    assert len(read(tmp_path)["authorities"]) == first


def test_the_source_set_can_be_restricted(search, tmp_path):
    http = FakeHttp()

    result = search.run("exemption 5", [], tmp_path, http, source_names=["courtlistener"])

    assert result["requested"] == 1
    assert all("courtlistener.com" in url for url in http.urls)


def test_an_unknown_source_is_named_rather_than_ignored(search, tmp_path):
    with pytest.raises(SystemExit, match="no such source"):
        search.run("exemption 5", [], tmp_path, FakeHttp(), source_names=["westlaw"])


def test_everything_arrives_unjudged(search, tmp_path):
    """Triage is a human decision; nothing upstream is allowed to pre-empt it."""
    search.run("exemption 5", [], tmp_path, FakeHttp())

    assert all(a["status"] == "new" for a in read(tmp_path)["authorities"])
    assert all(not a["weight"] for a in read(tmp_path)["authorities"])
