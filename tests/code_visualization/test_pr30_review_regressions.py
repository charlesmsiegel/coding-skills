"""CLI regressions found while independently reviewing PR 30."""
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills/code-visualization/scripts"


@pytest.mark.parametrize("comment", ["/* explanatory comment */", "// explanatory comment\n"])
def test_a_comment_before_a_regex_does_not_hide_the_next_import(repo, tabs, run_script, comment):
    repo.write("src/dep.ts", "export const x = 1\n")
    repo.write("src/main.ts", f"const marker = {comment} /`/;\nimport {{x}} from './dep';\n")

    result = run_script(SCRIPTS / "analyze_deps.py", repo.path, "--tabs-dir", tabs)
    summary = json.loads(result.stdout)

    assert summary["import_edges"] == 1
    assert summary["resolution"]["TypeScript"]["first_party"] == 1
    assert summary["resolution"]["TypeScript"]["skipped_in_strings"] == 0


@pytest.mark.parametrize("script", ["analyze_inventory.py", "analyze_deps.py",
                                    "analyze_hotspots.py", "analyze_coverage.py", "analyze_llm.py"])
@pytest.mark.parametrize("bad", ["missing", "../outside"])
def test_a_whole_repo_root_does_not_bypass_validation_of_later_roots(
        repo, tabs, run_script, script, bad):
    repo.write("src/app.py", "value = 1\n")

    result = run_script(SCRIPTS / script, repo.path, "--tabs-dir", tabs,
                        "--root", ".", "--root", bad, expect_rc=2)

    assert f"error: --root {bad} is" in result.stderr
