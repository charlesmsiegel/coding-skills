"""The envelope contract SKILL.md promises, pinned across all four scripts.

The skill tells an agent that every script prints a headline (its most likely
finding), a caveat (what it cannot see), and rows that are candidates to confirm.
That promise is what keeps script output from being pasted into a report as
findings, so it is checked here rather than trusted to prose.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "science-investigation" / "scripts"
INVOCATIONS = [
    ("find_metrics.py", []),
    ("count_examples.py", []),
    ("find_fail_soft.py", []),
    ("trace_value.py", ["0.75"]),
]


@pytest.fixture
def project(tmp_path) -> Path:
    """A small tree with something for each script to find."""
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "score.py").write_text(
        "QUALITY_THRESHOLD = 0.75\n"
        "\n"
        "def quality_score(row, client):\n"
        "    try:\n"
        "        return float(client.judge(row, model='gpt-4o', temperature=0.7))\n"
        "    except Exception:\n"
        "        return 0.0\n",
        encoding="utf-8",
    )
    (tmp_path / "eval" / "data.jsonl").write_text(
        "".join(json.dumps({"q": "x", "expected": "y"} if i < 2 else {"q": "x"}) + "\n" for i in range(20)),
        encoding="utf-8",
    )
    return tmp_path


def run(script, extra, root, fmt="json") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *extra, str(root), "--format", fmt],
        capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize("script,extra", INVOCATIONS)
def test_every_script_emits_the_documented_envelope(project, script, extra):
    result = run(script, extra, project)
    assert result.returncode == 0, result.stderr[-600:]
    payload = json.loads(result.stdout)

    assert payload["tool"] == script.removesuffix(".py")
    assert len(payload["headline"]) > 40, "a headline has to say something, not just count rows"
    assert len(payload["caveat"]) > 60, "the caveat is what stops a row being read as a finding"
    assert isinstance(payload["counts"], dict)
    for row in payload["candidates"]:
        assert row["status"] == "candidate"
        assert row["confirm"].strip()
        assert row["file"] and isinstance(row["line"], int)


@pytest.mark.parametrize("script,extra", INVOCATIONS)
def test_text_output_carries_the_same_headline_and_caveat(project, script, extra):
    payload = json.loads(run(script, extra, project).stdout)
    text = run(script, extra, project, fmt="text").stdout
    assert payload["headline"] in text
    assert payload["caveat"] in text
    if payload["candidates"]:
        assert "candidate(s)" in text and "confirm:" in text


@pytest.mark.parametrize("script,extra", INVOCATIONS)
def test_no_script_writes_to_the_tree_it_audits(project, script, extra):
    """An audit that mutates its subject is not an audit."""
    before = {p: p.stat().st_mtime_ns for p in project.rglob("*") if p.is_file()}
    run(script, extra, project)
    after = {p: p.stat().st_mtime_ns for p in project.rglob("*") if p.is_file()}
    assert before == after
