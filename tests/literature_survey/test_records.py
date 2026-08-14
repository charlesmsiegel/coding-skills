"""The records that carry the skill's confidence discipline.

A survey's credibility rests on two asymmetries: something not read must say why
it was not read, and something not established must not be dressed as a finding.
The dataclasses enforce both in __post_init__, so a script cannot report an
absence as a fact even by accident. These tests exist because that enforcement is
the difference between a schema and a comment.
"""

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "literature-survey" / "scripts"


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS, "common")


# --- Candidate: a drop must carry its reason ------------------------------

def test_a_dropped_candidate_without_a_reason_is_rejected(common):
    with pytest.raises(ValueError, match="drop_reason"):
        common.Candidate(title="Transactional memory in multicore CPUs", status="dropped")


def test_a_dropped_candidate_with_a_reason_is_fine(common):
    candidate = common.Candidate(title="Software transactional memory", status="dropped",
                                 drop_reason="wrong field: computer architecture, not team cognition")

    assert candidate.status == "dropped"


def test_a_reason_on_a_kept_candidate_is_rejected(common):
    """Otherwise a selected paper carrying stale text reads as declined in the tab."""
    with pytest.raises(ValueError, match="only meaningful on a dropped candidate"):
        common.Candidate(title="Transactive memory systems", status="selected",
                         drop_reason="off-topic")


def test_an_unknown_status_is_rejected(common):
    with pytest.raises(ValueError, match="status must be one of"):
        common.Candidate(title="x", status="maybe")


def test_a_candidate_round_trips_through_its_dict(common):
    original = common.Candidate(title="Transactive memory systems", year=1985,
                                external_ids={"doi": "10.1007/978-1-4612-4634-3_9"},
                                status="selected")

    assert common.Candidate.from_dict(original.to_dict()) == original


# --- ManifestEntry: an unobtained artifact is a gap, never an absence -----

def test_a_failed_entry_must_say_why(common):
    with pytest.raises(ValueError, match="failure_reason"):
        common.ManifestEntry(artifact_id="1234.5678", kind="paper",
                             url="https://example.org/p.pdf", status="failed")


def test_a_failed_entry_may_not_claim_a_file(common):
    """A gap that also claims a path would pass the locator gate on a file that is not the paper."""
    with pytest.raises(ValueError, match="must not claim a path or sha256"):
        common.ManifestEntry(artifact_id="1234.5678", kind="paper", url="https://example.org/p.pdf",
                             status="paywalled", failure_reason="HTTP 403",
                             path="docs/papers/p.pdf")


def test_an_ok_entry_without_a_hash_is_rejected(common):
    with pytest.raises(ValueError, match="sha256"):
        common.ManifestEntry(artifact_id="1234.5678", kind="paper", url="u", status="ok",
                             path="docs/papers/p.pdf")


def test_an_ok_entry_without_a_path_is_rejected(common):
    with pytest.raises(ValueError, match="must carry a path"):
        common.ManifestEntry(artifact_id="1234.5678", kind="paper", url="u", status="ok",
                             sha256="abc")


def test_an_ok_entry_may_not_also_carry_a_failure(common):
    with pytest.raises(ValueError, match="must not carry a failure_reason"):
        common.ManifestEntry(artifact_id="a", kind="paper", url="u", status="ok",
                             path="p", sha256="h", failure_reason="HTTP 403")


@pytest.mark.parametrize("status,gap", [("ok", False), ("paywalled", True),
                                        ("robots_blocked", True), ("failed", True)])
def test_every_non_ok_status_counts_as_a_gap(common, status, gap):
    kwargs = {"path": "docs/papers/p.pdf", "sha256": "h"} if status == "ok" else {"failure_reason": "why"}
    entry = common.ManifestEntry(artifact_id="a", kind="paper", url="u", status=status, **kwargs)

    assert entry.is_gap is gap


# --- Locator / Claim / Lead: evidence, or say it is not evidence ----------

def test_an_artifact_id_alone_is_not_a_locator(common):
    with pytest.raises(ValueError, match="page, a section or a quote"):
        common.Locator(artifact_id="1234.5678")


@pytest.mark.parametrize("kwargs", [{"page": 4}, {"section": "5.2"}, {"quote": "we find no effect"}])
def test_any_one_of_page_section_or_quote_makes_a_locator(common, kwargs):
    assert common.Locator(artifact_id="1234.5678", **kwargs).artifact_id == "1234.5678"


def test_a_claim_without_a_locator_is_rejected(common):
    """This is the failure the whole skill exists to prevent, so it fails at construction."""
    with pytest.raises(ValueError, match="at least one locator"):
        common.Claim(text="shared mental models improve team performance")


