"""Hygiene detector: what is a defect, and what is only a lead."""

import json
import subprocess
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"
SCRIPT = SKILL / "scripts" / "find_hygiene_issues.py"


def types_in(result) -> set[str]:
    return {f["smell_type"] for f in json.loads(result.stdout)}


def records_of(result, smell_type):
    return [f for f in json.loads(result.stdout) if f["smell_type"] == smell_type]


def test_merge_marker_without_git_conflict_is_a_candidate(repo, run_script):
    """A marker in a committed file is not proof of an unresolved conflict.

    Documentation examples, snapshots, and fixtures that exist to test
    conflict handling all legitimately contain these lines.
    """
    repo.write("app.go", "package main\n<<<<<<< HEAD\nx := 1\n=======\nx := 2\n>>>>>>> other\n")
    repo.commit("add fixture")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "merge_conflict_marker")[0]
    assert record["kind"] == "candidate"
    assert record["suggestion"] == ""
    assert any("fixture" in reason or "documentation" in reason
               for reason in record["also_caused_by"])


def test_merge_marker_in_a_genuinely_unmerged_path_is_a_finding(repo, run_script):
    """Git's own unmerged-path list is the evidence that upgrades this."""
    repo.write("app.go", "package main\nx := 0\n")
    repo.commit("base")
    repo.git("checkout", "-q", "-b", "other")
    repo.write("app.go", "package main\nx := 2\n")
    repo.commit("theirs")
    repo.git("checkout", "-q", "-")
    repo.write("app.go", "package main\nx := 1\n")
    repo.commit("ours")
    # A conflicting merge leaves the path unmerged and the markers in the file.
    merge = subprocess.run(["git", "-C", str(repo.path), "merge", "other"],
                           capture_output=True, text=True)
    assert merge.returncode != 0, "expected the merge to conflict"

    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "merge_conflict_marker")[0]
    assert record["kind"] == "finding"
    assert record["suggestion"]
    assert record["severity"] == "high"


def test_commented_out_code_is_a_candidate_with_reasons(repo, run_script):
    repo.write("app.go", "package main\n// x := compute(1, 2);\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "commented_out_code")[0]
    assert record["kind"] == "candidate"
    assert record["suggestion"] == ""
    assert record["also_caused_by"], "a candidate must name the benign explanations"


def test_url_scheme_inside_a_string_is_not_mistaken_for_the_comment_opener(repo, run_script):
    """Literals are blanked before comment prefixes are matched.

    The `//` inside the URL string must not be what triggers this: blanking
    removes it, so the `;` statement separator is what's found instead. No
    prefix is unambiguous without a parser, so `;` still yields a candidate —
    just not one manufactured out of misreading the URL's own `//`.
    """
    repo.write("app.go", 'package main\nurl := "https://example.com/a"; doWork()\n')
    result = run_script(SCRIPT, repo.path, "--format", "json")
    record = records_of(result, "commented_out_code")[0]
    assert record["kind"] == "candidate"
    assert record["suggestion"] == ""


def test_prose_is_not_scanned_for_commented_out_code(repo, run_script):
    """Markdown is not source; a fenced code sample is not a leftover."""
    repo.write("README.md", "# Title\n// x := compute(1, 2);\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "commented_out_code" not in types_in(result)


def test_merge_marker_in_yaml_is_still_reported(repo, run_script):
    """Merge markers are checked in any text file, so that check takes the wide walk."""
    repo.write("config.yaml", "a: 1\n<<<<<<< HEAD\nb: 2\n=======\nb: 3\n>>>>>>> other\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "merge_conflict_marker" in types_in(result)


def test_tracked_env_file_is_a_finding(repo, run_script):
    repo.write(".env", "API_KEY=abc123\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "committed_env_file" in types_in(result)


def test_untracked_env_file_is_not_reported(repo, run_script):
    """An untracked, gitignored .env is correct practice, not a leak.

    Telling someone to rotate credentials and purge history over their local
    .env is a false positive with a real cost.
    """
    repo.write(".gitignore", ".env\n")
    repo.commit("ignore env")
    repo.write(".env", "API_KEY=abc123\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "committed_env_file" not in types_in(result)


def test_todo_in_a_non_source_text_file_is_inventoried(repo, run_script):
    """The design scopes the TODO inventory to all text, not just source."""
    repo.write("deployment.yaml", "replicas: 1  # TODO: raise before launch\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "todo_inventory" in types_in(result)


def test_todo_records_are_candidates_not_defects(repo, run_script):
    """No comment prefix is unambiguous without a parser.

    Python's `total // TODO` is floor division on a variable named TODO, so
    even `//` cannot support a confirmed finding.
    """
    repo.write("app.py", "result = total // TODO\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    for record in records_of(result, "todo_inventory"):
        assert record["kind"] == "candidate"
        assert record["suggestion"] == ""


def test_oversized_file_is_reported_once(repo, run_script):
    repo.write("big.go", "package main\n" + "var x = 1\n" * 1500)
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert len(records_of(result, "oversized_file")) == 1


def test_clean_repo_reports_nothing(repo, run_script):
    repo.write("app.go", "package main\n\nfunc main() {}\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert json.loads(result.stdout) == []


def test_ignore_suppresses_a_type(repo, run_script):
    repo.write("app.go", "package main\n// TODO: fix this\n")
    result = run_script(SCRIPT, repo.path, "--format", "json", "--ignore", "todo_inventory")
    assert "todo_inventory" not in types_in(result)
