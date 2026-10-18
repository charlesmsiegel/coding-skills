"""Exercise the deletion budget against real, dirty Git worktrees."""

import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "skills/code-deleter/scripts/line_budget.py"


def snapshot(run_script, repo, tmp_path, *extra):
    baseline = tmp_path / "budget.json"
    run_script(SCRIPT, "snapshot", "--root", repo.path, "--baseline", baseline,
               "--include", "*.py", "--tests", "tests/*", *extra)
    return baseline


def check(run_script, repo, baseline, expected=0):
    result = run_script(SCRIPT, "check", "--root", repo.path, "--baseline", baseline, expect_rc=expected)
    return json.loads(result.stdout)


def test_noop_and_reduction_without_a_commit(run_script, repo, tmp_path):
    repo.write("app.py", "def answer():\n    return 42\n")
    baseline = snapshot(run_script, repo, tmp_path)
    assert check(run_script, repo, baseline)["code"]["delta"] == 0
    repo.write("app.py", "answer = 42\n")
    report = check(run_script, repo, baseline)
    assert report["code"] == {"before": 2, "after": 1, "delta": -1}
    assert report["passed"] is True


def test_dirty_worktree_is_the_baseline_not_head(run_script, repo, tmp_path):
    repo.write("app.py", "a = 1\nb = 2\nc = 3\n")
    repo.commit()
    repo.write("app.py", "a = 1\n")
    baseline = snapshot(run_script, repo, tmp_path)
    repo.write("app.py", "a = 1\nb = 2\n")
    report = check(run_script, repo, baseline, 1)
    assert report["code"] == {"before": 1, "after": 2, "delta": 1}


def test_new_untracked_tests_count_against_savings(run_script, repo, tmp_path):
    repo.write("app.py", "a = 1\nb = 2\n")
    baseline = snapshot(run_script, repo, tmp_path)
    repo.write("app.py", "a = 1\n")
    repo.write("tests/test_app.py", "def test_app():\n    assert True\n")
    report = check(run_script, repo, baseline, 1)
    assert report["code"]["delta"] == 1
    assert report["production"]["delta"] == -1


def test_test_deletions_cannot_fund_production_growth(run_script, repo, tmp_path):
    repo.write("app.py", "a = 1\n")
    repo.write("tests/test_app.py", "def test_app():\n    assert True\n")
    baseline = snapshot(run_script, repo, tmp_path)
    repo.delete("tests/test_app.py")
    repo.write("app.py", "a = 1\nb = 2\n")
    report = check(run_script, repo, baseline, 1)
    assert report["code"]["delta"] == -1
    assert report["production"]["delta"] == 1
    assert report["passed"] is False


def test_production_savings_can_fund_necessary_tests(run_script, repo, tmp_path):
    repo.write("app.py", "a = 1\nb = 2\nc = 3\n")
    baseline = snapshot(run_script, repo, tmp_path)
    repo.write("app.py", "a = 1\n")
    repo.write("tests/test_app.py", "def test_app():\n    assert True\n")
    assert check(run_script, repo, baseline)["code"]["delta"] == 0


def test_blank_line_deletions_cannot_hide_nonblank_growth(run_script, repo, tmp_path):
    repo.write("app.py", "a = 1\n\n\n\n")
    baseline = snapshot(run_script, repo, tmp_path)
    repo.write("app.py", "a = 1\nb = 2\n")
    report = check(run_script, repo, baseline, 1)
    assert report["code"]["delta"] == -2
    assert report["code_nonblank"]["delta"] == 1


def test_documentation_deletions_do_not_pay_for_code(run_script, repo, tmp_path):
    repo.write("app.py", "a = 1\n")
    repo.write("README.md", "one\ntwo\nthree\n")
    baseline = snapshot(run_script, repo, tmp_path)
    repo.delete("README.md")
    repo.write("app.py", "a = 1\nb = 2\n")
    assert check(run_script, repo, baseline, 1)["code"]["delta"] == 1


def test_new_ignore_rule_cannot_hide_existing_untracked_source(run_script, repo, tmp_path):
    repo.write("scratch.py", "a = 1\n")
    baseline = snapshot(run_script, repo, tmp_path)
    repo.write(".gitignore", "scratch.py\n")
    repo.write("scratch.py", "a = 1\nb = 2\n")
    assert check(run_script, repo, baseline, 1)["code"]["delta"] == 1


def test_rename_is_neutral_and_nested_new_source_counts(run_script, repo, tmp_path):
    repo.write("app.py", "a = 1\n")
    baseline = snapshot(run_script, repo, tmp_path)
    repo.delete("app.py")
    repo.write("pkg/renamed.py", "a = 1\n")
    assert check(run_script, repo, baseline)["code"]["delta"] == 0
    repo.write("pkg/new.py", "b = 2\n")
    assert check(run_script, repo, baseline, 1)["code"]["delta"] == 1


@pytest.mark.parametrize("content", [b"a = 1", b"a = 1\n", b"a = 1\r\n"])
def test_final_newline_and_crlf_count_as_one_line(run_script, repo, tmp_path, content):
    repo.write("app.py", "").write_bytes(content)
    baseline = snapshot(run_script, repo, tmp_path)
    assert check(run_script, repo, baseline)["code"]["before"] == 1


def test_snapshot_refuses_to_rebase_over_existing_budget(run_script, repo, tmp_path):
    repo.write("app.py", "a = 1\n")
    baseline = snapshot(run_script, repo, tmp_path)
    original = baseline.read_bytes()
    repo.write("app.py", "a = 1\nb = 2\n")
    run_script(SCRIPT, "snapshot", "--root", repo.path, "--baseline", baseline,
               "--include", "*.py", expect_rc=2)
    assert baseline.read_bytes() == original


@pytest.mark.parametrize("content", [None, b"\x00binary"])
def test_unmeasurable_source_fails_closed(run_script, repo, tmp_path, content):
    if content is not None:
        repo.write("app.py", "").write_bytes(content)
    result = run_script(SCRIPT, "snapshot", "--root", repo.path,
                        "--baseline", tmp_path / "budget.json", "--include", "*.py", expect_rc=2)
    assert "error" in json.loads(result.stdout)


def test_wrong_root_and_missing_baseline_are_errors(run_script, repo, tmp_path):
    repo.write("app.py", "a = 1\n")
    baseline = snapshot(run_script, repo, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = run_script(SCRIPT, "check", "--root", elsewhere, "--baseline", baseline, expect_rc=2)
    assert "error" in json.loads(result.stdout)
    baseline.unlink()
    result = run_script(SCRIPT, "check", "--root", repo.path, "--baseline", baseline, expect_rc=2)
    assert "error" in json.loads(result.stdout)


@pytest.mark.parametrize("command", ["snapshot", "check"])
def test_nested_git_worktree_cannot_be_silently_omitted(run_script, repo, tmp_path, command):
    repo.write("app.py", "a = 1\n")
    baseline = tmp_path / "budget.json"
    if command == "check":
        snapshot(run_script, repo, tmp_path)
    repo.git("init", "-q", "nested")
    repo.write("nested/child.py", "b = 2\n")
    extra = ["--include", "*.py"] if command == "snapshot" else []
    result = run_script(SCRIPT, command, "--root", repo.path, "--baseline", baseline, *extra, expect_rc=2)
    assert "nested" in json.loads(result.stdout)["error"]
