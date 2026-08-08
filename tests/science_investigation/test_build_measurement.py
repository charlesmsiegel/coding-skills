"""The measurement document.

Every tab is rendered from the inventory, so the score on the page and the
table under it cannot disagree. The tests that matter most are the ones about
what the page refuses to claim: a null score renders as a dash, an unmeasurable
row appears in the denominator, and an invalid inventory produces no document
at all rather than a document with a flattering number on it.
"""

import json
import re
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[2] / "skills" / "science-investigation"
SCRIPT = SKILL / "scripts" / "build_measurement.py"
CV_TEMPLATE = Path(__file__).resolve().parents[2] / "skills" / "code-visualization" / "assets" / "template.html"


def token_blocks(text: str) -> str:
    """The :root token block and its light-mode twin, which must not drift."""
    root = re.search(r":root\{.*?\n\}", text, re.S)
    light = re.search(r"@media \(prefers-color-scheme: light\)\{.*?\n\s*\}\n\}", text, re.S)
    assert root and light, "template is missing its design-token blocks"
    return root.group(0) + "\n" + light.group(0)


def inventory(tmp_path, **overrides) -> Path:
    payload = {
        "schema": "measurement-inventory/1",
        "subject": "billing",
        "rows": [
            {"name": "judge_accuracy", "importance": 3,
             "importance_reason": "gates the weekly model rollout",
             "credit": 0.25, "credit_reason": "computed over 3 labelled rows of 412",
             "finding": "small_n", "n": 3, "n_total": 412,
             "formula": "mean(judge_score == gold)",
             "consumer": "scripts/rollout.py:88 gates the release",
             "evidence": ["evals/judge.py:41"], "status": "measured",
             "unmeasurable_reason": ""},
            {"name": "recall", "importance": 2,
             "importance_reason": "informs the retrieval roadmap",
             "credit": 0.0, "credit_reason": "no gold set exists", "finding": "",
             "n": None, "n_total": None, "formula": "", "consumer": "nobody",
             "evidence": ["evals/retrieval.py:12"], "status": "unmeasurable",
             "unmeasurable_reason": "no gold set exists, so recall cannot be computed"},
        ],
        "findings": [
            {"id": "small_n", "severity": "high", "title": "Judge accuracy rests on n=3",
             "detail": "3 of 412 rows carry a gold label.",
             "evidence": ["evals/judge.py:41"], "blast_radius": "the weekly rollout gate"},
        ],
        "not_audited": ["the analytics dashboard — no access"],
    }
    payload.update(overrides)
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def meta_of(page: Path) -> dict:
    text = page.read_text(encoding="utf-8")
    match = re.search(r'id="measurement-meta">(.*?)</script>', text, re.S)
    assert match, "the page carries no measurement-meta block"
    return json.loads(match.group(1).replace("<\\/", "</"))


def build(run_script, tmp_path, *extra, expect_rc=0) -> Path:
    out = tmp_path / "measurement.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path),
               "--name", "billing", *extra, expect_rc=expect_rc)
    return out


# --- the shell ------------------------------------------------------------

def test_the_design_tokens_are_byte_identical_to_code_visualizations():
    ours = (SKILL / "assets" / "template.html").read_text(encoding="utf-8")

    assert token_blocks(ours) == token_blocks(CV_TEMPLATE.read_text(encoding="utf-8")), (
        "the four documents in a code overview share one palette; this is what keeps them "
        "reading as one artifact instead of three tools' output"
    )


def test_the_shell_uses_code_visualizations_tab_contract():
    ours = (SKILL / "assets" / "template.html").read_text(encoding="utf-8")

    for slot in ("<!--DOC_TITLE-->", "<!--DOC_LABEL-->", "<!--DOC_SUBTITLE-->",
                 "<!--DOC_META-->", "<!--DOC_FOOTER-->", "<!--TABS_NAV-->",
                 "<!--TABS_PANELS-->"):
        assert slot in ours, f"{slot} is what lets code-overview force one shell on both generators"


# --- the document ---------------------------------------------------------

