"""Regression tests for the pr-visualization skill's analyzer scripts.

The report's job is to help a reviewer find what the author's description did
not mention: changed contracts, callers that inherit new behavior untouched,
source that moved without its tests. These tests build a repo whose diff has
each of those properties on purpose and assert the analyzers actually surface
them — a review report that silently stops reporting a signal is worse than no
report, because it reads as an all-clear.
"""

import ast
import json
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2] / "skills"
SCRIPTS = SKILLS / "pr-visualization" / "scripts"

BASE_CORE = """\
def parse(text, strict=False):
    return text.strip()


def helper(x):
    return x * 2
"""

CHANGED_CORE = """\
def parse(text, strict=False, encoding="utf8"):
    return text.strip()


def helper(x, scale=1):
    return x * 2 * scale


def brand_new(y):
    return y
"""


def make_pr(repo, *, with_test_change: bool = True):
    """A repo whose HEAD commit changes two signatures and adds one symbol."""
    repo.write("src/core.py", BASE_CORE)
    repo.write("src/caller.py", "from src.core import helper\n\n\ndef call():\n    return helper(3)\n")
    repo.write("tests/test_core.py", "from src.core import parse\n\n\ndef test_parse():\n    assert parse(' a ') == 'a'\n")
    repo.commit("base")
    repo.write("src/core.py", CHANGED_CORE)
    if with_test_change:
        repo.write(
            "tests/test_core.py",
            "from src.core import parse\n\n\ndef test_parse():\n    assert parse(' a ') == 'a'\n\n\n"
            "def test_more():\n    assert parse('b') == 'b'\n",
        )
    repo.commit("change")
    return repo


def test_every_script_parses():
    scripts = sorted(SCRIPTS.glob("*.py"))
    assert len(scripts) >= 8
    for script in scripts:
        ast.parse(script.read_text(encoding="utf-8"), filename=str(script))


# --------------------------------------------------------------------------- #
# Footprint / contracts / test delta
# --------------------------------------------------------------------------- #


def test_diff_reports_changed_files_and_totals(repo, tabs, run_script, fragment):
    make_pr(repo)

    summary = json.loads(
        run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1").stdout
    )

    assert summary["base"] == "HEAD~1"
    assert summary["totals"]["files"] == 2  # src/core.py + tests/test_core.py
    assert summary["totals"]["additions"] > 0
    assert summary["by_category"]["source"] == 1
    assert summary["by_category"]["test"] == 1
    assert fragment.title(tabs / "02-footprint.html") == "Footprint"
    assert fragment.title(tabs / "03-contracts-tests.html") == "Contracts & Tests"


def test_diff_detects_a_changed_signature(repo, tabs, run_script):
    make_pr(repo)

    summary = json.loads(
        run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1").stdout
    )

    changed = {s["name"] for s in summary["signature_changes"]}
    assert {"parse", "helper"} <= changed


def test_diff_reports_added_symbols(repo, tabs, run_script):
    make_pr(repo)

    summary = json.loads(
        run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1").stdout
    )

    assert "brand_new" in summary["new_symbols"]


def test_diff_flags_source_changed_without_its_tests(repo, tabs, run_script):
    make_pr(repo, with_test_change=False)

    summary = json.loads(
        run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1").stdout
    )

    assert "src/core.py" in summary["source_files_without_test_changes"]


def test_diff_does_not_flag_source_that_moved_with_its_tests(repo, tabs, run_script):
    make_pr(repo, with_test_change=True)

    summary = json.loads(
        run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1").stdout
    )

    assert summary["source_files_without_test_changes"] == []


def test_diff_excludes_its_own_generated_reports(repo, tabs, run_script):
    """The toolchain commits docs/*.html; the report must not review itself."""
    repo.write("src/core.py", BASE_CORE)
    repo.commit("base")
    repo.write("src/core.py", CHANGED_CORE)
    repo.write("docs/codemap.html", "<html>atlas</html>\n")
    repo.write("docs/pr-12.html", "<html>report</html>\n")
    repo.commit("change plus regenerated docs")

    summary = json.loads(
        run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1").stdout
    )

    assert sorted(summary["excluded_generated_docs"]) == ["docs/codemap.html", "docs/pr-12.html"]
    assert summary["totals"]["files"] == 1


def test_diff_reports_test_files_it_could_not_read(repo, tabs, run_script):
    """A test file deleted at HEAD cannot be read back, which weakens the
    coverage heuristic — the summary has to say so rather than under-count."""
    repo.write("src/core.py", BASE_CORE)
    repo.write("tests/test_gone.py", "def test_old():\n    assert True\n")
    repo.commit("base")
    repo.write("src/core.py", CHANGED_CORE)
    repo.delete("tests/test_gone.py")
    repo.commit("delete the test")

    summary = json.loads(
        run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1").stdout
    )

    assert summary["unreadable_test_files"] == ["tests/test_gone.py"]


def test_diff_omits_the_unreadable_key_when_every_test_reads(repo, tabs, run_script):
    make_pr(repo)

    summary = json.loads(
        run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1").stdout
    )

    assert "unreadable_test_files" not in summary


