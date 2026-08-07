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


def test_walk_paths_yields_binaries_for_metadata_checks(common, repo):
    (repo.path / "blob.bin").write_bytes(b"\x00" * 32)
    repo.write("app.go", "package main\n")
    assert {p.name for p in common.walk_paths(repo.path)} == {"blob.bin", "app.go"}
    assert {p.name for p in common.walk_files(repo.path, source_only=False)} == {"app.go"}
