"""build_summary.py — the page a reader lands on.

Its one hard rule is that it invents nothing: the grade is read back out of
health.html rather than passed in, so the summary and the health page cannot
disagree about what the package scored.
"""

import json
import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-overview" / "scripts"
BUILD_HEALTH = SCRIPTS / "build_health.py"
BUILD_SUMMARY = SCRIPTS / "build_summary.py"


@pytest.fixture
def graded(run_script, repo):
    """A package with a real health.html and a stand-in codemap.html."""
    for module in range(4):
        repo.write(f"src/app/m{module}.py", "\n".join(f"x{i}=1" for i in range(300)))
    report = repo.path / "findings.json"
    report.write_text(json.dumps([
        {"file": "src/app/m0.py", "line": 4, "severity": "high", "category": "security",
         "description": "hardcoded secret", "suggestion": "load it from the environment"},
        {"file": "src/app/m1.py", "line": 9, "severity": "medium", "category": "duplicates",
         "description": "duplicate block"},
    ]))
    run_script(BUILD_HEALTH, "--out", repo.path / "src/app/docs/health.html",
               "--findings", report, "--repo", repo.path, "--name", "app",
               "--root-dir", "src/app", "--language", "python",
               "--doctor", "python-code-doctor")
    repo.write("src/app/docs/codemap.html",
               '<!DOCTYPE html><html><head><title>t</title></head><body>'
               '<header class="doc"><h1 class="doc-title">app — Codebase Atlas</h1>'
               '<div class="doc-meta">generated 2026-08-07 · abc1234</div></header>'
               '<nav class="tabs" role="tablist"><button>Overview</button>'
               '<button>Dependencies</button></nav><main></main></body></html>')
    repo.commit("init")
    return repo


def build(run_script, repo, *args, out="src/app/docs/summary.html"):
    target = repo.path / out
    run_script(BUILD_SUMMARY, "--out", target, "--repo", repo.path, "--name", "app", *args)
    return target


def test_the_grade_comes_from_the_health_page(run_script, graded):
    health = json.loads(re.search(r'id="code-health-meta">(.*?)</script>',
                                  (graded.path / "src/app/docs/health.html").read_text(),
                                  re.DOTALL).group(1).replace("<\\/", "</"))
    text = build(run_script, graded).read_text()
    assert f'<div class="letter">{health["grade"]}</div>' in text
    assert f'{health["score"]:.1f} / 100' in text


def test_both_documents_are_linked(run_script, graded):
    text = build(run_script, graded).read_text()
    assert 'href="codemap.html"' in text
    assert 'href="health.html"' in text


def test_a_missing_codemap_is_disabled_not_dangling(run_script, graded):
    (graded.path / "src/app/docs/codemap.html").unlink()
    text = build(run_script, graded).read_text()
    assert 'aria-disabled="true"' in text
    assert 'href="codemap.html"' not in text
    assert "No code map was generated" in text


def test_the_atlas_describes_itself(run_script, graded):
    text = build(run_script, graded).read_text()
    assert "app — Codebase Atlas" in text
    assert "Overview" in text and "Dependencies" in text, "the atlas's own tab names"


def test_author_prose_is_carried_through(run_script, graded, tmp_path):
    intro = tmp_path / "intro.html"
    intro.write_text("<p>Billing owns money movement and nothing else.</p>")
    text = build(run_script, graded, "--intro-file", intro,
                 "--highlight", "Two import cycles run through models.py").read_text()
    assert "Billing owns money movement" in text
    assert "Two import cycles run through models.py" in text
    assert "What stands out" in text


def test_a_summary_with_no_prose_says_so(run_script, graded):
    text = build(run_script, graded).read_text()
    assert "generated without a written overview" in text, (
        "the missing half has to be visible, not papered over"
    )


def test_category_scores_and_worst_findings_are_reproduced(run_script, graded):
    text = build(run_script, graded).read_text()
    assert "Category scores" in text
    assert "Worst findings" in text
    assert "hardcoded secret" in text


def test_a_missing_health_page_degrades_rather_than_crashing(run_script, graded):
    (graded.path / "src/app/docs/health.html").unlink()
    result = run_script(BUILD_SUMMARY, "--out", graded.path / "src/app/docs/summary.html",
                        "--repo", graded.path, "--name", "app")
    assert "no code-health metadata" in result.stderr
    assert (graded.path / "src/app/docs/summary.html").is_file()


def test_the_root_portal_lists_every_package(run_script, graded):
    graded.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [{"name": "app", "roots": ["src/app"], "docs": "src/app/docs",
                      "language": "python", "doctor": "python-code-doctor"}],
    }))
    build(run_script, graded)  # so the package summary exists to link to
    report = graded.path / "findings.json"
    run_script(BUILD_HEALTH, "--root", "--out", graded.path / "docs/health.html",
               "--map", graded.path / "docs/code-overview.json", "--findings", report,
               "--repo", graded.path, "--name", "whole-repo")
    out = graded.path / "docs/summary.html"
    run_script(BUILD_SUMMARY, "--root", "--out", out, "--repo", graded.path,
               "--name", "whole-repo", "--map", graded.path / "docs/code-overview.json")
    text = out.read_text()
    assert "Packages" in text
    assert 'href="../src/app/docs/summary.html"' in text
    assert "the whole repository" in text


def test_the_root_portal_omits_a_root_collapsed_package(run_script, repo):
    """Single-package repo: the package's documents *are* the repo's."""
    repo.write("m.py", "x = 1\n")
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [{"name": "whole", "roots": ["."], "docs": "docs",
                      "language": "python", "doctor": "python-code-doctor"}],
    }))
    repo.commit("init")
    out = repo.path / "docs/summary.html"
    run_script(BUILD_SUMMARY, "--root", "--out", out, "--repo", repo.path,
               "--name", "whole-repo", "--map", repo.path / "docs/code-overview.json")
    text = out.read_text()
    assert "<h2>Packages</h2>" not in text, (
        "advertising a package document set that is this very page misleads the reader"
    )
    assert 'href="summary.html"' not in text, "and it must not link back to itself"


def test_the_portal_does_not_call_a_partial_grade_an_upper_bound(run_script, repo):
    """The portal is where readers land; its caveats must match health.html's."""
    for module in range(4):
        repo.write(f"src/app/m{module}.py", "\n".join(f"x{i}=1" for i in range(300)))
    report = repo.path / "f.json"
    report.write_text(json.dumps({
        "meta": {"analyzer_errors": {"duplicates": "boom"},
                 "analyzers_run": ["security"]},
        "categories": {"security": {"issues": []}},
    }))
    repo.commit("init")
    run_script(BUILD_HEALTH, "--out", repo.path / "src/app/docs/health.html",
               "--findings", report, "--repo", repo.path, "--name", "app",
               "--root-dir", "src/app", "--doctor", "python-code-doctor")
    out = repo.path / "src/app/docs/summary.html"
    run_script(BUILD_SUMMARY, "--out", out, "--repo", repo.path, "--name", "app")
    text = out.read_text()
    assert "did not complete" in text, "the portal has to carry the caveat at all"
    assert "upper bound" not in text, (
        "a dropped category is renormalized away, so the score is partial, not bounded above"
    )
    assert "partial" in text


def test_prose_is_html_escaped_where_it_is_not_meant_to_be_html(run_script, graded):
    text = build(run_script, graded, "--highlight", "<img src=x onerror=alert(1)>").read_text()
    assert "<img src=x" not in text
    assert "&lt;img" in text
