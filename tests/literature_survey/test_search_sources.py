"""Discovery: what happens when the network, not the literature, is empty.

The distinction this stage has to hold is between "three of four indexes
answered" (a caveat) and "nothing answered" (an error). Getting it wrong renders
a dead network as a field with no publications in it, which is the most
expensive kind of wrong a survey can be.
"""

import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "literature-survey" / "scripts"

ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <title>Transactive memory systems</title>
    <summary>Abstract.</summary>
    <published>2024-01-02T00:00:00Z</published>
    <link type="application/pdf" href="http://arxiv.org/pdf/2401.00001v1"/>
  </entry>
</feed>
""".encode()

S2_PAYLOAD = b'{"data": [{"paperId": "abc", "title": "Shared mental models", "year": 2019}]}'
OPENALEX_PAYLOAD = (b'{"results": [{"id": "https://openalex.org/W1", "title": "Team cognition",'
                    b' "publication_year": 2001}]}')
CROSSREF_PAYLOAD = b'{"message": {"items": [{"DOI": "10.1/y", "title": ["Team cognition"]}]}}'

BY_HOST = {
    "export.arxiv.org": ARXIV_FEED,
    "api.semanticscholar.org": S2_PAYLOAD,
    "api.openalex.org": OPENALEX_PAYLOAD,
    "api.crossref.org": CROSSREF_PAYLOAD,
}


@pytest.fixture
def search(load_module):
    return load_module(SCRIPTS, "search_sources")


class FakeHttp:
    """Answers by host, so a single source can be made to fail on its own."""

    def __init__(self, dead=()):
        self.dead = set(dead)
        self.urls: list[str] = []

    def get(self, url, accept="*/*"):
        self.urls.append(url)
        host = url.split("/")[2]
        if host in self.dead:
            raise OSError("connection refused")
        return type("R", (), {"body": BY_HOST[host], "content_type": ""})()


def read_candidates(out: Path) -> dict:
    return json.loads((out / "candidates.json").read_text(encoding="utf-8"))


def test_a_search_records_every_source_it_reached(search, tmp_path):
    result = search.run("team knowledge", [], tmp_path, FakeHttp())

    assert result["searched"] == 4
    payload = read_candidates(tmp_path)
    assert payload["sources_searched"] == ["arxiv", "crossref", "openalex", "semantic_scholar"]
    assert payload["sources_failed"] == []
    assert payload["topic"] == "team knowledge"


def test_one_dead_source_is_a_caveat_not_a_crash(search, tmp_path):
    """Semantic Scholar rate-limits without a key; three indexes are still a corpus."""
    result = search.run("team knowledge", [], tmp_path, FakeHttp(dead=["api.semanticscholar.org"]))

    assert result["searched"] == 3
    assert any("semantic_scholar failed" in c for c in result["caveats"])
    assert read_candidates(tmp_path)["sources_failed"] == ["semantic_scholar"]


class FlakyArxiv(FakeHttp):
    """arXiv answers the topic query and times out on the subtopic."""

    def __init__(self):
        super().__init__()
        self.arxiv_calls = 0

    def get(self, url, accept="*/*"):
        if url.split("/")[2] == "export.arxiv.org":
            self.arxiv_calls += 1
            if self.arxiv_calls == 2:
                raise OSError("timed out")
        return super().get(url, accept)


def test_a_source_that_answered_only_some_queries_is_not_reported_as_complete(search, tmp_path):
    """It used to count as searched *and* be listed as failed, so the headline claimed
    4 of 4 sources over a file that said arXiv was never searched."""
    result = search.run("team knowledge", ["transactive memory"], tmp_path, FlakyArxiv())

    assert result["searched"] == 4
    assert result["complete"] == 3
    assert any("arxiv answered 1 of 2 queries" in c for c in result["caveats"])


def test_a_partly_failed_source_is_still_listed_as_searched(search, tmp_path):
    """Its results are in the corpus; pretending otherwise misstates provenance."""
    search.run("team knowledge", ["transactive memory"], tmp_path, FlakyArxiv())

    payload = read_candidates(tmp_path)
    assert "arxiv" in payload["sources_searched"]
    assert payload["sources_failed"] == []
    assert payload["sources_partial"] == {"arxiv": 1}
    assert payload["queries_per_source"] == 2
    assert payload["query_failures"]["arxiv"][0].startswith("transactive memory: ")


def test_the_cli_headline_says_how_many_sources_answered_everything(search, tmp_path,
                                                                    monkeypatch, capsys):
    monkeypatch.setattr(search, "Http", FlakyArxiv)
    monkeypatch.setattr("sys.argv", ["search_sources.py", "--topic", "team knowledge",
                                     "--subtopic", "transactive memory", "--out", str(tmp_path)])

    search.main()

    assert "from 4 of 4 sources (3 answered every query)" in capsys.readouterr().out


def test_every_source_failing_is_an_error_not_an_empty_literature(search, tmp_path):
    with pytest.raises(SystemExit, match="connectivity problem"):
        search.run("team knowledge", [], tmp_path, FakeHttp(dead=BY_HOST))


def test_a_failing_source_is_not_listed_as_searched(search, tmp_path):
    search.run("team knowledge", [], tmp_path, FakeHttp(dead=["api.crossref.org"]))

    assert "crossref" not in read_candidates(tmp_path)["sources_searched"]


def test_every_subtopic_is_queried_against_every_source(search, tmp_path):
    http = FakeHttp()

    search.run("team knowledge", ["transactive memory", "shared mental models"], tmp_path, http)

    assert len(http.urls) == 4 * 3
    assert read_candidates(tmp_path)["subtopics"] == ["transactive memory", "shared mental models"]


def test_a_rerun_preserves_triage_rather_than_overwriting_it(search, tmp_path):
    """Otherwise the wide-triage stage — the expensive judgment — is redone on every search."""
    search.run("team knowledge", [], tmp_path, FakeHttp())
    payload = read_candidates(tmp_path)
    for candidate in payload["candidates"]:
        candidate["status"] = "dropped"
        candidate["drop_reason"] = "wrong discipline"
    (tmp_path / "candidates.json").write_text(json.dumps(payload), encoding="utf-8")

    search.run("team knowledge", [], tmp_path, FakeHttp())

    after = read_candidates(tmp_path)["candidates"]
    assert after, "the re-run must not empty the file"
    assert {c["status"] for c in after} == {"dropped"}
    assert {c["drop_reason"] for c in after} == {"wrong discipline"}


def test_a_rerun_does_not_duplicate_what_it_already_found(search, tmp_path):
    search.run("team knowledge", [], tmp_path, FakeHttp())
    first = len(read_candidates(tmp_path)["candidates"])

    search.run("team knowledge", [], tmp_path, FakeHttp())

    assert len(read_candidates(tmp_path)["candidates"]) == first


def test_every_new_candidate_starts_untriaged(search, tmp_path):
    search.run("team knowledge", [], tmp_path, FakeHttp())

    assert {c["status"] for c in read_candidates(tmp_path)["candidates"]} == {"new"}


def test_the_cli_leads_with_a_headline_and_demands_triage(search, tmp_path, monkeypatch, capsys):
    """The agent reads the headline first and sometimes only, so the CLI has to say both
    how much of the field answered and that nothing is selected yet."""
    monkeypatch.setattr(search, "Http", FakeHttp)
    monkeypatch.setattr("sys.argv", ["search_sources.py", "--topic", "team knowledge",
                                     "--out", str(tmp_path)])

    assert search.main() == 0

    out = capsys.readouterr().out
    assert "distinct candidates from 4 of 4 sources" in out.splitlines()[0]
    assert "Triage must set selected or dropped" in out
