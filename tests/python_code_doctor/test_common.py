"""The record contract in python-code-doctor's common.py.

Python detectors spell their records several ways — `issue_type` for the
smell, `before`/`after` for the fix, `confidence` for the severity — so the
contract is enforced through validate_record at the report hop rather than at
each of 25 constructors. The Finding class itself is the same one every other
doctor ships.
"""

import dataclasses
import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS_DIR, "common")


def test_finding_requires_a_suggestion(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a.py", line=1, smell_type="x", description="d", suggestion="")


def test_candidate_requires_benign_explanations(common):
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.Finding(file="a.py", line=1, smell_type="x", description="d",
                       kind="candidate", also_caused_by=[])


def test_candidate_may_not_carry_a_fix(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.Finding(file="a.py", line=1, smell_type="x", description="d",
                       kind="candidate", suggestion="delete it", also_caused_by=["benign"])


def test_finding_is_frozen_and_round_trips(common):
    original = common.Finding(file="a.py", line=2, smell_type="y", description="d",
                              kind="candidate", also_caused_by=["benign"], related_lines=[3])
    with pytest.raises(dataclasses.FrozenInstanceError):
        original.suggestion = "x"
    assert common.Finding(**json.loads(json.dumps(dataclasses.asdict(original)))) == original


def test_validate_record_accepts_the_detectors_spellings(common):
    dead_code = {"file": "a.py", "line": 1, "issue_type": "unused_import", "name": "os",
                 "description": "d", "confidence": 95, "suggestion": "Remove it."}
    unpythonic = {"file": "a.py", "line": 1, "pattern_type": "range_len", "description": "d",
                  "before": "for i in range(len(x))", "after": "for item in x", "severity": "low"}
    plain = {"file": "a.py", "line": 1, "smell_type": "x", "description": "d",
             "suggestion": "fix", "severity": "high"}
    for record in (dead_code, unpythonic, plain):
        assert common.validate_record(record)["kind"] == "finding"


def test_validate_record_rejects_a_finding_without_a_fix(common):
    with pytest.raises(common.SchemaError, match="suggestion"):
        common.validate_record({"file": "a.py", "line": 1, "issue_type": "unused_function",
                                "description": "d", "confidence": 60})


def test_validate_record_rejects_a_candidate_without_reasons(common):
    with pytest.raises(common.SchemaError, match="also_caused_by"):
        common.validate_record({"file": "a.py", "line": 1, "smell_type": "sql_injection",
                                "description": "d", "suggestion": "", "severity": "high",
                                "kind": "candidate"})


def test_validate_record_does_not_mutate_its_argument(common):
    record = {"file": "a.py", "line": 1, "smell_type": "x", "description": "d", "suggestion": "fix"}
    out = common.validate_record(record)
    assert "kind" not in record and out["kind"] == "finding"


def _tree_with_nested_repositories(root: Path) -> None:
    """The analysed repository, plus three repositories that only live below it."""
    (root / ".git").mkdir(parents=True)  # the analysed root's own repository
    (root / "app.py").write_text("x = 1\n")
    worktree = root / ".worktrees" / "ui"  # a git worktree: `.git` is a file
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/ui\n")
    (worktree / "app.py").write_text("x = 1\n")
    submodule = root / "sub"  # a submodule: `.git` is a file too
    (submodule / "pkg").mkdir(parents=True)
    (submodule / ".git").write_text("gitdir: ../.git/modules/sub\n")
    (submodule / "pkg" / "mod.py").write_text("x = 1\n")
    clone = root / "vendor" / "clone"  # a nested clone: `.git` is a directory
    (clone / ".git").mkdir(parents=True)
    (clone / "lib.py").write_text("x = 1\n")
    (root / "vendor" / "own.py").write_text("x = 1\n")


def test_a_directory_holding_a_git_entry_is_another_repository(common, tmp_path):
    _tree_with_nested_repositories(tmp_path)
    found = sorted(p.relative_to(tmp_path).as_posix() for p in common.find_python_files(tmp_path))
    assert found == ["app.py", "vendor/own.py"]


def test_the_scanned_root_may_itself_be_inside_a_worktree(common, tmp_path):
    """Only directories *below* the root count: scanning a worktree is scanning a repository."""
    _tree_with_nested_repositories(tmp_path)
    worktree = tmp_path / ".worktrees" / "ui"
    assert [p.name for p in common.find_python_files(worktree)] == ["app.py"]
