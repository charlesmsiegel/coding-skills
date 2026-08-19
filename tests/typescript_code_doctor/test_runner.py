"""The pooled runner must produce exactly what the sequential one produced.

analyze_all used to run each of the 25 detectors as its own subprocess over the
whole tree — 18 parsing every file for themselves and 6 more each calling
load_project(). It now parses once and shards the files across a pool. These
tests pin the properties that keep the report identical: the shards reassemble
in the original order, and the worker count changes nothing.
"""

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"

DIRTY = """\
export function handle(input: any, retry: boolean) {
  var seen = [];
  if (input == null) { return null; }
  for (let i = 0; i < 10; i++) {
    seen.push(i);
  }
  try {
    eval(input);
  } catch (e) {}
  return seen as unknown as string;
}
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A tree with enough files to be split across more than one shard."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text('{"name": "p", "version": "1.0.0"}', encoding="utf-8")
    for index in range(12):
        (root / "src" / f"mod{index:02d}.ts").write_text(DIRTY, encoding="utf-8")
    return root


@pytest.fixture
def ts_runner(monkeypatch):
    sys.modules.pop("runner", None)
    sys.modules.pop("common", None)
    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))
    module = importlib.import_module("runner")
    yield module
    sys.modules.pop("runner", None)
    sys.modules.pop("common", None)


def analyze_all(target: Path, *extra: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_all.py"), str(target),
         "--format", "json", *extra],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout)
    report["meta"]["timestamp"] = "pinned"
    return report


def test_chunk_covers_every_item_in_order(ts_runner):
    items = list(range(17))
    shards = ts_runner.chunk(items, 4)
    assert len(shards) == 4
    assert [item for shard in shards for item in shard] == items, \
        "concatenating the shards must rebuild the original order"


def test_chunk_never_returns_more_shards_than_items(ts_runner):
    assert ts_runner.chunk([1, 2], 8) == [[1], [2]]
    assert ts_runner.chunk([], 4) == []


def test_one_worker_and_many_report_exactly_the_same(project):
    assert analyze_all(project, "--jobs", "1") == analyze_all(project, "--jobs", "4")


def test_the_pooled_run_still_finds_the_planted_defects(project):
    report = analyze_all(project, "--jobs", "4")
    assert report["summary"]["total_issues"] > 0
    found = {issue.get("smell_type") for data in report["categories"].values()
             for issue in data["issues"]}
    assert {"loose_equality", "var_declaration"} & found, sorted(f for f in found if f)


def test_a_skipped_category_is_absent_rather_than_empty(project):
    report = analyze_all(project, "--skip", "duplicates")
    assert "duplicates" not in report["categories"]
    assert report["meta"]["analyzers_skipped"] == ["duplicates"]
