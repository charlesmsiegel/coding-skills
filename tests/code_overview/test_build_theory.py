"""The theory document.

Every tab is rendered from the verdicts, so the letter and the rows under it
cannot disagree. The assertions here are panel-scoped on purpose: the verdicts
are embedded in the page's metadata block, so a whole-document substring check
would match the JSON rather than the rendering — the failure shape that has bitten
this project repeatedly.
"""

import json
import re
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "code-overview"
          / "scripts" / "build_theory.py")


def panel_of(page: str, tab_id: str) -> str:
    """Just that tab's own markup, cut at its own closing tag."""
    marker = f'id="{tab_id}"'
    assert marker in page, f"no panel {tab_id}"
    chunk = page.split(marker, 1)[1]
    return chunk.split("\n</section>")[0]


def meta_of(page: Path) -> dict:
    match = re.search(r'id="theory-meta">(.*?)</script>',
                      page.read_text(encoding="utf-8"), re.S)
    assert match, "the page carries no theory-meta block"
    return json.loads(match.group(1).replace("<\\/", "</"))


def verdict_file(tmp_path, index: int, **overrides) -> Path:
    dims = {}
    for key in ("absorption", "world_mapping", "abstraction", "justification", "honest_limits"):
        dims[key] = {"step": overrides.get(key, 1.0),
                     "rationale": f"judge {index} on {key}",
                     "evidence": [f"src/billing/{key}.py:{index + 1}"]}
    payload = {
        "schema": "theory-verdict/1",
        "unit": "billing",
        "theory": "Money moves between accounts; every move is an idempotent event.",
        "instead_of": "A mutable balance per account, rejected because replays double-charge.",
        "trivial": overrides.get("trivial", False),
        "trivial_reason": overrides.get("trivial_reason", ""),
        "dimensions": dims,
        "rehearsals": [
            {"requirement": "refunds in a second currency", "verdict": "extension",
             "why": "currency is a field on Money", "evidence": ["src/billing/money.py:8"]},
            {"requirement": "partial captures", "verdict": "patch",
             "why": "capture assumes the full amount", "evidence": ["src/billing/charge.py:40"]},
            {"requirement": "chargebacks", "verdict": "extension",
             "why": "a reversing event", "evidence": ["src/billing/ledger.py:60"]},
        ],
    }
    path = tmp_path / f"verdict-{index}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def repo_with_code(repo):
    for i in range(12):
        repo.write(f"src/billing/mod{i}.py", "def f():\n    return 1\n" * 20)
    repo.commit()
    return repo


def build(repo, run_script, tmp_path, *files, extra=(), expect_rc=0) -> Path:
    out = tmp_path / "theory.html"
    args = []
    for path in files:
        args += ["--verdict", str(path)]
    run_script(SCRIPT, "--out", out, "--name", "billing", "--repo", repo.path,
               "--root-dir", "src/billing", *args, *extra, expect_rc=expect_rc)
    return out


def three(tmp_path, **overrides) -> tuple:
    return tuple(verdict_file(tmp_path, i, **overrides) for i in range(3))


def verdict_args(files) -> list:
    args = []
    for path in files:
        args += ["--verdict", str(path)]
    return args


_ROW_RE = re.compile(r"<tr><td><strong>(.*?)</strong>(.*?)</tr>", re.S)


def dimension_rows(panel: str) -> dict:
    """label -> the rest of that dimension's row, from the Dimensions table.

    Row-scoped rather than panel-scoped: `holds` appears in the tab's own
    static intro ("A step below *holds* must cite…"), so a substring check
    against the panel passes whatever the rubric computed — which is how the
    median step, the output of the whole rubric, went untested.
    """
    return {label: cells for label, cells in _ROW_RE.findall(panel)}


def doc_meta_line(text: str) -> str:
    match = re.search(r'<div class="doc-meta">(.*?)</div>', text, re.S)
    assert match, "the page carries no doc-meta line"
    return match.group(1)


# --- the grade -------------------------------------------------------------

def test_a_unanimous_panel_renders_its_grade(repo_with_code, run_script, tmp_path):
    page = build(repo_with_code, run_script, tmp_path, *three(tmp_path))

    meta = meta_of(page)
    assert meta["score"] == pytest.approx(100.0)
    assert meta["grade"] == "A+"
    assert meta["schema"] == "theory/1"
    assert meta["scope"] == "package"
    assert meta["panel_size"] == 3


