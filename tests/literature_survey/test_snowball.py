"""Stopping, and being honest about why you stopped.

"Saturated" and "we ran out of budget" produce the same-sized corpus and mean
opposite things about coverage. The verdict is the only thing that tells them
apart downstream, so the verdicts are what these tests pin — along with the
refusal to snowball from nothing read, which would otherwise bank a barren round
the first time someone runs the stages out of order.
"""

import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "literature-survey" / "scripts"


@pytest.fixture
def snowball(load_module):
    return load_module(SCRIPTS, "snowball")


def work(oa_id: str, title: str, year: int = 2020) -> dict:
    return {"id": "https://openalex.org/" + oa_id, "title": title, "publication_year": year}


class FakeHttp:
    """Answers OpenAlex graph queries from a canned neighbour map."""

    def __init__(self, neighbours=None, fail=()):
        self.neighbours = neighbours or {}
        self.fail = set(fail)
        self.urls: list[str] = []

    def __call_meta(self, results):
        return {"count": self.total if self.total is not None else len(results)}

    total = None

    def get(self, url, accept="*/*"):
        self.urls.append(url)
        if any(token in url for token in self.fail):
            raise OSError("openalex is down")
        results = []
        for token, works in self.neighbours.items():
            if token in url:
                results = works
        payload = {"meta": self.__call_meta(results), "results": results}
        return type("R", (), {"body": json.dumps(payload).encode(), "content_type": ""})()


def survey(out: Path, read_ids=("W1",), candidates=None) -> None:
    notes = out / "docs" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    for artifact in read_ids:
        (notes / (artifact + ".json")).write_text('{"artifact_id": "' + artifact + '"}',
                                                  encoding="utf-8")
    rows = candidates if candidates is not None else [
        {"title": "Seed", "external_ids": {"openalex": artifact}, "status": "selected"}
        for artifact in read_ids
    ]
    (out / "candidates.json").write_text(json.dumps({"candidates": rows}), encoding="utf-8")


def state(out: Path) -> dict:
    return json.loads((out / "snowball.json").read_text(encoding="utf-8"))


def test_a_round_that_finds_new_work_is_productive(snowball, tmp_path):
    survey(tmp_path)
    http = FakeHttp({"W1": [work("W2", "A citer"), work("W3", "A reference")]})

    result = snowball.run(tmp_path, http, round_number=1)

    assert result["verdict"] == "productive"
    assert len(result["new_candidates"]) == 2
    titles = [c["title"] for c in json.loads(
        (tmp_path / "candidates.json").read_text(encoding="utf-8"))["candidates"]]
    assert titles == ["Seed", "A citer", "A reference"]


def test_both_directions_of_the_citation_graph_are_walked(snowball, tmp_path):
    survey(tmp_path)
    http = FakeHttp({"W1": []})

    snowball.run(tmp_path, http, round_number=1)

    assert any("cited_by:W1" in url for url in http.urls)
    assert any("cites:W1" in url for url in http.urls)


def test_work_already_known_does_not_count_as_new(snowball, tmp_path):
    survey(tmp_path)
    http = FakeHttp({"W1": [work("W1", "Seed")]})

    result = snowball.run(tmp_path, http, round_number=1)

    assert result["new_candidates"] == []
    assert result["verdict"] == "barren"


def test_two_barren_rounds_in_a_row_are_saturation(snowball, tmp_path):
    survey(tmp_path)
    http = FakeHttp({"W1": []})

    assert snowball.run(tmp_path, http, round_number=1)["verdict"] == "barren"
    assert snowball.run(tmp_path, http, round_number=2)["verdict"] == "saturated"
    assert state(tmp_path)["stopped_because"] == "saturated"


def test_a_productive_round_resets_the_barren_count(snowball, tmp_path):
    """Otherwise one lull plus one lull two rounds later reads as an exhausted literature."""
    survey(tmp_path)
    snowball.run(tmp_path, FakeHttp({"W1": []}), round_number=1)
    snowball.run(tmp_path, FakeHttp({"W1": [work("W9", "Late find")]}), round_number=2)

    assert snowball.run(tmp_path, FakeHttp({"W1": []}), round_number=3)["verdict"] == "barren"
    assert state(tmp_path)["barren_rounds"] == 1


