"""The gate. Non-zero exit means the report is not finished.

Two families of failure it exists to catch, and both are tested here: a citation
that resolves to nothing (or to the wrong document), and a claim resting on an
authority that is no longer good law. The third thing it does is refuse to
certify what it could not check — `unverifiable` is not `clean`, and a green exit
code over checks that never ran is the shape of a laundered result.
"""

import hashlib
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "legal-survey" / "scripts"

OPINION = (b"<html><body><p>Doe v. Agency, 901 F.3d 1099 (9th Cir. 2019). "
           b"II.B Deliberative process. The privilege protects drafts.</p></body></html>")

MINIMAL_PDF = (b"%PDF-1.4\n1 0 obj<</Type /Pages /Count 3>>endobj\n"
               b"2 0 obj<</Type /Page>>endobj\n%%EOF\n")


@pytest.fixture
def verify(load_module):
    return load_module(SCRIPTS, "verify_authorities")


class Corpus:
    """An out/ directory built a piece at a time, so each test states its own setup."""

    def __init__(self, root: Path):
        self.root = root
        self.artifacts: list[dict] = []
        self.treatments: list[dict] = []
        root.mkdir(parents=True, exist_ok=True)

    def archive(self, aid, body=OPINION, kind="case", citation="901 F.3d 1099",
                suffix=".html", as_of="", sha=None):
        rel = "docs/" + kind + "/" + aid + suffix
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        self.artifacts.append({
            "artifact_id": aid, "kind": kind, "url": "u", "status": "ok", "path": rel,
            "sha256": sha or hashlib.sha256(body).hexdigest(), "bytes_len": len(body),
            "citation": citation, "as_of": as_of,
        })
        return self

    def gap(self, aid, status="paywalled"):
        self.artifacts.append({"artifact_id": aid, "kind": "case", "url": "u",
                               "status": status, "failure_reason": "403"})
        return self

    def treatment(self, aid, status, **kwargs):
        self.treatments.append({"artifact_id": aid, "status": status, **kwargs})
        return self

    def note(self, aid, claims):
        notes = self.root / "docs" / "notes"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / (aid + ".json")).write_text(
            json.dumps({"artifact_id": aid, "claims": claims}), encoding="utf-8")
        return self

    def commit(self):
        (self.root / "manifest.json").write_text(
            json.dumps({"artifacts": self.artifacts}), encoding="utf-8")
        (self.root / "treatment.json").write_text(
            json.dumps({"treatments": self.treatments}), encoding="utf-8")
        return self.root


def claim(aid, text="drafts are protected", basis="holding", ack="", **locator):
    return {"text": text, "basis": basis, "treatment_ack": ack,
            "locators": [{"artifact_id": aid, **locator}]}


@pytest.fixture
def corpus(tmp_path):
    return Corpus(tmp_path / "out")


def reasons(result) -> str:
    return " ".join(f["reason"] for f in result["failures"])


def caveats(result) -> str:
    return " ".join(u["reason"] for u in result["unverifiable"])


# --- does the authority exist, unchanged --------------------------------

def test_a_resolving_citation_passes(verify, corpus):
    out = (corpus.archive("901f3d1099").treatment("901f3d1099", "good")
           .note("901f3d1099", [claim("901f3d1099", quote="The privilege protects drafts.")])
           .commit())

    result = verify.run(out)

    assert result["failures"] == []
    assert result["checked"] == 1


def test_a_citation_to_an_authority_nobody_obtained_fails(verify, corpus):
    """The fabricated-citation failure with extra steps."""
    out = corpus.note("ghost", [claim("ghost", page=1)]).commit()

    result = verify.run(out)

    assert "not in the manifest" in reasons(result)


def test_a_citation_to_a_recorded_gap_fails(verify, corpus):
    out = corpus.gap("901f3d1099").note("901f3d1099", [claim("901f3d1099", page=1)]).commit()

    result = verify.run(out)

    assert "never obtained" in reasons(result)


def test_a_file_that_changed_after_it_was_cited_fails(verify, corpus):
    out = (corpus.archive("901f3d1099", sha="deadbeef")
           .note("901f3d1099", [claim("901f3d1099", quote="The privilege protects drafts.")])
           .commit())

    result = verify.run(out)

    assert "sha256 no longer matches" in reasons(result)


def test_a_missing_file_fails(verify, corpus):
    out = (corpus.archive("901f3d1099").treatment("901f3d1099", "good")
           .note("901f3d1099", [claim("901f3d1099", page=1)]).commit())
    (out / "docs" / "case" / "901f3d1099.html").unlink()

    assert "file missing" in reasons(verify.run(out))


# --- does the pincite land ----------------------------------------------