def test_the_visible_grade_card_carries_the_letter(repo_with_code, run_script, tmp_path):
    text = build(repo_with_code, run_script, tmp_path,
                 *three(tmp_path)).read_text(encoding="utf-8")

    card = re.search(r'<div class="letter">([^<]*)</div>', panel_of(text, "tab-grade"))
    assert card and card.group(1).strip() == "A+"


def test_all_four_tabs_render(repo_with_code, run_script, tmp_path):
    text = build(repo_with_code, run_script, tmp_path,
                 *three(tmp_path)).read_text(encoding="utf-8")

    assert text.count('<button role="tab"') == 4
    for title in ("Grade", "Theory", "Dimensions", "Disagreement"):
        assert f">{title}<" in text


# --- the theory itself -----------------------------------------------------

def test_the_theory_statement_and_the_rejected_reading_render(repo_with_code, run_script,
                                                              tmp_path):
    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *three(tmp_path)).read_text(encoding="utf-8"), "tab-theory")

    assert "idempotent event" in panel
    assert "replays double-charge" in panel, "the rejected reading is not recoverable from code"


def test_the_rehearsals_render_verbatim_with_their_verdicts(repo_with_code, run_script,
                                                            tmp_path):
    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *three(tmp_path)).read_text(encoding="utf-8"), "tab-theory")

    assert "partial captures" in panel
    assert "patch" in panel
    assert "capture assumes the full amount" in panel


# --- dimensions ------------------------------------------------------------

def test_every_dimension_renders_its_median_step_weight_and_evidence(repo_with_code,
                                                                     run_script, tmp_path):
    # One dimension the whole panel put a rung lower, so the median that
    # renders is not the same word in every row.
    files = three(tmp_path, abstraction=0.5)
    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *files).read_text(encoding="utf-8"), "tab-dimensions")

    rows = dimension_rows(panel)
    assert set(rows) == {"Absorption", "World-mapping", "Abstraction", "Justification",
                         "Honest limits"}
    assert "<td>partial<br>" in rows["Abstraction"], "the median step the rubric computed"
    for label in ("Absorption", "World-mapping", "Justification", "Honest limits"):
        assert "<td>holds<br>" in rows[label]
    assert '<td class="num">30</td>' in rows["Absorption"], "absorption's weight"
    assert '<td class="num">10</td>' in rows["Honest limits"], "honest limits' weight"
    assert "src/billing/absorption.py:1" in rows["Absorption"]


def test_a_disputed_row_shows_each_judges_step_the_spread_and_their_rationales(
        repo_with_code, run_script, tmp_path):
    files = (verdict_file(tmp_path, 0, abstraction=1.0),
             verdict_file(tmp_path, 1, abstraction=0.25),
             verdict_file(tmp_path, 2, abstraction=1.0))

    rows = dimension_rows(panel_of(build(repo_with_code, run_script, tmp_path,
                                         *files).read_text(encoding="utf-8"),
                                   "tab-dimensions"))

    disputed = rows["Abstraction"]
    assert "<td>holds<br>" in disputed, "median_low of (holds, strained, holds)"
    assert "holds, strained, holds" in disputed, "each judge's own step, in panel order"
    assert "2 rungs apart" in disputed, "the spread, not smoothed away"
    for judge in range(3):
        assert f"judge {judge} on abstraction" in disputed, "every judge's rationale"
    assert "0 rung(s)" in rows["Justification"], "an agreed row still states its spread"


# --- disagreement ----------------------------------------------------------

def test_a_disputed_dimension_is_named_on_the_disagreement_tab(repo_with_code, run_script,
                                                               tmp_path):
    files = (verdict_file(tmp_path, 0, abstraction=1.0),
             verdict_file(tmp_path, 1, abstraction=0.25),
             verdict_file(tmp_path, 2, abstraction=1.0))

    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *files).read_text(encoding="utf-8"), "tab-disagreement")

    assert "Abstraction" in panel
    assert "could not agree" in panel.lower()


