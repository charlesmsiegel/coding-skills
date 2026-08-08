"""The walk: what counts as source, what counts as text, what is skipped."""

import json
import os

import pytest


@pytest.fixture
def common(load_module):
    from pathlib import Path
    scripts = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor" / "scripts"
    return load_module(scripts, "common")


def test_unknown_extension_is_treated_as_source(common, repo):
    """Language-blindness: a language nobody has heard of is still code."""
    repo.write("main.zig", "const x = 1;\n")
    repo.write("thing.somelang", "whatever\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert found == {"main.zig", "thing.somelang"}


def test_known_non_code_text_is_excluded_from_source(common, repo):
    """Prose and generated data must not reach the code-only detectors."""
    repo.write("README.md", "# hi\n")
    repo.write("data.json", "{}\n")
    repo.write("Cargo.lock", "[[package]]\n")
    repo.write("main.go", "package main\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert found == {"main.go"}


def test_non_source_walk_still_yields_text_files(common, repo):
    """Secrets and merge markers are real findings in a YAML file too."""
    repo.write("README.md", "# hi\n")
    repo.write("main.go", "package main\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=False)}
    assert found == {"README.md", "main.go"}


def test_vendor_directories_are_skipped(common, repo):
    repo.write("node_modules/pkg/index.js", "module.exports = 1\n")
    repo.write("src/app.js", "export const a = 1\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert found == {"app.js"}


def test_binary_files_are_never_yielded(common, repo):
    (repo.path / "blob.bin").write_bytes(b"\x00\x01\x02\x03binary")
    repo.write("main.go", "package main\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=False)}
    assert found == {"main.go"}


def test_documentation_directory_is_not_source(common, repo):
    repo.write("docs/guide.rst", "Guide\n=====\n")
    repo.write("docs/example.py", "print(1)\n")
    repo.write("app.py", "print(2)\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert found == {"app.py"}


def test_symlinks_are_never_followed(common, repo, tmp_path):
    """A link out of the tree would otherwise let this skill read host data
    and report a credential found there as committed to this repository."""
    outside = tmp_path / "outside.txt"
    outside.write_text("AWS_SECRET=hJ8s0Kd93LwmZq2XvRt7YbNc4PfGh6Aa\n")
    repo.write("app.go", "package main\n")
    (repo.path / "link.go").symlink_to(outside)
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert found == {"app.go"}


def test_symlink_scan_root_raises(common, repo, tmp_path):
    """Refusing to follow it is right; reporting it clean is not."""
    link = tmp_path / "link-to-repo"
    link.symlink_to(repo.path)
    with pytest.raises(common.ScanPathError, match="symlink"):
        list(common.walk_files(link, source_only=True))


def test_gitignored_files_are_not_walked(common, repo):
    """The design promises .gitignore awareness, not just EXCLUDE_DIRS."""
    repo.write(".gitignore", "generated/\n*.gen.go\n")
    repo.write("generated/big.go", "package generated\n")
    repo.write("thing.gen.go", "package thing\n")
    repo.write("app.go", "package main\n")
    repo.commit("ignore generated")
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert "app.go" in found
    assert "big.go" not in found, "walked into a gitignored directory"
    assert "thing.gen.go" not in found, "walked a gitignored file"


def test_unreadable_file_is_not_silently_classified_binary(common, repo):
    """An OSError must reach the caller, not masquerade as a binary skip."""
    target = repo.write("locked.go", "package main\n")
    os.chmod(target, 0o000)
    try:
        if os.access(target, os.R_OK):
            pytest.skip("running as a user that ignores file permissions (root)")
        with pytest.raises(OSError):
            common.is_probably_binary(target)
    finally:
        os.chmod(target, 0o644)


def test_bin_directory_is_scanned(common, repo):
    """Ruby gems and shell CLIs keep executable source in bin/."""
    repo.write("bin/console", "#!/usr/bin/env ruby\nputs 1\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert "console" in found


def test_missing_root_raises_rather_than_reporting_clean(common, tmp_path):
    """A typo in an audit path must not produce an authoritative empty report."""
    with pytest.raises(common.ScanPathError):
        list(common.walk_files(tmp_path / "nope", source_only=True))


def test_excluded_directories_are_not_descended_into(common, repo, monkeypatch):
    """Pruning during traversal, not filtering after enumeration."""
    repo.write("node_modules/pkg/deep/nested/index.js", "module.exports = 1\n")
    repo.write("src/app.js", "export const a = 1\n")
    seen = []
    real_walk = common.os.walk

    def spy(top, **kwargs):
        for dirpath, dirnames, filenames in real_walk(top, **kwargs):
            seen.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(common.os, "walk", spy)
    list(common.walk_files(repo.path, source_only=True))
    assert not any("node_modules" in d for d in seen), (
        "walked into an excluded tree instead of pruning it"
    )


def test_walk_paths_yields_binaries_for_metadata_checks(common, repo):
    (repo.path / "blob.bin").write_bytes(b"\x00" * 32)
    repo.write("app.go", "package main\n")
    assert {p.name for p in common.walk_paths(repo.path)} == {"blob.bin", "app.go"}
    assert {p.name for p in common.walk_files(repo.path, source_only=False)} == {"app.go"}


def test_finding_requires_a_suggestion(common):
    """A finding asserts a defect, so it must say what to do about it."""
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a.go", line=1, smell_type="x",
                       description="d", suggestion="")


def test_finding_may_not_carry_benign_explanations(common):
    """also_caused_by is the candidate's honesty field; on a finding it is a lie."""
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a.go", line=1, smell_type="x", description="d",
                       suggestion="fix it", also_caused_by=["something benign"])


def test_candidate_requires_benign_explanations(common):
    """A candidate must name the ways a healthy codebase produces this."""
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a.go", line=1, smell_type="x", description="d",
                       kind="candidate", also_caused_by=[])


def test_candidate_may_not_carry_a_fix(common):
    """The whole point: an unverified lead must not recommend an edit."""
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a.go", line=1, smell_type="x", description="d",
                       kind="candidate", suggestion="delete it",
                       also_caused_by=["it is an entry point"])


def test_valid_finding_and_candidate_construct(common):
    finding = common.Finding(file="a.go", line=1, smell_type="x",
                             description="d", suggestion="fix it")
    candidate = common.Finding(file="a.go", line=2, smell_type="y",
                               description="d", kind="candidate",
                               also_caused_by=["it is an entry point"])
    assert finding.kind == "finding"
    assert candidate.suggestion == ""


def test_unknown_kind_is_rejected(common):
    with pytest.raises(common.SchemaError, match="kind"):
        common.Finding(file="a.go", line=1, smell_type="x", description="d",
                       suggestion="fix", kind="probably")


def test_reporter_honours_ignore(common):
    from pathlib import Path
    reporter = common.Reporter(Path("a.go"), {"skipme"})
    reporter.finding(1, "skipme", "d", "fix it")
    reporter.finding(2, "keepme", "d", "fix it")
    assert [f.smell_type for f in reporter.findings] == ["keepme"]


def test_reporter_candidate_sets_kind(common):
    from pathlib import Path
    reporter = common.Reporter(Path("a.go"), set())
    reporter.candidate(1, "lead", "d", ["it may be loaded by convention"])
    assert reporter.findings[0].kind == "candidate"
    assert reporter.findings[0].suggestion == ""


def test_finding_is_frozen(common):
    """Frozen dataclass ensures schema enforcement holds across the object lifetime."""
    import dataclasses
    candidate = common.Finding(file="a.go", line=1, smell_type="x", description="d",
                               kind="candidate", also_caused_by=["it is an entry point"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.suggestion = "delete it"


def test_blank_reasons_are_rejected(common):
    """also_caused_by requires at least one non-blank entry."""
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a.go", line=1, smell_type="x", description="d",
                       kind="candidate", also_caused_by=[""])


def test_asdict_round_trips_through_finding(common):
    """asdict() on a frozen Finding can reconstruct the original."""
    from dataclasses import asdict
    original = common.Finding(file="a.go", line=42, smell_type="x",
                              description="d", suggestion="fix it")
    reconstructed = common.Finding(**asdict(original))
    assert original == reconstructed
    assert original.line == reconstructed.line


def test_probe_history_reports_not_a_repo(common, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    depth = common.probe_history(plain)
    assert depth.is_repo is False
    assert depth.usable is False


def test_probe_history_flags_thin_history_as_unusable(common, repo):
    """Two commits cannot support a bus-factor claim."""
    repo.write("a.go", "package main\n")
    repo.commit("one")
    repo.write("b.go", "package b\n")
    repo.commit("two")
    depth = common.probe_history(repo.path, min_commits=20)
    assert depth.is_repo is True
    assert depth.is_shallow is False
    assert depth.commit_count == 2
    assert depth.usable is False


def test_probe_history_accepts_sufficient_history(common, repo):
    for i in range(25):
        repo.write(f"f{i}.go", f"package p{i}\n")
        repo.commit(f"commit {i}")
    depth = common.probe_history(repo.path, min_commits=20)
    assert depth.usable is True


def test_emit_json_is_a_bare_list_without_completeness(common, capsys):
    common.emit([common.Finding(file="a.go", line=1, smell_type="x",
                                description="d", suggestion="fix")],
                "json", "clean")
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["kind"] == "finding"


def test_emit_json_wraps_when_completeness_is_given(common, capsys):
    common.emit([], "json", "clean", completeness={"history": "shallow clone"})
    payload = json.loads(capsys.readouterr().out)
    assert payload["completeness"] == {"history": "shallow clone"}
    assert payload["findings"] == []


def test_emit_text_prints_completeness_banner(common, capsys):
    common.emit([], "text", "no problems found",
                completeness={"history": "shallow clone — findings skipped"})
    out = capsys.readouterr().out
    assert "shallow clone" in out
    assert "no problems found" in out


def test_emit_text_separates_candidates_from_findings(common, capsys):
    records = [
        common.Finding(file="a.go", line=1, smell_type="defect",
                       description="broken", suggestion="fix it", severity="high"),
        common.Finding(file="b.go", line=2, smell_type="lead", description="maybe",
                       kind="candidate", also_caused_by=["it is an entry point"]),
    ]
    common.emit(records, "text", "clean")
    out = capsys.readouterr().out
    assert "1 finding(s)" in out
    assert "1 candidate(s)" in out
    assert "it is an entry point" in out


def test_oldest_commit_days_is_age_of_root(common, repo):
    """oldest_commit_days is computed from the root commit, not the last."""
    import time
    repo.write("initial.go", "package main\n")
    # Backdate the first commit to 30 days ago (use Unix timestamp format)
    old_time = int(time.time()) - (30 * 86400)
    repo.commit("initial", date=str(old_time))
    # Add recent commits
    for i in range(21):
        repo.write(f"f{i}.go", f"package p{i}\n")
        repo.commit(f"commit {i}")
    depth = common.probe_history(repo.path, min_commits=20)
    # oldest_commit_days should be approximately 30, not 0
    assert depth.oldest_commit_days is not None
    assert depth.oldest_commit_days >= 29  # Allow 1 day margin for test execution time


def test_probe_history_window_gates_on_age(common, repo):
    """Sufficient commits but insufficient age fails the window_days check."""
    repo.write("initial.go", "package main\n")
    # Create initial commit at current time (today)
    repo.commit("initial")
    # Add 24 commits quickly (all recent)
    for i in range(24):
        repo.write(f"f{i}.go", f"package p{i}\n")
        repo.commit(f"commit {i}")
    # 25 commits but all within a day cannot answer a question about a year
    depth = common.probe_history(repo.path, min_commits=20, window_days=365)
    assert depth.is_repo is True
    assert depth.is_shallow is False
    assert depth.commit_count >= 20
    assert depth.usable is False


def test_extensionless_prose_basenames_excluded_from_source(common, repo):
    """Files like README, LICENSE, CHANGELOG are prose, not code."""
    repo.write("README", "# README\n")
    repo.write("LICENSE", "MIT License\n")
    repo.write("AUTHORS", "Alice\nBob\n")
    repo.write("main.zig", "const x = 1;\n")
    found = {p.name for p in common.walk_files(repo.path, source_only=True)}
    assert found == {"main.zig"}
    assert "README" not in found
    assert "LICENSE" not in found
    assert "AUTHORS" not in found


def test_shallow_clone_detected(common, repo, tmp_path):
    """A shallow clone reports is_shallow=True and usable=False."""
    import subprocess
    # Ensure the repo has at least one commit
    repo.write("initial.go", "package main\n")
    repo.commit("initial")

    # Create a local bare repo
    bare_repo = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare_repo)], check=True,
                   capture_output=True)
    # Push repo to the bare repo (so we can clone from it)
    subprocess.run(["git", "-C", str(repo.path), "remote", "add", "origin",
                    str(bare_repo)], check=True, capture_output=True)
    # Get the current branch and push it
    current_branch = subprocess.run(["git", "-C", str(repo.path), "rev-parse",
                                     "--abbrev-ref", "HEAD"],
                                    check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(repo.path), "push", "-u", "origin",
                    current_branch], check=True, capture_output=True)
    # Clone from file:// URL with --depth to create a shallow clone
    shallow_clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--depth", "1",
                    f"file://{bare_repo}", str(shallow_clone)],
                   check=True, capture_output=True)
    depth = common.probe_history(shallow_clone)
    assert depth.is_repo is True
    assert depth.is_shallow is True
    assert depth.usable is False
