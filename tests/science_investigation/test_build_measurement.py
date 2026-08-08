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


def panel_of(page: Path, tab_id: str) -> str:
    """One rendered panel's own markup, so "on the X tab" can be asserted.

    Two traps this closes, and both have already produced green suites over
    deleted rendering.

    Split on the panel boundary rather than the next `</section>`: the grade
    card is itself a `<section>`, so a lazy match to `</section>` stops short.

    Then cut at the panel's own end — `panels()` emits every body followed by
    a newline and `</section>` — because the chunk for the *last* panel
    otherwise runs to end-of-file and swallows the `measurement-meta` script,
    which carries the whole inventory as text. A panel helper that includes
    that block is no better than asserting against the whole document.
    """
    for chunk in page.read_text(encoding="utf-8").split('<section class="panel')[1:]:
        if re.match(rf'[^>]*id="{tab_id}"', chunk):
            return chunk.split("\n</section>")[0]
    raise AssertionError(f"no {tab_id} panel on the page")


def nav_of(page: Path) -> str:
    """The tab bar alone — the buttons, not every string on the page."""
    match = re.search(r'<nav class="tabs"[^>]*>(.*?)</nav>',
                      page.read_text(encoding="utf-8"), re.S)
    assert match, "the page has no tab bar"
    return match.group(1)


def table_rows(panel: str) -> list[list[str]]:
    """Every body `<tr>` in a panel as its list of cell contents."""
    return [cells for row in re.findall(r"<tr>(.*?)</tr>", panel, re.S)
            if (cells := re.findall(r"<td[^>]*>(.*?)</td>", row, re.S))]


def kpis_of(panel: str) -> dict[str, str]:
    """The KPI row as {label: number}, read the way a reader reads it."""
    return {label: number for number, label in
            re.findall(r'<div class="n">([^<]*)</div><div class="l">([^<]*)</div>', panel)}


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


# --- what a reader actually sees ------------------------------------------
#
# Every assertion above this line reads meta_of(), the JSON block no reader
# opens. The whole Score tab could render weight_measured and weight_total the
# wrong way round — an F page claiming 5.00 of 0 weight measured — and the
# metadata would still be right, so the suite would still be green. These pin
# the rendered numbers instead.

def test_the_grade_card_shows_the_letter_and_the_score(run_script, tmp_path):
    panel = panel_of(build(run_script, tmp_path), "tab-score")

    letter = re.search(r'<div class="letter">([^<]*)</div>', panel)
    assert letter, "the grade card has no letter"
    assert letter.group(1) == "F"
    assert re.search(r'<div class="score">([^<]*)</div>', panel).group(1) == "15.0 / 100"
    assert 'class="gradecard g-f"' in panel, "the card is coloured by the grade it shows"


def test_the_kpi_row_shows_the_numbers_the_score_was_computed_from(run_script, tmp_path):
    # 3*0.25 + 2*0.0 = 0.75 measured, of 5 total weight, over 2 things.
    kpis = kpis_of(panel_of(build(run_script, tmp_path), "tab-score"))

    assert kpis["measurable things"] == "2"
    assert kpis["weight measured"] == "0.75"
    assert kpis["weight total"] == "5"


def test_the_ship_gate_sentence_carries_the_gate_share_not_the_headline(run_script, tmp_path):
    # 0.75 of 3 gating weight = 25%, which is deliberately not the 15.0 in the
    # grade card: the headline averages every importance level and this line
    # does not.
    panel = panel_of(build(run_script, tmp_path), "tab-score")

    assert "25% of the weight that gates a ship decision is soundly measured" in panel


def test_the_by_importance_table_shows_each_levels_weight_and_share(run_script, tmp_path):
    panel = panel_of(build(run_script, tmp_path), "tab-score")

    table = panel.split("<h3>By importance</h3>")[1]
    cells = [found for row_html in re.findall(r"<tr>(.*?)</tr>", table, re.S)
             if (found := re.findall(r"<td[^>]*>([^<]*)</td>", row_html))]
    assert cells == [
        ["gates a ship decision", "1", "3", "0.75", "25%"],
        ["informs a decision", "1", "2", "0.00", "0%"],
        # No informational rows: a share of nothing is a dash, not 0% and not
        # 100%, for the same reason an empty unit scores null.
        ["informational", "0", "0", "0.00", "—"],
    ]


def test_every_tab_is_present(run_script, tmp_path):
    nav = nav_of(build(run_script, tmp_path))

    for tab in ("Score", "Inventory", "Findings", "Unmeasurable"):
        assert f">{tab}<" in nav, f"the {tab} tab button is missing"


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
    # Read the *rendered* table, not the document. The metadata block below the
    # panels serializes every row, reason and citation verbatim, so
    # `"judge_accuracy" in page_html` passed with the whole Inventory tab
    # replaced by a single empty paragraph — which is the one tab this page
    # exists to ship.
    panel = panel_of(build(run_script, tmp_path), "tab-inventory")

    cells = {re.search(r"<strong>([^<]*)</strong>", row[0]).group(1): row
             for row in table_rows(panel) if row and "<strong>" in row[0]}
    assert sorted(cells) == ["judge_accuracy", "recall"]

    judge = cells["judge_accuracy"]
    assert "mean(judge_score == gold)" in judge[0]
    assert "gates a ship decision" in judge[1]
    assert "gates the weekly model rollout" in judge[1]
    assert judge[2] == "0.25", "the credit column carries the credit that was scored"
    assert "computed over 3 labelled rows of 412" in judge[3]
    assert judge[4] == "3 / 412", "the N a reader needs to dispute the row"
    assert "scripts/rollout.py:88" in judge[5]
    assert "evals/judge.py:41" in judge[6], "the file:line somebody actually opened"

    recall = cells["recall"]
    assert recall[2] == "0.00"
    assert recall[4] == "—", "no N is a dash, never a zero that reads as measured"


