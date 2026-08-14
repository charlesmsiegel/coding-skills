"""The manifest is the skill's ground truth, so its absences have to be entries.

Everything downstream reads this file: citations resolve against it, the masthead
counts come from it, and the gaps tab is built from it. An artifact that could
not be obtained therefore has to appear *as a record with a reason* — an absent
entry would let an unreachable paper read as one nobody wanted to read.
"""

import json
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "literature-survey" / "scripts"


@pytest.fixture
def fetch(load_module):
    return load_module(SCRIPTS, "fetch_artifacts")


class FakeHttp:
    """Serves bodies by URL; anything unlisted raises the error it is mapped to."""

    def __init__(self, bodies=None, errors=None, robots_blocked=()):
        self.bodies = bodies or {}
        self.errors = errors or {}
        self.robots_blocked = set(robots_blocked)
        self.fetched: list[str] = []

    def get(self, url, accept="*/*"):
        self.fetched.append(url)
        if url in self.errors:
            raise self.errors[url]
        return type("R", (), {"body": self.bodies[url], "content_type": "application/pdf"})()

    def robots_allows(self, url):
        return url not in self.robots_blocked


def write_candidates(out: Path, *candidates) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "candidates.json").write_text(json.dumps({"candidates": list(candidates)}),
                                         encoding="utf-8")


def paper(title="Transactive memory systems", arxiv="2401.00001", pdf="https://x/p.pdf", **extra):
    return {"title": title, "external_ids": {"arxiv": arxiv}, "pdf_url": pdf,
            "status": "selected", **extra}


def manifest(out: Path) -> dict:
    return {a["artifact_id"]: a
            for a in json.loads((out / "manifest.json").read_text(encoding="utf-8"))["artifacts"]}


def clock():
    return lambda: "2026-01-01T00:00:00Z"


# --- the happy path ------------------------------------------------------

def test_a_fetched_paper_lands_on_disk_hashed_and_named_readably(fetch, tmp_path):
    write_candidates(tmp_path, paper())
    http = FakeHttp({"https://x/p.pdf": b"%PDF-1.4 body"})

    result = fetch.run(tmp_path, http, now=clock())

    assert result == {"fetched": 1, "skipped": 0, "gaps": 0, "cloned": 0, "selected": 1}
    entry = manifest(tmp_path)["2401.00001"]
    assert entry["path"] == "docs/papers/2401.00001-transactive-memory-systems.pdf"
    assert (tmp_path / entry["path"]).read_bytes() == b"%PDF-1.4 body"
    assert entry["sha256"] and entry["bytes_len"] == len(b"%PDF-1.4 body")
    assert entry["content_type"] == "application/pdf"


def test_an_unselected_candidate_is_not_fetched(fetch, tmp_path):
    """Triage decides what is read; fetch does not get a second opinion."""
    write_candidates(tmp_path,
                     paper(status="new"),
                     paper(title="Other", arxiv="2401.00002", status="dropped",
                           drop_reason="wrong field"))
    http = FakeHttp()

    result = fetch.run(tmp_path, http, now=clock())

    assert result["selected"] == 0 and http.fetched == []


def test_a_landing_page_is_used_when_there_is_no_pdf(fetch, tmp_path):
    write_candidates(tmp_path, {"title": "Thread", "external_ids": {"doi": "10.1/x"},
                                "landing_url": "https://x/page", "status": "selected"})
    http = FakeHttp({"https://x/page": b"<html>text</html>"})

    fetch.run(tmp_path, http, now=clock())

    entry = manifest(tmp_path)["10.1_x"]
    assert entry["kind"] == "web" and entry["path"].startswith("docs/web/")


# --- idempotence ---------------------------------------------------------

def test_an_artifact_already_on_disk_and_matching_is_not_refetched(fetch, tmp_path):
    write_candidates(tmp_path, paper())
    http = FakeHttp({"https://x/p.pdf": b"%PDF-1.4 body"})
    fetch.run(tmp_path, http, now=clock())

    result = fetch.run(tmp_path, FakeHttp(), now=clock())

    assert result == {"fetched": 0, "skipped": 1, "gaps": 0, "cloned": 0, "selected": 1}


def test_an_artifact_whose_bytes_changed_is_refetched_rather_than_trusted(fetch, tmp_path):
    """A file that no longer hashes to its manifest entry cannot support a citation."""
    write_candidates(tmp_path, paper())
    fetch.run(tmp_path, FakeHttp({"https://x/p.pdf": b"%PDF-1.4 body"}), now=clock())
    path = tmp_path / manifest(tmp_path)["2401.00001"]["path"]
    path.write_bytes(b"%PDF-1.4 tampered")

    result = fetch.run(tmp_path, FakeHttp({"https://x/p.pdf": b"%PDF-1.4 body"}), now=clock())

    assert result["fetched"] == 1
    assert path.read_bytes() == b"%PDF-1.4 body"


