"""Regression tests for the code-visualization skill's analyzer scripts.

Each analyzer is driven as a subprocess over a throwaway repo, exactly as the
skill drives it, and asserted on twice: the JSON summary it prints (what the
agent reads) and the HTML fragment it writes (what the reader sees). The point
is to pin the signals the atlas is built on — cycles, churn ranking, citation
breakage, staleness verdicts — so a refactor that quietly stops reporting one
of them fails here instead of shipping a confidently wrong atlas.
"""

import ast
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-visualization" / "scripts"


def test_every_script_parses():
    scripts = sorted(SCRIPTS.glob("*.py"))
    assert len(scripts) >= 8
    for script in scripts:
        ast.parse(script.read_text(encoding="utf-8"), filename=str(script))


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #


def test_inventory_counts_languages_and_test_share(repo, tabs, run_script, fragment):
    repo.write("src/a.py", "def a():\n    return 1\n")
    repo.write("src/b.py", "def b(x):\n    if x:\n        return 2\n    return 3\n")
    repo.write("web/app.js", "export function c() { return 1; }\n")
    repo.write("tests/test_a.py", "def test_a():\n    assert True\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_inventory.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["languages"]["Python"] >= 3
    assert summary["languages"]["JavaScript"] == 1
    assert summary["total_loc"] >= 8
    # tests/test_a.py is 2 of the 9 lines — the share must be non-zero and sane.
    assert 0 < summary["test_loc_share_pct"] < 100
    assert fragment.title(tabs / "02-inventory.html") == "Inventory"


def test_inventory_ranks_largest_file_first(repo, tabs, run_script):
    repo.write("small.py", "x = 1\n")
    repo.write("big.py", "".join(f"line_{i} = {i}\n" for i in range(60)))

    summary = json.loads(run_script(SCRIPTS / "analyze_inventory.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["largest_files"][0]["path"] == "big.py"
    assert summary["largest_files"][0]["loc"] == 60


def test_inventory_escapes_html_in_paths(repo, tabs, run_script, fragment):
    """A path with HTML-significant characters must not inject into the fragment."""
    # Windows forbids <>:"|?* in filenames, so & is the portable payload here.
    repo.write("weird/a&b.py", "x = 1\n")

    run_script(SCRIPTS / "analyze_inventory.py", repo.path, "--tabs-dir", tabs)

    body = fragment.body(tabs / "02-inventory.html")
    assert "weird/a&amp;b.py" in body
    assert "weird/a&b.py" not in body


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


def test_deps_detects_import_cycle(repo, tabs, run_script):
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/a.py", "from pkg import b\n")
    repo.write("pkg/b.py", "from pkg import a\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert ["pkg/a.py", "pkg/b.py"] in [sorted(c) for c in summary["file_cycles"]]


def test_deps_reports_no_cycle_for_a_chain(repo, tabs, run_script):
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/a.py", "from pkg import b\n")
    repo.write("pkg/b.py", "from pkg import c\n")
    repo.write("pkg/c.py", "value = 1\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["file_cycles"] == []
    assert summary["import_edges"] >= 2


def test_deps_counts_fan_in_for_a_shared_module(repo, tabs, run_script):
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/util.py", "def helper():\n    return 1\n")
    for name in ("one", "two", "three"):
        repo.write(f"pkg/{name}.py", "from pkg import util\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in["pkg/util.py"] == 3


def test_deps_extracts_javascript_imports(repo, tabs, run_script):
    repo.write("src/index.js", "import { thing } from './lib';\n")
    repo.write("src/lib.js", "export const thing = 1;\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["import_edges"] >= 1
    fan_in = {row["path"]: row["fan_in"] for row in summary["top_fan_in_files"]}
    assert fan_in.get("src/lib.js") == 1


# --------------------------------------------------------------------------- #
# Module grouping
#
# The failure these pin down: a repo whose code lives under two arms used to
# collapse to one node per arm, so a 12-module monorepo rendered as two boxes
# and one arrow. Grouping has to peel packaging directories (src/, app/) while
# keeping directories that merely sound structural but actually hold code.
# --------------------------------------------------------------------------- #


def _write_tree(repo, arms, ext="py", files_per_module=3):
    for arm, modules in arms.items():
        for module in modules:
            for i in range(files_per_module):
                repo.write(f"{arm}/{module}/f{i}.{ext}", "x = 1\n" * 40)


def test_deps_splits_a_two_arm_monorepo_into_submodules(repo, tabs, run_script):
    _write_tree(repo, {
        "frontend/src": ["components", "pages", "hooks", "api", "state", "utils"],
        "backend/app": ["routers", "services", "models", "db", "auth", "tasks"],
    })

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    displays = {m["display"] for m in summary["module_list"]}
    assert displays == {
        "frontend/components", "frontend/pages", "frontend/hooks",
        "frontend/api", "frontend/state", "frontend/utils",
        "backend/routers", "backend/services", "backend/models",
        "backend/db", "backend/auth", "backend/tasks",
    }
    assert summary["top_level_arms"] == ["backend", "frontend"]
    assert sorted(summary["structural_dirs_peeled"]) == ["app", "src"]


def test_deps_strips_src_and_repo_name_from_module_names(repo, tabs, run_script):
    name = Path(repo.path).name
    _write_tree(repo, {f"src/{name}": ["core", "api", "cli", "db", "render", "utils"]})

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    modules = {m["display"]: m["id"] for m in summary["module_list"]}
    assert set(modules) == {"core", "api", "cli", "db", "render", "utils"}
    # The id keeps the real path so citations elsewhere in the atlas resolve.
    assert modules["core"] == f"src/{name}/core"


def test_deps_keeps_a_structural_sounding_dir_that_holds_code(repo, tabs, run_script):
    # "app" is a structural hint, but this one is a module: the code is in it,
    # not merely under it. The pass-through check has to override the name.
    repo.write("backend/app/handlers.py", "x = 1\n" * 200)
    repo.write("backend/app/models.py", "x = 1\n" * 200)
    repo.write("backend/app/helpers/tiny.py", "x = 1\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    ids = {m["id"] for m in summary["module_list"]}
    assert "backend/app" in ids


def test_deps_does_not_over_split_a_flat_repo(repo, tabs, run_script):
    for i in range(8):
        repo.write(f"pkg/f{i}.py", "x = 1\n" * 40)

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    assert [m["display"] for m in summary["module_list"]] == ["pkg"]


def test_deps_assigns_loose_files_to_the_directory_holding_them(repo, tabs, run_script):
    repo.write("main.py", "x = 1\n" * 10)
    _write_tree(repo, {"frontend/src": ["a", "b"], "backend/app": ["c", "d"]})

    summary = json.loads(run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs).stdout)

    by_display = {m["display"]: m for m in summary["module_list"]}
    assert by_display["(root)"]["files"] == 1
    assert sum(m["files"] for m in summary["module_list"]) == 13


def test_deps_depth_override_counts_only_non_structural_dirs(repo, tabs, run_script):
    _write_tree(repo, {"frontend/src": ["components", "pages"], "backend/app": ["routers", "db"]})

    summary = json.loads(
        run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs, "--depth", "1").stdout)

    assert {m["display"] for m in summary["module_list"]} == {"frontend", "backend"}


# --------------------------------------------------------------------------- #
# Hotspots (churn x complexity, so it needs real git history)
# --------------------------------------------------------------------------- #


def test_hotspots_ranks_churn_above_stable_code(repo, tabs, run_script):
    for i in range(5):
        repo.write("hot.py", f"def f(x):\n    if x:\n        return {i}\n    return 0\n")
        repo.commit(f"change {i}")
    repo.write("cold.py", "def g():\n    return 1\n")
    repo.commit("add cold")

    summary = json.loads(run_script(SCRIPTS / "analyze_hotspots.py", repo.path, "--tabs-dir", tabs).stdout)

    ranked = [h["path"] for h in summary["top_hotspots"]]
    assert ranked[0] == "hot.py"
    by_path = {h["path"]: h for h in summary["top_hotspots"]}
    assert by_path["hot.py"]["churn"] == 5
    assert by_path["cold.py"]["churn"] == 1
    assert by_path["hot.py"]["score"] > by_path["cold.py"]["score"]


def test_hotspots_since_window_excludes_older_commits(repo, tabs, run_script):
    repo.write("ancient.py", "x = 1\n")
    repo.commit("long ago", date="2015-01-01T00:00:00")
    repo.write("recent.py", "y = 2\n")
    repo.commit("just now")

    summary = json.loads(
        run_script(SCRIPTS / "analyze_hotspots.py", repo.path, "--tabs-dir", tabs, "--since", "30 days ago").stdout
    )

    # Effort follows recent change, so the window must actually drop old commits.
    # Files outside it still appear, but with no churn credited to them.
    assert summary["commits_analyzed"] == 1
    churn = {h["path"]: h["churn"] for h in summary["top_hotspots"]}
    assert churn == {"recent.py": 1, "ancient.py": 0}


# --------------------------------------------------------------------------- #
# Codemap staleness verdicts
# --------------------------------------------------------------------------- #


def codemap_state(run_script, repo):
    result = run_script(SCRIPTS / "check_codemap_state.py", repo.path, expect_rc=None)
    return json.loads(result.stdout), result.returncode


def test_codemap_missing_reports_rc_2(repo, run_script):
    repo.write("m.py", "x = 1\n")
    repo.commit("base")

    state, rc = codemap_state(run_script, repo)

    assert state["verdict"] == "missing"
    assert rc == 2


def test_codemap_current_when_meta_sha_is_head(repo, run_script):
    repo.write("m.py", "x = 1\n")
    sha = repo.commit("base")
    repo.write("docs/codemap.html", f'<html><div class="doc-meta">generated from {sha}</div></html>\n')
    repo.commit("codemap")

    state, rc = codemap_state(run_script, repo)

    assert state["verdict"] == "current"
    assert state["meta_sha"] == sha
    assert rc == 0


def test_codemap_stale_after_a_source_commit(repo, run_script):
    repo.write("m.py", "x = 1\n")
    sha = repo.commit("base")
    repo.write("docs/codemap.html", f'<html><div class="doc-meta">generated from {sha}</div></html>\n')
    repo.commit("codemap")
    repo.write("m2.py", "y = 2\n")
    repo.commit("more source")

    state, rc = codemap_state(run_script, repo)

    assert state["verdict"] == "stale"
    assert state["files_changed_since"] >= 1
    assert rc == 1


def test_codemap_generated_docs_do_not_count_as_source_change(repo, run_script):
    """A codemap that only ever changed itself is current, not stale."""
    repo.write("m.py", "x = 1\n")
    sha = repo.commit("base")
    repo.write("docs/codemap.html", f'<html><div class="doc-meta">from {sha}</div></html>\n')
    repo.commit("codemap")
    repo.write("docs/pr-7.html", "<html>report</html>\n")
    repo.commit("a generated pr report")

    state, rc = codemap_state(run_script, repo)

    assert state["files_changed_since"] == 0
    assert state["generated_docs_changed_since"] >= 1
    assert state["verdict"] == "current"
    assert rc == 0


def test_codemap_unknown_vintage_without_a_resolvable_sha(repo, run_script):
    repo.write("m.py", "x = 1\n")
    repo.commit("base")
    repo.write("docs/codemap.html", '<html><div class="doc-meta">no sha here</div></html>\n')
    repo.commit("codemap")

    state, rc = codemap_state(run_script, repo)

    assert state["verdict"] == "unknown-vintage"
    assert rc == 1


def test_codemap_conflict_markers_beat_every_other_verdict(repo, run_script):
    repo.write("m.py", "x = 1\n")
    repo.commit("base")
    repo.write("docs/codemap.html", "<html>\n<<<<<<< HEAD\na\n=======\nb\n>>>>>>> other\n</html>\n")
    repo.commit("committed a conflict")

    state, rc = codemap_state(run_script, repo)

    assert state["verdict"] == "conflict-markers"
    assert "rebuild" in state["detail"]
    assert rc == 1


def test_codemap_merge_commit_is_flagged_as_a_suspect_splice(repo, run_script):
    repo.write("m.py", "x = 1\n")
    sha = repo.commit("base")
    repo.write("docs/codemap.html", f'<html><div class="doc-meta">from {sha}</div>main</html>\n')
    repo.commit("codemap on main")
    main = repo.git("rev-parse", "--abbrev-ref", "HEAD").strip()
    repo.git("checkout", "-q", "-b", "side", "HEAD~1")
    repo.write("docs/codemap.html", f'<html><div class="doc-meta">from {sha}</div>side</html>\n')
    repo.commit("codemap on side")
    repo.git("checkout", "-q", main)
    repo.git("merge", "-q", "-X", "ours", "side", "-m", "merge side")

    state, rc = codemap_state(run_script, repo)

    assert state["verdict"] == "merge-resolution-suspect"
    assert state["last_commit_touching_codemap"]["is_merge"] is True
    # Both parents are recorded so a reviewer can diff the two revisions.
    assert state["last_commit_touching_codemap"]["parent1"]
    assert state["last_commit_touching_codemap"]["parent2"]
    assert rc == 1


# --------------------------------------------------------------------------- #
# Citation verification
# --------------------------------------------------------------------------- #


def test_citations_resolve_and_report_the_cited_line(repo, tabs, run_script):
    repo.write("src/core.py", "def parse(text):\n    return text.strip()\n")
    tabs.joinpath("01-overview.html").write_text(
        "<!-- tab: Overview -->\n<p>See <code>src/core.py:2</code>.</p>\n", encoding="utf-8"
    )

    report = json.loads(run_script(SCRIPTS / "verify_citations.py", repo.path, "--tabs-dir", tabs).stdout)

    assert report["citations_ok"] == 1
    assert report["citations_broken"] == 0
    (citation,) = report["citations"]
    assert citation["status"] == "ok"
    assert citation["resolved"] == "src/core.py"
    # The checker prints the cited line so a reader can judge whether it still
    # supports the claim — that content is the whole point of the tab.
    assert citation["cited_content"][0]["content"] == ["    return text.strip()"]


@pytest.mark.parametrize(
    "citation, expected",
    [
        ("src/core.py:99", "line-out-of-range"),
        ("src/nope.py:1", "missing"),
    ],
)
def test_citations_report_broken_references(repo, tabs, run_script, citation, expected):
    repo.write("src/core.py", "def parse(text):\n    return text.strip()\n")
    tabs.joinpath("01-overview.html").write_text(
        f"<!-- tab: Overview -->\n<p>See <code>{citation}</code>.</p>\n", encoding="utf-8"
    )

    result = run_script(SCRIPTS / "verify_citations.py", repo.path, "--tabs-dir", tabs, expect_rc=1)

    assert expected in result.stdout


def test_citations_exit_zero_when_everything_resolves(repo, tabs, run_script):
    repo.write("src/core.py", "def parse(text):\n    return text.strip()\n")
    tabs.joinpath("01-overview.html").write_text(
        "<!-- tab: Overview -->\n<p>See <code>src/core.py:1</code>.</p>\n", encoding="utf-8"
    )

    run_script(SCRIPTS / "verify_citations.py", repo.path, "--tabs-dir", tabs, expect_rc=0)


# --------------------------------------------------------------------------- #
# assemble <-> extract_tabs round trip
# --------------------------------------------------------------------------- #


def test_assembled_atlas_round_trips_back_to_fragments(tmp_path, tabs, run_script, fragment):
    tabs.joinpath("01-overview.html").write_text(
        "<!-- tab: Overview -->\n<h2>Hi</h2><p>body one</p>\n", encoding="utf-8"
    )
    tabs.joinpath("05-risks.html").write_text(
        "<!-- tab: Risks &amp; Gaps -->\n<h2>Risks</h2><ul><li>x</li></ul>\n", encoding="utf-8"
    )
    atlas = tmp_path / "codemap.html"

    run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", atlas,
               "--title", "demo — Codebase Atlas", "--meta", "generated at deadbee")
    recovered = tmp_path / "recovered"
    result = run_script(SCRIPTS / "extract_tabs.py", atlas, "--out-dir", recovered)

    extracted = json.loads(result.stdout)
    assert extracted["title"] == "demo — Codebase Atlas"
    assert extracted["meta"] == "generated at deadbee"
    # Known tabs keep their canonical prefix; a custom tab ("risks" is not a
    # canonical atlas tab) takes the first free slot at its display position.
    assert extracted["fragments"] == ["01-overview.html", "02-risks.html"]
    assert fragment.title(recovered / "01-overview.html") == "Overview"
    assert "body one" in fragment.body(recovered / "01-overview.html")
    assert "<li>x</li>" in fragment.body(recovered / "02-risks.html")


def test_assemble_skips_empty_fragments(tmp_path, tabs, run_script):
    tabs.joinpath("01-overview.html").write_text("<!-- tab: Overview -->\n<p>real</p>\n", encoding="utf-8")
    tabs.joinpath("02-empty.html").write_text("<!-- tab: Empty -->\n\n", encoding="utf-8")
    atlas = tmp_path / "codemap.html"

    result = run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", atlas, "--title", "T")

    assert 'data-tab="overview"' in atlas.read_text(encoding="utf-8")
    assert 'data-tab="empty"' not in atlas.read_text(encoding="utf-8")
    assert "1 tabs" in result.stdout or "1 tab" in result.stdout


def test_atlas_survives_the_mermaid_cdn_being_unreachable(tmp_path, tabs, run_script):
    """Diagrams render via a CDN, so the atlas must degrade, not break, offline.

    Mermaid is fetched at runtime rather than vendored. That is a real network
    dependency, and the only thing making it acceptable is the fallback that
    shows the diagram source as preformatted text when the fetch fails. This
    pins the fallback so it cannot be dropped silently.
    """
    tabs.joinpath("01-overview.html").write_text(
        '<!-- tab: Overview -->\n<pre class="mermaid">graph TD; a--&gt;b;</pre>\n', encoding="utf-8"
    )
    atlas = tmp_path / "codemap.html"

    run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", atlas, "--title", "T")

    html = atlas.read_text(encoding="utf-8")
    # Nothing is fetched while parsing: the loader is injected by script.
    assert "<script src=" not in html
    assert "cdn.jsdelivr.net/npm/mermaid" in html
    assert "onerror" in html, "no fallback if the mermaid fetch fails"
    # The diagram source survives in the document either way.
    assert "graph TD" in html


def test_assemble_defaults_to_the_atlas_label(tmp_path, tabs, run_script):
    tabs.joinpath("01-overview.html").write_text("<!-- tab: Overview -->\n<p>x</p>\n", encoding="utf-8")
    atlas = tmp_path / "codemap.html"

    run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", atlas, "--title", "T")

    assert "CODEBASE ATLAS" in atlas.read_text(encoding="utf-8")


def test_extraction_preserves_canonical_numbering_when_a_tab_was_dropped(tmp_path, tabs, run_script):
    # An atlas without git history legitimately has no Hotspots (04). Under
    # display-order renumbering, extraction used to shift flows/boundaries/...
    # down one slot; re-running the analyzers then wrote a NEW 04-hotspots
    # beside the shifted 04-flows and the documented
    # `--fragments 01,05,06,07,08` verified the wrong set, silently skipping
    # Critical Flows. Canonical prefixes must survive the round trip.
    for name, title in [
        ("01-overview.html", "Overview"), ("02-inventory.html", "Inventory"),
        ("03-dependencies.html", "Dependencies"),  # 04-hotspots deliberately absent
        ("05-flows.html", "Critical Flows"), ("06-boundaries.html", "Boundaries &amp; Ownership"),
        ("07-invariants.html", "Invariants &amp; Risks"), ("08-glossary.html", "Glossary"),
    ]:
        tabs.joinpath(name).write_text(f"<!-- tab: {title} -->\n<p>{name}</p>\n", encoding="utf-8")
    atlas = tmp_path / "codemap.html"
    run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", atlas, "--title", "T")

    recovered = tmp_path / "recovered"
    result = run_script(SCRIPTS / "extract_tabs.py", atlas, "--out-dir", recovered)

    extracted = json.loads(result.stdout)
    assert extracted["fragments"] == [
        "01-overview.html", "02-inventory.html", "03-dependencies.html",
        "05-flows.html", "06-boundaries.html", "07-invariants.html", "08-glossary.html",
    ]


def test_hotspots_correct_when_target_is_a_subdirectory_of_a_repo(repo, tabs, run_script):
    # git log paths are toplevel-relative; analyzing a subdirectory used to
    # match them against subdir-relative paths, giving every file churn 0 and
    # a confidently wrong tab.
    for i in range(3):
        repo.write("service/api.py", f"def handler(x):\n    if x:\n        return {i}\n    return 0\n")
        repo.commit(f"change {i}")
    repo.write("unrelated/other.py", "x = 1\n")
    repo.commit("outside the subtree")

    summary = json.loads(
        run_script(SCRIPTS / "analyze_hotspots.py", repo.path / "service", "--tabs-dir", tabs).stdout
    )

    by_path = {h["path"]: h for h in summary["top_hotspots"]}
    assert by_path["api.py"]["churn"] == 3
    assert "unrelated/other.py" not in by_path
    assert summary["commits_analyzed"] == 3  # the outside-subtree commit is not counted


def test_hotspots_reports_shallow_history_flag(repo, tabs, run_script):
    repo.write("a.py", "x = 1\n")
    repo.commit("one")

    summary = json.loads(run_script(SCRIPTS / "analyze_hotspots.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["shallow_history"] is False


# --------------------------------------------------------------------------- #
# Coverage tab (renders existing artifacts; never runs tests)
# --------------------------------------------------------------------------- #

COBERTURA = """<?xml version="1.0"?>
<coverage>
  <packages><package name="src"><classes>
    <class filename="src/core.py"><lines>
      <line number="1" hits="1"/><line number="2" hits="1"/>
      <line number="3" hits="0"/><line number="4" hits="0"/>
    </lines></class>
    <class filename="src/util.py"><lines>
      <line number="1" hits="1"/><line number="2" hits="1"/>
    </lines></class>
  </classes></package></packages>
</coverage>
"""


def test_coverage_tab_renders_cobertura(repo, tabs, run_script, fragment):
    repo.write("src/core.py", "def f(x):\n    if x:\n        return 1\n    return 0\n")
    repo.write("src/util.py", "def g():\n    return 2\n")
    repo.write("coverage.xml", COBERTURA)

    summary = json.loads(run_script(SCRIPTS / "analyze_coverage.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["format"] == "cobertura"
    assert summary["files_measured"] == 2
    assert summary["overall_line_coverage_pct"] == 66.7  # 4 of 6 lines
    assert fragment.title(tabs / "09-coverage.html") == "Coverage"
    assert "50%" in fragment.body(tabs / "09-coverage.html")  # core.py 2/4


def test_coverage_absent_asks_instead_of_fabricating(repo, tabs, run_script):
    repo.write("src/core.py", "def f():\n    return 1\n")

    result = run_script(SCRIPTS / "analyze_coverage.py", repo.path, "--tabs-dir", tabs)

    summary = json.loads(result.stdout)
    assert "ask_user" in summary
    assert not (tabs / "09-coverage.html").exists()


def test_coverage_hints_at_convertible_artifacts(repo, tabs, run_script):
    repo.write("src/core.py", "def f():\n    return 1\n")
    repo.write(".coverage", "sqlite-ish blob")

    summary = json.loads(run_script(SCRIPTS / "analyze_coverage.py", repo.path, "--tabs-dir", tabs).stdout)

    assert any("coverage xml" in h for h in summary["hints"])


def test_coverage_lcov_and_unmeasured_files(repo, tabs, run_script):
    repo.write("src/a.js", "export const a = () => 1;\n")
    repo.write("src/b.js", "export const b = () => { if (x) { return 1; } return 2; };\n")
    repo.write("lcov.info", "SF:src/a.js\nDA:1,1\nDA:2,0\nend_of_record\n")

    summary = json.loads(run_script(SCRIPTS / "analyze_coverage.py", repo.path, "--tabs-dir", tabs).stdout)

    assert summary["format"] == "lcov"
    assert summary["files_measured"] == 1
    assert "src/b.js" in summary["unmeasured_complex"]


def test_extraction_gives_coverage_tab_its_canonical_prefix(tmp_path, tabs, run_script):
    tabs.joinpath("01-overview.html").write_text("<!-- tab: Overview -->\n<p>o</p>\n", encoding="utf-8")
    tabs.joinpath("09-coverage.html").write_text("<!-- tab: Coverage -->\n<p>c</p>\n", encoding="utf-8")
    atlas = tmp_path / "codemap.html"
    run_script(SCRIPTS / "assemble.py", "--tabs-dir", tabs, "--out", atlas, "--title", "T")

    result = run_script(SCRIPTS / "extract_tabs.py", atlas, "--out-dir", tmp_path / "rec")

    assert json.loads(result.stdout)["fragments"] == ["01-overview.html", "09-coverage.html"]