def test_a_quote_that_is_not_there_fails(verify, corpus):
    out = (corpus.archive("901f3d1099").treatment("901f3d1099", "good")
           .note("901f3d1099", [claim("901f3d1099", quote="the privilege is absolute")])
           .commit())

    assert "quote not found" in reasons(verify.run(out))


def test_a_quote_broken_across_lines_still_resolves(verify, corpus):
    body = b"<html><body>Doe, 901 F.3d 1099. The privilege\n   protects drafts.</body></html>"
    out = (corpus.archive("901f3d1099", body=body).treatment("901f3d1099", "good")
           .note("901f3d1099", [claim("901f3d1099", quote="The privilege protects drafts.")])
           .commit())

    assert verify.run(out)["failures"] == []


def test_a_page_past_the_end_of_a_pdf_fails(verify, corpus):
    out = (corpus.archive("901f3d1099", body=MINIMAL_PDF, suffix=".pdf")
           .treatment("901f3d1099", "good")
           .note("901f3d1099", [claim("901f3d1099", page=40)]).commit())

    assert "outside the document" in reasons(verify.run(out))


def test_an_undeterminable_page_count_is_unverifiable_not_clean(verify, corpus):
    out = (corpus.archive("901f3d1099").treatment("901f3d1099", "good")
           .note("901f3d1099", [claim("901f3d1099", page=7)]).commit())

    result = verify.run(out)

    assert result["failures"] == []
    assert "page count could not be determined" in caveats(result)


def test_a_section_only_locator_is_searched_where_it_can_be(verify, corpus):
    """The cheapest locator to fabricate must not also be the one that always passes."""
    out = (corpus.archive("901f3d1099").treatment("901f3d1099", "good")
           .note("901f3d1099", [claim("901f3d1099", section="XII The Nonexistent Part")])
           .commit())

    assert "not found in" in reasons(verify.run(out))


def test_a_section_only_locator_in_a_pdf_is_unverifiable(verify, corpus):
    out = (corpus.archive("901f3d1099", body=MINIMAL_PDF, suffix=".pdf")
           .treatment("901f3d1099", "good")
           .note("901f3d1099", [claim("901f3d1099", section="II.B")]).commit())

    result = verify.run(out)

    assert result["failures"] == []
    assert "cannot be resolved in a binary artifact" in caveats(result)


# --- is this document the authority it claims to be ---------------------

def test_a_document_missing_its_own_citation_fails(verify, corpus):
    """A real opinion filed under a cite belonging to a different case resolves
    every other check and is still a fabricated cite."""
    other = b"<html><body>Smith v. Board, 12 F.4th 34 (2d Cir. 2021). Something else.</body></html>"
    out = (corpus.archive("901f3d1099", body=other).treatment("901f3d1099", "good")
           .note("901f3d1099", [claim("901f3d1099", quote="Something else.")]).commit())

    assert "not the same authority" in reasons(verify.run(out))


def test_a_repunctuated_citation_in_the_document_still_matches(verify, corpus):
    body = b"<html><body>Roe v. Wade, 410 U. S. 113, 118 (1973). The right is qualified.</body></html>"
    out = (corpus.archive("410us113", body=body, citation="410 U.S. 113")
           .treatment("410us113", "good")
           .note("410us113", [claim("410us113", quote="The right is qualified.")]).commit())

    assert verify.run(out)["failures"] == []


def test_the_citation_check_is_unverifiable_in_a_pdf(verify, corpus):
    out = (corpus.archive("901f3d1099", body=MINIMAL_PDF, suffix=".pdf")
           .treatment("901f3d1099", "good")
           .note("901f3d1099", [claim("901f3d1099", page=2)]).commit())

    assert "cannot be located in a binary artifact" in caveats(verify.run(out))


def test_a_statute_is_not_subjected_to_the_reporter_citation_check(verify, corpus):
    body = b"<?xml version='1.0'?><P>Each agency shall make available...</P>"
    out = (corpus.archive("5usc552", body=body, kind="statute", citation="5 U.S.C. 552",
                          suffix=".xml", as_of="2026-01-01")
           .note("5usc552", [claim("5usc552", basis="statutory_text",
                                   quote="Each agency shall make available...")])
           .commit())

    assert verify.run(out)["failures"] == []


# --- is it still good law -----------------------------------------------

def test_a_claim_resting_on_overruled_authority_fails(verify, corpus):
    out = (corpus.archive("901f3d1099")
           .treatment("901f3d1099", "negative",
                      signals=[{"severity": "negative", "label": "overruled"}])
           .note("901f3d1099", [claim("901f3d1099", quote="The privilege protects drafts.")])
           .commit())

    result = verify.run(out)

    assert "negative treatment" in reasons(result)
    assert "overruled" in reasons(result)


