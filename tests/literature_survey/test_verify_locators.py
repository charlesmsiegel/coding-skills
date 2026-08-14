"""The gate. Non-zero exit means the report is not finished.

This is the only mechanical defence against the failure the skill exists to
prevent — fluent synthesis over unread abstracts. It cannot tell you a claim
misreads its page. It can tell you the page was never opened, the file changed
under the citation, or the paper is not there at all, and those have to be exit
codes rather than advice, because advice at the end of a long run gets skimmed.

The other half of its job is refusing to launder ignorance: a check that could
not be performed is reported *unverifiable*, and unverifiable is not clean.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "literature-survey" / "scripts"

PDF_TWO_PAGES = b"%PDF-1.4\n/Type /Pages /Count 2\n/Type /Page\n/Type /Page\n%%EOF\n"


@pytest.fixture
def verify(load_module):
    return load_module(SCRIPTS, "verify_locators")


class Survey:
    """A survey directory built artifact by artifact, the way the pipeline leaves it."""

    def __init__(self, root: Path):
        self.root = root
        (root / "docs" / "notes").mkdir(parents=True, exist_ok=True)
        self.artifacts: list[dict] = []

    def artifact(self, artifact_id: str, body: bytes, suffix=".pdf") -> "Survey":
        rel = "docs/papers/" + artifact_id + suffix
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        self.artifacts.append({"artifact_id": artifact_id, "kind": "paper", "url": "https://x",
                               "status": "ok", "path": rel,
                               "sha256": hashlib.sha256(body).hexdigest(),
                               "bytes_len": len(body)})
        return self

    def gap(self, artifact_id: str, status="paywalled") -> "Survey":
        self.artifacts.append({"artifact_id": artifact_id, "kind": "paper", "url": "https://x",
                               "status": status, "failure_reason": "HTTP 403"})
        return self

    def note(self, artifact_id: str, *locators, text="teams remember more together") -> "Survey":
        (self.root / "docs" / "notes" / (artifact_id + ".json")).write_text(json.dumps({
            "artifact_id": artifact_id,
            "claims": [{"text": text, "locators": list(locators)}],
        }), encoding="utf-8")
        return self

    def write(self) -> Path:
        (self.root / "manifest.json").write_text(json.dumps({"artifacts": self.artifacts}),
                                                 encoding="utf-8")
        return self.root


@pytest.fixture
def survey(tmp_path) -> Survey:
    return Survey(tmp_path / "survey")


def loc(artifact_id, **kwargs):
    return {"artifact_id": artifact_id, "page": None, "section": "", "quote": "", **kwargs}


# --- what passes ---------------------------------------------------------

def test_a_locator_into_an_archived_paper_resolves(verify, survey):
    out = survey.artifact("2401.1", PDF_TWO_PAGES).note("2401.1", loc("2401.1", page=2)).write()

    result = verify.run(out)

    assert result == {"checked": 1, "failures": [], "unverifiable": []}


def test_a_quote_spanning_a_line_break_still_resolves(verify, survey):
    """A PDF-to-text extraction wraps mid-sentence; failing that would train people to drop quotes."""
    out = (survey.artifact("w1", b"<p>teams remember\nmore together</p>", suffix=".html")
           .note("w1", loc("w1", quote="teams remember more together")).write())

    assert verify.run(out)["failures"] == []


def test_markup_between_the_words_does_not_break_a_quote(verify, survey):
    out = (survey.artifact("w1", b"<p>teams <em>remember</em> more together</p>", suffix=".html")
           .note("w1", loc("w1", quote="teams remember more together")).write())

    assert verify.run(out)["failures"] == []


# --- what fails ----------------------------------------------------------

def test_a_claim_citing_an_artifact_that_was_never_fetched_fails(verify, survey):
    out = survey.artifact("2401.1", PDF_TWO_PAGES).note("2401.1", loc("2401.9", page=1)).write()

    [failure] = verify.run(out)["failures"]

    assert "not in the manifest" in failure["reason"]


def test_a_claim_resting_on_a_paywalled_paper_fails(verify, survey):
    """The gap is recorded honestly; what it must not do is quietly support a claim."""
    out = survey.gap("10.1_x").note("10.1_x", loc("10.1_x", page=3)).write()

    [failure] = verify.run(out)["failures"]

    assert "never obtained (paywalled)" in failure["reason"]


def test_a_missing_file_fails_even_though_the_manifest_still_lists_it(verify, survey):
    out = survey.artifact("2401.1", PDF_TWO_PAGES).note("2401.1", loc("2401.1", page=1)).write()
    (out / "docs" / "papers" / "2401.1.pdf").unlink()

    [failure] = verify.run(out)["failures"]

    assert "file missing" in failure["reason"]


def test_a_file_edited_after_it_was_cited_fails_on_its_hash(verify, survey):
    out = survey.artifact("2401.1", PDF_TWO_PAGES).note("2401.1", loc("2401.1", page=1)).write()
    (out / "docs" / "papers" / "2401.1.pdf").write_bytes(PDF_TWO_PAGES + b"appended\n")

    [failure] = verify.run(out)["failures"]

    assert "sha256 no longer matches" in failure["reason"]


def test_a_page_beyond_the_end_of_the_document_fails(verify, survey):
    out = survey.artifact("2401.1", PDF_TWO_PAGES).note("2401.1", loc("2401.1", page=40)).write()

    [failure] = verify.run(out)["failures"]

    assert "outside the document's 2 pages" in failure["reason"]


def test_page_zero_fails(verify, survey):
    out = survey.artifact("2401.1", PDF_TWO_PAGES).note("2401.1", loc("2401.1", page=0)).write()

    assert verify.run(out)["failures"], "there is no page 0; the locator was never resolved"


def test_a_quote_that_is_not_in_the_file_fails(verify, survey):
    out = (survey.artifact("w1", b"<p>we find no effect</p>", suffix=".html")
           .note("w1", loc("w1", quote="we find a large effect")).write())

    [failure] = verify.run(out)["failures"]

    assert "quote not found" in failure["reason"]


def test_the_failure_names_the_claim_so_it_can_be_fixed(verify, survey):
    out = survey.note("2401.1", loc("2401.9", page=1), text="shared mental models help").write()

    [failure] = verify.run(out)["failures"]

    assert failure["note"] == "2401.1" and "shared mental models help" in failure["claim"]


# --- what is unverifiable, which is not clean ----------------------------

def test_a_page_in_a_document_of_unknown_length_is_unverifiable_not_passing(verify, survey):
    out = (survey.artifact("w1", b"<html>a page</html>", suffix=".html")
           .note("w1", loc("w1", page=3)).write())

    result = verify.run(out)

    assert result["failures"] == []
    assert "page count could not be determined" in result["unverifiable"][0]["reason"]


def test_a_quote_in_a_binary_artifact_is_unverifiable_rather_than_assumed(verify, survey):
    out = (survey.artifact("2401.1", PDF_TWO_PAGES)
           .note("2401.1", loc("2401.1", quote="teams remember")).write())

    result = verify.run(out)

    assert result["failures"] == []
    assert "binary artifact" in result["unverifiable"][0]["reason"]


# --- the report's own links ---------------------------------------------

def test_a_report_link_that_resolves_to_nothing_on_disk_fails(verify, survey):
    out = survey.artifact("2401.1", PDF_TWO_PAGES).write()
    report = out / "summary.html"
    report.write_text('<a href="docs/papers/2401.1.pdf">read</a>'
                      '<a href="docs/papers/ghost.pdf">read</a>', encoding="utf-8")

    [failure] = verify.run(out, report=report)["failures"]

    assert "docs/papers/ghost.pdf" in failure["reason"]


def test_a_report_that_has_not_been_built_yet_is_not_a_failure(verify, survey):
    out = survey.artifact("2401.1", PDF_TWO_PAGES).write()

    assert verify.run(out, report=out / "summary.html")["failures"] == []


# --- exit codes, which are the actual interface --------------------------

def run_cli(out: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "verify_locators.py"), "--out", str(out),
                           *args], capture_output=True, text=True, timeout=120)


def test_the_gate_exits_zero_when_everything_resolves(survey):
    out = survey.artifact("2401.1", PDF_TWO_PAGES).note("2401.1", loc("2401.1", page=1)).write()

    result = run_cli(out)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("0 unresolvable of 1 locators checked")


def test_the_gate_exits_non_zero_on_an_unresolvable_locator(survey):
    out = survey.artifact("2401.1", PDF_TWO_PAGES).note("2401.1", loc("2401.9", page=1)).write()

    result = run_cli(out)

    assert result.returncode == 1
    assert "not in the manifest" in result.stdout


def test_an_unverifiable_check_is_surfaced_as_a_caveat_not_hidden_by_a_zero(survey):
    out = (survey.artifact("2401.1", PDF_TWO_PAGES)
           .note("2401.1", loc("2401.1", quote="teams remember")).write())

    result = run_cli(out)

    assert result.returncode == 0
    assert "1 unverifiable" in result.stdout and "caveat:" in result.stdout


def test_a_gate_with_nothing_to_check_says_it_proved_nothing(survey):
    """Zero failures over zero locators is the shape a skipped read leaves behind."""
    out = survey.artifact("2401.1", PDF_TWO_PAGES).write()

    result = run_cli(out)

    assert result.returncode == 0
    assert "this gate proves nothing" in result.stdout


def test_a_survey_with_no_manifest_is_an_error_rather_than_a_pass(survey, tmp_path):
    result = run_cli(tmp_path / "empty")

    assert result.returncode != 0
    assert "no manifest.json" in result.stderr


def test_the_json_format_carries_the_failures_for_a_machine_reader(survey):
    out = survey.artifact("2401.1", PDF_TWO_PAGES).note("2401.1", loc("2401.9", page=1)).write()

    result = run_cli(out, "--format", "json")

    payload = json.loads(result.stdout)
    assert payload["tool"] == "verify_locators"
    assert payload["rows"][0]["artifact"] == "2401.9"