def test_the_cap_is_never_reported_as_saturation(snowball, tmp_path):
    """One means the literature is exhausted; the other means we stopped looking."""
    survey(tmp_path, candidates=[
        {"title": "Seed", "external_ids": {"openalex": "W1"}, "status": "selected"},
        {"title": "Other", "external_ids": {"openalex": "W8"}, "status": "new"},
    ])
    http = FakeHttp({"W1": [work("W2", "A citer")]})

    result = snowball.run(tmp_path, http, round_number=1, cap=2)

    assert result["verdict"] == "cap_reached"
    assert result["new_candidates"] == [], "nothing is banked from a round the cap truncated"
    assert state(tmp_path)["stopped_because"] == "cap_reached"
    assert any("bounded by budget" in c for c in result["caveats"])


def test_a_neighbour_list_longer_than_one_page_is_declared_truncated(snowball, tmp_path):
    """A heavily-cited paper has thousands of citers and the walk fetches one page.
    Taking the first hundred quietly is the silent truncation the cap rule forbids
    one level up — and it would let a round report 'barren' over an unfinished graph."""
    survey(tmp_path)
    http = FakeHttp({"W1": [work("W2", "A citer")]})
    http.total = 4200

    result = snowball.run(tmp_path, http, round_number=1)

    assert any("only the first 100 of 4200 neighbours" in c for c in result["caveats"])
    assert any("citers" in c for c in result["caveats"])
    assert any("references" in c for c in result["caveats"])


def test_a_neighbour_list_that_fits_in_one_page_says_nothing(snowball, tmp_path):
    survey(tmp_path)

    result = snowball.run(tmp_path, FakeHttp({"W1": [work("W2", "A citer")]}), round_number=1)

    assert not any("bounded by page size" in c for c in result["caveats"])


def test_an_unresolvable_artifact_is_a_caveat_about_coverage(snowball, tmp_path):
    """A note whose id OpenAlex will not filter on leaves a hole this round cannot see."""
    survey(tmp_path, read_ids=("t0123456789",), candidates=[
        {"title": "Seed", "external_ids": {}, "status": "selected"}])

    result = snowball.run(tmp_path, FakeHttp(), round_number=1)

    assert result["unresolved"] == 1
    assert any("coverage is incomplete" in c for c in result["caveats"])
    assert state(tmp_path)["rounds"][0]["unresolved"] == 1


def test_a_doi_is_resolved_to_a_work_id_before_the_graph_is_walked(snowball, tmp_path):
    survey(tmp_path, read_ids=("10.1_x",), candidates=[
        {"title": "Seed", "external_ids": {"doi": "10.1/x"}, "status": "selected"}])

    class Resolving(FakeHttp):
        def get(self, url, accept="*/*"):
            if url.endswith("/https://doi.org/10.1/x"):
                self.urls.append(url)
                return type("R", (), {"body": b'{"id": "https://openalex.org/W7"}',
                                      "content_type": ""})()
            return super().get(url, accept)

    http = Resolving({"W7": [work("W8", "A citer")]})
    result = snowball.run(tmp_path, http, round_number=1)

    assert result["unresolved"] == 0
    assert result["verdict"] == "productive"


def test_a_failed_doi_lookup_is_a_gap_rather_than_a_guessed_work_id(snowball, tmp_path):
    """A wrong work id returns somebody else's citation graph — worse than a recorded gap."""
    survey(tmp_path, read_ids=("10.1_x",), candidates=[
        {"title": "Seed", "external_ids": {"doi": "10.1/x"}, "status": "selected"}])

    result = snowball.run(tmp_path, FakeHttp(fail=["doi.org"]), round_number=1)

    assert result["unresolved"] == 1


def test_one_dead_neighbour_query_does_not_kill_the_round(snowball, tmp_path):
    survey(tmp_path)

    result = snowball.run(tmp_path, FakeHttp({"W1": []}, fail=["cites:W1"]), round_number=1)

    assert any("partly unavailable" in c for c in result["caveats"])
    assert result["verdict"] == "barren"


def test_snowballing_before_anything_is_read_refuses_to_bank_a_barren_round(snowball, tmp_path):
    (tmp_path / "candidates.json").write_text('{"candidates": []}', encoding="utf-8")

    with pytest.raises(SystemExit, match="snowballing from nothing read"):
        snowball.run(tmp_path, FakeHttp(), round_number=1)

    assert not (tmp_path / "snowball.json").exists()


def test_every_round_is_recorded_for_the_masthead(snowball, tmp_path):
    survey(tmp_path)
    snowball.run(tmp_path, FakeHttp({"W1": [work("W2", "A citer")]}), round_number=1)
    snowball.run(tmp_path, FakeHttp({"W1": []}), round_number=2)

    rounds = state(tmp_path)["rounds"]
    assert [r["round"] for r in rounds] == [1, 2]
    assert [r["verdict"] for r in rounds] == ["productive", "barren"]
    assert rounds[0]["new_candidates"] == 1 and rounds[0]["read_at_start"] == 1
