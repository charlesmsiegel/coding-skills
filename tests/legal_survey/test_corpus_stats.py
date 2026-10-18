"""The masthead numbers, which the report presents as the ones that can be checked.

The treatment cell is the figure a reader will act on, so most of these tests are
about it: it must never render as a citator's clean flag, an uncounted case must
never read as a validated one, and "nothing was checked" must not look like
"nothing was wrong".
"""

import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "legal-survey" / "scripts"


@pytest.fixture
def stats(load_module):
    return load_module(SCRIPTS, "corpus_stats")


def build(out: Path, artifacts=None, notes=None, authorities=None, treatments=None,
          snowball=None, forum="Ninth Circuit") -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps({"artifacts": artifacts or []}), encoding="utf-8")
    if authorities is not None:
        (out / "candidates.json").write_text(
            json.dumps({"forum": forum, "as_of": "2026-09-04", "authorities": authorities}),
            encoding="utf-8")
    if treatments is not None:
        (out / "treatment.json").write_text(
            json.dumps({"is_citator": False, "treatments": treatments}), encoding="utf-8")
    if snowball is not None:
        (out / "snowball.json").write_text(json.dumps(snowball), encoding="utf-8")
    for note in notes or []:
        notes_dir = out / "docs" / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        (notes_dir / (note + ".json")).write_text(
            json.dumps({"artifact_id": note}), encoding="utf-8")


def ok(aid, kind="case", size=1000, as_of=""):
    row = {"artifact_id": aid, "kind": kind, "url": "u", "status": "ok",
           "path": "docs/" + kind + "/" + aid + ".html", "sha256": "h", "bytes_len": size,
           "as_of": as_of, "citation": ""}
    return row


def selected(aid, weight="binding"):
    return {"title": aid, "authority_type": "case", "external_ids": {}, "citation": "",
            "court": "", "jurisdiction": "", "decided": "", "precedential_status": "",
            "authors": [], "venue": "", "citation_count": None, "summary": "", "as_of": "",
            "pdf_url": "", "landing_url": "", "sources": [], "status": "selected",
            "drop_reason": "", "weight": weight, "weight_reason": "9th Cir."}


def test_the_kinds_are_counted_from_the_manifest(stats, tmp_path):
    build(tmp_path, artifacts=[
        ok("a"), ok("b"),
        ok("s", kind="statute", as_of="2026-01-01"),
        ok("r", kind="regulation", as_of="2026-01-01"),
        ok("x", kind="secondary"),
    ])

    result = stats.compute(tmp_path)

    assert result["cases"]["count"] == 2
    assert result["codified"]["count"] == 2
    assert result["secondary"]["count"] == 1


def test_read_in_full_is_measured_against_every_archived_artifact(stats, tmp_path):
    """Comparing notes against the case count alone produced a ratio above one."""
    build(tmp_path, artifacts=[ok("a"), ok("s", kind="statute", as_of="2026-01-01")],
          notes=["a", "s"])

    result = stats.compute(tmp_path)

    assert result["read_in_full"] == 2
    assert result["archived"] == 2


def test_a_note_for_an_unarchived_authority_is_reported_not_counted(stats, tmp_path):
    """It is a reading that will fail the gate, and silence hides it until then."""
    build(tmp_path, artifacts=[ok("a")], notes=["a", "ghost"])

    result = stats.compute(tmp_path)

    assert result["read_in_full"] == 1
    assert result["orphan_notes"] == 1


def test_the_binding_split_comes_from_the_triage_weights(stats, tmp_path):
    build(tmp_path, artifacts=[ok("a")],
          authorities=[selected("a"), selected("b", "persuasive"),
                       selected("c", "secondary")])

    result = stats.compute(tmp_path)

    assert result["weights"] == {"binding": 1, "persuasive": 1, "secondary": 1}
    assert result["forum"] == "Ninth Circuit"


def test_an_unweighed_selection_is_visible_in_the_stats(stats, tmp_path):
    row = selected("a")
    row.update({"weight": "", "weight_reason": "", "status": "new"})
    build(tmp_path, artifacts=[ok("a")], authorities=[row])

    assert stats.compute(tmp_path)["selected"] == 0


