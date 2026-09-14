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


@pytest.mark.parametrize("name, expected", [
    ("types.d.ts", True), ("types.d.mts", True), ("types.d.cts", True),
    ("types.ts", False), ("d.ts", False), ("mod.mts", False),
])
def test_declaration_files_are_recognized_in_every_module_flavour(common, name, expected):
    assert common.is_declaration_file(Path("src") / name) is expected
