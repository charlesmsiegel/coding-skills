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


_WANTED = ("common", "runner", "tsproject", "find_dead_code", "find_duplicates",
           "find_module_issues")


@pytest.fixture
def ts_modules(monkeypatch):
    """The skill's scripts, imported once so they share one `tsproject`."""
    for name in _WANTED:
        sys.modules.pop(name, None)
    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))
    loaded = {name: importlib.import_module(name) for name in _WANTED}
    yield loaded
    for name in _WANTED:
        sys.modules.pop(name, None)


@pytest.fixture
def ts_runner(ts_modules):
    return ts_modules["runner"]


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


# --------------------------------------------------------------------------- #
# The shared project
# --------------------------------------------------------------------------- #

def test_load_project_is_built_once_per_root(ts_modules, project):
    """Six of the seven tree detectors call load_project for themselves.

    Without this they each re-parse the whole tree, which was ~93% of the
    TypeScript run's critical path.
    """
    load_project = ts_modules["tsproject"].load_project
    assert load_project(project) is load_project(project)


def test_load_project_rebuilds_for_a_different_root(ts_modules, project, tmp_path):
    load_project = ts_modules["tsproject"].load_project
    other = tmp_path / "other"
    (other / "src").mkdir(parents=True)
    (other / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")

    first = load_project(project)
    assert load_project(other) is not first


def test_tree_detectors_do_not_mutate_the_project_they_share(ts_modules, project):
    """They now get the same object, so one that rewrote it would corrupt the rest."""
    tsproject = ts_modules["tsproject"]
    shared = tsproject.load_project(project)
    before = (sorted(map(str, shared.files)), sorted(map(str, shared.failed)),
              {path: len(tsfile.tokens) for path, tsfile in shared.files.items()})

    for name in ("find_dead_code", "find_duplicates", "find_module_issues"):
        ts_modules[name].analyze(project, set(), None)

    after = (sorted(map(str, shared.files)), sorted(map(str, shared.failed)),
             {path: len(tsfile.tokens) for path, tsfile in shared.files.items()})
    assert after == before


def test_weighted_chunks_stay_contiguous_and_lose_nothing(ts_runner):
    """Contiguity is what preserves finding order — a reordered split reorders the report."""
    import random
    chunk = ts_runner.chunk
    random.seed(20260819)
    for _ in range(500):
        items = list(range(random.randint(0, 40)))
        weights = {i: random.choice([0, 1, 1, 5, 100, 10_000]) for i in items}
        count = random.randint(1, 12)
        shards = chunk(items, count, weight=lambda i: weights[i])
        assert [x for shard in shards for x in shard] == items
        assert all(shard for shard in shards), "an empty shard wastes a worker"
        assert len(shards) <= max(1, min(count, len(items)))


def test_weighting_by_size_balances_better_than_by_count(ts_runner):
    """Equal file counts is the wrong split: the run waits on the heaviest shard."""
    chunk = ts_runner.chunk
    # The big modules cluster at the front, as they do when a package's core
    # sorts before its helpers. Splitting eight-and-eight puts all of them in
    # one shard and the run waits on it.
    sizes = [1000] * 4 + [100] * 12
    items = list(range(len(sizes)))

    def heaviest(shards):
        return max(sum(sizes[i] for i in shard) for shard in shards)

    by_count = heaviest(chunk(items, 2))
    by_size = heaviest(chunk(items, 2, weight=lambda i: sizes[i]))
    assert by_size < by_count, f"by_size={by_size} did not beat by_count={by_count}"


def test_a_dead_worker_falls_back_instead_of_crashing(ts_modules, project, monkeypatch):
    """A worker killed by the OOM reaper must not read as a clean repository."""
    import concurrent.futures
    runner = ts_modules["runner"]
    broken = concurrent.futures.process.BrokenProcessPool("worker died")

    class DeadFuture:
        def result(self):
            raise broken

    class DeadPool:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def submit(self, *a, **k): return DeadFuture()

    monkeypatch.setattr(runner, "ProcessPoolExecutor", DeadPool)
    results = runner.run_detectors(
        str(project), [("code_smells", "find_code_smells")], [], jobs=4)
    assert results["code_smells"], "fell back to nothing — a dead pool read as a clean repo"