def test_the_page_carries_the_score_and_the_grade(run_script, tmp_path):
    page = build(run_script, tmp_path)

    meta = meta_of(page)
    # 3*0.25 + 2*0.0 = 0.75 over 5 → 15.
    assert meta["score"] == pytest.approx(15.0)
    assert meta["grade"] == "F"
    assert meta["schema"] == "measurement/1"
    assert meta["scope"] == "package"


def test_every_tab_is_present(run_script, tmp_path):
    text = build(run_script, tmp_path).read_text(encoding="utf-8")

    for tab in ("Score", "Inventory", "Findings", "Unmeasurable"):
        assert f">{tab}<" in text, f"the {tab} tab is missing"


def test_exactly_four_tabs_are_rendered_no_bogus_preamble_tab(run_script, tmp_path):
    text = build(run_script, tmp_path).read_text(encoding="utf-8")

    assert text.count('<button role="tab"') == 4, (
        "the scaffold's leading comment must not become a fifth, bogus tab"
    )


def test_the_score_panel_is_the_one_active_on_load(run_script, tmp_path):
    text = build(run_script, tmp_path).read_text(encoding="utf-8")

    match = re.search(r'<section class="panel active" id="([^"]+)"', text)
    assert match, "no panel is active on load"
    assert match.group(1) == "tab-score", (
        "the grade card must not be display:none behind a bogus first panel"
    )


def test_the_first_tab_button_is_score_and_selected(run_script, tmp_path):
    text = build(run_script, tmp_path).read_text(encoding="utf-8")

    match = re.search(r'<button role="tab"[^>]*>', text)
    assert match, "no tab button is rendered"
    first = match.group(0)
    assert 'aria-selected="true"' in first
    assert 'data-tab="tab-score"' in first


def test_the_inventory_table_shows_every_row_with_its_weight_and_credit(run_script, tmp_path):
    text = build(run_script, tmp_path).read_text(encoding="utf-8")

    assert "judge_accuracy" in text
    assert "recall" in text
    assert "gates the weekly model rollout" in text
    assert "evals/judge.py:41" in text


def test_a_reduced_credit_names_the_finding_that_caused_it(run_script, tmp_path):
    text = build(run_script, tmp_path).read_text(encoding="utf-8")

    assert "small_n" in text
    assert "Judge accuracy rests on n=3" in text


def test_the_unmeasurable_row_is_labelled_not_counted_as_a_defect(run_script, tmp_path):
    text = build(run_script, tmp_path).read_text(encoding="utf-8")
    meta = meta_of(tmp_path / "measurement.html")

    assert "no gold set exists" in text
    assert meta["weight_total"] == pytest.approx(5.0), "it stays in the denominator"
    assert [r["name"] for r in meta["rows"] if r["status"] == "unmeasurable"] == ["recall"]


def test_the_ship_gate_share_is_reported_separately(run_script, tmp_path):
    meta = meta_of(build(run_script, tmp_path))

    assert meta["by_importance"]["3"]["share"] == pytest.approx(25.0)


def test_an_empty_inventory_scores_null_and_says_so(run_script, tmp_path):
    out = tmp_path / "measurement.html"
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"schema": "measurement-inventory/1", "subject": "utils",
                                "rows": [], "findings": [], "not_audited": []}),
                    encoding="utf-8")
    run_script(SCRIPT, "--out", out, "--inventory", path, "--name", "utils")

    meta = meta_of(out)
    assert meta["score"] is None
    assert meta["grade"] == "—"
    assert "no measurement content" in out.read_text(encoding="utf-8").lower()


def test_an_invalid_inventory_writes_no_document(run_script, tmp_path):
    out = tmp_path / "measurement.html"
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({
        "schema": "measurement-inventory/1", "subject": "x",
        "rows": [{"name": "acc", "importance": 3, "importance_reason": "gates release",
                  "credit": 0.5, "credit_reason": "shaky", "finding": "", "n": 10,
                  "evidence": ["a.py:1"], "status": "measured"}],
        "findings": [], "not_audited": []}), encoding="utf-8")

    result = run_script(SCRIPT, "--out", out, "--inventory", path,
                        "--name", "x", expect_rc=2)

    assert not out.exists(), "a document with an unarguable score is worse than none"
    assert "finding" in result.stderr