def test_diff_fails_loudly_on_an_unresolvable_base(repo, tabs, run_script):
    """Better to fail than to emit a report built from the wrong comparison."""
    repo.write("a.py", "x = 1\n")
    repo.commit("base")

    result = run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs,
                        "--base", "no-such-ref", expect_rc=1)

    assert "no-such-ref" in result.stderr
    assert list(tabs.iterdir()) == [], "a failed run must not leave half a report behind"


# --------------------------------------------------------------------------- #
# Blast radius
# --------------------------------------------------------------------------- #


def test_blast_radius_finds_untouched_callers(repo, tabs, run_script, fragment):
    """src/caller.py calls helper() and the PR never touches it — that caller
    silently inherits the new behavior, which is the whole point of the tab."""
    make_pr(repo)

    summary = json.loads(
        run_script(SCRIPTS / "analyze_blast_radius.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1").stdout
    )

    by_name = {s["name"]: s for s in summary["symbols"]}
    assert by_name["helper"]["untouched_callers"] == ["src/caller.py"]
    assert by_name["helper"]["outside"] == 1
    assert fragment.title(tabs / "04-blast-radius.html") == "Blast Radius"


def test_blast_radius_reports_no_callers_for_a_brand_new_symbol(repo, tabs, run_script):
    make_pr(repo)

    summary = json.loads(
        run_script(SCRIPTS / "analyze_blast_radius.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1").stdout
    )

    by_name = {s["name"]: s for s in summary["symbols"]}
    assert by_name["brand_new"]["callers"] == 0
    assert by_name["brand_new"]["untouched_callers"] == []


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #


def test_assemble_defaults_to_the_pr_review_label(tmp_path, tabs, run_script):
    """The only intended divergence from code-visualization's assemble.py."""
    tabs.joinpath("01-summary.html").write_text("<!-- tab: Summary -->\n<p>x</p>\n", encoding="utf-8")
    report = tmp_path / "pr-1.html"

    run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", report, "--title", "PR 1")

    assert "PULL REQUEST REVIEW" in report.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# The two skills share four scripts by copy; drift between them is a bug.
# --------------------------------------------------------------------------- #


def test_shared_scripts_stay_byte_identical_across_the_two_skills():
    """Each skill directory ships standalone, so the copies are deliberate.

    Fixing one copy and forgetting the other is the failure this guards: the
    duplication is only safe while the files are actually identical.
    """
    shared = ["common.py", "extract_tabs.py", "check_codemap_state.py", "verify_citations.py"]
    drifted = [
        name for name in shared
        if (SKILLS / "code-visualization" / "scripts" / name).read_bytes()
        != (SKILLS / "pr-visualization" / "scripts" / name).read_bytes()
    ]
    assert drifted == [], f"shared scripts diverged between the two skills: {drifted}"


def test_assemble_differs_only_in_its_default_label():
    """assemble.py is intentionally *not* identical — pin exactly how."""
    cv = (SKILLS / "code-visualization" / "scripts" / "assemble.py").read_text(encoding="utf-8")
    pr = (SKILLS / "pr-visualization" / "scripts" / "assemble.py").read_text(encoding="utf-8")

    assert cv != pr
    assert cv.replace('default="CODEBASE ATLAS"', 'default="PULL REQUEST REVIEW"') == pr


# --------------------------------------------------------------------------- #
# Review-round fixes: JS keywords, worktree flows, renames, blast-radius honesty
# --------------------------------------------------------------------------- #


def test_lint_fragments_catches_stray_script_and_style(tabs, run_script):
    tabs.joinpath("01-summary.html").write_text(
        "<!-- tab: Summary -->\n<p>fine</p>\n"
        '<div class="viz" data-render="treemap"></div>\n'
        '<script type="application/json">{"items":[]}</script>\n', encoding="utf-8")
    tabs.joinpath("05-flow-impact.html").write_text(
        "<!-- tab: Flow Impact -->\n<script>alert(1)</script>\n<style>body{}</style>\n", encoding="utf-8")

    result = run_script(SCRIPTS / "lint_fragments.py", "--tabs-dir", tabs, expect_rc=1)

    report = json.loads(result.stdout)
    problems = {(p["fragment"], p["problem"].split(" ")[0]) for p in report["problems"]}
    assert ("05-flow-impact.html", "executable") in problems
    assert ("05-flow-impact.html", "<style>") in problems
    assert all(p[0] != "01-summary.html" for p in problems), "the JSON data block is legal"


def test_diff_reports_a_docs_only_diff_as_an_error(repo, tabs, run_script):
    """Regenerated docs alone are not a reviewable change."""
    repo.write("src/core.py", BASE_CORE)
    repo.write("docs/codemap.html", "<html>old</html>\n")
    repo.commit("base")
    repo.write("docs/codemap.html", "<html>new</html>\n")
    repo.commit("only regenerate the atlas")

    result = run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs,
                        "--base", "HEAD~1", expect_rc=1)

    assert "generated report docs" in json.loads(result.stdout)["error"]
