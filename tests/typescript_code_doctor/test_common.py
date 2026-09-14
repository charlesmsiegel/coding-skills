"""The record contract in typescript-code-doctor's common.py.

A finding asserts a defect and carries a fix; a candidate reports a lead and
carries the benign explanations instead. The dataclass raises when a detector
confuses them, so the distinction cannot depend on a detector author's memory.
"""

import dataclasses
import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS_DIR, "common")


class _File:
    """The two attributes Reporter reads from a parsed file."""

    def __init__(self, path):
        self.path = path

    def snippet(self, line):
        return f"line {line}"


def test_finding_requires_a_suggestion(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a.ts", line=1, smell_type="x", description="d", suggestion="")


def test_finding_may_not_carry_benign_explanations(common):
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a.ts", line=1, smell_type="x", description="d",
                       suggestion="fix it", also_caused_by=["something benign"])


def test_candidate_requires_benign_explanations(common):
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a.ts", line=1, smell_type="x", description="d",
                       kind="candidate", also_caused_by=[])


def test_candidate_may_not_carry_a_fix(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a.ts", line=1, smell_type="x", description="d",
                       kind="candidate", suggestion="delete it",
                       also_caused_by=["it is an entry point"])


def test_unknown_kind_is_rejected(common):
    with pytest.raises(common.SchemaError, match="kind"):
        common.Finding(file="a.ts", line=1, smell_type="x", description="d",
                       suggestion="fix", kind="probably")


def test_finding_is_frozen_and_collections_are_tuples(common):
    candidate = common.Finding(file="a.ts", line=1, smell_type="x", description="d",
                               kind="candidate", also_caused_by=["it is an entry point"],
                               related_lines=[2, 3])
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.suggestion = "delete it"
    assert isinstance(candidate.also_caused_by, tuple)
    assert isinstance(candidate.related_lines, tuple)


def test_finding_survives_json_round_trip(common):
    original = common.Finding(file="a.ts", line=2, smell_type="y", description="d",
                              kind="candidate", also_caused_by=["it is an entry point"],
                              related_lines=[3, 4])
    payload = json.loads(json.dumps(dataclasses.asdict(original)))
    assert payload["kind"] == "candidate"
    assert common.Finding(**payload) == original


def test_reporter_add_makes_a_finding_with_the_snippet(common):
    reporter = common.Reporter(_File(Path("a.ts")), set())
    reporter.add(4, "smell", "d", "fix it", "high", related=[7])
    record = reporter.findings[0]
    assert record.kind == "finding"
    assert record.code_snippet == "line 4"
    assert record.related_lines == (7,)


def test_reporter_candidate_makes_a_candidate_without_a_fix(common):
    reporter = common.Reporter(_File(Path("a.ts")), set())
    reporter.candidate(4, "lead", "d", ["it may be loaded by convention"])
    record = reporter.findings[0]
    assert record.kind == "candidate"
    assert record.suggestion == ""
    assert record.severity == "low"
    assert record.code_snippet == "line 4"


def test_reporter_candidate_honours_ignore(common):
    reporter = common.Reporter(_File(Path("a.ts")), {"lead"})
    reporter.candidate(1, "lead", "d", ["benign"])
    reporter.candidate(2, "kept", "d", ["benign"])
    assert [f.smell_type for f in reporter.findings] == ["kept"]


def test_sort_puts_findings_before_candidates(common):
    low_finding = common.Finding(file="a.ts", line=9, smell_type="x", description="d",
                                 suggestion="fix", severity="low")
    high_candidate = common.Finding(file="a.ts", line=1, smell_type="y", description="d",
                                    kind="candidate", also_caused_by=["benign"], severity="high")
    ordered = common.sort_findings([high_candidate, low_finding])
    assert [f.kind for f in ordered] == ["finding", "candidate"]


def test_text_report_separates_candidates(common, capsys):
    finding = common.Finding(file="a.ts", line=1, smell_type="x", description="a defect",
                             suggestion="fix it")
    candidate = common.Finding(file="a.ts", line=2, smell_type="y", description="a lead",
                               kind="candidate", also_caused_by=["it is a test double"])
    common.print_findings([finding, candidate], "clean")
    out = capsys.readouterr().out
    assert "1 finding(s), 1 candidate(s)" in out
    assert "Candidates — unverified leads" in out
    assert out.index("a defect") < out.index("Candidates — unverified leads") < out.index("a lead")
    assert "Also caused by:" in out and "it is a test double" in out


def test_a_checkout_under_a_tests_directory_is_not_test_code(common, tmp_path):
    """Every component of the absolute path is the wrong scope: a repo cloned
    to /tmp/tests/repo had all of its sources classified as tests, which
    silenced the security and error-handling findings on exactly those files."""
    repo = tmp_path / "tests" / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text('{"name": "repo"}')
    source = repo / "src" / "app.ts"
    source.write_text("export const a = 1;\n")
    assert common.project_root_of(source) == repo
    assert not common.is_test_file(source)


def test_test_directories_below_the_root_still_classify(common, tmp_path):
    repo = tmp_path / "repo"
    for rel in ("tests/a.ts", "src/__tests__/b.ts", "e2e/c.ts", "src/d.test.ts", "src/e.spec.tsx"):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export {};\n")
    (repo / "tsconfig.json").write_text("{}")
    for rel in ("tests/a.ts", "src/__tests__/b.ts", "e2e/c.ts", "src/d.test.ts", "src/e.spec.tsx"):
        assert common.is_test_file(repo / rel), rel
    assert not common.is_test_file(repo / "src" / "f.ts")