def test_an_inventory_declaring_an_unknown_schema_is_refused(run_script, tmp_path):
    # The rules the score assumes are the ones this version's validator
    # enforces. A file written against some other version is not a slightly
    # different inventory; it is an unknown contract.
    out = tmp_path / "measurement.html"
    path = inventory(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema"] = "totally-bogus/9"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = run_script(SCRIPT, "--out", out, "--inventory", path,
                        "--name", "billing", expect_rc=2)

    assert not out.exists()
    assert "schema" in result.stderr


def test_an_inventory_with_no_schema_at_all_is_refused(run_script, tmp_path):
    out = tmp_path / "measurement.html"
    path = inventory(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["schema"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = run_script(SCRIPT, "--out", out, "--inventory", path,
                        "--name", "billing", expect_rc=2)

    assert not out.exists()
    assert "schema" in result.stderr


def test_the_inventory_tab_explains_what_each_credit_step_means(run_script, tmp_path):
    # A bare 0.25 in a Credit column is meaningless to a reader who has not
    # memorised the ladder, and a row nobody can read is a row nobody disputes.
    text = build(run_script, tmp_path).read_text(encoding="utf-8")

    for label in ("measured, nothing found against it", "measured, one medium finding",
                  "measured, one high finding",
                  # the apostrophe is HTML-escaped on the way out
                  "not measured, or unmeasurable with today&#x27;s data"):
        assert label in text, f"the credit legend is missing {label!r}"


def test_a_finding_missing_its_optional_prose_never_renders_the_word_None(run_script, tmp_path):
    # Findings carry no validator, so a half-written one reaches the renderer.
    # "None" in a title reads as an authored value rather than a missing one.
    path = inventory(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["findings"] = [{"id": "small_n", "evidence": []}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "measurement.html"

    run_script(SCRIPT, "--out", out, "--inventory", path, "--name", "billing")

    text = out.read_text(encoding="utf-8")
    assert "<h3>None</h3>" not in text and "<p>None</p>" not in text
    assert "untitled finding" in text
    assert ">unrated<" in text


def test_optional_strings_fall_back_rather_than_printing_None(load_module):
    build_measurement = load_module(SKILL / "scripts", "build_measurement")

    assert build_measurement.opt(None) == "—"
    assert build_measurement.opt("   ") == "—"
    assert build_measurement.opt(None, "nobody reads it") == "nobody reads it"
    assert build_measurement.opt("<b>") == "&lt;b&gt;"


def test_the_intro_prose_is_placed_on_the_score_tab(run_script, tmp_path):
    intro = tmp_path / "intro.html"
    intro.write_text("<p>Billing scores itself with a judge nobody pinned.</p>",
                     encoding="utf-8")

    text = build(run_script, tmp_path, "--intro-file", intro).read_text(encoding="utf-8")

    assert "nobody pinned" in text


def test_a_missing_intro_file_is_said_on_the_page_not_silently_dropped(run_script, tmp_path):
    text = build(run_script, tmp_path).read_text(encoding="utf-8")

    assert "no written summary" in text.lower()


def test_a_template_override_is_used(run_script, tmp_path):
    custom = tmp_path / "shell.html"
    custom.write_text("<html><body>MARKER<!--TABS_NAV--><!--TABS_PANELS--></body></html>",
                      encoding="utf-8")

    text = build(run_script, tmp_path, "--template", custom).read_text(encoding="utf-8")

    assert "MARKER" in text
    assert "judge_accuracy" in text


def test_script_closing_tags_inside_the_metadata_cannot_end_the_block(run_script, tmp_path):
    path = inventory(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rows"][0]["consumer"] = "</script><script>alert(1)</script>"
    path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "measurement.html"

    run_script(SCRIPT, "--out", out, "--inventory", path, "--name", "billing")

    assert "</script><script>alert(1)" not in out.read_text(encoding="utf-8")
    assert meta_of(out)["rows"][0]["consumer"].startswith("</script>")