def test_an_undisputed_panel_says_so_rather_than_showing_an_empty_tab(repo_with_code,
                                                                      run_script, tmp_path):
    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *three(tmp_path)).read_text(encoding="utf-8"), "tab-disagreement")

    assert "agreed" in panel.lower()


def test_one_rung_of_disagreement_does_not_raise_the_banner(repo_with_code, run_script,
                                                            tmp_path):
    files = (verdict_file(tmp_path, 0, abstraction=1.0),
             verdict_file(tmp_path, 1, abstraction=0.5),
             verdict_file(tmp_path, 2, abstraction=1.0))

    meta = meta_of(build(repo_with_code, run_script, tmp_path, *files))
    assert meta["disputed"] == []


# --- honesty ---------------------------------------------------------------

def test_the_page_says_the_grade_is_a_reading_not_a_measurement(repo_with_code, run_script,
                                                                tmp_path):
    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *three(tmp_path)).read_text(encoding="utf-8"), "tab-grade")

    lowered = panel.lower()
    assert "reading" in lowered
    assert "circular" in lowered, "the panel narrows the circularity; it does not close it"


def test_the_grade_tab_names_the_dimensions_the_panel_disagreed_on(repo_with_code,
                                                                   run_script, tmp_path):
    """The Disagreement tab is pinned; this is the tab readers land on first."""
    files = (verdict_file(tmp_path, 0, world_mapping=1.0),
             verdict_file(tmp_path, 1, world_mapping=0.25),
             verdict_file(tmp_path, 2, world_mapping=1.0))

    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *files).read_text(encoding="utf-8"), "tab-grade")

    assert "The panel disagreed on:" in panel
    assert "World-mapping" in panel, "named, so the reader knows which row to go argue with"
    assert "Disagreement tab" in panel


def test_the_model_and_panel_size_are_stamped(repo_with_code, run_script, tmp_path):
    page = build(repo_with_code, run_script, tmp_path, *three(tmp_path),
                 extra=("--model", "claude-opus-5"))

    meta = meta_of(page)
    assert meta["model"] == "claude-opus-5"
    assert meta["panel_size"] == 3


def test_an_exempt_unit_scores_null_and_says_why(repo, run_script, tmp_path):
    repo.write("src/tiny/a.py", "x = 1\n")
    repo.write("src/tiny/b.py", "y = 2\n")
    repo.commit()
    files = (verdict_file(tmp_path, 0, trivial=True, trivial_reason="two constants"),
             verdict_file(tmp_path, 1, trivial=True, trivial_reason="two constants"),
             verdict_file(tmp_path, 2))
    out = tmp_path / "theory.html"
    args = []
    for path in files:
        args += ["--verdict", str(path)]
    run_script(SCRIPT, "--out", out, "--name", "tiny", "--repo", repo.path,
               "--root-dir", "src/tiny", *args)

    meta = meta_of(out)
    assert meta["score"] is None
    assert meta["grade"] == "—"
    assert "too small" in panel_of(out.read_text(encoding="utf-8"), "tab-grade").lower()


def test_an_invalid_verdict_writes_no_document(repo_with_code, run_script, tmp_path):
    bad = tmp_path / "bad.json"
    payload = json.loads(verdict_file(tmp_path, 0).read_text(encoding="utf-8"))
    payload["dimensions"]["abstraction"] = {"step": 0.5, "rationale": "thin", "evidence": []}
    bad.write_text(json.dumps(payload), encoding="utf-8")

    out = tmp_path / "theory.html"
    result = run_script(SCRIPT, "--out", out, "--name", "billing", "--repo", repo_with_code.path,
                        "--root-dir", "src/billing",
                        "--verdict", str(verdict_file(tmp_path, 1)),
                        "--verdict", str(verdict_file(tmp_path, 2)),
                        "--verdict", str(bad), expect_rc=2)

    assert not out.exists(), "a page with an unarguable grade is worse than none"
    assert "evidence" in result.stderr


def test_two_verdicts_are_refused(repo_with_code, run_script, tmp_path):
    out = tmp_path / "theory.html"
    files = three(tmp_path)
    args = []
    for path in files[:2]:
        args += ["--verdict", str(path)]
    result = run_script(SCRIPT, "--out", out, "--name", "billing", "--repo", repo_with_code.path,
                        "--root-dir", "src/billing", *args, expect_rc=2)

    assert "three" in result.stderr
    assert not out.exists()


