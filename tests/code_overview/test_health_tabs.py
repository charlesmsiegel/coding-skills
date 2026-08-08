"""The health page's tabs, and the line the Candidates tab has to hold.

A candidate is a lead. Rendering it beside confirmed defects, in a document
whose headline is a grade, is how a reader concludes that a dead-function
candidate is a dead function — and deletes live code.
"""

import json
import re
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "code-overview" /
          "scripts" / "build_health.py")


def envelope(tmp_path) -> Path:
    payload = {
        "schema": "code-doctor-merge/1",
        "doctors_run": ["code-doctor"],
        "analyzers_run": {"code-doctor": ["hygiene", "secrets"]},
        "analyzers_skipped": {"code-doctor": ["duplication"]},
        "analyzer_errors": {},
        "doctor_errors": {"django-code-doctor": "crashed reading settings"},
        "completeness": {"code-doctor": {"reference_graph": {"adequate": False,
                                                             "resolution_rate": 0.31}}},
        "coverage_unknown": [],
        "findings": [{"doctor": "code-doctor", "file": "src/app/settings.py", "line": 4,
                      "smell_type": "hardcoded_secret_assignment", "severity": "high",
                      "description": "SECRET_KEY literal", "suggestion": "read from env",
                      "kind": "finding"}],
        "candidates": [{"doctor": "code-doctor", "file": "src/app/legacy.py", "line": 12,
                        "smell_type": "dead_function_candidate", "severity": "medium",
                        "description": "identifier occurs once in the tree",
                        "kind": "candidate",
                        "also_caused_by": ["a library's public surface has no internal referrer",
                                           "convention-loaded plugins are never named"]}],
    }
    path = tmp_path / "merged.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def page(repo, run_script, tmp_path) -> str:
    for i in range(12):
        repo.write(f"src/app/mod{i}.py", "def f():\n    return 1\n" * 20)
    repo.commit()
    out = tmp_path / "health.html"
    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app",
               "--root-dir", "src/app", "--merged", envelope(tmp_path))
    return out.read_text(encoding="utf-8")


def test_all_four_tabs_are_present(page):
    # Exactly four, not "at least four": the preamble of a body scaffold has
    # already once been split into a fifth, bogus tab that rendered *active* and
    # hid the grade card. Counting the buttons is what catches that; asserting a
    # title appears somewhere in the HTML does not.
    assert page.count('<button role="tab"') == 4
    for title in ("Grade", "Findings", "Candidates", "Coverage"):
        assert f">{title}<" in page, f"the {title} tab is missing"


def test_the_visible_grade_card_carries_the_grade(page):
    # Every other assertion here reads the page as one string, which a hidden or
    # mis-rendered panel satisfies just as well as a correct one. This one pins
    # what a reader actually sees: the letter inside the grade card element.
    card = re.search(r'<div class="letter">([^<]*)</div>', page)
    assert card, "no grade card rendered"
    assert card.group(1).strip() not in ("", "None")


def test_the_grade_tab_is_the_one_that_opens(page):
    first = re.search(r'<section class="panel active" id="([^"]+)"', page)
    assert first and first.group(1) == "tab-grade"


def test_the_candidates_tab_says_it_did_not_affect_the_grade(page):
    assert "did not affect the grade" in page.lower()


def test_a_candidate_carries_the_benign_explanations(page):
    assert "convention-loaded plugins are never named" in page


def test_the_coverage_tab_names_the_failed_doctor(page):
    assert "django-code-doctor" in page
    assert "crashed reading settings" in page


def test_the_coverage_tab_reports_the_thin_graph_rather_than_hiding_it(page):
    assert "0.31" in page or "31" in page
    assert "design" in page.lower()


def test_the_coverage_tab_lists_the_skipped_analyzer(page):
    assert "duplication" in page.lower()


def test_a_page_with_no_candidates_says_so_rather_than_showing_an_empty_table(
        repo, run_script, tmp_path):
    repo.write("src/app/mod.py", "x = 1\n")
    repo.commit()
    payload = json.loads(envelope(tmp_path).read_text(encoding="utf-8"))
    payload["candidates"] = []
    path = tmp_path / "m2.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "health.html"
    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app",
               "--root-dir", "src/app", "--merged", path)

    assert "no candidates" in out.read_text(encoding="utf-8").lower()