def test_a_deleted_file_is_refetched(fetch, tmp_path):
    write_candidates(tmp_path, paper())
    fetch.run(tmp_path, FakeHttp({"https://x/p.pdf": b"%PDF-1.4 body"}), now=clock())
    (tmp_path / manifest(tmp_path)["2401.00001"]["path"]).unlink()

    result = fetch.run(tmp_path, FakeHttp({"https://x/p.pdf": b"%PDF-1.4 body"}), now=clock())

    assert result["fetched"] == 1


# --- gaps ----------------------------------------------------------------

def test_a_403_is_recorded_as_a_paywall_rather_than_a_generic_failure(fetch, tmp_path):
    write_candidates(tmp_path, paper())
    http = FakeHttp(errors={"https://x/p.pdf": OSError("HTTP 403 for https://x/p.pdf")})

    result = fetch.run(tmp_path, http, now=clock())

    entry = manifest(tmp_path)["2401.00001"]
    assert result["gaps"] == 1
    assert entry["status"] == "paywalled" and "403" in entry["failure_reason"]
    assert not entry["path"] and not entry["sha256"]


def test_a_timeout_on_a_url_containing_403_is_not_called_a_paywall(fetch, tmp_path):
    """The reason string carries the URL, so a substring test put a PLOS paper whose
    DOI happens to contain 403 in the gaps tab as deliberately inaccessible."""
    url = "https://journals.plos.org/article/file?id=10.1371/journal.pone.0040312"
    write_candidates(tmp_path, paper(pdf=url))
    http = FakeHttp(errors={url: OSError("gave up on " + url + " after 4 attempts: timed out")})

    fetch.run(tmp_path, http, now=clock())

    assert manifest(tmp_path)["2401.00001"]["status"] == "failed"


def test_a_transport_failure_is_recorded_as_a_gap_with_its_reason(fetch, tmp_path):
    write_candidates(tmp_path, paper())
    http = FakeHttp(errors={"https://x/p.pdf": OSError("gave up after 4 attempts")})

    fetch.run(tmp_path, http, now=clock())

    entry = manifest(tmp_path)["2401.00001"]
    assert entry["status"] == "failed" and "gave up" in entry["failure_reason"]


def test_a_candidate_with_nowhere_to_fetch_from_is_a_gap_not_a_silence(fetch, tmp_path):
    write_candidates(tmp_path, {"title": "Paper behind a login", "external_ids": {"doi": "10.1/x"},
                                "status": "selected"})

    result = fetch.run(tmp_path, FakeHttp(), now=clock())

    assert result["gaps"] == 1
    assert "no retrievable location" in manifest(tmp_path)["10.1_x"]["failure_reason"]


def test_an_html_interstitial_served_from_a_pdf_url_is_a_gap_not_an_archived_paper(fetch, tmp_path):
    """Publishers answer a PDF URL with "sign in through your institution" and a 200.
    Saving that under .pdf archived the paywall as the work: counted in "papers
    archived", absent from the gaps tab, and available to be cited."""
    write_candidates(tmp_path, paper())
    http = FakeHttp({"https://x/p.pdf": b"<html><body>Access denied. Sign in.</body></html>"})

    result = fetch.run(tmp_path, http, now=clock())

    entry = manifest(tmp_path)["2401.00001"]
    assert result == {"fetched": 0, "skipped": 0, "gaps": 1, "cloned": 0, "selected": 1}
    assert entry["status"] == "paywalled" and "rather than a PDF" in entry["failure_reason"]
    assert not (tmp_path / "docs" / "papers").exists(), "an access wall is not an artifact"


def test_a_pdf_whose_header_is_not_at_byte_zero_is_still_a_paper(fetch, tmp_path):
    """Real files carry junk before the header; a strict check belongs at the gate."""
    write_candidates(tmp_path, paper())
    http = FakeHttp({"https://x/p.pdf": b"\r\n   %PDF-1.7\nbody"})

    assert fetch.run(tmp_path, http, now=clock())["fetched"] == 1


def test_a_landing_page_is_not_held_to_the_pdf_check(fetch, tmp_path):
    write_candidates(tmp_path, {"title": "Thread", "external_ids": {"doi": "10.1/x"},
                                "landing_url": "https://x/page", "status": "selected"})

    result = fetch.run(tmp_path, FakeHttp({"https://x/page": b"<html>text</html>"}), now=clock())

    assert result["fetched"] == 1


def test_a_robots_disallowed_page_is_recorded_and_never_fetched(fetch, tmp_path):
    write_candidates(tmp_path, {"title": "Forum thread", "external_ids": {"doi": "10.1/x"},
                                "landing_url": "https://x/private/thread", "status": "selected"})
    http = FakeHttp(robots_blocked=["https://x/private/thread"])

    result = fetch.run(tmp_path, http, now=clock())

    assert result["gaps"] == 1
    assert manifest(tmp_path)["10.1_x"]["status"] == "robots_blocked"
    assert http.fetched == [], "a blocked path must not be requested anyway"


