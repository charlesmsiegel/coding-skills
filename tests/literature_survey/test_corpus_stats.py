"""The masthead numbers, computed rather than asserted.

Every stat in the report's header comes from here, which makes this the one
script whose job is to establish that the rest of the document can be trusted.
Two properties carry that: nothing is counted that is not on disk, and anything
that cannot be computed comes back *unknown* — "no publication dates at all" and
"0% recent" are different facts, and only one of them is about the literature.
"""

import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "literature-survey" / "scripts"


@pytest.fixture
def stats(load_module):
    return load_module(SCRIPTS, "corpus_stats")


def ok(artifact_id, kind="paper", size=1000):
    return {"artifact_id": artifact_id, "kind": kind, "url": "https://x", "status": "ok",
            "path": "docs/" + kind + "/" + artifact_id, "sha256": "h", "bytes_len": size}


def gap(artifact_id, status="paywalled"):
    return {"artifact_id": artifact_id, "kind": "paper", "url": "https://x", "status": status,
            "failure_reason": "HTTP 403"}


def survey(out: Path, artifacts=(), notes=(), candidates=None, snowball=None) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps({"artifacts": list(artifacts)}), encoding="utf-8")
    if notes:
        notes_dir = out / "docs" / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        for artifact in notes:
            (notes_dir / (artifact + ".json")).write_text(
                '{"artifact_id": "' + artifact + '"}', encoding="utf-8")
    if candidates is not None:
        (out / "candidates.json").write_text(json.dumps({"candidates": candidates}),
                                             encoding="utf-8")
    if snowball is not None:
        (out / "snowball.json").write_text(json.dumps(snowball), encoding="utf-8")
    return out


def test_only_obtained_artifacts_are_counted_by_kind(stats, tmp_path):
    survey(tmp_path, artifacts=[ok("a"), ok("b"), gap("c"),
                                ok("d", kind="web", size=50), ok("e", kind="repo", size=0)])

    computed = stats.compute(tmp_path)

    assert computed["papers"] == {"count": 2, "bytes": 2000}
    assert computed["web"] == {"count": 1, "bytes": 50}
    assert computed["repos"] == {"count": 1, "bytes": 0}
    assert computed["archived"] == 4
    assert computed["gaps"] == {"count": 1, "by_status": {"paywalled": 1}}


def test_gaps_are_broken_down_by_why_they_are_gaps(stats, tmp_path):
    survey(tmp_path, artifacts=[gap("a"), gap("b", "failed"), gap("c", "robots_blocked"),
                                gap("d", "failed")])

    assert stats.compute(tmp_path)["gaps"]["by_status"] == {"failed": 2, "paywalled": 1,
                                                            "robots_blocked": 1}


def test_read_in_full_counts_notes_not_downloads(stats, tmp_path):
    """Downloading is not reading, and the gap between the two is the thing worth reporting."""
    survey(tmp_path, artifacts=[ok("a"), ok("b"), ok("c")], notes=["a"])

    computed = stats.compute(tmp_path)

    assert computed["read_in_full"] == 1 and computed["archived"] == 3


def test_read_in_full_is_measured_against_every_archived_artifact(stats, tmp_path):
    """Notes cover pages and repos too, so a paper-only denominator produced a ratio above 1."""
    survey(tmp_path, artifacts=[ok("a"), ok("b", kind="web")], notes=["a", "b"])

    strip = stats.meta_strip_html(stats.compute(tmp_path))

    assert "of 2 archived" in strip


def test_recency_is_unknown_rather_than_zero_when_nothing_carries_a_year(stats, tmp_path):
    survey(tmp_path, artifacts=[ok("a")], candidates=[{"title": "T"}])

    computed = stats.compute(tmp_path)

    assert computed["recency"]["percent"] is None
    assert computed["recency"]["undated"] == 1
    assert "no publication dates" in stats.meta_strip_html(computed)