def test_a_lead_must_carry_its_benign_readings(common):
    with pytest.raises(ValueError, match="also_explained_by"):
        common.Lead(text="the effect may be publication bias")


def test_a_lead_may_not_carry_locators(common):
    """Locators on a lead would present a guess as established."""
    with pytest.raises(ValueError, match="must not carry locators"):
        common.Lead(text="possible publication bias", also_explained_by=["small-sample noise"],
                    locators=[common.Locator(artifact_id="a", page=1)])


def test_a_lead_serializes_without_a_locators_key(common):
    lead = common.Lead(text="possible publication bias", also_explained_by=["small-sample noise"])

    assert lead.to_dict() == {"text": "possible publication bias",
                              "also_explained_by": ["small-sample noise"]}


# --- slugify / artifact_id: filenames a human can still read -------------

def test_a_slug_truncates_on_a_word_boundary(common):
    slug = common.slugify("transactive memory systems in distributed engineering teams", maxlen=30)

    assert not slug.endswith("-")
    assert len(slug) <= 30
    assert slug in "transactive-memory-systems-in-distributed-engineering-teams"


def test_an_en_dash_becomes_a_separator_rather_than_vanishing(common):
    """Deleting it fuses '1985-2010' into '19852010' — two numbers into a wrong one."""
    assert common.slugify("survey 1985–2010") == "survey-1985-2010"


def test_accents_are_folded_not_dropped(common):
    assert common.slugify("Théorie des jeux") == "theorie-des-jeux"


def test_a_title_of_pure_punctuation_still_yields_a_filename(common):
    assert common.slugify("!!! ???") == "untitled"


def test_artifact_id_prefers_arxiv_then_doi_then_openalex(common):
    both = common.Candidate(title="t", external_ids={"arxiv": "2401.00001", "doi": "10.1/x"})
    doi_only = common.Candidate(title="t", external_ids={"doi": "10.1145/3411764.3445092"})
    oa_only = common.Candidate(title="t", external_ids={"openalex": "W2741809807"})

    assert common.artifact_id(both) == "2401.00001"
    assert common.artifact_id(doi_only) == "10.1145_3411764.3445092", "a DOI slash cannot be a path"
    assert common.artifact_id(oa_only) == "W2741809807"


def test_an_id_less_candidate_hashes_stably_so_a_resumed_fetch_finds_its_file(common):
    first = common.Candidate(title="An untitled workshop note")
    second = common.Candidate(title="An untitled workshop note")

    assert common.artifact_id(first) == common.artifact_id(second)
    assert common.artifact_id(first) != common.artifact_id(common.Candidate(title="Another note"))


# --- notes: a note that will not parse must not read as "not read" -------

def test_load_notes_is_empty_when_nothing_has_been_read(common, tmp_path):
    assert common.load_notes(tmp_path) == []


def test_an_unparseable_note_raises_rather_than_being_skipped(common, tmp_path):
    """Skipping it would understate 'read in full', the most honest number in the report."""
    notes = tmp_path / "docs" / "notes"
    notes.mkdir(parents=True)
    (notes / "1234.5678.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="did not parse"):
        common.load_notes(tmp_path)


def test_a_note_loads_its_claims_and_leads(common, tmp_path):
    notes = tmp_path / "docs" / "notes"
    notes.mkdir(parents=True)
    (notes / "1234.5678.json").write_text(
        '{"artifact_id": "1234.5678",'
        ' "claims": [{"text": "no effect", "locators": [{"artifact_id": "1234.5678", "page": 7}]}],'
        ' "leads": [{"text": "maybe bias", "also_explained_by": ["small n"]}]}',
        encoding="utf-8")

    loaded = common.load_notes(tmp_path)

    assert len(loaded) == 1
    assert loaded[0].claims[0].locators[0].page == 7
    assert loaded[0].leads[0].also_explained_by == ["small n"]


# --- pdf_page_count: unknown is not zero ---------------------------------

def test_a_non_pdf_has_an_unknown_page_count(common):
    assert common.pdf_page_count(b"<html>hello</html>") is None


def test_the_pages_tree_node_is_not_counted_as_a_page(common):
    """/Pages shares the /Page prefix; counting it inflates every document by one."""
    data = b"%PDF-1.4\n/Type /Pages /Count 2\n/Type /Page\n/Type /Page\n"

    assert common.pdf_page_count(data) == 2


def test_a_pdf_with_no_page_objects_is_unknown_rather_than_zero(common):
    assert common.pdf_page_count(b"%PDF-1.4\ngarbage\n") is None
