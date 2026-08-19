"""The pooled runner must produce exactly what the sequential one produced.

analyze_all used to run each detector as its own subprocess over the whole tree.
It now parses each file once, asks every detector about that one tree, and
spreads the files over a process pool. That is a large change to *when* things
run and no change at all to *what* is reported, so these tests pin the
properties that make the second half true: the shared parse behaves like a
private one, the shards reassemble in the original order, and the worker count
does not change a single finding.
"""

import ast
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"

# Loaded together in one import session on purpose: the point of these tests is
# that the detectors and the cache are looking at the *same* `common`, which the
# shared `load_module` fixture deliberately prevents by evicting between loads.
_WANTED = ("common", "runner", "find_code_smells", "find_security_issues",
           "find_dead_code", "find_naming_issues", "find_unpythonic")


@pytest.fixture
def runner_modules(monkeypatch):
    """python-code-doctor's scripts, imported once so they share one `common`."""
    for name in _WANTED:
        sys.modules.pop(name, None)
    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))
    loaded = {name: importlib.import_module(name) for name in _WANTED}
    assert loaded["find_code_smells"].cached_parse is loaded["common"].cached_parse
    yield loaded
    for name in _WANTED:
        sys.modules.pop(name, None)

DIRTY = '''\
import os
import sys


def handler(data=[], flag=False):
    try:
        exec(data)
    except:
        pass
    for i in range(len(data)):
        print(data[i])
    return None
'''


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A tree with enough files to be split across more than one shard."""
    root = tmp_path / "proj"
    root.mkdir()
    for index in range(12):
        (root / f"module_{index:02d}.py").write_text(DIRTY, encoding="utf-8")
    return root


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


# --------------------------------------------------------------------------- #
# The shared parse
# --------------------------------------------------------------------------- #

def test_cached_parse_returns_the_same_tree_to_the_second_caller(runner_modules, tmp_path):
    common = runner_modules["common"]
    source_file = tmp_path / "m.py"
    source_file.write_text("x = 1\n", encoding="utf-8")

    first_source, first_tree = common.cached_parse(source_file)
    second_source, second_tree = common.cached_parse(source_file)

    assert first_source == second_source == "x = 1\n"
    assert first_tree is second_tree, "the second detector re-parsed instead of reusing the tree"


def test_cached_parse_holds_only_one_file(runner_modules, tmp_path):
    """Holding every tree would cost gigabytes on a large repository."""
    common = runner_modules["common"]
    first, second = tmp_path / "a.py", tmp_path / "b.py"
    first.write_text("a = 1\n", encoding="utf-8")
    second.write_text("b = 2\n", encoding="utf-8")

    _, first_tree = common.cached_parse(first)
    common.cached_parse(second)
    _, first_again = common.cached_parse(first)

    assert first_again is not first_tree, "the first file was still being held"


def test_cached_parse_reraises_a_syntax_error_like_ast_parse(runner_modules, tmp_path):
    """Detectors classify unparseable files with their own `except SyntaxError`."""
    common = runner_modules["common"]
    broken = tmp_path / "broken.py"
    broken.write_text("def (:\n", encoding="utf-8")

    with pytest.raises(SyntaxError):
        common.cached_parse(broken)
    with pytest.raises(SyntaxError):  # cached, and still the same class of failure
        common.cached_parse(broken)


def test_cached_source_still_yields_text_for_a_file_that_will_not_parse(runner_modules, tmp_path):
    """find_comment_smells reads text, and a file that will not parse still has text."""
    common = runner_modules["common"]
    broken = tmp_path / "broken.py"
    broken.write_text("# TODO: fix\ndef (:\n", encoding="utf-8")

    assert common.cached_source(broken) == "# TODO: fix\ndef (:\n"


def test_cached_source_raises_when_the_read_itself_failed(runner_modules, tmp_path):
    common = runner_modules["common"]
    with pytest.raises(OSError):
        common.cached_source(tmp_path / "absent.py")


def test_no_detector_mutates_the_tree_it_is_handed(runner_modules, tmp_path):
    """Detectors share one tree per file, so one that rewrote it would corrupt the rest.

    find_duplicates does normalise identifiers in the trees it hashes, which is
    exactly why it parses for itself instead of joining the shared pass.
    """
    common = runner_modules["common"]
    module = tmp_path / "m.py"
    module.write_text(DIRTY, encoding="utf-8")

    _, tree = common.cached_parse(module)
    before = ast.dump(tree)
    for name in ("find_code_smells", "find_security_issues", "find_dead_code",
                 "find_naming_issues", "find_unpythonic"):
        runner_modules[name].analyze_file(module, set())
    assert ast.dump(tree) == before


# --------------------------------------------------------------------------- #
# Sharding
# --------------------------------------------------------------------------- #

def test_chunk_covers_every_item_in_order(runner_modules):
    chunk = runner_modules["runner"].chunk
    items = list(range(17))
    shards = chunk(items, 4)

    assert len(shards) == 4
    assert [item for shard in shards for item in shard] == items, \
        "concatenating the shards must rebuild the original order"


def test_chunk_never_returns_more_shards_than_items(runner_modules):
    chunk = runner_modules["runner"].chunk
    assert chunk([1, 2], 8) == [[1], [2]]
    assert chunk([], 4) == []


# --------------------------------------------------------------------------- #
# End to end: the worker count is not allowed to matter
# --------------------------------------------------------------------------- #

def test_one_worker_and_many_report_exactly_the_same(project):
    serial = analyze_all(project, "--jobs", "1")
    pooled = analyze_all(project, "--jobs", "4")
    assert serial == pooled


def test_the_pooled_run_still_finds_the_planted_defects(project):
    report = analyze_all(project, "--jobs", "4")
    assert report["summary"]["total_issues"] > 0
    found = {issue.get("smell_type") or issue.get("issue_type") or issue.get("pattern_type")
             for data in report["categories"].values() for issue in data["issues"]}
    assert {"mutable_default", "bare_except"} <= found


def test_a_skipped_category_is_absent_rather_than_empty(project):
    report = analyze_all(project, "--skip", "duplicates")
    assert "duplicates" not in report["categories"]
    assert report["meta"]["analyzers_skipped"] == ["duplicates"]


# --------------------------------------------------------------------------- #
# Degrading
# --------------------------------------------------------------------------- #

def test_one_unparseable_file_is_named_once_not_once_per_detector(project, tmp_path):
    """~28 detectors trip over the same broken file; the reader needs one line."""
    (project / "broken.py").write_text("def (:\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_all.py"), str(project),
         "--format", "json", "--jobs", "1"],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    named = [line for line in result.stderr.splitlines() if "does not parse" in line]
    assert len(named) == 1, f"said it {len(named)} times:\n" + "\n".join(named[:5])
    assert "broken.py" in named[0]


def test_a_dead_worker_falls_back_instead_of_crashing(project, monkeypatch):
    """A worker killed by the OOM reaper must not read as a clean repository."""
    import concurrent.futures
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        runner = __import__("runner")
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
    finally:
        sys.path.remove(str(SCRIPTS_DIR))
        for name in _WANTED:
            sys.modules.pop(name, None)

    assert results["code_smells"], "fell back to nothing — a dead pool read as a clean repo"


def test_the_sort_the_runner_uses_is_the_one_the_detectors_use(runner_modules, tmp_path):
    """Two spellings of one order is how a pooled run and a CLI drift apart."""
    common, runner = runner_modules["common"], runner_modules["runner"]

    class Rec:
        def __init__(self, severity, file, line):
            self.severity, self.file, self.line = severity, file, line
        def as_dict(self):
            return {"severity": self.severity, "file": self.file, "line": self.line}

    # Within a severity, file order and line order must disagree, or the test
    # cannot tell (severity, file, line) from (severity, line, file).
    records = [Rec("low", "b.py", 1), Rec("low", "a.py", 9),
               Rec("high", "b.py", 2), Rec("high", "a.py", 8),
               Rec("medium", "b.py", 3), Rec("medium", "a.py", 7)]
    by_detector = [r.as_dict() for r in common.sort_findings(list(records))]
    by_runner = sorted((r.as_dict() for r in records), key=runner._standard_key)
    assert by_detector == by_runner


def test_weighted_chunks_stay_contiguous_and_lose_nothing(runner_modules):
    """Contiguity is what preserves finding order — a reordered split reorders the report."""
    import random
    chunk = runner_modules["runner"].chunk
    random.seed(20260819)
    for _ in range(500):
        items = list(range(random.randint(0, 40)))
        weights = {i: random.choice([0, 1, 1, 5, 100, 10_000]) for i in items}
        count = random.randint(1, 12)
        shards = chunk(items, count, weight=lambda i: weights[i])
        assert [x for shard in shards for x in shard] == items
        assert all(shard for shard in shards), "an empty shard wastes a worker"
        assert len(shards) <= max(1, min(count, len(items)))


def test_weighting_by_size_balances_better_than_by_count(runner_modules):
    """Equal file counts is the wrong split: the run waits on the heaviest shard."""
    chunk = runner_modules["runner"].chunk
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
