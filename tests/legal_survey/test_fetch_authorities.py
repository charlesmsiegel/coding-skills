"""Acquisition: an absence must never be silent.

The manifest is the skill's ground truth, so what is tested here is mostly what
happens when a fetch does *not* work. An unreachable opinion that leaves no
manifest entry reads as one nobody wanted, and in a legal memo the difference
between "no authority is on point" and "we could not get it" is the whole memo.
"""

import hashlib
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "legal-survey" / "scripts"


@pytest.fixture
def fetch(load_module):
    return load_module(SCRIPTS, "fetch_authorities")


class FakeHttp:
    """A scripted fetcher keyed by URL. Missing URLs raise, as a dead link would."""

    def __init__(self, bodies=None, errors=None, robots_denies=()):
        self.bodies = bodies or {}
        self.errors = errors or {}
        self.robots_denies = set(robots_denies)
        self.urls: list[str] = []

    def get(self, url, accept="*/*"):
        self.urls.append(url)
        if url in self.errors:
            raise self.errors[url]
        if url not in self.bodies:
            raise OSError("no route to host")
        body, content_type = self.bodies[url]
        return type("R", (), {"body": body, "content_type": content_type})()

    def robots_allows(self, url):
        return url not in self.robots_denies


CASE_HTML = b"<html><body>Doe v. Agency, 901 F.3d 1099 (9th Cir. 2019)</body></html>"
REG_XML = b"<?xml version='1.0'?><DIV8><P>The provisions of this part apply.</P></DIV8>"


def write_candidates(out: Path, rows: list[dict], **extra) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "candidates.json").write_text(
        json.dumps({"question": "q", "forum": "Ninth Circuit", "authorities": rows, **extra}),
        encoding="utf-8")


def selected(**kwargs) -> dict:
    row = {
        "title": "Doe v. Agency", "authority_type": "case", "external_ids": {"cl": "1"},
        "citation": "901 F.3d 1099", "court": "ca9", "jurisdiction": "ca9",
        "decided": "2019-04-02", "precedential_status": "Published", "authors": [],
        "venue": "", "citation_count": 41, "summary": "", "as_of": "",
        "pdf_url": "", "landing_url": "https://www.courtlistener.com/opinion/1/doe/",
        "sources": ["courtlistener"], "status": "selected", "drop_reason": "",
        "weight": "binding", "weight_reason": "9th Cir.; forum is N.D. Cal.",
    }
    row.update(kwargs)
    return row


def manifest(out: Path) -> dict[str, dict]:
    payload = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    return {a["artifact_id"]: a for a in payload["artifacts"]}


def test_a_selected_case_is_archived_and_hashed(fetch, tmp_path):
    write_candidates(tmp_path, [selected()])
    http = FakeHttp({"https://www.courtlistener.com/opinion/1/doe/": (CASE_HTML, "text/html")})

    result = fetch.run(tmp_path, http)

    entry = manifest(tmp_path)["901f3d1099"]
    assert result["fetched"] == 1
    assert entry["status"] == "ok"
    assert entry["sha256"] == hashlib.sha256(CASE_HTML).hexdigest()
    assert (tmp_path / entry["path"]).read_bytes() == CASE_HTML
    assert entry["citation"] == "901 F.3d 1099"


def test_an_unselected_authority_is_not_fetched(fetch, tmp_path):
    write_candidates(tmp_path, [selected(status="new", weight="", weight_reason="")])
    http = FakeHttp()

    fetch.run(tmp_path, http)

    assert http.urls == []


def test_an_unreachable_authority_becomes_a_gap_with_a_reason(fetch, tmp_path):
    write_candidates(tmp_path, [selected()])
    http = FakeHttp(errors={"https://www.courtlistener.com/opinion/1/doe/": OSError("boom")})

    result = fetch.run(tmp_path, http)

    entry = manifest(tmp_path)["901f3d1099"]
    assert result["gaps"] == 1
    assert entry["status"] == "failed"
    assert entry["failure_reason"]
    assert not entry["path"], "a gap must not also claim a file"


def test_a_403_is_recorded_as_a_paywall(fetch, tmp_path):
    write_candidates(tmp_path, [selected()])
    url = "https://www.courtlistener.com/opinion/1/doe/"
    http = FakeHttp(errors={url: fetch.FetchError("HTTP 403 for " + url)})

    fetch.run(tmp_path, http)

    assert manifest(tmp_path)["901f3d1099"]["status"] == "paywalled"


def test_a_timeout_naming_a_number_is_not_mistaken_for_a_paywall(fetch, tmp_path):
    """The reason carries the URL, and a bare substring test called a DOI a 403."""
    write_candidates(tmp_path, [selected()])
    url = "https://www.courtlistener.com/opinion/1/doe/"
    http = FakeHttp(errors={url: fetch.FetchError(
        "gave up on https://doi.org/10.1177/0003603X403 after 4 attempts")})

    fetch.run(tmp_path, http)

    assert manifest(tmp_path)["901f3d1099"]["status"] == "failed"


def test_a_sign_in_interstitial_is_a_wall_not_a_document(fetch, tmp_path):
    """A 200 is not an opinion. Archiving the wall lets the paywall be cited as law."""
    write_candidates(tmp_path, [selected()])
    wall = b"<html><body>Please sign in to continue reading.</body></html>"
    http = FakeHttp({"https://www.courtlistener.com/opinion/1/doe/": (wall, "text/html")})

    result = fetch.run(tmp_path, http)

    entry = manifest(tmp_path)["901f3d1099"]
    assert result["gaps"] == 1
    assert entry["status"] == "paywalled"
    assert "access wall" in entry["failure_reason"]


