"""inject_nav.py — the thing that turns nine files into one navigable set.

It writes into documents three different generators produced, including
code-visualization's atlas, so the tests care most about the properties that
make that safe: idempotency, existence-checked links, correct relative paths at
depth, and rendering that does not depend on the host page's stylesheet.
"""

import json
import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-overview" / "scripts"
INJECT_NAV = SCRIPTS / "inject_nav.py"

KINDS = ("summary", "codemap", "health")


def page(title: str) -> str:
    """A minimal stand-in for any of the three generators' output."""
    return (f"<!DOCTYPE html>\n<html><head><title>{title}</title></head><body>\n"
            f'<header class="doc"><h1>{title}</h1></header>\n'
            "<main><p>body</p></main>\n</body></html>\n")


@pytest.fixture
def doc_set(repo):
    """Two packages at different depths, all nine documents present."""
    packages = [
        {"name": "billing", "roots": ["src/billing"], "docs": "src/billing/docs",
         "language": "python", "doctor": "python-code-doctor"},
        {"name": "web", "roots": ["frontend"], "docs": "frontend/docs",
         "language": "typescript", "doctor": "typescript-code-doctor"},
    ]
    repo.write("docs/code-overview.json",
               json.dumps({"schema": "code-overview/1", "packages": packages}))
    for directory in ("docs", "src/billing/docs", "frontend/docs"):
        for kind in KINDS:
            repo.write(f"{directory}/{kind}.html", page(f"{directory} {kind}"))
    repo.commit("init")
    return repo


def run(run_script, repo, *args, expect_rc=0):
    return run_script(INJECT_NAV, "--map", repo.path / "docs/code-overview.json",
                      "--repo", repo.path, *args, expect_rc=expect_rc)


def hrefs(text: str) -> list[str]:
    block = re.search(r"<!-- code-overview:nav -->(.*?)<!-- /code-overview:nav -->",
                      text, re.DOTALL)
    assert block, "no nav block"
    return re.findall(r'href="([^"#][^"]*)"', block.group(1))


def test_every_document_gets_a_nav(run_script, doc_set):
    run(run_script, doc_set)
    for directory in ("docs", "src/billing/docs", "frontend/docs"):
        for kind in KINDS:
            text = (doc_set.path / directory / f"{kind}.html").read_text()
            assert "code-overview:nav" in text, f"{directory}/{kind}.html has no nav"


def test_every_link_resolves(run_script, doc_set):
    result = run(run_script, doc_set)
    assert "BROKEN LINK" not in result.stderr
    for directory in ("docs", "src/billing/docs", "frontend/docs"):
        for kind in KINDS:
            path = doc_set.path / directory / f"{kind}.html"
            for href in hrefs(path.read_text()):
                assert (path.parent / href).resolve().is_file(), f"{path}: {href}"


def test_relative_paths_are_correct_at_depth(run_script, doc_set):
    run(run_script, doc_set)
    billing = doc_set.path / "src/billing/docs/health.html"
    links = hrefs(billing.read_text())
    assert "../../../docs/health.html" in links, "up-link climbs out of src/billing/docs"
    assert "summary.html" in links, "sibling links stay in the same directory"
    assert "../../../frontend/docs/health.html" in links, "sideways link reaches the other package"


def test_up_goes_to_the_same_document_type(run_script, doc_set):
    run(run_script, doc_set)
    for kind in KINDS:
        text = (doc_set.path / "src/billing/docs" / f"{kind}.html").read_text()
        assert f"../../../docs/{kind}.html" in hrefs(text), (
            f"{kind}.html must go up to the repo-level {kind}.html, not to the portal"
        )


def test_sideways_links_stay_within_one_document_type(run_script, doc_set):
    run(run_script, doc_set)
    text = (doc_set.path / "src/billing/docs/codemap.html").read_text()
    assert "../../../frontend/docs/codemap.html" in hrefs(text)
    assert "../../../frontend/docs/health.html" not in hrefs(text)


def test_the_current_document_is_marked(run_script, doc_set):
    run(run_script, doc_set)
    text = (doc_set.path / "src/billing/docs/health.html").read_text()
    block = re.search(r"code-overview:nav -->(.*?)<!-- /", text, re.DOTALL).group(1)
    assert 'aria-current="page"' in block
    assert block.count('aria-current="page"') == 1