def test_robots_does_not_gate_a_pdf_from_a_scholarly_index(fetch, tmp_path):
    """The etiquette rule is about open-web fetches; an arXiv PDF is not one."""
    write_candidates(tmp_path, paper())
    http = FakeHttp({"https://x/p.pdf": b"%PDF-1.4 body"}, robots_blocked=["https://x/p.pdf"])

    result = fetch.run(tmp_path, http, now=clock())

    assert result["fetched"] == 1


def test_a_previously_failed_artifact_is_retried_on_the_next_run(fetch, tmp_path):
    write_candidates(tmp_path, paper())
    fetch.run(tmp_path, FakeHttp(errors={"https://x/p.pdf": OSError("timeout")}), now=clock())

    result = fetch.run(tmp_path, FakeHttp({"https://x/p.pdf": b"%PDF-1.4 body"}), now=clock())

    assert result["fetched"] == 1
    assert manifest(tmp_path)["2401.00001"]["status"] == "ok"


def test_the_manifest_survives_a_run_that_fetched_nothing_new(fetch, tmp_path):
    write_candidates(tmp_path, paper())
    fetch.run(tmp_path, FakeHttp({"https://x/p.pdf": b"%PDF-1.4 body"}), now=clock())

    fetch.run(tmp_path, FakeHttp(), now=clock())

    assert "2401.00001" in manifest(tmp_path)


def test_no_candidates_file_is_an_error_naming_the_stage_that_was_skipped(fetch, tmp_path):
    with pytest.raises(SystemExit, match="search_sources.py first"):
        fetch.run(tmp_path, FakeHttp(), now=clock())


# --- repositories --------------------------------------------------------

def origin(tmp_path: Path) -> str:
    src = tmp_path / "origin"
    src.mkdir()
    (src / "LICENSE").write_text("MIT License\n\nCopyright (c)\n", encoding="utf-8")
    (src / "README.md").write_text("# graphrag\n", encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.email", "t@t.co"], ["config", "user.name", "t"],
                 ["config", "commit.gpgsign", "false"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(src), *args], check=True, capture_output=True, timeout=120)
    return str(src)


def test_a_repository_is_cloned_pinned_to_its_head_and_licence(fetch, tmp_path):
    write_candidates(tmp_path)

    result = fetch.run(tmp_path, FakeHttp(), now=clock(),
                       repos=[{"name": "graphrag", "url": origin(tmp_path)}])

    entry = manifest(tmp_path)["repo:graphrag"]
    assert result["cloned"] == 1
    assert entry["status"] == "ok" and entry["license"] == "MIT"
    assert len(entry["sha256"]) == 40, "a clone is pinned by commit, not by file hash"
    assert (tmp_path / "docs" / "repos" / "graphrag" / "README.md").is_file()


def test_a_second_run_leaves_an_existing_clone_alone(fetch, tmp_path):
    write_candidates(tmp_path)
    repos = [{"name": "graphrag", "url": origin(tmp_path)}]
    fetch.run(tmp_path, FakeHttp(), now=clock(), repos=repos)

    result = fetch.run(tmp_path, FakeHttp(), now=clock(), repos=repos)

    assert result == {"fetched": 0, "skipped": 1, "gaps": 0, "cloned": 0, "selected": 0}


def test_an_unclonable_repository_is_a_gap_with_its_reason(fetch, tmp_path):
    write_candidates(tmp_path)

    result = fetch.run(tmp_path, FakeHttp(), now=clock(),
                       repos=[{"name": "nope", "url": str(tmp_path / "does-not-exist")}])

    entry = manifest(tmp_path)["repo:nope"]
    assert result["gaps"] == 1
    assert entry["status"] == "failed" and "clone failed" in entry["failure_reason"]


def test_the_clone_index_names_what_was_cloned_and_what_was_not(fetch, tmp_path):
    write_candidates(tmp_path)

    fetch.run(tmp_path, FakeHttp(), now=clock(), repos=[
        {"name": "graphrag", "url": origin(tmp_path)},
        {"name": "nope", "url": str(tmp_path / "does-not-exist")},
    ])

    text = (tmp_path / "docs" / "repos" / "CLONES.md").read_text(encoding="utf-8")
    assert "**graphrag**" in text and "(MIT)" in text
    assert "NOT CLONED" in text


def test_an_unrecognized_licence_is_unknown_rather_than_guessed(fetch, tmp_path):
    src = Path(origin(tmp_path))
    (src / "LICENSE").write_text("Everything is permitted.\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(src), "commit", "-qam", "relicense"], check=True,
                   capture_output=True, timeout=120)
    write_candidates(tmp_path)

    fetch.run(tmp_path, FakeHttp(), now=clock(), repos=[{"name": "graphrag", "url": str(src)}])

    assert manifest(tmp_path)["repo:graphrag"]["license"] == "unknown"


def test_a_repo_flag_without_a_url_is_rejected_at_the_cli(fetch):
    with pytest.raises(Exception, match="NAME=URL"):
        fetch._parse_repo_flag("graphrag")
