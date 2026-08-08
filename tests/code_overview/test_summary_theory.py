"""The portal's third grade card, read back out of theory.html."""

import json
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "code-overview"
          / "scripts" / "build_summary.py")


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

    assert "disagreed" in out.read_text(encoding="utf-8").lower()
