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


def test_diff_reports_a_docs_only_diff_as_a_note_not_a_failure(repo, tabs, run_script):
    """Regenerated docs alone are a legitimate (empty-after-exclusion) outcome:
    exit 0 with a note, matching analyze_blast_radius's convention — a docs-only
    PR must not read as an analyzer failure."""
    repo.write("src/core.py", BASE_CORE)
    repo.write("docs/codemap.html", "<html>old</html>\n")
    repo.commit("base")
    repo.write("docs/codemap.html", "<html>new</html>\n")
    repo.commit("only regenerate the atlas")

    result = run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs,
                        "--base", "HEAD~1", expect_rc=0)

    summary = json.loads(result.stdout)
    assert "generated report docs" in summary["note"]
    assert summary["totals"]["files"] == 0


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


def test_js_control_flow_keywords_are_not_symbols(repo, tabs, run_script):
    # `for (` / `while (` / `switch (` match the method-shorthand shape; they
    # used to surface as changed signatures and junk blast-radius symbols.
    repo.write("src/app.js", "export function handler(x) { return x; }\n")
    repo.commit("base")
    repo.write("src/app.js",
               "export function handler(x) {\n"
               "  for (const item of x) {\n"
               "    while (item.busy) {\n"
               "      switch (item.kind) {\n"
               "        default: break;\n"
               "      }\n"
               "    }\n"
               "  }\n"
               "  return x;\n"
               "}\n")
    repo.commit("add loops")

    result = run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1")
    summary = json.loads(result.stdout)
    sig_names = {s["name"] for s in summary["signature_changes"]} | set(summary["new_symbols"])
    assert not {"for", "while", "switch"} & sig_names

    result = run_script(SCRIPTS / "analyze_blast_radius.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1")
    blast = json.loads(result.stdout)
    traced = {s["name"] for s in blast.get("symbols", [])}
    assert not {"for", "while", "switch"} & traced


def test_worktree_on_mainline_reviews_uncommitted_changes(repo, tabs, run_script):
    # "Review my uncommitted changes" on a checkout of main used to raise from
    # base auto-detection (every candidate's merge-base IS HEAD).
    repo.git("checkout", "-qb", "main")
    repo.write("src/core.py", BASE_CORE)
    repo.commit("base")
    repo.write("src/core.py", BASE_CORE + "\n\ndef fresh(x):\n    return x + 1\n")
    repo.write("src/brand_new.py", "def created(y):\n    return y * 2\n")  # untracked

    result = run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs, "--worktree")

    summary = json.loads(result.stdout)
    paths = {f["path"] for f in summary["risk_ordered_files"]}
    assert "src/core.py" in paths
    assert "src/brand_new.py" in paths, "untracked files must be part of a worktree review"


def test_head_with_worktree_is_rejected(repo, tabs, run_script):
    repo.write("a.py", "x = 1\n")
    repo.commit("base")

    result = run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs,
                        "--worktree", "--head", "HEAD~1", expect_rc=2)

    assert "--worktree" in result.stderr


def test_renames_are_reported(repo, tabs, run_script):
    repo.write("src/old_name.py", BASE_CORE)
    repo.commit("base")
    repo.git("mv", "src/old_name.py", "src/new_name.py")
    repo.commit("rename")

    result = run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1")

    summary = json.loads(result.stdout)
    assert {"from": "src/old_name.py", "to": "src/new_name.py"} in summary["renames"]


def test_blast_radius_reports_truncation_and_caveat(repo, tabs, run_script):
    files = {}
    for i in range(6):
        files[f"src/mod{i}.py"] = f"def widget_fn_{i}(x):\n    return x\n"
        repo.write(f"src/mod{i}.py", files[f"src/mod{i}.py"])
    repo.commit("base")
    for i in range(6):
        repo.write(f"src/mod{i}.py", f"def widget_fn_{i}(x, y=0):\n    return x + y\n")
    repo.commit("change all")

    result = run_script(SCRIPTS / "analyze_blast_radius.py", repo.path, "--tabs-dir", tabs,
                        "--base", "HEAD~1", "--max-symbols", "3")

    summary = json.loads(result.stdout)
    assert summary["symbols_traced"] == 3
    assert summary["symbols_total"] == 6
    assert summary["symbols_not_traced"] == 3
    assert "name-match" in summary["caveat"] or "textual" in summary["caveat"]
    body = (tabs / "04-blast-radius.html").read_text(encoding="utf-8")
    assert "3 of 6" in body


def test_same_named_def_in_another_file_is_not_a_caller(repo, tabs, run_script):
    repo.write("src/alpha.py", "def compute_widget(x):\n    return x\n")
    repo.write("src/beta.py", "def compute_widget(x):\n    return x * 2\n")
    repo.commit("base")
    repo.write("src/alpha.py", "def compute_widget(x, y=0):\n    return x + y\n")
    repo.commit("change alpha's")

    result = run_script(SCRIPTS / "analyze_blast_radius.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1")

    summary = json.loads(result.stdout)
    row = next(s for s in summary["symbols"] if s["name"] == "compute_widget")
    assert "src/beta.py" not in row["untouched_callers"], \
        "a same-named definition elsewhere is not a call site"


def test_assemble_template_override_beats_the_pr_report_theme(repo, tabs, run_script, tmp_path):
    # --template fully replaces the bundled theme rather than layering on it:
    # pointed at the code-visualization template, this assembler emits the atlas
    # theme, not its own default. Nothing in the PR workflow writes an atlas —
    # this pins the flag's semantics for anyone reusing the assembler.
    tabs.joinpath("01-overview.html").write_text("<!-- tab: Overview -->\n<p>atlas</p>\n", encoding="utf-8")
    out = tmp_path / "codemap.html"
    cv_template = SKILLS / "code-visualization" / "assets" / "template.html"

    run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", out,
               "--title", "T", "--label", "CODEBASE ATLAS", "--template", cv_template)

    html = out.read_text(encoding="utf-8")
    # The atlas template is dark-default with a light media block; the PR
    # template is the inverse and carries diff-snippet styles the atlas lacks.
    assert "color-scheme: light){" in html
    assert "diff-snippet" not in html
    assert "CODEBASE ATLAS" in html


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


def test_changed_file_coverage_is_reported_when_an_artifact_exists(repo, tabs, run_script):
    make_pr(repo)
    repo.write("coverage.xml", """<?xml version="1.0"?>
<coverage><packages><package name="src"><classes>
  <class filename="src/core.py"><lines>
    <line number="1" hits="1"/><line number="2" hits="0"/>
  </lines></class>
</classes></package></packages></coverage>
""")

    summary = json.loads(
        run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1").stdout
    )

    assert summary["coverage"]["format"] == "cobertura"
    entries = {c["path"]: c["line_coverage_pct"] for c in summary["coverage"]["changed_files"]}
    assert entries["src/core.py"] == 50.0
    body = (tabs / "03-contracts-tests.html").read_text(encoding="utf-8")
    assert "Measured coverage of changed files" in body


def test_no_coverage_artifact_means_no_coverage_key(repo, tabs, run_script):
    make_pr(repo)

    summary = json.loads(
        run_script(SCRIPTS / "analyze_diff.py", repo.path, "--tabs-dir", tabs, "--base", "HEAD~1").stdout
    )

    assert "coverage" not in summary
