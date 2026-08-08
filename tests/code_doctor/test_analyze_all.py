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
    # Lists, not comma-joined strings: merge_reports.py reads `categories_run`
    # as a list, and a string is not one, so every real code-doctor report used
    # to land in `coverage_unknown` and grant a grader nothing.
    assert payload["completeness"]["categories_run"] == ["hygiene"]
    assert payload["completeness"]["categories_skipped"] == ["secrets"]


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
    completeness block, keyed by the *category* it cost, and must not be listed
    among the categories that ran. A report with no surviving findings must
    refuse to claim the repo is clean — never silently read as "found nothing".

    The keying is the load-bearing part. Failures used to be a single string
    keyed by script filename while `categories_run` listed every *selected*
    category, so a consumer holding both could not line them up — and once
    `categories_run` became real coverage evidence, a crashed detector's
    category would have been graded from silence."""
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
    failed = payload["completeness"]["categories_failed"]
    assert set(failed) == {"hygiene"}, "a failure is filed under the category it cost"
    assert "dying_detector.py" in failed["hygiene"], "and still names the script that died"
    assert "exited 3" in failed["hygiene"]
    assert payload["completeness"]["categories_run"] == [], (
        "a category whose detector crashed did not run; listing it would grade it from silence"
    )

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
    failed = payload["completeness"]["categories_failed"]
    assert "garbled_detector.py" in failed["hygiene"]
    assert "invalid JSON" in failed["hygiene"]
    assert payload["completeness"]["categories_run"] == ["secrets"], (
        "the healthy sibling ran; the garbled one did not"
    )


KEY_BODY = "MIIEowIBAAKCAQEAvQ8Z1kQyBmT3xN0pLrWc7HgYdFvJqR2sKtBnM4eXhAoGBAJ9x\n" * 3


def test_text_output_separates_findings_from_candidates(repo, run_script):
    """An unverified lead must not be printed as an asserted defect.

    The assertion this replaces was `"candidate" in stdout.lower()`, which
    could not fail: common.py prints the "N finding(s), M candidate(s)" header
    on every non-empty report, so the word was there whether or not anything
    was separated. Deleting the split entirely left all nine tests in this file
    green.

    So pin the split itself — the two counts, the header that introduces the
    unverified block, and which record lands on which side of it. A committed
    private key is an asserted defect (it names a fix); a commented-out line is
    a lead (a documentation example produces the same observation), and the
    difference is the whole reason candidates never enter a grade.
    """
    repo.write("deploy.sh",
               f"-----BEGIN RSA PRIVATE KEY-----\n{KEY_BODY}-----END RSA PRIVATE KEY-----\n")
    repo.write("app.go", "package main\n// x := compute(1);\n")
    repo.commit()

    out = run_script(SCRIPT, repo.path).stdout

    assert "1 finding(s), 1 candidate(s)" in out
    split = out.index("Candidates")
    assert out.index("private_key_material") < split, "an asserted defect belongs above the split"
    assert split < out.index("commented_out_code"), "an unverified lead belongs below it"
    assert "[candidate]" not in out[:split], "nothing above the split is a lead"
    assert "[HIGH]" not in out[split:], "nothing below it is graded like a defect"
    assert "Also caused by:" in out[split:], "a lead must carry the benign explanations"


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