# --- treatment ----------------------------------------------------------

def test_treatment_counts_come_from_the_treatment_file(stats, tmp_path):
    build(tmp_path, artifacts=[ok("a"), ok("b"), ok("c")], treatments=[
        {"artifact_id": "a", "status": "good"},
        {"artifact_id": "b", "status": "negative",
         "signals": [{"severity": "negative", "label": "overruled"}]},
        {"artifact_id": "c", "status": "unchecked", "reason": "nothing has cited it"},
    ])

    result = stats.compute(tmp_path)

    assert result["treatment"]["by_status"] == {"good": 1, "negative": 1, "unchecked": 1}
    assert result["treatment"]["checked"] == 3


def test_the_strip_never_renders_treatment_as_a_clean_flag(stats, tmp_path):
    """'12 good' with no qualifier reads as a citator's green flag, which this
    skill has no way to claim."""
    build(tmp_path, artifacts=[ok("a")],
          treatments=[{"artifact_id": "a", "status": "good"}])

    html = stats.meta_strip_html(stats.compute(tmp_path))

    assert "not a citator" in html


def test_an_unrun_treatment_check_says_so_rather_than_showing_zero(stats, tmp_path):
    """'No case was validated' and 'no case has a negative signal' are different facts."""
    build(tmp_path, artifacts=[ok("a")])

    result = stats.compute(tmp_path)
    html = stats.meta_strip_html(result)

    assert result["treatment"]["by_status"] == {}
    assert "check_treatment.py has not run" in html


def test_the_strip_reports_unvalidated_cases_alongside_the_negatives(stats, tmp_path):
    build(tmp_path, artifacts=[ok("a"), ok("b")], treatments=[
        {"artifact_id": "a", "status": "unchecked", "reason": "nothing has cited it"},
        {"artifact_id": "b", "status": "caution",
         "signals": [{"severity": "caution", "label": "distinguished"}]},
    ])

    html = stats.meta_strip_html(stats.compute(tmp_path))

    assert "1 unvalidated" in html
    assert "1 caution" in html


# --- gaps, currency and saturation --------------------------------------

def test_gaps_are_counted_by_status(stats, tmp_path):
    build(tmp_path, artifacts=[
        ok("a"),
        {"artifact_id": "b", "kind": "case", "url": "u", "status": "paywalled",
         "failure_reason": "403"},
        {"artifact_id": "c", "kind": "case", "url": "u", "status": "robots_blocked",
         "failure_reason": "disallowed"},
    ])

    result = stats.compute(tmp_path)

    assert result["gaps"]["count"] == 2
    assert result["gaps"]["by_status"] == {"paywalled": 1, "robots_blocked": 1}


def test_an_undated_regulation_is_surfaced(stats, tmp_path):
    """The manifest forbids it, but a hand-edited file can still carry one."""
    build(tmp_path, artifacts=[ok("r", kind="regulation", as_of="")])

    assert stats.compute(tmp_path)["undated"] == 1


def test_the_cap_is_not_rendered_as_saturation(stats, tmp_path):
    build(tmp_path, artifacts=[ok("a")],
          snowball={"rounds": [{"round": 1}], "stopped_because": "cap_reached"})

    result = stats.compute(tmp_path)
    html = stats.meta_strip_html(result)

    assert result["saturation"]["saturated"] is False
    assert "stopped at the cap, not saturated" in html


def test_saturation_is_rendered_when_it_is_real(stats, tmp_path):
    build(tmp_path, artifacts=[ok("a")],
          snowball={"rounds": [{"round": 1}, {"round": 2}], "stopped_because": "saturated"})

    html = stats.meta_strip_html(stats.compute(tmp_path))

    assert "saturated" in html


def test_a_missing_manifest_is_a_clear_stop(stats, tmp_path):
    with pytest.raises(SystemExit, match="no manifest.json"):
        stats.compute(tmp_path)


def test_human_bytes_is_readable(stats):
    assert stats.human_bytes(512) == "512 B"
    assert stats.human_bytes(2048) == "2.0 KB"
