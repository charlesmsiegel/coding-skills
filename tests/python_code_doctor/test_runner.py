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

    common.cached_parse(first)
    common.cached_parse(second)

    assert list(common._LAST_PARSE) == [str(second)]


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
