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

def test_every_dimension_renders_with_its_step_and_evidence(repo_with_code, run_script,
                                                            tmp_path):
    panel = panel_of(build(repo_with_code, run_script, tmp_path,
                           *three(tmp_path)).read_text(encoding="utf-8"), "tab-dimensions")

    for label in ("Absorption", "World-mapping", "Abstraction", "Justification",
                  "Honest limits"):
        assert label in panel
    assert "holds" in panel
    assert "src/billing/absorption.py:1" in panel


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
