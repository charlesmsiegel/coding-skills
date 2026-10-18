"""Following the citation graph, and refusing to call a budget a conclusion.

The verdict is the whole output. "saturated" is a claim about the law; "cap_reached"
is a claim about how long we were willing to look. A run that conflates them tells a
reader there is no contrary authority when nobody finished checking.
"""

import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "legal-survey" / "scripts"


@pytest.fixture
def snowball(load_module):
    return load_module(SCRIPTS, "snowball")


class FakeHttp:
    """Answers each neighbour URL from a script; anything unscripted is a dead link."""

    def __init__(self, bodies=None):
        self.bodies = bodies or {}
        self.urls: list[str] = []

    def get(self, url, accept="*/*"):
        self.urls.append(url)
        for fragment, body in self.bodies.items():
            if fragment in url:
                return type("R", (), {"body": body, "content_type": ""})()
        raise OSError("no route to host")

    def get_json(self, url):
        return json.loads(self.get(url).body.decode("utf-8"))


def cl_body(count=1, cluster=2, name="Later v. Agency", cite="950 F.3d 1"):
    return json.dumps({"count": count, "results": [{
        "cluster_id": cluster, "caseName": name, "court_id": "ca9",
        "dateFiled": "2021-01-01T00:00:00Z", "citation": [cite],
        "absolute_url": "/opinion/" + str(cluster) + "/later/",
        "opinions": [{"id": cluster * 10}],
    }]}).encode()


EMPTY_CL = json.dumps({"count": 0, "results": []}).encode()


def case(**kwargs) -> dict:
    row = {
        "title": "Doe v. Agency", "authority_type": "case",
        "external_ids": {"cl": "1", "cl_opinion": "90"},
        "citation": "901 F.3d 1099", "court": "ca9", "jurisdiction": "ca9",
        "decided": "2019-04-02", "precedential_status": "Published", "authors": [],
        "venue": "", "citation_count": 1, "summary": "", "as_of": "",
        "pdf_url": "", "landing_url": "https://x/", "sources": ["courtlistener"],
        "status": "selected", "drop_reason": "", "weight": "binding",
        "weight_reason": "9th Cir.; forum is N.D. Cal.",
    }
    row.update(kwargs)
    return row


def setup(out: Path, rows, notes=("901f3d1099",)) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "candidates.json").write_text(json.dumps({"authorities": rows}), encoding="utf-8")
    notes_dir = out / "docs" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    for note in notes:
        (notes_dir / (note + ".json")).write_text(
            json.dumps({"artifact_id": note}), encoding="utf-8")


def state(out: Path) -> dict:
    return json.loads((out / "snowball.json").read_text(encoding="utf-8"))


def test_a_new_neighbour_makes_the_round_productive(snowball, tmp_path):
    setup(tmp_path, [case()])
    http = FakeHttp({"courtlistener.com": cl_body()})

    result = snowball.run(tmp_path, http, 1)

    assert result["verdict"] == "productive"
    assert len(result["new_authorities"]) == 1


def test_both_directions_of_the_case_graph_are_walked(snowball, tmp_path):
    setup(tmp_path, [case()])
    http = FakeHttp({"courtlistener.com": EMPTY_CL})

    snowball.run(tmp_path, http, 1)

    assert any("cited-by%3A(90)" in u for u in http.urls), "what this opinion relies on"
    assert any("cites%3A(90)" in u for u in http.urls), "what has relied on it"


def test_a_secondary_source_is_walked_through_openalex(snowball, tmp_path):
    setup(tmp_path, [case(authority_type="secondary", citation="",
                          external_ids={"openalex": "W1"})],
          notes=("openalex-W1",))
    http = FakeHttp({"api.openalex.org": json.dumps({"meta": {"count": 0},
                                                     "results": []}).encode()})

    snowball.run(tmp_path, http, 1)

    assert all("openalex.org" in u for u in http.urls)


def test_an_already_known_neighbour_does_not_count_as_new(snowball, tmp_path):
    known = case(title="Later v. Agency", citation="950 F.3d 1",
                 external_ids={"cl": "2", "cl_opinion": "20"}, status="new",
                 weight="", weight_reason="")
    setup(tmp_path, [case(), known])
    http = FakeHttp({"courtlistener.com": cl_body()})

    result = snowball.run(tmp_path, http, 1)

    assert result["verdict"] == "barren"


def test_two_barren_rounds_are_saturation(snowball, tmp_path):
    setup(tmp_path, [case()])
    http = FakeHttp({"courtlistener.com": EMPTY_CL})

    snowball.run(tmp_path, http, 1)
    result = snowball.run(tmp_path, http, 2)

    assert result["verdict"] == "saturated"
    assert state(tmp_path)["stopped_because"] == "saturated"


def test_the_cap_is_never_reported_as_saturation(snowball, tmp_path):
    """One means the authority is exhausted; the other means we stopped looking."""
    setup(tmp_path, [case()])
    http = FakeHttp({"courtlistener.com": cl_body()})

    result = snowball.run(tmp_path, http, 1, cap=1)

    assert result["verdict"] == "cap_reached"
    assert any("bounded by budget rather than by the law" in c for c in result["caveats"])
    assert state(tmp_path)["stopped_because"] == "cap_reached"


def test_a_truncated_neighbour_page_is_declared(snowball, tmp_path):
    """A 'barren' verdict over a graph that was never finished is a claim about budget."""
    setup(tmp_path, [case()])
    http = FakeHttp({"courtlistener.com": cl_body(count=4000)})

    result = snowball.run(tmp_path, http, 1)

    assert any("bounded by page size, not by the graph" in c for c in result["caveats"])


def test_an_unresolvable_authority_is_reported_rather_than_skipped(snowball, tmp_path):
    setup(tmp_path, [case(external_ids={"cl": "1"})])

    result = snowball.run(tmp_path, http := FakeHttp({}), 1)

    assert result["unresolved"] == 1
    assert any("no CourtListener opinion id" in c for c in result["caveats"])
    assert http.urls == []


def test_a_dead_neighbour_call_is_a_caveat_not_a_dead_round(snowball, tmp_path):
    setup(tmp_path, [case()])

    result = snowball.run(tmp_path, FakeHttp({}), 1)

    assert result["verdict"] == "barren"
    assert any("partly unavailable" in c for c in result["caveats"])


def test_a_note_naming_an_unknown_authority_is_reported(snowball, tmp_path):
    setup(tmp_path, [case()], notes=("901f3d1099", "999xyz1"))

    result = snowball.run(tmp_path, FakeHttp({"courtlistener.com": EMPTY_CL}), 1)

    assert result["unresolved"] == 1
    assert any("not in candidates.json" in c for c in result["caveats"])


def test_snowballing_before_anything_is_read_is_a_clear_stop(snowball, tmp_path):
    """It would look like a barren round rather than an unstarted one."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "candidates.json").write_text(json.dumps({"authorities": []}), encoding="utf-8")

    with pytest.raises(SystemExit, match="snowballing from nothing read"):
        snowball.run(tmp_path, FakeHttp(), 1)


def test_new_authorities_are_appended_unjudged(snowball, tmp_path):
    setup(tmp_path, [case()])

    snowball.run(tmp_path, FakeHttp({"courtlistener.com": cl_body()}), 1)

    rows = json.loads((tmp_path / "candidates.json").read_text(encoding="utf-8"))["authorities"]
    assert rows[-1]["status"] == "new", "re-entering triage is the point"
