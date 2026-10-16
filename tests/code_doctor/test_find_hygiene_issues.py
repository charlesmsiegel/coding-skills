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
    """Neither the URL's own `//` nor the `;` separating two statements makes
    this line a comment: it is live code from its first token, and commented-out
    code is a line somebody disabled."""
    repo.write("app.go", 'package main\nurl := "https://example.com/a"; doWork()\n')
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "commented_out_code" not in types_in(result)


def test_a_prefix_mid_line_is_not_commented_out_code(repo, run_script):
    """`;` inside a for header and a CSS declaration, `--` as a decrement:
    each was read as a comment opener and the rest of the line as its code."""
    repo.write("loop.ts", "for (let i = 1; i <= 40; i++) {\n  n--;\n}\n")
    repo.write("site.css", ".list > li:last-child { border-bottom: 0; }\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "commented_out_code" not in types_in(result)


def test_flush_prefixes_that_open_something_else_are_not_comments(repo, run_script):
    """At the start of a line, `--name:` is a CSS custom property, `#id {` a CSS
    selector and `#include` a preprocessor directive — none is a comment."""
    repo.write("theme.css", ":root {\n  --accent: #0a6;\n}\n#main {\n  color: red;\n}\n")
    repo.write("main.c", "#include <stdio.h>\n#define SQUARE(x) ((x) * (x))\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "commented_out_code" not in types_in(result)


def test_documentation_comments_and_prose_asides_are_not_code(repo, run_script):
    """A doc comment's example is an example, and prose puts a space before
    its parenthesis — "every route (see the docstring)" is not a call."""
    repo.write("lib.rs", "/// let x = compute(1, 2);\nfn compute() {}\n")
    repo.write("guard.py", "#: How the client is spelled at every route (see the docstring)\n"
                           "CLIENT = 'client'\n"
                           "# Sized well past any real banner (a handful of rows)\n"
                           "CAP = 9\n"
                           "# Each root uniquifies `slugify(name)` against itself, per ruling 6(c)\n"
                           "ROOTS = ()\n"
                           "# a direct caller of validate() sees instead of a ClimateError\n"
                           "ERRORS = ()\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "commented_out_code" not in types_in(result)


def test_disabled_lines_are_still_candidates_in_each_comment_style(repo, run_script):
    """The narrowing must not lose the thing it exists for."""
    repo.write("a.py", "def f():\n    # result = compute(1, 2)\n    return 1\n")
    repo.write("b.sql", "-- SELECT name FROM users WHERE id = 1;\nSELECT 1;\n")
    repo.write("c.ts", "    // if (ready) { start(); }\nexport {};\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    flagged = {Path(r["file"]).name for r in records_of(result, "commented_out_code")}
    assert flagged == {"a.py", "b.sql", "c.ts"}


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


# --------------------------------------------------------------------------- #
# B1 — the `.env.<environment>` family, minus documented templates.
# --------------------------------------------------------------------------- #

def test_tracked_env_staging_file_is_a_finding(repo, run_script):
    """`.env.staging` is as credential-bearing as `.env.production`."""
    repo.write(".env.staging", "API_KEY=abc123\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "committed_env_file" in types_in(result)


def test_tracked_env_development_file_is_a_finding(repo, run_script):
    repo.write(".env.development", "API_KEY=abc123\n")
    repo.commit("oops")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "committed_env_file" in types_in(result)


def test_env_example_template_is_not_reported(repo, run_script):
    """`.env.example` exists to be a committed, secret-free template."""
    repo.write(".env.example", "API_KEY=your-key-here\n")
    repo.commit("template")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "committed_env_file" not in types_in(result)


def test_env_sample_and_template_names_are_not_reported(repo, run_script):
    repo.write(".env.sample", "API_KEY=your-key-here\n")
    repo.write(".env.template", "API_KEY=your-key-here\n")
    repo.commit("templates")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "committed_env_file" not in types_in(result)


# --------------------------------------------------------------------------- #
# B2 — `--ignore a, b` must strip whitespace on every entry.
# --------------------------------------------------------------------------- #

def test_ignore_strips_whitespace_between_entries(repo, run_script):
    """`--ignore a, b` must suppress b too, not just a with a leading space
    nothing ever matches."""
    repo.write("big.go", "package main\n" + "x" * 305 + "\n")
    result = run_script(SCRIPT, repo.path, "--format", "json",
                        "--ignore", "oversized_file, oversized_line")
    assert "oversized_line" not in types_in(result)


# --------------------------------------------------------------------------- #
# C2 — UTF-16 text was classified binary and skipped entirely.
# --------------------------------------------------------------------------- #

def test_merge_marker_in_a_utf16_file_is_still_detected(repo, run_script):
    """UTF-16 is NUL-riddled by construction; a BOM marks it as text anyway."""
    content = "package main\n<<<<<<< HEAD\nx := 1\n=======\nx := 2\n>>>>>>> other\n"
    (repo.path / "app.txt").write_bytes(content.encode("utf-16"))
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "merge_conflict_marker" in types_in(result)


def test_a_minified_bundle_does_not_stall_the_scan(repo, run_script):
    """Backslash runs inside an unterminated quote used to backtrack
    exponentially in the string-literal blanker; one committed Vite bundle
    held the whole detector past a ten-minute timeout."""
    line = "var a='" + "\\\\" * 40 + "x" + ";b=\"\\\\q\"" * 2000
    repo.write("static/assets/index-U_FZj3ND.js", (line + "\n") * 3)
    result = run_script(SCRIPT, repo.path, "--format", "json")
    payload = json.loads(result.stdout)
    assert payload["findings"] == []
    # Read for merge markers only, and the report says so.
    assert "index-U_FZj3ND.js" in payload["completeness"]["generated_files_skipped"]


def test_a_todo_comment_on_an_ordinary_line_is_still_inventoried(repo, run_script):
    repo.write("app.py", "x = 1  # TODO: name this\n")
    result = run_script(SCRIPT, repo.path, "--format", "json")
    assert "todo_inventory" in types_in(result)
