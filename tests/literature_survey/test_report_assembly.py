"""Assembling the report, and injecting numbers nobody typed.

assemble.py is the shared assembler — byte-identical to the visualization
skills' copy but for its --label default, which CI pins — so what is tested here
is the part that is this skill's own: the masthead injection. It exists because
the alternative was a raw-HTML flag on the shared assembler, and a flag one skill
needs is how three copies of a file start to drift.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[2] / "skills" / "literature-survey"
SCRIPTS = SKILL / "scripts"


@pytest.fixture
def inject(load_module):
    return load_module(SCRIPTS, "inject_masthead")


def build_report(tabs_dir: Path, out: Path, *, subtitle="The bottleneck is measurement.") -> Path:
    tabs_dir.mkdir(parents=True, exist_ok=True)
    (tabs_dir / "01-answer.html").write_text(
        "<!-- tab: The answer -->\n<p>Retrieval wins on recall.</p>\n", encoding="utf-8")
    (tabs_dir / "02-gaps.html").write_text(
        "<!-- tab: Gaps -->\n<p>Two papers were paywalled.</p>\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPTS / "assemble.py"),
                             "--tabs-dir", str(tabs_dir), "--out", str(out),
                             "--title", "Team knowledge", "--subtitle", subtitle],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return out


STATS = {"read_in_full": 2, "archived": 3, "gaps": {"count": 1}}
STRIP = '<div class="meta-strip"><div><span class="k">Read in full</span></div></div>'


# --- the template the skill ships ---------------------------------------

def test_the_template_carries_every_slot_the_two_scripts_write_into():
    text = (SKILL / "assets" / "template.html").read_text(encoding="utf-8")

    for slot in ("<!--DOC_TITLE-->", "<!--DOC_LABEL-->", "<!--DOC_SUBTITLE-->", "<!--DOC_META-->",
                 "<!--DOC_FOOTER-->", "<!--TABS_NAV-->", "<!--TABS_PANELS-->",
                 "<!--META_STRIP-->", "<!--SURVEY_META_JSON-->"):
        assert slot in text, slot + " missing — the pipeline would have nowhere to put its output"


def test_the_template_styles_the_strip_corpus_stats_emits():
    """The strip is generated markup; a class the theme does not know about renders as a list."""
    text = (SKILL / "assets" / "template.html").read_text(encoding="utf-8")

    for selector in (".meta-strip", ".meta-strip .k", ".meta-strip .v"):
        assert selector in text


def test_the_assembler_defaults_to_this_skills_label():
    """The three copies of assemble.py differ in exactly this one line, and CI checks it."""
    text = (SCRIPTS / "assemble.py").read_text(encoding="utf-8")

    assert '"--label", default="LITERATURE SURVEY"' in text.replace("'", '"')


def test_a_built_report_is_one_self_contained_file(tmp_path):
    report = build_report(tmp_path / "tabs", tmp_path / "summary.html")
    text = report.read_text(encoding="utf-8")

    assert "The answer" in text and "Gaps" in text
    assert "LITERATURE SURVEY" in text
    assert "src=\"http" not in text and "<link rel=\"stylesheet\"" not in text


# --- injection -----------------------------------------------------------

def test_the_computed_strip_and_meta_land_in_the_report(inject, tmp_path):
    report = build_report(tmp_path / "tabs", tmp_path / "summary.html")

    inject.run(report, STRIP, STATS)

    text = report.read_text(encoding="utf-8")
    assert "Read in full" in text
    assert '<script type="application/json" id="literature-survey-meta">' in text
    assert '"read_in_full": 2' in text


def test_a_second_run_replaces_the_numbers_rather_than_stacking_them(inject, tmp_path):
    """A report re-built after another snowball round must not show two mastheads."""
    report = build_report(tmp_path / "tabs", tmp_path / "summary.html")
    inject.run(report, STRIP, STATS)

    inject.run(report, STRIP.replace("Read in full", "Read in full (updated)"),
               {**STATS, "read_in_full": 3})

    text = report.read_text(encoding="utf-8")
    assert text.count('<div class="meta-strip">') == 1, "two mastheads is two sets of numbers"
    assert "Read in full (updated)" in text
    assert text.count('id="literature-survey-meta"') == 1
    assert '"read_in_full": 3' in text and '"read_in_full": 2' not in text


def test_a_meta_value_containing_a_closing_script_tag_cannot_escape_the_block(inject, tmp_path):
    """Otherwise a paper titled '</script><img onerror=...>' spills markup into the report."""
    report = build_report(tmp_path / "tabs", tmp_path / "summary.html")

    inject.run(report, STRIP, {**STATS, "title": "</script><script>alert(1)</script>"})

    text = report.read_text(encoding="utf-8")
    block = text.split('id="literature-survey-meta">', 1)[1].split("</script>", 1)[0]
    assert "alert(1)" in block, "the payload stays inside the JSON block"
    assert "<\\/script>" in block


def test_the_injected_json_is_still_json(inject, tmp_path):
    report = build_report(tmp_path / "tabs", tmp_path / "summary.html")

    inject.run(report, STRIP, STATS)

    text = report.read_text(encoding="utf-8")
    block = text.split('id="literature-survey-meta">', 1)[1].split("</script>", 1)[0]
    assert json.loads(block.replace("<\\/", "</"))["archived"] == 3


def test_injecting_into_a_foreign_document_says_what_is_wrong(inject, tmp_path):
    """Silently doing nothing would leave a report whose masthead is simply absent."""
    stray = tmp_path / "someone-elses.html"
    stray.write_text("<html><body>no markers here</body></html>", encoding="utf-8")

    with pytest.raises(SystemExit, match="built from this skill's template"):
        inject.run(stray, STRIP, STATS)


def test_the_cli_wires_the_three_files_together(tmp_path):
    report = build_report(tmp_path / "tabs", tmp_path / "summary.html")
    strip = tmp_path / "strip.html"
    strip.write_text(STRIP, encoding="utf-8")
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps(STATS), encoding="utf-8")

    result = subprocess.run([sys.executable, str(SCRIPTS / "inject_masthead.py"),
                             "--report", str(report), "--strip", str(strip), "--meta", str(meta)],
                            capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr
    assert "Read in full" in report.read_text(encoding="utf-8")


def test_the_whole_report_pipeline_runs_end_to_end(tmp_path):
    """corpus_stats -> assemble -> inject_masthead, exactly as SKILL.md documents it."""
    out = tmp_path / "survey"
    (out / "docs" / "notes").mkdir(parents=True)
    (out / "docs" / "papers").mkdir(parents=True)
    (out / "docs" / "papers" / "p.pdf").write_bytes(b"%PDF-1.4\n/Type /Page\n")
    (out / "manifest.json").write_text(json.dumps({"artifacts": [
        {"artifact_id": "2401.1", "kind": "paper", "url": "https://x", "status": "ok",
         "path": "docs/papers/p.pdf", "sha256": "h", "bytes_len": 4096}]}), encoding="utf-8")
    (out / "docs" / "notes" / "2401.1.json").write_text('{"artifact_id": "2401.1"}',
                                                        encoding="utf-8")

    stats = subprocess.run([sys.executable, str(SCRIPTS / "corpus_stats.py"), "--out", str(out),
                            "--json-out", str(out / "meta.json"),
                            "--html-out", str(out / "strip.html")],
                           capture_output=True, text=True, timeout=120)
    assert stats.returncode == 0, stats.stderr

    report = build_report(out / "tabs", out / "summary.html")
    injected = subprocess.run([sys.executable, str(SCRIPTS / "inject_masthead.py"),
                               "--report", str(report), "--strip", str(out / "strip.html"),
                               "--meta", str(out / "meta.json")],
                              capture_output=True, text=True, timeout=120)
    assert injected.returncode == 0, injected.stderr

    text = report.read_text(encoding="utf-8")
    assert "of 1 archived" in text, "the masthead reports what the manifest and notes say"
    assert "no snowball rounds run" in text, "an unrun stage is named, not rendered as zero"
    assert '"read_in_full": 1' in text
