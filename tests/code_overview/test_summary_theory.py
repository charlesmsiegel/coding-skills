"""The portal's third grade card, read back out of theory.html."""

import json
import re
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "code-overview"
          / "scripts" / "build_summary.py")


def theory_card_of(text: str) -> str:
    """Just the markup render_theory_card emits, not the whole summary page.

    Two shapes: a `<section class="gradecard …">` with an `<h2>Theory</h2>`
    (graded — possibly with a disputed-panel note inside it), or a
    `<div class="callout"><strong>Theory: …` (the exempt case). The page also
    carries a *static* doc-link description of the theory document elsewhere
    (in the DOC_LINKS block) — that description is prose about what the
    document contains, not the reading itself, and must not be mistaken for
    it. `<section class="gradecard …">…</section>` never nests, so splitting
    the page into whole sections and keeping the one that mentions "Theory"
    isolates the real card from that description and from the other two
    gradecards (the overall grade, the measurement grade) that share the same
    CSS class.
    """
    for section in re.findall(r'<section class="gradecard[^"]*">.*?</section>', text, re.S):
        if "<h2>Theory</h2>" in section:
            return section
    exempt = re.search(r'<div class="callout"><strong>Theory:.*?</div>', text, re.S)
    assert exempt, "no theory card found on the page at all"
    return exempt.group(0)


def theory_page(path: Path, score, grade, disputed=()) -> Path:
    meta = {"schema": "theory/1", "scope": "package", "package": path.parent.name,
            "score": score, "grade": grade, "exempt": score is None,
            "exempt_reason": "two constants" if score is None else "",
            "panel_size": 3, "dimensions": [], "disputed": list(disputed),
            "theory": "Money moves between accounts.", "verdicts": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<html><body><script type="application/json" id="theory-meta">'
                    + json.dumps(meta) + "</script></body></html>", encoding="utf-8")
    return path


def test_the_theory_grade_is_read_back_out_of_the_document(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    theory_page(docs / "theory.html", 72.5, "C-")
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    text = out.read_text(encoding="utf-8")
    assert "C-" in text and "72.5" in text
    assert "theory.html" in text


def test_an_exempt_theory_reads_as_too_small_not_as_a_pass(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    theory_page(docs / "theory.html", None, "—")
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    assert "too small" in out.read_text(encoding="utf-8").lower()


def test_a_missing_theory_document_is_not_linked(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    docs.mkdir(parents=True)
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    assert 'href="theory.html"' not in out.read_text(encoding="utf-8")


def test_a_disputed_panel_is_flagged_on_the_portal(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    theory_page(docs / "theory.html", 61.0, "D-", disputed=["abstraction"])
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    card = theory_card_of(out.read_text(encoding="utf-8"))
    assert "disagreed" in card.lower()
    assert "abstraction" in card
