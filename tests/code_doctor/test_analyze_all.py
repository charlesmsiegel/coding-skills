"""The orchestrator."""

import json
import sys
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


def test_only_rejects_an_unknown_category(repo, run_script):
    """--only naming a category absent from DETECTORS is an argument-validation
    error caught before any subprocess runs. It does NOT exercise a detector
    dying mid-run — see the two tests below for that."""
    repo.write("app.go", "package main\n")
    result = run_script(SCRIPT, repo.path, "--format", "json",
                        "--only", "nosuchcategory", expect_rc=2)
    assert "nosuchcategory" in result.stderr


def test_a_crashing_detector_is_named_not_swallowed(repo, tmp_path, load_module,
                                                     monkeypatch, capsys):
    """A detector subprocess that exits non-zero must be named in the
    completeness block, and a report with no surviving findings must refuse to
    claim the repo is clean — never silently read as "found nothing"."""
    module = load_module(SCRIPT.parent, "analyze_all")
    dying = tmp_path / "dying_detector.py"
    dying.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    # An absolute path as the registry value overrides SCRIPTS_DIR entirely
    # (Path.__truediv__ discards the left side for an absolute right side),
    # so this substitutes the one category's script without touching the
    # committed detectors.
    monkeypatch.setattr(module, "DETECTORS", {"hygiene": str(dying)})

    monkeypatch.setattr(sys, "argv", ["analyze_all.py", str(repo.path), "--format", "json"])
    rc = module.main()
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["findings"] == []
    failed = payload["completeness"]["detectors_failed"]
    assert "dying_detector.py" in failed
    assert "exited 3" in failed

    monkeypatch.setattr(sys, "argv", ["analyze_all.py", str(repo.path)])
    rc = module.main()
    text_out = capsys.readouterr().out
    assert rc == 0
    assert "not a confirmed-clean result" in text_out.lower()


def test_a_detector_emitting_malformed_json_is_named_not_swallowed(repo, tmp_path, load_module,
                                                                    monkeypatch, capsys):
    """A detector that exits zero but prints unparseable JSON takes a
    different branch than a non-zero exit (json.JSONDecodeError, not the
    returncode check) — covered separately. A healthy sibling detector's
    findings must still reach the report."""
    module = load_module(SCRIPT.parent, "analyze_all")
    garbled = tmp_path / "garbled_detector.py"
    garbled.write_text("print('not json {')\n", encoding="utf-8")
    monkeypatch.setattr(module, "DETECTORS", {
        "hygiene": str(garbled),
        "secrets": str(SCRIPT.parent / "find_secrets.py"),
    })
    repo.write("deploy.sh", "-----BEGIN RSA PRIVATE KEY-----\n")

    monkeypatch.setattr(sys, "argv", ["analyze_all.py", str(repo.path), "--format", "json"])
    rc = module.main()
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    types = {f["smell_type"] for f in payload["findings"]}
    assert "private_key_material" in types
    failed = payload["completeness"]["detectors_failed"]
    assert "garbled_detector.py" in failed
    assert "invalid JSON" in failed


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
