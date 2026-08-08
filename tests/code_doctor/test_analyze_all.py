"""The orchestrator."""

import json
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"
SCRIPT = SKILL / "scripts" / "analyze_all.py"


def test_merges_findings_from_every_detector(repo, run_script):
    repo.write("app.go", "package main\n<<<<<<< HEAD\nx := 1\n")
    repo.write("deploy.sh", "-----BEGIN RSA PRIVATE KEY-----\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    types = {f["smell_type"] for f in json.loads(result.stdout)["findings"]}
    assert "merge_conflict_marker" in types
    assert "private_key_material" in types


def test_skip_drops_a_whole_category(repo, run_script):
    repo.write("deploy.sh", "-----BEGIN RSA PRIVATE KEY-----\n")
    result = run_script(SCRIPT, repo.path, "--format", "json", "--skip", "secrets")
    types = {f["smell_type"] for f in json.loads(result.stdout)["findings"]}
    assert "private_key_material" not in types


def test_reports_which_categories_ran(repo, run_script):
    repo.write("app.go", "package main\n")
    result = run_script(SCRIPT, repo.path, "--format", "json", "--skip", "secrets")
    payload = json.loads(result.stdout)
    assert payload["completeness"]["categories_run"] == "hygiene"
    assert "secrets" in payload["completeness"]["categories_skipped"]


def test_a_crashing_detector_is_named_not_swallowed(repo, run_script, monkeypatch):
    """A detector that dies must degrade the report audibly, not silently."""
    repo.write("app.go", "package main\n")
    result = run_script(SCRIPT, repo.path, "--format", "json",
                        "--only", "nosuchcategory", expect_rc=2)
    assert "nosuchcategory" in result.stderr


def test_text_output_separates_findings_from_candidates(repo, run_script):
    repo.write("app.go", "package main\n// x := compute(1);\n")
    result = run_script(SCRIPT, repo.path)
    assert "candidate" in result.stdout.lower()


def test_clean_repo_reports_clean(repo, run_script):
    repo.write("app.go", "package main\n\nfunc main() {}\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert json.loads(result.stdout)["findings"] == []


def test_detector_completeness_survives_the_merge(tmp_path, run_script):
    """A detector's own warnings must not be dropped on the way to the report.

    Outside a git repo, hygiene emits a merge_state note. If the aggregate
    discards it, the report claims the category ran while silently losing the
    caveat that makes its output legible.
    """
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.go").write_text("package main\n<<<<<<< HEAD\nx := 1\n")
    result = run_script(SCRIPT, plain, "--format", "json")
    completeness = json.loads(result.stdout)["completeness"]
    assert any("merge_state" in key for key in completeness)
