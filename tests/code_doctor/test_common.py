"""The walk: what counts as source, what counts as text, what is skipped."""

import json
import os
import pathlib

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


def test_documentation_directory_matched_case_insensitively(common, repo):
    """`Docs/`, `DOCS/`, and `Examples/` are documentation on a case-sensitive
    filesystem too — the comparison must lowercase each path component, the
    same way NON_CODE_BASENAMES already does.
    """
    repo.write("Docs/guide.py", "print(1)\n")
    repo.write("EXAMPLES/sample.py", "print(2)\n")
    repo.write("app.py", "print(3)\n")
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


def test_wide_walk_still_sees_gitignored_files(common, repo):
    """The security scan must not inherit the source walk's blind spot.

    A credential in a gitignored file is still worth reporting — as a
    candidate, not a committed leak — and an ignore rule anyone can edit must
    not be a way to hide one from the scanner.
    """
    repo.write(".gitignore", "local.yaml\n")
    repo.write("local.yaml", "token: abc\n")
    repo.commit("ignore local")
    found = {p.name for p in common.walk_files(repo.path, source_only=False)}
    assert "local.yaml" in found


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


def test_also_caused_by_and_related_lines_are_tuples(common):
    """The schema-bearing collections are tuples, not lists.

    `frozen=True` blocks reassignment but not in-place mutation, so a list
    field would let `candidate.also_caused_by.clear()` walk a validated
    record into a schema-invalid state that __post_init__ never re-checks.
    """
    candidate = common.Finding(file="a.go", line=1, smell_type="x", description="d",
                               kind="candidate", also_caused_by=["it is an entry point"],
                               related_lines=[2, 3])
    assert isinstance(candidate.also_caused_by, tuple)
    assert isinstance(candidate.related_lines, tuple)
    assert not hasattr(candidate.also_caused_by, "clear")
    assert not hasattr(candidate.related_lines, "clear")


def test_finding_survives_json_round_trip(common):
    """also_caused_by and related_lines must survive a JSON round trip.

    json.dumps turns the internal tuples into JSON arrays; json.loads turns
    those back into plain Python lists, not tuples. Finding(**record) must
    still construct from that record, and the tuple type must be restored.
    """
    from dataclasses import asdict
    original = common.Finding(file="a.go", line=2, smell_type="y", description="d",
                              kind="candidate", also_caused_by=["it is an entry point"],
                              related_lines=[3, 4])
    payload = json.loads(json.dumps(asdict(original)))
    assert payload["also_caused_by"] == ["it is an entry point"], "JSON has no tuple type"
    reconstructed = common.Finding(**payload)
    assert reconstructed.also_caused_by == ("it is an entry point",)
    assert reconstructed.related_lines == (3, 4)
    assert reconstructed == original


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


# --------------------------------------------------------------------------- #
# coverage_gaps — the completeness record that keeps lost files from
# silently aggregating into "No problems found".
# --------------------------------------------------------------------------- #

def test_coverage_gaps_empty_in_empty_out(common):
    assert common.coverage_gaps([], []) == {}


def test_coverage_gaps_unreadable_only(common):
    gaps = common.coverage_gaps(["a.go", "b.go"], [])
    assert set(gaps) == {"files_unreadable"}
    assert "2 file(s)" in gaps["files_unreadable"]
    assert "a.go" in gaps["files_unreadable"]
    assert "b.go" in gaps["files_unreadable"]


def test_coverage_gaps_failed_only(common):
    gaps = common.coverage_gaps([], ["c.go"])
    assert set(gaps) == {"files_detector_failed"}
    assert "1 file(s)" in gaps["files_detector_failed"]
    assert "c.go" in gaps["files_detector_failed"]


def test_coverage_gaps_both(common):
    gaps = common.coverage_gaps(["a.go"], ["c.go"])
    assert set(gaps) == {"files_unreadable", "files_detector_failed"}
    assert "a.go" in gaps["files_unreadable"]
    assert "c.go" in gaps["files_detector_failed"]


def test_coverage_gaps_truncates_long_list_but_keeps_true_count(common):
    """The message shows only the first 5 names but the true total count.

    Source truncates the joined names to ``[:5]`` and appends an ellipsis when
    there were more, while the leading count comes from ``len(unreadable)`` —
    the untruncated list. A naive implementation could truncate the count too
    (silently understating how much coverage was actually lost), which is
    exactly the failure this test exists to catch.
    """
    unreadable = [f"file{i}.go" for i in range(7)]
    gaps = common.coverage_gaps(unreadable, [])
    note = gaps["files_unreadable"]
    assert "7 file(s)" in note, "must report the true total, not the truncated count"
    assert "file0.go" in note
    assert "file4.go" in note
    assert "file5.go" not in note, "sixth name onward must be truncated away"
    assert "file6.go" not in note
    assert note.endswith("…")


# --------------------------------------------------------------------------- #
# run_file_detector — the end-to-end per-file main(), driven through argv so
# no subprocess is needed.
# --------------------------------------------------------------------------- #

def _noop_analyze(path, text, reporter):
    pass