def test_a_long_document_mentioning_subscriptions_is_not_a_wall(fetch, tmp_path):
    """'Subscribe' is ordinary English; a rulemaking docket that contains it is law."""
    write_candidates(tmp_path, [selected()])
    body = b"<html><body>" + (b"Members may subscribe to the register. " * 200) + b"</body></html>"
    http = FakeHttp({"https://www.courtlistener.com/opinion/1/doe/": (body, "text/html")})

    result = fetch.run(tmp_path, http)

    assert result["fetched"] == 1


def test_a_pdf_url_answering_with_html_is_a_wall(fetch, tmp_path):
    write_candidates(tmp_path, [selected(landing_url="", pdf_url="https://x.example/o.pdf")])
    html = b"<html>" + b"x" * 9000 + b"</html>"
    http = FakeHttp({"https://x.example/o.pdf": (html, "text/html")})

    fetch.run(tmp_path, http)

    assert manifest(tmp_path)["901f3d1099"]["status"] == "paywalled"


def test_robots_denial_is_its_own_status(fetch, tmp_path):
    url = "https://www.courtlistener.com/opinion/1/doe/"
    write_candidates(tmp_path, [selected()])
    http = FakeHttp({url: (CASE_HTML, "text/html")}, robots_denies={url})

    fetch.run(tmp_path, http)

    assert manifest(tmp_path)["901f3d1099"]["status"] == "robots_blocked"


def test_an_authority_with_nowhere_to_fetch_from_is_still_recorded(fetch, tmp_path):
    write_candidates(tmp_path, [selected(landing_url="", pdf_url="")])

    fetch.run(tmp_path, FakeHttp())

    entry = manifest(tmp_path)["901f3d1099"]
    assert entry["status"] == "failed"
    assert "no retrievable location" in entry["failure_reason"]


# --- currency -----------------------------------------------------------

def test_an_archived_regulation_carries_its_version_date(fetch, tmp_path):
    url = "https://www.ecfr.gov/api/versioner/v1/full/2019-07-01/title-40.xml"
    write_candidates(tmp_path, [selected(
        title="40 CFR 60.1", authority_type="regulation", citation="40 CFR 60.1",
        external_ids={"ecfr": "40-60-60.1"}, as_of="2019-07-01", landing_url=url)])
    http = FakeHttp({url: (REG_XML, "application/xml")})

    fetch.run(tmp_path, http)

    entry = manifest(tmp_path)["40cfr601"]
    assert entry["as_of"] == "2019-07-01"
    assert entry["path"].endswith(".xml")


def test_a_regulation_with_no_date_falls_back_to_the_fetch_date(fetch, tmp_path):
    """A versionless request returns current law; recording that is honest, and
    leaving it blank would trip the manifest invariant for no gain."""
    url = "https://www.ecfr.gov/x"
    write_candidates(tmp_path, [selected(
        title="40 CFR 60.1", authority_type="regulation", citation="40 CFR 60.1",
        as_of="", decided="", landing_url=url)])
    http = FakeHttp({url: (REG_XML, "application/xml")})

    fetch.run(tmp_path, http, today=lambda: "2026-09-04")

    assert manifest(tmp_path)["40cfr601"]["as_of"] == "2026-09-04"


# --- idempotence --------------------------------------------------------

def test_an_unchanged_artifact_is_skipped_on_a_rerun(fetch, tmp_path):
    write_candidates(tmp_path, [selected()])
    http = FakeHttp({"https://www.courtlistener.com/opinion/1/doe/": (CASE_HTML, "text/html")})
    fetch.run(tmp_path, http)

    result = fetch.run(tmp_path, http)

    assert result["skipped"] == 1 and result["fetched"] == 0


def test_an_artifact_whose_bytes_changed_is_refetched_rather_than_trusted(fetch, tmp_path):
    write_candidates(tmp_path, [selected()])
    http = FakeHttp({"https://www.courtlistener.com/opinion/1/doe/": (CASE_HTML, "text/html")})
    fetch.run(tmp_path, http)
    entry = manifest(tmp_path)["901f3d1099"]
    (tmp_path / entry["path"]).write_bytes(b"tampered")

    result = fetch.run(tmp_path, http)

    assert result["fetched"] == 1
    assert (tmp_path / entry["path"]).read_bytes() == CASE_HTML


# --- hand-added authorities ---------------------------------------------

def test_a_hand_added_url_lands_in_the_manifest_like_everything_else(fetch, tmp_path):
    """An authority not in the manifest cannot be cited, so this is what keeps that
    rule from narrowing the corpus to whatever happens to have an API."""
    write_candidates(tmp_path, [])
    url = "https://www.justice.gov/oip/exemption-5"
    http = FakeHttp({url: (b"<html>" + b"guidance " * 500 + b"</html>", "text/html")})

    result = fetch.run(tmp_path, http, extra_urls=[{"name": "oip-guidance", "url": url}])

    assert result["fetched"] == 1
    assert manifest(tmp_path)["oip-guidance"]["status"] == "ok"


def test_no_candidates_file_is_a_clear_stop(fetch, tmp_path):
    with pytest.raises(SystemExit, match="run search_sources.py first"):
        fetch.run(tmp_path, FakeHttp())
