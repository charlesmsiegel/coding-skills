"""Assembly and masthead injection, end to end over the documented commands.

assemble.py is the shared assembler — byte-identical to the other report-building
skills' copies but for its --label default, which is asserted here. The computed
strip goes in afterwards at a marker, because the alternative was a raw-HTML flag
on the shared assembler that only these skills need.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "legal-survey"
SCRIPTS = SKILL / "scripts"


@pytest.fixture
def inject(load_module):
    return load_module(SCRIPTS, "inject_masthead")


def build_report(tabs: Path, out: Path, **flags) -> subprocess.CompletedProcess:
    args = [sys.executable, str(SCRIPTS / "assemble.py"), "--tabs-dir", str(tabs),
            "--out", str(out), "--title", flags.pop("title", "A Legal Question")]
    for key, value in flags.items():
        args += ["--" + key.replace("_", "-"), value]
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return result


def fragment(tabs: Path, name: str, title: str, body: str) -> None:
    (tabs / name).write_text("<!-- tab: " + title + " -->\n" + body, encoding="utf-8")


def test_fragments_become_tabs_in_filename_order(tabs, tmp_path):
    fragment(tabs, "01-summary.html", "Summary", "<p>The answer.</p>")
    fragment(tabs, "92-authorities.html", "Authorities", "<p>The list.</p>")
    fragment(tabs, "02-method.html", "Question and method", "<p>How.</p>")
    out = tmp_path / "summary.html"

    build_report(tabs, out)

    html = out.read_text(encoding="utf-8")
    order = [re.search(r'data-tab="' + slug + r'"', html).start()
             for slug in ("summary", "method", "authorities")]
    assert order == sorted(order)


def test_the_first_tab_is_the_selected_one(tabs, tmp_path):
    fragment(tabs, "01-summary.html", "Summary", "<p>a</p>")
    fragment(tabs, "02-method.html", "Method", "<p>b</p>")
    out = tmp_path / "summary.html"

    build_report(tabs, out)

    html = out.read_text(encoding="utf-8")
    assert 'data-tab="summary" aria-selected="true"' in html
    assert 'data-tab="method" aria-selected="false"' in html


def test_an_empty_fragment_is_skipped(tabs, tmp_path):
    fragment(tabs, "01-summary.html", "Summary", "<p>a</p>")
    fragment(tabs, "50-nothing.html", "Nothing", "   \n")
    out = tmp_path / "summary.html"

    build_report(tabs, out)

    assert 'data-tab="nothing"' not in out.read_text(encoding="utf-8")


def test_no_fragments_at_all_is_an_error(tabs, tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "assemble.py"), "--tabs-dir", str(tabs),
         "--out", str(tmp_path / "x.html"), "--title", "t"],
        capture_output=True, text=True, timeout=120)

    assert result.returncode == 1


def test_the_assembler_defaults_to_this_skills_label(tabs, tmp_path):
    """The copies of assemble.py differ in exactly this one line, and CI checks it."""
    text = (SCRIPTS / "assemble.py").read_text(encoding="utf-8")

    assert 'ap.add_argument("--label", default="LEGAL SURVEY")' in text


def test_assemble_is_otherwise_identical_to_the_shared_copy():
    ours = (SCRIPTS / "assemble.py").read_text(encoding="utf-8")
    theirs = (ROOT / "skills" / "literature-survey" / "scripts" / "assemble.py").read_text(
        encoding="utf-8")

    assert (ours.replace("LEGAL SURVEY", "X")
            == theirs.replace("LITERATURE SURVEY", "X")), (
        "the shared assembler has drifted; a divergent copy is a second assembler "
        "nobody knows they are maintaining")


def test_the_footer_carries_the_not_legal_advice_line(tabs, tmp_path):
    """It is injected as raw HTML, which is why it must never carry fetched text."""
    fragment(tabs, "01-summary.html", "Summary", "<p>a</p>")
    out = tmp_path / "summary.html"

    build_report(tabs, out,
                 footer="This report is legal research, <em>not legal advice</em>.")

    assert "not legal advice" in out.read_text(encoding="utf-8")


def test_the_dek_reaches_the_masthead(tabs, tmp_path):
    fragment(tabs, "01-summary.html", "Summary", "<p>a</p>")
    out = tmp_path / "summary.html"

    build_report(tabs, out, subtitle="The two-part test decides almost nothing.")

    assert "The two-part test decides almost nothing." in out.read_text(encoding="utf-8")


# --- masthead injection --------------------------------------------------

def test_the_strip_and_meta_land_at_their_markers(inject, tabs, tmp_path):
    fragment(tabs, "01-summary.html", "Summary", "<p>a</p>")
    out = tmp_path / "summary.html"
    build_report(tabs, out)

    inject.run(out, '<div class="meta-strip">computed</div>', {"cases": {"count": 3}})

    html = out.read_text(encoding="utf-8")
    assert '<div class="meta-strip">computed</div>' in html
    assert 'id="legal-survey-meta"' in html


def test_injection_replaces_rather_than_stacking(inject, tabs, tmp_path):
    fragment(tabs, "01-summary.html", "Summary", "<p>a</p>")
    out = tmp_path / "summary.html"
    build_report(tabs, out)

    inject.run(out, "<div>STRIP-ONE</div>", {"n": 1})
    inject.run(out, "<div>STRIP-TWO</div>", {"n": 2})

    html = out.read_text(encoding="utf-8")
    assert "STRIP-ONE" not in html and "STRIP-TWO" in html
    assert html.count('id="legal-survey-meta"') == 1


def test_a_script_close_inside_the_meta_cannot_escape_the_block(inject, tabs, tmp_path):
    fragment(tabs, "01-summary.html", "Summary", "<p>a</p>")
    out = tmp_path / "summary.html"
    build_report(tabs, out)

    inject.run(out, "<div>x</div>", {"note": "</script><img src=x>"})

    assert "</script><img src=x>" not in out.read_text(encoding="utf-8")


def test_injecting_into_a_foreign_document_says_why_it_cannot(inject, tmp_path):
    report = tmp_path / "other.html"
    report.write_text("<html><body>not from this template</body></html>", encoding="utf-8")

    with pytest.raises(SystemExit, match="nowhere to put the computed numbers"):
        inject.run(report, "<div>x</div>", {})


# --- the whole documented sequence ---------------------------------------

def test_the_documented_pipeline_runs_in_the_documented_order(tabs, tmp_path, run_script):
    """corpus_stats -> assemble -> inject_masthead -> verify_authorities."""
    out = tmp_path / "out"
    out.mkdir()
    body = b"<html><body>Doe v. Agency, 901 F.3d 1099. The privilege protects drafts.</body></html>"
    (out / "docs" / "case").mkdir(parents=True)
    (out / "docs" / "case" / "doe.html").write_bytes(body)
    import hashlib
    (out / "manifest.json").write_text(json.dumps({"artifacts": [{
        "artifact_id": "901f3d1099", "kind": "case", "url": "u", "status": "ok",
        "path": "docs/case/doe.html", "sha256": hashlib.sha256(body).hexdigest(),
        "bytes_len": len(body), "citation": "901 F.3d 1099", "as_of": "",
    }]}), encoding="utf-8")
    (out / "treatment.json").write_text(json.dumps({"treatments": [
        {"artifact_id": "901f3d1099", "status": "good"}]}), encoding="utf-8")
    (out / "docs" / "notes").mkdir(parents=True)
    (out / "docs" / "notes" / "901f3d1099.json").write_text(json.dumps({
        "artifact_id": "901f3d1099",
        "claims": [{"text": "drafts are protected", "basis": "holding", "treatment_ack": "",
                    "locators": [{"artifact_id": "901f3d1099",
                                  "quote": "The privilege protects drafts."}]}],
    }), encoding="utf-8")

    fragment(tabs, "01-summary.html", "Summary",
             '<p>See <a href="docs/case/doe.html">Doe</a>.</p>')

    run_script(SCRIPTS / "corpus_stats.py", "--out", out,
               "--json-out", out / "meta.json", "--html-out", out / "strip.html")
    build_report(tabs, out / "summary.html", subtitle="It decides almost nothing.")
    run_script(SCRIPTS / "inject_masthead.py", "--report", out / "summary.html",
               "--strip", out / "strip.html", "--meta", out / "meta.json")
    run_script(SCRIPTS / "verify_authorities.py", "--out", out,
               "--report", out / "summary.html", expect_rc=0)

    html = (out / "summary.html").read_text(encoding="utf-8")
    assert "Read in full" in html
    assert "not a citator" in html


def test_the_template_carries_the_markers_injection_needs():
    text = (SKILL / "assets" / "template.html").read_text(encoding="utf-8")

    for marker in ("<!--META_STRIP-->", "<!--SURVEY_META_JSON-->", "<!--TABS_NAV-->",
                   "<!--TABS_PANELS-->", "<!--DOC_FOOTER-->"):
        assert marker in text, marker + " missing from the shell"


def test_the_template_matches_the_skill_it_was_copied_from():
    """The reports sit side by side; a divergent palette is visible immediately."""
    ours = (SKILL / "assets" / "template.html").read_bytes()
    theirs = (ROOT / "skills" / "literature-survey" / "assets" / "template.html").read_bytes()

    assert ours == theirs


def test_no_source_file_carries_a_stray_control_character():
    """A shell heredoc once wrote a literal backspace where a regex escape belonged,
    and the pattern silently matched nothing."""
    offenders = []
    for path in list(SKILL.rglob("*.py")) + list(SKILL.rglob("*.md")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
            offenders.append(str(path.relative_to(ROOT)))

    assert not offenders, offenders