def test_script_closing_tags_in_a_verdict_cannot_end_the_metadata_block(repo_with_code,
                                                                        run_script, tmp_path):
    files = list(three(tmp_path))
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    payload["theory"] = "</script><script>alert(1)</script>"
    files[0].write_text(json.dumps(payload), encoding="utf-8")

    page = build(repo_with_code, run_script, tmp_path, *files)

    assert "</script><script>alert(1)" not in page.read_text(encoding="utf-8")
    assert meta_of(page)["theory"].startswith("</script>")


# --- the roots the panel is a claim about -----------------------------------

def test_a_root_that_does_not_exist_is_refused_rather_than_graded(repo_with_code,
                                                                  run_script, tmp_path):
    """A typo'd --root-dir measured nothing and rendered A+ (100.0) over
    "0 files · 0 lines", because the grade comes from the verdicts and nothing
    about measuring nothing lowers it."""
    out = tmp_path / "theory.html"
    result = run_script(SCRIPT, "--out", out, "--name", "billing", "--repo", repo_with_code.path,
                        "--root-dir", "src/biling", *verdict_args(three(tmp_path)),
                        expect_rc=2)

    assert not out.exists(), "a confident letter over code this run never saw is worse than none"
    assert "src/biling" in result.stderr, "the stderr has to name the root that is not there"


def test_a_missing_root_cannot_exempt_a_unit_on_the_vote_alone(repo_with_code, run_script,
                                                               tmp_path):
    """The size gate is the only non-judgment half of the exemption. Over a root
    that measures 0 files it passes for free, so two trivial votes rendered
    exempt/null — the two gates collapsed into the one that is gameable."""
    files = (verdict_file(tmp_path, 0, trivial=True, trivial_reason="nothing much here"),
             verdict_file(tmp_path, 1, trivial=True, trivial_reason="nothing much here"),
             verdict_file(tmp_path, 2))
    out = tmp_path / "theory.html"
    run_script(SCRIPT, "--out", out, "--name", "billing", "--repo", repo_with_code.path,
               "--root-dir", "src/biling", *verdict_args(files), expect_rc=2)

    assert not out.exists(), "'too small to warrant a theory' must not be said about unread code"


def test_a_root_with_no_source_files_is_refused(repo, run_script, tmp_path):
    """The other shape of the same drift: the directory is there and empty."""
    repo.write("src/billing/NOTES.md", "no code here\n")
    repo.commit()
    out = tmp_path / "theory.html"
    result = run_script(SCRIPT, "--out", out, "--name", "billing", "--repo", repo.path,
                        "--root-dir", "src/billing", *verdict_args(three(tmp_path)),
                        expect_rc=2)

    assert not out.exists()
    assert "no source files" in result.stderr


# --- no exemption floor at repo scope ---------------------------------------

def test_repo_scope_offers_no_exemption_floor_however_trivial_the_repo(repo, run_script,
                                                                       tmp_path):
    """Same fixture that is exempt at package scope, built with --root.

    A repository of individually trivial packages still has a system-level
    question worth asking, so `--root` must reach score_panel with the floor
    switched off. Only the parameter was tested before, never that --root
    passes it — a repo could have come back null with nothing noticing.
    """
    repo.write("a.py", "x = 1\n")
    repo.write("b.py", "y = 2\n")
    repo.commit()
    files = (verdict_file(tmp_path, 0, trivial=True, trivial_reason="two constants"),
             verdict_file(tmp_path, 1, trivial=True, trivial_reason="two constants"),
             verdict_file(tmp_path, 2))
    out = tmp_path / "theory.html"
    run_script(SCRIPT, "--out", out, "--name", "repo", "--repo", repo.path, "--root",
               *verdict_args(files))

    meta = meta_of(out)
    assert meta["scope"] == "repository", "the fixture must actually be at repo scope"
    assert meta["exempt"] is False
    assert meta["score"] is not None and meta["grade"] != "—"
    assert "too small" not in panel_of(out.read_text(encoding="utf-8"), "tab-grade").lower()