def test_a_reduced_credit_names_the_finding_that_caused_it(run_script, tmp_path):
    page = build(run_script, tmp_path)

    # The row's credit is 0.25 rather than 1.00 *because of* small_n, so the
    # inventory row has to carry the finding id, and the Findings tab has to
    # carry the finding itself. Both ids and titles also sit in the metadata
    # JSON, so both halves are asserted inside their own panel.
    inventory_panel = panel_of(page, "tab-inventory")
    assert '<span class="badge bad">small_n</span>' in inventory_panel

    findings_panel = panel_of(page, "tab-findings")
    assert "Judge accuracy rests on n=3" in findings_panel
    assert "<code>small_n</code>" in findings_panel
    assert "the weekly rollout gate" in findings_panel, "blast radius, rendered"


def test_the_unmeasurable_row_is_labelled_not_counted_as_a_defect(run_script, tmp_path):
    page = build(run_script, tmp_path)
    panel = panel_of(page, "tab-unmeasurable")
    meta = meta_of(page)

    rows = table_rows(panel)
    assert [re.search(r"<strong>([^<]*)</strong>", row[0]).group(1) for row in rows] == ["recall"]
    assert "informs a decision" in rows[0][1]
    assert "no gold set exists, so recall cannot be computed" in rows[0][2]
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
    assert "no measurement content" in panel_of(out, "tab-score").lower()


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
    panel = panel_of(build(run_script, tmp_path), "tab-inventory")

    for label in ("measured, nothing found against it", "measured, one medium finding",
                  "measured, one high finding",
                  # the apostrophe is HTML-escaped on the way out
                  "not measured, or unmeasurable with today&#x27;s data"):
        assert label in panel, f"the credit legend is missing {label!r}"


def test_a_finding_missing_its_optional_prose_never_renders_the_word_None(run_script, tmp_path):
    # Findings carry no validator, so a half-written one reaches the renderer.
    # "None" in a title reads as an authored value rather than a missing one.
    path = inventory(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["findings"] = [{"id": "small_n", "evidence": []}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "measurement.html"

    run_script(SCRIPT, "--out", out, "--inventory", path, "--name", "billing")

    panel = panel_of(out, "tab-findings")
    assert "<h3>None</h3>" not in panel and "<p>None</p>" not in panel
    assert "untitled finding" in panel
    assert ">unrated<" in panel


def test_optional_strings_fall_back_rather_than_printing_None(load_module):
    build_measurement = load_module(SKILL / "scripts", "build_measurement")

    assert build_measurement.opt(None) == "—"
    assert build_measurement.opt("   ") == "—"
    assert build_measurement.opt(None, "nobody reads it") == "nobody reads it"
    assert build_measurement.opt("<b>") == "&lt;b&gt;"


def test_the_intro_prose_is_placed_on_the_score_tab(run_script, tmp_path):
    # "in the page" is not what this test is named for: the prose landed on any
    # tab at all — or on none, with only the metadata block matching — and this
    # still passed. The claim is *placement*, so it is checked as placement.
    intro = tmp_path / "intro.html"
    intro.write_text("<p>Billing scores itself with a judge nobody pinned.</p>",
                     encoding="utf-8")

    page = build(run_script, tmp_path, "--intro-file", intro)

    assert "nobody pinned" in panel_of(page, "tab-score")
    for elsewhere in ("tab-inventory", "tab-findings", "tab-unmeasurable"):
        assert "nobody pinned" not in panel_of(page, elsewhere), (
            f"the prose belongs beside the grade it explains, not on {elsewhere}"
        )


def test_a_missing_intro_file_is_said_on_the_page_not_silently_dropped(run_script, tmp_path):
    panel = panel_of(build(run_script, tmp_path), "tab-score")

    assert "no written summary" in panel.lower()


def test_a_template_override_is_used(run_script, tmp_path):
    custom = tmp_path / "shell.html"
    custom.write_text("<html><body>MARKER<!--TABS_NAV--><!--TABS_PANELS--></body></html>",
                      encoding="utf-8")

    page = build(run_script, tmp_path, "--template", custom)

    assert "MARKER" in page.read_text(encoding="utf-8")
    assert "judge_accuracy" in panel_of(page, "tab-inventory"), (
        "the override replaces the shell, not the panels rendered into it"
    )


def test_script_closing_tags_inside_the_metadata_cannot_end_the_block(run_script, tmp_path):
    path = inventory(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rows"][0]["consumer"] = "</script><script>alert(1)</script>"
    path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "measurement.html"

    run_script(SCRIPT, "--out", out, "--inventory", path, "--name", "billing")

    assert "</script><script>alert(1)" not in out.read_text(encoding="utf-8")
    assert meta_of(out)["rows"][0]["consumer"].startswith("</script>")