def test_running_twice_replaces_rather_than_stacks(run_script, doc_set):
    run(run_script, doc_set)
    once = (doc_set.path / "docs/summary.html").read_text()
    run(run_script, doc_set)
    twice = (doc_set.path / "docs/summary.html").read_text()
    assert once == twice
    assert twice.count("<!-- code-overview:nav -->") == 1


def test_check_mode_writes_nothing_and_reports_a_clean_set(run_script, doc_set):
    run(run_script, doc_set)
    before = {p: p.read_text() for p in doc_set.path.rglob("*.html")}
    result = run(run_script, doc_set, "--check")
    assert all(p.read_text() == text for p, text in before.items())
    assert "would update 0" in result.stderr


def test_a_missing_document_is_reported_and_never_linked(run_script, doc_set):
    (doc_set.path / "src/billing/docs/codemap.html").unlink()
    result = run(run_script, doc_set)
    assert "not generated" in result.stderr
    for path in doc_set.path.rglob("*.html"):
        assert "billing/docs/codemap.html" not in "".join(hrefs(path.read_text()))


def test_the_nav_styles_itself_with_literal_fallbacks(run_script, doc_set):
    """It is injected into pages that never heard of this skill."""
    run(run_script, doc_set)
    block = re.search(r"<!-- code-overview:nav -->(.*?)</style>",
                      (doc_set.path / "docs/summary.html").read_text(), re.DOTALL).group(1)
    assert "<style>" in block, "the block carries its own CSS"
    used = re.findall(r"var\((--[a-z-]+)([^)]*)\)", block)
    assert used, "the block does use the host's design tokens when present"
    assert all(fallback.strip().startswith(",") for _, fallback in used), (
        "every custom property needs a literal fallback or the nav is invisible in a "
        "page shell that does not define it"
    )


def test_the_nav_lands_below_the_page_header(run_script, doc_set):
    run(run_script, doc_set)
    text = (doc_set.path / "docs/summary.html").read_text()
    assert text.index("</header>") < text.index("<!-- code-overview:nav -->")
    assert text.index("<!-- code-overview:nav -->") < text.index("<main>")


def test_a_document_without_a_header_still_gets_a_nav(run_script, doc_set):
    headerless = doc_set.path / "docs/health.html"
    headerless.write_text("<!DOCTYPE html><html><body><p>bare</p></body></html>")
    run(run_script, doc_set)
    text = headerless.read_text()
    assert "code-overview:nav" in text
    assert text.index("<body>") < text.index("<!-- code-overview:nav -->")


def test_check_fails_when_the_set_on_disk_has_drifted(run_script, doc_set):
    """A document deleted since the last run dangles from every nav that linked it."""
    run(run_script, doc_set)
    (doc_set.path / "docs/health.html").unlink()
    result = run(run_script, doc_set, "--check", expect_rc=1)
    assert "BROKEN LINK" in result.stderr
    assert "docs/health.html" in result.stderr


def test_rerunning_heals_a_link_whose_target_vanished(run_script, doc_set):
    run(run_script, doc_set)
    gone = (doc_set.path / "docs/health.html").resolve()
    gone.unlink()
    run(run_script, doc_set)
    for path in doc_set.path.rglob("*.html"):
        resolved = {(path.parent / href).resolve() for href in hrefs(path.read_text())}
        assert gone not in resolved, f"{path} still links the deleted document"
    assert "BROKEN LINK" not in run(run_script, doc_set, "--check").stderr


def test_a_single_package_repo_has_no_package_layer(run_script, repo):
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [{"name": "whole", "roots": ["."], "docs": "docs"}],
    }))
    for kind in KINDS:
        repo.write(f"docs/{kind}.html", page(kind))
    repo.commit("init")
    run(run_script, repo)
    text = (repo.path / "docs/summary.html").read_text()
    block = re.search(r"code-overview:nav -->(.*?)<!-- /", text, re.DOTALL).group(1)
    assert "Other packages" not in block
    assert "Overall Summary" not in block, (
        "a document that already is the overall one must not offer to link to itself"
    )
    assert "Code Map" in block, "the across-row is still useful with one package"


def test_a_missing_map_fails_with_a_usable_message(run_script, repo, tmp_path):
    result = run_script(INJECT_NAV, "--map", tmp_path / "nope.json", "--repo", repo.path,
                        expect_rc=1)
    assert "no package map" in result.stderr
