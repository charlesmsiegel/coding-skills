"""Tests for update-docs' staleness checker.

The value of this script is entirely in its precision: a doc checker that cries
wolf gets ignored, and then the docs rot anyway. So most of these pin what it must
*not* report — placeholders, home-directory paths, prose in backticks — alongside
the four things it must catch.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "update-docs" / "scripts" / "check_doc_staleness.py"


@pytest.fixture
def checker(load_module):
    return load_module(SCRIPT.parent, "check_doc_staleness")


# ---- citation parsing ------------------------------------------------------- #

def test_paths_are_found_in_backticks_links_and_prose(checker):
    text = (
        "See `src/api/client.py:88` for the entry point.\n"
        "The [visualizer](skills/code-visualization/) owns the atlas.\n"
        "Bare mention of pkg/cmd/run/list.go in a sentence.\n"
    )
    assert checker.parse_citations(text) == [
        ("src/api/client.py", 88),
        ("skills/code-visualization/", None),
        ("pkg/cmd/run/list.go", None),
    ]


def test_a_directory_link_is_captured_even_without_an_extension(checker):
    # This is why markdown links are parsed separately: `[x](skills/foo/)` has no
    # file suffix, so the bare-token pattern cannot see it, and the directory then
    # looks undocumented when it is documented.
    assert checker.parse_citations("[the skill](skills/foo/)") == [("skills/foo/", None)]


@pytest.mark.parametrize("text", [
    "run `git log --oneline` first",              # a command
    "the `--format` flag",                        # a flag
    "`Path` and `dict[str, int]`",                # types
    "see https://github.com/cli/cli/issues/1",    # a URL
    "[docs](https://example.com/a/b.html)",       # a link to a URL
    "installs into `~/.claude/skills/`",          # outside the repo
    "writes to `/etc/hosts`",                     # absolute
    "each `skills/<skill>/scripts/` directory",   # a placeholder
    "matches `skills/*/SKILL.md`",                # a glob
    "`$HOME/config.toml`",                        # a shell expansion
    "[anchor](#installing-locally)",              # an in-page anchor
    "the `README` file",                          # no separator: prose
])
def test_things_that_are_not_repo_paths_are_never_cited(checker, text):
    assert checker.parse_citations(text) == []


def test_line_ranges_resolve_to_their_first_line(checker):
    assert checker.parse_citations("`a/b.py:10-20`") == [("a/b.py", 10)]


# ---- resolution ------------------------------------------------------------- #

def test_a_citation_resolves_from_the_root_the_page_or_a_suffix(checker):
    index = {"skills", "skills/viz", "skills/viz/assets", "skills/viz/assets/template.html",
             "README.md", "docs", "docs/guide.md"}
    assert checker.resolve("README.md", index) == "README.md"
    assert checker.resolve("guide.md", index, page_dir="docs") == "docs/guide.md"
    # Docs cite paths relative to whatever they were describing at the time.
    assert checker.resolve("assets/template.html", index) == "skills/viz/assets/template.html"
    assert checker.resolve("skills/viz/", index) == "skills/viz"
    assert checker.resolve("nope/gone.py", index) is None


def test_the_shortest_match_wins_when_a_suffix_is_ambiguous(checker):
    index = {"a/common.py", "deep/nested/a/common.py"}
    assert checker.resolve("a/common.py", index) == "a/common.py"


# ---- churn ------------------------------------------------------------------ #

def test_churn_is_grouped_two_levels_deep_and_ranked(checker):
    changed = {
        "skills/viz/a.py", "skills/viz/b.py", "skills/viz/c.py",
        "tests/viz/a.py",
        ".venv/lib/junk.py",     # vendored: never the user's code
        "README.md",             # top-level file: no directory to report
    }
    assert checker.top_churn_dirs(changed) == [("skills/viz", 3), ("tests/viz", 1)]


# ---- analysis --------------------------------------------------------------- #

def test_the_four_kinds_of_staleness_are_reported(checker, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "live.py").write_text("one\ntwo\n")
    page = tmp_path / "SKILL.md"
    page.write_text("")
    text = ("`src/live.py:99` is the hot loop.\n"
            "`src/deleted.py` used to hold the parser.\n"
            "`src/live.py` is the entry point.\n")

    findings = checker.analyze(
        repo=tmp_path, docs_dir=tmp_path, doc_pages=[(page, text)],
        changed={"src/live.py", "worker/a.py", "worker/b.py"},
        last_updated="2026-01-01T00:00:00Z",
    )
    kinds = {f["smell_type"] for f in findings}
    assert kinds == {"citation_past_eof", "missing_path", "changed_since_documented",
                     "undocumented_churn"}

    by_kind = {f["smell_type"]: f for f in findings}
    assert "src/deleted.py" in by_kind["missing_path"]["description"]
    assert "2 lines" in by_kind["citation_past_eof"]["description"]
    assert "worker/" in by_kind["undocumented_churn"]["description"]
    # High-severity findings sort first so a reader hits the broken links first.
    assert findings[0]["severity"] == "high"


def test_a_documented_directory_is_not_reported_as_undocumented_churn(checker, tmp_path):
    (tmp_path / "worker").mkdir()
    (tmp_path / "worker" / "a.py").write_text("x\n")
    page = tmp_path / "SKILL.md"
    page.write_text("")

    findings = checker.analyze(
        repo=tmp_path, docs_dir=tmp_path,
        doc_pages=[(page, "The [worker](worker/) handles retries.")],
        changed={"worker/a.py", "worker/b.py"}, last_updated="2026-01-01T00:00:00Z",
    )
    assert not any(f["smell_type"] == "undocumented_churn" for f in findings)


def test_uncommitted_docs_are_flagged_because_their_age_is_unknowable(checker, tmp_path):
    page = tmp_path / "SKILL.md"
    page.write_text("")
    findings = checker.analyze(repo=tmp_path, docs_dir=tmp_path,
                               doc_pages=[(page, "no citations here")],
                               changed=set(), last_updated=None)
    assert [f["smell_type"] for f in findings] == ["never_committed"]


def test_accurate_docs_produce_no_findings(checker, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "live.py").write_text("one\ntwo\nthree\n")
    page = tmp_path / "SKILL.md"
    page.write_text("")
    findings = checker.analyze(
        repo=tmp_path, docs_dir=tmp_path,
        doc_pages=[(page, "`src/live.py:2` is the entry point.")],
        changed=set(), last_updated="2026-01-01T00:00:00Z",
    )
    assert findings == []


# ---- CLI -------------------------------------------------------------------- #

def _run(*args):
    result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                            capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert result.returncode == 0, result.stderr[:500]
    return result


def test_cli_reports_real_staleness_over_a_git_repo(repo):
    repo.write("src/parser.py", "def parse():\n    return 1\n")
    repo.write("docs/SKILL.md", "The parser lives in `src/parser.py`.\n")
    repo.commit("docs and code", date="2026-01-01T00:00:00")
    repo.delete("src/parser.py")
    repo.write("worker/queue.py", "x = 1\n")
    repo.commit("move things", date="2026-06-01T00:00:00")

    findings = json.loads(_run("--docs", "docs", "--repo", repo.path, "--format", "json").stdout)
    assert {f["smell_type"] for f in findings} >= {"missing_path", "undocumented_churn"}


def test_cli_says_plainly_when_there_are_no_docs_to_check(repo):
    repo.write("src/a.py", "x = 1\n")
    repo.commit("code only")
    result = _run("--docs", "docs", "--repo", repo.path, "--format", "json")
    assert json.loads(result.stdout) == []
    # A first-time write is a different job from a refresh, and saying so beats
    # reporting a clean bill of health on documentation that does not exist.
    assert "first-time write" in result.stderr
