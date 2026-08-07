"""The walk: what counts as source, what counts as text, what is skipped."""

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
