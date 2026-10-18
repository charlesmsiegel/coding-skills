"""Validation: is it still good law, and what does this stage's answer actually mean.

The statuses are the point. `good` is a weak claim and has to stay weak; `unchecked`
must never drift into meaning `good`; and a case nothing has cited is unvalidated
rather than clean. Every test here is about keeping those three apart, because the
report's most consequential number is computed from them.
"""

import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "legal-survey" / "scripts"


@pytest.fixture
def treat(load_module):
    return load_module(SCRIPTS, "check_treatment")


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS, "common")


class FakeHttp:
    """Answers the citing-search and opinion-text endpoints from a script."""

    def __init__(self, citers=None, texts=None, dead=False):
        self.citers = citers if citers is not None else {"count": 0, "results": []}
        self.texts = texts or {}
        self.dead = dead
        self.urls: list[str] = []

    def get_json(self, url):
        self.urls.append(url)
        if self.dead:
            raise OSError("connection refused")
        if "/opinions/" in url:
            opinion_id = url.split("/opinions/")[1].split("/")[0]
            return self.texts.get(opinion_id, {})
        return self.citers


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


def setup(out: Path, rows: list[dict], archived=("901f3d1099",)) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "candidates.json").write_text(json.dumps({"authorities": rows}), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({"artifacts": [
        {"artifact_id": aid, "kind": "case", "url": "u", "status": "ok",
         "path": "docs/case/" + aid + ".html", "sha256": "h", "citation": "901 F.3d 1099"}
        for aid in archived
    ]}), encoding="utf-8")


def citers(*opinion_ids) -> dict:
    return {"count": len(opinion_ids),
            "results": [{"id": i, "opinions": [{"id": i}]} for i in opinion_ids]}


def read(out: Path) -> dict:
    return json.loads((out / "treatment.json").read_text(encoding="utf-8"))


# --- the signal scan ----------------------------------------------------

def test_an_overruling_is_found_near_the_citation(treat):
    pattern = treat.citation_pattern("901 F.3d 1099")
    text = "We today overrule Doe v. Agency, 901 F.3d 1099, and adopt the contrary rule."

    signals = treat.find_signals(text, pattern)

    assert [s["label"] for s in signals] == ["overruled"]
    assert signals[0]["severity"] == "negative"


def test_treatment_language_far_from_the_citation_is_ignored(treat):
    """Widening the window mostly imports the treatment of the next case discussed."""
    pattern = treat.citation_pattern("901 F.3d 1099")
    text = ("Doe v. Agency, 901 F.3d 1099, controls. " + "Filler sentence. " * 60
            + "We overrule Smith on other questions.")

    assert treat.find_signals(text, pattern) == []


def test_a_distinguishing_opinion_is_caution_not_negative(treat):
    pattern = treat.citation_pattern("901 F.3d 1099")
    text = "We distinguish Doe v. Agency, 901 F.3d 1099, on its facts."

    (signal,) = treat.find_signals(text, pattern)

    assert signal["severity"] == "caution"


def test_the_citation_pattern_survives_repunctuation(treat):
    pattern = treat.citation_pattern("410 U.S. 113")

    assert pattern.search("see 410 U. S. 113, 118")
    assert pattern.search("see 410 US 113")


def test_the_pattern_does_not_leap_between_unrelated_numbers(treat):
    pattern = treat.citation_pattern("410 U.S. 113")

    assert not pattern.search("410 defendants were charged under section 113 of the act")


def test_html_tags_do_not_hide_a_signal(treat):
    pattern = treat.citation_pattern("901 F.3d 1099")
    text = "<p>We <em>overrule</em> <i>Doe</i>, 901 F.3d 1099.</p>"

    assert treat.find_signals(text, pattern)


# --- statuses -----------------------------------------------------------

def test_a_clean_citing_opinion_yields_good(treat, tmp_path):
    setup(tmp_path, [case()])
    http = FakeHttp(citers(500), {"500": {"plain_text": "Following Doe, 901 F.3d 1099, we ..."}})

    result = treat.run(tmp_path, http)

    assert result["by_status"] == {"good": 1}
    assert read(tmp_path)["treatments"][0]["citing_checked"] == 1


def test_an_overruling_citer_yields_negative_with_its_signals(treat, tmp_path):
    setup(tmp_path, [case()])
    http = FakeHttp(citers(500),
                    {"500": {"plain_text": "We overrule Doe, 901 F.3d 1099, in part."}})

    treat.run(tmp_path, http)

    record = read(tmp_path)["treatments"][0]
    assert record["status"] == "negative"
    assert record["signals"], "a negative flag nobody can check is not a flag"


def test_a_case_nothing_has_cited_is_unchecked_not_good(treat, tmp_path):
    """An uncited authority has been ignored, not approved."""
    setup(tmp_path, [case()])

    treat.run(tmp_path, FakeHttp())

    record = read(tmp_path)["treatments"][0]
    assert record["status"] == "unchecked"
    assert "nothing has cited it" in record["reason"]