def test_run_file_detector_clean_tree_returns_zero_no_findings(common, repo, capsys):
    repo.write("a.go", "package main\n")
    repo.write("b.go", "package main\n")
    rc = common.run_file_detector(
        "desc", "clean", _noop_analyze,
        argv=[str(repo.path), "--format", "json"],
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == []


def test_run_file_detector_crash_is_isolated_to_one_file(common, repo, capsys):
    """A detector bug costs the one file it crashed on, not the whole scan."""
    repo.write("good.go", "package main\n")
    repo.write("bad.go", "package main\n")

    def analyze(path, text, reporter):
        if path.name == "bad.go":
            raise ValueError("boom")
        reporter.finding(1, "smell", "d", "fix it")

    rc = common.run_file_detector(
        "desc", "clean", analyze,
        argv=[str(repo.path), "--format", "json"],
    )
    assert rc == 0
    err = capsys.readouterr()
    payload = json.loads(err.out)
    assert payload["completeness"]["files_detector_failed"].startswith("1 file(s)")
    assert "bad.go" in payload["completeness"]["files_detector_failed"]
    # the crash on bad.go must not have prevented good.go from being analyzed
    assert len(payload["findings"]) == 1
    assert payload["findings"][0]["file"].endswith("good.go")


def test_run_file_detector_unreadable_file_surfaces_in_completeness(
    common, repo, capsys, monkeypatch
):
    """Simulates an unreadable file via monkeypatched Path.read_bytes.

    The suite runs as root, where ``chmod 000`` does not deny access (root
    ignores DAC permission bits), so a real permission-denied file would be
    read successfully and this test would pass vacuously. Monkeypatching
    ``Path.read_bytes`` to raise ``OSError`` for one specific filename produces
    a genuine, unconditional read failure regardless of the user running the
    suite, while leaving every other file (including git's own reads during
    the walk) untouched. ``read_bytes``, not ``read_text``: ``common.read_text``
    sniffs a BOM before decoding, so it reads bytes internally rather than
    calling ``Path.read_text`` directly.
    """
    repo.write("good.go", "package main\n")
    repo.write("unreadable.go", "package main\n")

    original_read_bytes = pathlib.Path.read_bytes

    def fake_read_bytes(self, *args, **kwargs):
        if self.name == "unreadable.go":
            raise OSError("simulated permission denied")
        return original_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_bytes", fake_read_bytes)

    rc = common.run_file_detector(
        "desc", "clean", _noop_analyze,
        argv=[str(repo.path), "--format", "json"],
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["completeness"]["files_unreadable"].startswith("1 file(s)")
    assert "unreadable.go" in payload["completeness"]["files_unreadable"]


def test_run_file_detector_missing_path_returns_2_and_prints_nothing_clean(
    common, tmp_path, capsys
):
    """A typo'd scan path must exit loudly, never resemble a clean report."""
    rc = common.run_file_detector(
        "desc", "clean", _noop_analyze,
        argv=[str(tmp_path / "does-not-exist"), "--format", "json"],
    )
    assert rc == 2
    out = capsys.readouterr()
    assert out.out == "", "a failed scan must not print anything on stdout"
    assert "does-not-exist" in out.err
    assert "clean" not in out.err.lower()


# --------------------------------------------------------------------------- #
# fail_on_bad_path — stderr, never stdout, on a bad scan root.
# --------------------------------------------------------------------------- #

def test_fail_on_bad_path_writes_stderr_not_stdout(common, capsys):
    rc = common.fail_on_bad_path(common.ScanPathError("nope: no such file or directory"))
    assert rc == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "nope" in out.err
    assert "error:" in out.err


# --------------------------------------------------------------------------- #
# build_parser — the shared path / --format / --ignore CLI surface.
# --------------------------------------------------------------------------- #

def test_build_parser_defaults(common):
    parser = common.build_parser("a detector")
    args = parser.parse_args([])
    assert args.path == "."
    assert args.format == "text"
    assert args.ignore == ""


def test_build_parser_rejects_unknown_format(common, capsys):
    parser = common.build_parser("a detector")
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--format", "xml"])
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# parse_ignore — B2: `--ignore a, b` must strip whitespace on every entry.
# --------------------------------------------------------------------------- #

def test_parse_ignore_strips_whitespace_around_entries(common):
    assert common.parse_ignore("a, b") == {"a", "b"}
    assert common.parse_ignore(" a ,b ,  c") == {"a", "b", "c"}


def test_parse_ignore_drops_blank_entries(common):
    assert common.parse_ignore("") == set()
    assert common.parse_ignore("a,,b,") == {"a", "b"}


def test_run_file_detector_ignore_strips_whitespace(common, repo, capsys):
    """The second entry of `--ignore "other, smell"` must still suppress it."""
    repo.write("a.go", "package main\n")

    def analyze(path, text, reporter):
        reporter.finding(1, "smell", "d", "fix it")

    rc = common.run_file_detector(
        "desc", "clean", analyze,
        argv=[str(repo.path), "--format", "json", "--ignore", "other, smell"],
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == []


# --------------------------------------------------------------------------- #
# read_text / is_probably_binary — C2: UTF-16 is NUL-riddled by construction,
# so a BOM must mark it as text rather than being classified binary.
# --------------------------------------------------------------------------- #

def test_utf16_bom_file_is_not_classified_binary(common, repo):
    target = repo.write("app.txt", "placeholder")
    target.write_bytes("hello world\n".encode("utf-16"))
    assert common.is_probably_binary(target) is False


def test_utf8_bom_file_with_a_nul_byte_is_not_classified_binary(common, repo):
    """Plain UTF-8 text never contains a NUL byte, so a BOM-less UTF-8 file
    can never exercise the NUL-sniff branch at all — this embeds one (a
    legitimate, if unusual, single-byte UTF-8 codepoint) so the BOM check is
    actually what saves the file from being misclassified as binary."""
    target = repo.write("app.txt", "placeholder")
    target.write_bytes(b"\xef\xbb\xbf" + b"hello\x00world\n")
    assert common.is_probably_binary(target) is False


def test_utf16_file_reads_back_correctly(common, repo):
    """The walk yielding it is not enough — the decode has to actually work.

    Reading UTF-16 bytes with a hardcoded ``utf-8`` + ``errors="replace"``
    would not raise, but every other byte becomes U+FFFD and the content is
    gone before any detector sees it.
    """
    target = repo.write("app.txt", "placeholder")
    original = "token: super-secret-value\n"
    target.write_bytes(original.encode("utf-16"))
    assert common.read_text(target) == original


def test_utf16_be_file_reads_back_correctly(common, repo):
    target = repo.write("app.txt", "placeholder")
    original = "<<<<<<< HEAD\nconflict\n"
    target.write_bytes(b"\xfe\xff" + original.encode("utf-16-be"))
    assert common.read_text(target) == original


def test_utf8_bom_file_reads_back_without_the_bom_character(common, repo):
    target = repo.write("app.txt", "placeholder")
    original = "aws_key: AKIA2E0RSCHEMAQ7VXBN\n"
    target.write_bytes(b"\xef\xbb\xbf" + original.encode("utf-8"))
    assert common.read_text(target) == original


def test_plain_utf8_file_still_reads_normally(common, repo):
    """No BOM at all must still take the plain UTF-8 path unchanged."""
    target = repo.write("app.go", "package main\n")
    assert common.read_text(target) == "package main\n"


# --------------------------------------------------------------------------- #
# git() / gitignored_paths — C3: git permits any non-NUL byte in a filename;
# the CLI must not raise UnicodeDecodeError before per-file isolation runs.
# --------------------------------------------------------------------------- #

def test_git_helper_survives_a_locale_invalid_filename(common, repo):
    """A tracked path with a byte invalid in the locale encoding must not
    kill the whole `git()` call with UnicodeDecodeError."""
    bad_name = os.fsdecode(b"bad-\xff-name.txt")
    (repo.path / bad_name).write_text("content\n", encoding="utf-8")
    repo.git("add", "-A")
    repo.git("commit", "-qm", "add a byte-invalid filename")
    listing = common.git(repo.path, "-c", "core.quotepath=false", "ls-files")
    assert "bad-" in listing


def test_tracked_paths_survives_a_locale_invalid_filename(common, repo):
    bad_name = os.fsdecode(b"bad-\xff-name.txt")
    (repo.path / bad_name).write_text("content\n", encoding="utf-8")
    repo.git("add", "-A")
    repo.git("commit", "-qm", "add a byte-invalid filename")
    tracked = common.tracked_paths(repo.path)
    assert tracked is not None


# --------------------------------------------------------------------------- #
# C1 — a partial scan (some analysis lost) must not print "No problems
# found".
# --------------------------------------------------------------------------- #

def test_emit_text_does_not_claim_clean_when_a_file_crashed_the_detector(common, capsys):
    common.emit([], "text", "no problems found",
                completeness={"files_detector_failed": "1 file(s) crashed the detector"})
    out = capsys.readouterr().out
    assert "no problems found" not in out.lower()
    assert "incomplete" in out.lower()


def test_emit_text_does_not_claim_clean_when_a_file_was_unreadable(common, capsys):
    common.emit([], "text", "no problems found",
                completeness={"files_unreadable": "1 file(s) could not be read"})
    out = capsys.readouterr().out
    assert "no problems found" not in out.lower()
    assert "incomplete" in out.lower()


def test_emit_text_still_claims_clean_when_completeness_is_scope_only(common, capsys):
    """`categories_run` (and similar scoping notes) narrow a *claim*, not
    coverage — they must not block the clean message."""
    common.emit([], "text", "no problems found",
                completeness={"categories_run": "hygiene, secrets"})
    out = capsys.readouterr().out
    assert "no problems found" in out.lower()


def test_run_file_detector_crash_does_not_print_clean_message(common, repo, capsys):
    """End-to-end: a detector crash on one file must not read as a clean scan."""
    repo.write("bad.go", "package main\n")

    def analyze(path, text, reporter):
        raise ValueError("boom")

    rc = common.run_file_detector(
        "desc", "no problems found", analyze,
        argv=[str(repo.path), "--format", "text"],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "no problems found" not in out.lower()
    assert "incomplete" in out.lower()