def test_without_a_root_marker_every_component_counts(common, tmp_path):
    loose = tmp_path / "spec" / "loose.ts"
    loose.parent.mkdir(parents=True)
    loose.write_text("export {};\n")
    assert common.project_root_of(loose) is None
    assert common.is_test_file(loose)


def test_an_inaccessible_ancestor_does_not_crash_the_walk(common, tmp_path, monkeypatch):
    """A directory above the file that the process cannot stat (a `700` home
    directory on a shared host, a restrictive bind-mount, locked-down CI)
    must not blow up the walk to the project root."""
    locked = tmp_path / "locked"
    repo = locked / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text('{"name": "repo"}')
    source = repo / "src" / "app.ts"
    source.write_text("export const a = 1;\n")

    real_exists = Path.exists

    def flaky_exists(self, *args, **kwargs):
        if locked in self.parents or self == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", flaky_exists)

    assert common.project_root_of(source) is None
    assert common.is_test_file(source) is False


@pytest.mark.parametrize("name, expected", [
    ("types.d.ts", True), ("types.d.mts", True), ("types.d.cts", True),
    ("types.ts", False), ("d.ts", False), ("mod.mts", False),
])
def test_declaration_files_are_recognized_in_every_module_flavour(common, name, expected):
    assert common.is_declaration_file(Path("src") / name) is expected


@pytest.mark.parametrize("head, expected", [
    ("/* eslint-disable */\n// @generated by protoc-gen-ts\n", True),
    ("// Code generated by openapi-typescript. DO NOT EDIT.\n", True),
    ("// Please do not edit this by hand without asking.\n", False),
    ("export const a = 1;\n" * 6 + "// @generated\n", False),
])
def test_generated_files_are_recognized_by_header_only(common, tmp_path, head, expected):
    path = tmp_path / "gen.ts"
    path.write_text(head + "export const x = 1;\n", encoding="utf-8")
    assert common.is_generated_file(path) is expected


def test_project_root_is_resolved_once_per_directory(common, tmp_path, monkeypatch):
    """Every detector asks for the root of every file it touches, and the answer
    is a property of the directory, not of the file. Walking the ancestors again
    per file doubles the filesystem work of a whole-tree run."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text('{"name": "repo"}')
    first = repo / "src" / "a.ts"
    second = repo / "src" / "b.ts"
    for path in (first, second):
        path.write_text("export const a = 1;\n")

    probes = []
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: (probes.append(self), real_exists(self))[1])

    assert common.project_root_of(first) == repo
    assert probes, "the first call must actually walk the ancestors"
    probes.clear()
    assert common.project_root_of(second) == repo
    assert probes == [], "the second file in the same directory re-walked the tree"


def test_a_nested_fixture_manifest_does_not_reset_the_project_root(common, tmp_path):
    """A fixture project committed under `tests/` carries its own package.json.

    Stopping at the nearest marker made that manifest the project root, so
    `tests/fixtures/app/src/x.ts` sat at `src/x.ts` relative to "its" root and
    classified as production code — the test-file leniency silently switched off
    for a whole fixture tree. Classification is relative to the checkout.
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text('{"name": "repo"}')
    (repo / "src" / "app.ts").write_text("export const a = 1;\n")
    fixture = repo / "tests" / "fixtures" / "app"
    (fixture / "src").mkdir(parents=True)
    (fixture / "package.json").write_text('{"name": "fixture"}')
    nested = fixture / "src" / "x.ts"
    nested.write_text("export const x = 1;\n")

    assert common.project_root_of(nested) == repo
    assert common.is_test_file(nested)
    assert not common.is_test_file(repo / "src" / "app.ts")


def test_git_boundary_stops_a_marker_above_the_checkout_from_winning(common, tmp_path):
    """A `.git` above the checkout — a home directory, a CI workspace, a monorepo
    umbrella — must not pull the project root above the checkout: that turns
    every production file's relative path into something starting with a test
    directory name and silences the security and error-handling findings."""
    common._root_of_dir.cache_clear()
    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)
    repo = outer / "tests" / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text('{"name": "repo"}')
    (repo / ".git").mkdir()
    source = repo / "src" / "app.ts"
    source.write_text("export const a = 1;\n")

    assert common.project_root_of(source) == repo
    assert not common.is_test_file(source)


def test_git_at_the_root_still_scopes_a_nested_fixture_to_the_checkout(common, tmp_path):
    """The nested-fixture case from the outermost-marker fix must keep passing
    once the walk also stops at `.git`: `.git` at the checkout root is itself
    the stopping point, not a reason to climb past it."""
    common._root_of_dir.cache_clear()
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "package.json").write_text('{"name": "repo"}')
    (repo / ".git").mkdir()
    fixture = repo / "tests" / "fixtures" / "app"
    (fixture / "src").mkdir(parents=True)
    (fixture / "package.json").write_text('{"name": "fixture"}')
    nested = fixture / "src" / "x.ts"
    nested.write_text("export const x = 1;\n")

    assert common.project_root_of(nested) == repo
    assert common.is_test_file(nested)