def test_an_unreachable_graph_is_unchecked_and_says_so(treat, tmp_path):
    """'Nothing cited it' and 'the graph was down' must not collapse into one status."""
    setup(tmp_path, [case()])

    treat.run(tmp_path, FakeHttp(dead=True))

    record = read(tmp_path)["treatments"][0]
    assert record["status"] == "unchecked"
    assert "unreachable" in record["reason"]


def test_a_case_with_no_opinion_id_cannot_be_validated(treat, tmp_path):
    setup(tmp_path, [case(external_ids={"cl": "1"})])

    treat.run(tmp_path, FakeHttp(citers(500)))

    record = read(tmp_path)["treatments"][0]
    assert record["status"] == "unchecked"
    assert "no CourtListener opinion id" in record["reason"]


def test_an_unparsable_citation_cannot_be_located_in_a_citer(treat, tmp_path):
    setup(tmp_path, [case(citation="slip op. no. 19-1234")], archived=("slipopno191234",))

    treat.run(tmp_path, FakeHttp(citers(500)))

    record = read(tmp_path)["treatments"][0]
    assert record["status"] == "unchecked"
    assert "no parsable reporter citation" in record["reason"]


def test_a_statute_is_not_applicable_rather_than_unchecked(treat, tmp_path):
    """Treatment is a property of case law; flagging a statute would be noise."""
    setup(tmp_path, [case(authority_type="statute", citation="5 U.S.C. 552")],
          archived=("5usc552",))

    treat.run(tmp_path, FakeHttp())

    assert read(tmp_path)["treatments"][0]["status"] == "not_applicable"


def test_an_opinion_stored_as_html_is_still_read(treat, tmp_path):
    """Trying only plain_text returns "" for most of the modern corpus, so every
    case would come back 'good' having been checked against nothing."""
    setup(tmp_path, [case()])
    http = FakeHttp(citers(500),
                    {"500": {"html_with_citations": "<p>We overrule Doe, 901 F.3d 1099.</p>"}})

    treat.run(tmp_path, http)

    assert read(tmp_path)["treatments"][0]["status"] == "negative"


@pytest.mark.parametrize("body", [{}, {"plain_text": "  "},
                                  {"html": "<p></p>"},
                                  {"plain_text": "A different opinion without the cited authority."}])
def test_unreadable_or_unlocated_citer_cannot_validate_a_case(treat, tmp_path, body):
    setup(tmp_path, [case()])

    treat.run(tmp_path, FakeHttp(citers(500), {"500": body}))

    record = read(tmp_path)["treatments"][0]
    assert record["status"] == "unchecked"
    assert record["citing_checked"] == 0


# --- honesty about limits ------------------------------------------------

def test_a_truncated_walk_is_reported_rather_than_absorbed(treat, tmp_path):
    setup(tmp_path, [case()])
    found = {"count": 400, "results": [{"id": 500, "opinions": [{"id": 500}]}]}
    http = FakeHttp(found, {"500": {"plain_text": "Following Doe, 901 F.3d 1099 ..."}})

    result = treat.run(tmp_path, http, citing_cap=1)

    assert result["truncated"] == 1
    assert read(tmp_path)["treatments"][0]["citing_total"] == 400


def test_the_cap_bounds_how_many_citers_are_read(treat, tmp_path):
    setup(tmp_path, [case()])
    texts = {str(i): {"plain_text": "Following Doe, 901 F.3d 1099 ..."} for i in range(500, 510)}
    http = FakeHttp(citers(*range(500, 510)), texts)

    treat.run(tmp_path, http, citing_cap=3)

    assert read(tmp_path)["treatments"][0]["citing_checked"] == 3


def test_the_output_declares_it_is_not_a_citator(treat, tmp_path):
    """Every consumer of this file reads the disclaimer out of it, so it lives here."""
    setup(tmp_path, [case()])

    treat.run(tmp_path, FakeHttp())

    payload = read(tmp_path)
    assert payload["is_citator"] is False
    assert "Shepard's or KeyCite" in payload["disclaimer"]


def test_an_authority_that_was_never_archived_is_not_flagged(treat, tmp_path):
    """A treatment flag on a case nobody can read is a claim with no document."""
    setup(tmp_path, [case()], archived=())

    result = treat.run(tmp_path, FakeHttp(citers(500)))

    assert result["checked"] == 0


def test_running_before_fetching_is_a_clear_stop(treat, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "candidates.json").write_text(json.dumps({"authorities": []}), encoding="utf-8")

    with pytest.raises(SystemExit, match="run fetch_authorities.py first"):
        treat.run(tmp_path, FakeHttp())


def test_the_cap_stops_further_calls_across_a_clusters_opinions(treat, tmp_path):
    """The budget is shared, so a spent cap must stop the traffic too, not just
    stop the reading."""
    setup(tmp_path, [case(external_ids={"cl": "1", "cl_opinion": "90,91,92"})])
    texts = {str(i): {"plain_text": "Following Doe, 901 F.3d 1099 ..."} for i in range(500, 505)}
    http = FakeHttp(citers(*range(500, 505)), texts)

    treat.run(tmp_path, http, citing_cap=2)

    assert read(tmp_path)["treatments"][0]["citing_checked"] == 2
    assert sum(1 for u in http.urls if "cites%3A" in u) == 1, (
        "the second and third opinion ids should not have been searched at all"
    )