def test_a_treatment_ack_lets_a_narrowed_authority_be_cited(verify, corpus):
    """Citing an overruled case is sometimes right; it just has to be said out loud."""
    out = (corpus.archive("901f3d1099")
           .treatment("901f3d1099", "negative",
                      signals=[{"severity": "negative", "label": "overruled"}])
           .note("901f3d1099", [claim(
               "901f3d1099", quote="The privilege protects drafts.",
               ack="Overruled on other grounds by Jones; the Exemption 5 holding stands.")])
           .commit())

    assert verify.run(out)["failures"] == []


def test_caution_is_unverifiable_rather_than_a_failure(verify, corpus):
    out = (corpus.archive("901f3d1099")
           .treatment("901f3d1099", "caution",
                      signals=[{"severity": "caution", "label": "distinguished"}])
           .note("901f3d1099", [claim("901f3d1099", quote="The privilege protects drafts.")])
           .commit())

    result = verify.run(out)

    assert result["failures"] == []
    assert "treatment is caution" in caveats(result)


def test_an_unvalidated_authority_is_unverifiable_not_clean(verify, corpus):
    out = (corpus.archive("901f3d1099")
           .treatment("901f3d1099", "unchecked", reason="nothing has cited it")
           .note("901f3d1099", [claim("901f3d1099", quote="The privilege protects drafts.")])
           .commit())

    result = verify.run(out)

    assert result["failures"] == []
    assert "nothing has cited it" in caveats(result)


def test_a_case_with_no_treatment_record_at_all_is_flagged(verify, corpus):
    """Nothing has asked whether it is still good law, and silence is not an answer."""
    out = (corpus.archive("901f3d1099")
           .note("901f3d1099", [claim("901f3d1099", quote="The privilege protects drafts.")])
           .commit())

    assert "no treatment record" in caveats(verify.run(out))


# --- the report's own links ---------------------------------------------

def test_a_report_link_to_nothing_on_disk_fails(verify, corpus):
    out = corpus.archive("901f3d1099").treatment("901f3d1099", "good").commit()
    report = out / "summary.html"
    report.write_text('<a href="docs/case/missing.html">Doe</a>', encoding="utf-8")

    assert "not on disk" in reasons(verify.run(out, report=report))


def test_a_deep_link_to_a_page_resolves_to_the_file(verify, corpus):
    """Failing this would have the author delete the most useful part of the citation."""
    out = corpus.archive("901f3d1099").treatment("901f3d1099", "good").commit()
    report = out / "summary.html"
    report.write_text('<a href="docs/case/901f3d1099.html#page=4">Doe</a>', encoding="utf-8")

    assert verify.run(out, report=report)["failures"] == []


def test_single_quoted_hrefs_are_checked_too(verify, corpus):
    out = corpus.archive("901f3d1099").commit()
    report = out / "summary.html"
    report.write_text("<a href='docs/case/missing.html'>Doe</a>", encoding="utf-8")

    assert verify.run(out, report=report)["failures"]


# --- the gate's own failure modes ---------------------------------------

def test_no_notes_means_the_gate_proves_nothing(verify, corpus):
    out = corpus.archive("901f3d1099").commit()

    assert verify.run(out)["checked"] == 0


def test_a_missing_manifest_is_a_clear_stop(verify, tmp_path):
    with pytest.raises(SystemExit, match="no manifest.json"):
        verify.run(tmp_path)


def test_the_cli_exits_non_zero_on_a_failure(verify, corpus, run_script):
    out = corpus.note("ghost", [claim("ghost", page=1)]).commit()

    result = run_script(SCRIPTS / "verify_authorities.py", "--out", out, expect_rc=1)

    assert "unresolvable" in result.stdout


def test_the_cli_exits_zero_on_a_clean_corpus(verify, corpus, run_script):
    out = (corpus.archive("901f3d1099").treatment("901f3d1099", "good")
           .note("901f3d1099", [claim("901f3d1099", quote="The privilege protects drafts.")])
           .commit())

    run_script(SCRIPTS / "verify_authorities.py", "--out", out, expect_rc=0)


def test_a_broken_note_stops_the_gate_with_an_explanation(verify, corpus, run_script):
    """A traceback out of a blocking gate reads as the gate breaking, and the
    difference decides whether anyone fixes the note."""
    out = corpus.archive("901f3d1099").commit()
    (out / "docs" / "notes").mkdir(parents=True, exist_ok=True)
    (out / "docs" / "notes" / "bad.json").write_text("{not json", encoding="utf-8")

    result = run_script(SCRIPTS / "verify_authorities.py", "--out", out, expect_rc=1)

    assert "the gate did not run" in result.stderr
