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


def package_rows(text: str) -> list[str]:
    """The repo portal's package table, one row's cells at a time."""
    table = re.search(r"<h2>Packages</h2>.*?</table>", text, re.S)
    assert table, "no package table on the portal"
    return re.findall(r"<tr>(.*?)</tr>", table.group(0), re.S)


def package_row(text: str, name: str) -> str:
    rows = [row for row in package_rows(text) if f"{name}<br>" in row]
    assert len(rows) == 1, f"expected exactly one row for {name}, got {len(rows)}"
    return rows[0]


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
    assert "Abstraction" in card, (
        "the dimension is named the same way here as inside theory.html — a reader "
        "cannot tell `world_mapping` and 'World-mapping' are one row"
    )


def test_an_exempt_panel_that_also_disagreed_keeps_its_disagreement(repo, run_script,
                                                                    tmp_path):
    """Exempt and disputed are not alternatives.

    Two judges can call a unit trivial while the panel still splits two rungs
    on a dimension; testing exemption first threw that away on the page most
    readers land on.
    """
    docs = repo.path / "src" / "app" / "docs"
    theory_page(docs / "theory.html", None, "—", disputed=["world_mapping"])
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    card = theory_card_of(out.read_text(encoding="utf-8"))
    assert "too small" in card.lower()
    assert "disagreed" in card.lower(), "an exempt unit's disagreement is still a finding"
    assert "World-mapping" in card


def test_the_portal_card_calls_the_theory_grade_a_reading_not_a_measurement(repo, run_script,
                                                                            tmp_path):
    """The portal is the one place three letters sit side by side, so it is the
    place a reader most needs telling that one of them was produced differently."""
    docs = repo.path / "src" / "app" / "docs"
    theory_page(docs / "theory.html", 88.0, "B+")
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    card = theory_card_of(out.read_text(encoding="utf-8"))
    assert "A reading, not a measurement" in card


def test_the_repo_portal_table_carries_each_packages_theory_grade(repo, run_script, tmp_path):
    """Three states, kept apart: no document, exempt-null, and graded."""
    theory_page(repo.path / "src" / "graded" / "docs" / "theory.html", 88.0, "B+")
    theory_page(repo.path / "src" / "tiny" / "docs" / "theory.html", None, "—")
    (repo.path / "src" / "unjudged" / "docs").mkdir(parents=True)
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [{"name": name, "roots": [f"src/{name}"], "docs": f"src/{name}/docs",
                      "language": "python", "doctor": "python-code-doctor"}
                     for name in ("graded", "tiny", "unjudged")],
    }))
    repo.commit()
    out = repo.path / "docs" / "summary.html"

    run_script(SCRIPT, "--root", "--out", out, "--repo", repo.path, "--name", "whole-repo",
               "--map", repo.path / "docs" / "code-overview.json")

    text = out.read_text(encoding="utf-8")
    assert '<th class="num">Theory</th>' in text
    # The theory cells are the last two on every row, so ending the row with
    # them is what distinguishes the column from the measurement one beside it
    # — both of which say "not generated" for a package nobody documented.
    assert package_row(text, "graded").endswith('<td class="num">88.0</td><td>B+</td>')
    assert package_row(text, "tiny").endswith(
        '<td class="num">—</td><td>too small to warrant a theory</td>')
    assert package_row(text, "unjudged").endswith(
        '<td class="num">—</td><td>not generated</td>')