def test_recency_is_a_share_of_the_dated_candidates_only(stats, tmp_path):
    survey(tmp_path, artifacts=[ok("a")], candidates=[
        {"title": "A", "year": 2026}, {"title": "B", "year": 2025},
        {"title": "C", "year": 2001}, {"title": "D"},
    ])

    computed = stats.compute(tmp_path, current_year=2026)

    assert computed["recency"] == {"recent": 2, "dated": 3, "undated": 1, "percent": 67}
    assert "of 3 dated" in stats.meta_strip_html(computed)


def test_a_cap_stop_is_reported_as_a_cap_stop(stats, tmp_path):
    survey(tmp_path, artifacts=[ok("a")],
           snowball={"rounds": [{"round": 1}, {"round": 2}], "stopped_because": "cap_reached"})

    computed = stats.compute(tmp_path)

    assert computed["saturation"] == {"rounds": 2, "stopped_because": "cap_reached",
                                      "saturated": False}
    assert "stopped at the cap, not saturated" in stats.meta_strip_html(computed)


def test_saturation_is_only_claimed_when_the_snowball_said_so(stats, tmp_path):
    survey(tmp_path, artifacts=[ok("a")],
           snowball={"rounds": [{"round": 1}], "stopped_because": "saturated"})

    assert stats.compute(tmp_path)["saturation"]["saturated"] is True
    assert "saturated" in stats.meta_strip_html(stats.compute(tmp_path))


def test_a_survey_that_never_snowballed_says_so_rather_than_showing_zero_rounds(stats, tmp_path):
    survey(tmp_path, artifacts=[ok("a")])

    computed = stats.compute(tmp_path)

    assert computed["saturation"]["stopped_because"] == "not-run"
    assert "no snowball rounds run" in stats.meta_strip_html(computed)


def test_a_survey_with_no_manifest_is_an_error_not_an_empty_corpus(stats, tmp_path):
    with pytest.raises(SystemExit, match="manifest.json"):
        stats.compute(tmp_path)


def test_the_strip_carries_every_masthead_cell(stats, tmp_path):
    survey(tmp_path, artifacts=[ok("a"), gap("b")], notes=["a"],
           candidates=[{"title": "A", "year": 2026}],
           snowball={"rounds": [{"round": 1}], "stopped_because": "saturated"})

    strip = stats.meta_strip_html(stats.compute(tmp_path))

    for label in ("Read in full", "Papers archived", "Repositories cloned",
                  "Web pages &amp; threads", "Corpus recency", "Snowball", "Unobtainable"):
        assert label in strip
    assert strip.startswith('<div class="meta-strip">') and strip.endswith("</div>")


def test_bytes_are_rendered_at_human_scale(stats):
    assert stats.human_bytes(512) == "512 B"
    assert stats.human_bytes(2048) == "2.0 KB"
    assert stats.human_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_the_cli_writes_both_the_json_and_the_strip(stats, tmp_path, monkeypatch, capsys):
    out = survey(tmp_path / "survey", artifacts=[ok("a")], notes=["a"])
    meta, strip = tmp_path / "meta.json", tmp_path / "strip.html"
    monkeypatch.setattr("sys.argv", ["corpus_stats.py", "--out", str(out),
                                     "--json-out", str(meta), "--html-out", str(strip)])

    assert stats.main() == 0

    assert json.loads(meta.read_text(encoding="utf-8"))["read_in_full"] == 1
    assert "meta-strip" in strip.read_text(encoding="utf-8")
    assert capsys.readouterr().out.startswith("1 read in full of 1 archived")


def test_downloading_without_reading_is_called_out(stats, tmp_path, monkeypatch, capsys):
    out = survey(tmp_path / "survey", artifacts=[ok("a"), ok("b")])
    monkeypatch.setattr("sys.argv", ["corpus_stats.py", "--out", str(out)])

    stats.main()

    assert "downloads are not readings" in capsys.readouterr().out
