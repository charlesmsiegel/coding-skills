"""The runner inverts the loop: parse each file once, ask every detector about it.

What has to stay true through that inversion is that the report is identical to
the one a sequential pass produced. These pin that, plus the two degradation
paths — a detector that cannot be imported, and a pool that cannot start — where
the wrong behaviour is a category that silently reads as clean.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "rust-code-doctor"
SCRIPTS_DIR = SKILL_DIR / "scripts"


@pytest.fixture
def runner(load_module):
    return load_module(SCRIPTS_DIR, "runner")


@pytest.fixture
def tree(tmp_path):
    """A small crate with something for several detectors to find."""
    root = tmp_path / "crate"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n[dependencies]\n', encoding="utf-8")
    (root / "src" / "lib.rs").write_text(
        "mod store;\n"
        "pub fn load(p: &String) -> Result<String, std::io::Error> {\n"
        "    let raw = std::fs::read_to_string(p).unwrap();\n"
        "    return Ok(raw);\n"
        "}\n", encoding="utf-8")
    (root / "src" / "store.rs").write_text(
        "pub fn save(items: &Vec<u32>) -> u32 {\n"
        "    let mut total = 0;\n"
        "    for i in 0..items.len() {\n"
        "        total += items[i];\n"
        "    }\n"
        "    total\n"
        "}\n", encoding="utf-8")
    (root / "src" / "orphan.rs").write_text("pub fn nobody() {}\n", encoding="utf-8")
    return root


def run_all(root: Path, *extra: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_all.py"), str(root), "--format", "json", *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    assert result.returncode == 0, result.stderr[-800:]
    return json.loads(result.stdout)


# --- chunking -------------------------------------------------------------- #

def test_chunks_are_contiguous_and_cover_everything(runner):
    items = list(range(23))
    shards = runner.chunk(items, 4)
    assert [x for shard in shards for x in shard] == items
    assert len(shards) <= 4


def test_chunking_by_weight_balances_bytes_not_counts(runner):
    items = [1, 1, 1, 1, 100]
    shards = runner.chunk(items, 2, weight=lambda x: x)
    assert [x for shard in shards for x in shard] == items
    assert all(shards), "a weighted split must not leave a shard empty"


def test_chunk_never_returns_more_shards_than_items(runner):
    assert len(runner.chunk([1, 2], 8)) <= 2
    assert runner.chunk([], 4) == []


# --- parallel and sequential agree ----------------------------------------- #

def test_one_process_and_many_agree_exactly(tree):
    serial = run_all(tree, "--jobs", "1")
    parallel = run_all(tree, "--jobs", "4")
    assert serial["summary"]["by_category"] == parallel["summary"]["by_category"]
    for category, data in serial["categories"].items():
        theirs = parallel["categories"][category]["issues"]
        assert [(i["file"], i["line"], i["smell_type"]) for i in data["issues"]] == \
               [(i["file"], i["line"], i["smell_type"]) for i in theirs], \
               f"{category} differs between one process and four"


def test_the_report_finds_both_file_and_tree_problems(tree):
    report = run_all(tree, "--jobs", "1")
    found = {i["smell_type"] for data in report["categories"].values() for i in data["issues"]}
    assert "unwrap_in_fallible_fn" in found            # a file detector
    assert "index_loop_over_len" in found              # a file detector, second file
    assert "file_never_compiled" in found              # a tree detector
    assert report["meta"]["analyzer_errors"] == {}


def test_skip_drops_a_category_and_records_it(tree):
    report = run_all(tree, "--skip", "duplicates", "--jobs", "1")
    assert "duplicates" not in report["categories"]
    assert report["meta"]["analyzers_skipped"] == ["duplicates"]


def test_an_unknown_skip_name_is_an_error(tree):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_all.py"), str(tree), "--skip", "nonsense"],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode != 0 and "unknown categories" in result.stderr


# --- degradation ----------------------------------------------------------- #

def test_a_detector_that_cannot_be_imported_reports_an_error_not_a_clean_category(runner, tree):
    results = runner.run_detectors(str(tree), [("bogus", "no_such_detector_module")], [], jobs=1)
    assert isinstance(results["bogus"], dict)
    assert "ModuleNotFoundError" in results["bogus"]["error"]
    assert results["bogus"]["issues"] == []


def test_a_failing_tree_detector_does_not_sink_the_others(runner, tree):
    # The detectors import each other by bare name, as they do when a script is
    # run directly; load_module removes the path again once `runner` is in.
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        results = runner.run_detectors(
            str(tree), [], [("bogus", "no_such_detector_module"), ("dead_code", "find_dead_code")],
            jobs=1)
    finally:
        sys.path.remove(str(SCRIPTS_DIR))
    assert isinstance(results["bogus"], dict) and results["bogus"].get("error")
    assert isinstance(results["dead_code"], list)


def test_analyzer_errors_surface_in_the_text_report(tree):
    """A category that crashed must not read as a zero-finding category."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); import analyze_all;"
         "analyze_all.ANALYZERS = [('bogus', 'no_such_detector_module', 'x', 'file')];"
         "analyze_all.CATEGORIES = ['bogus'];"
         "report = analyze_all.generate_report(%r, jobs=1);"
         "analyze_all.print_text_report(report)" % (str(SCRIPTS_DIR), str(tree))],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0, result.stderr[-500:]
    assert "ANALYSIS INCOMPLETE" in result.stdout
    assert "No issues found by the analyzers that completed" in result.stdout


# --- the project cache ----------------------------------------------------- #

def test_load_project_returns_the_same_object_for_a_repeated_root(load_module, tree):
    rsproject = load_module(SCRIPTS_DIR, "rsproject")
    first = rsproject.load_project(tree)
    second = rsproject.load_project(tree)
    assert first is second, "the tree would be parsed once per detector without this"


def test_the_project_knows_which_files_rustc_reaches(load_module, tree):
    rsproject = load_module(SCRIPTS_DIR, "rsproject")
    project = rsproject.load_project(tree)
    orphans = [p.name for p in project.orphan_files()]
    assert orphans == ["orphan.rs"]
    assert {p.name for p in project.modules} == {"lib.rs", "store.rs"}


def test_a_detector_that_crashes_on_one_file_marks_its_category_incomplete(runner, tree, tmp_path):
    """Warning on stderr alone let a JSON consumer read a partial category as
    clean — the one thing this repo's schema says must never happen."""
    boom = SCRIPTS_DIR / "_boom_for_tests.py"
    boom.write_text(
        "def analyze(rsfile, ignore):\n"
        "    raise ValueError('boom')\n", encoding="utf-8")
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        results = runner.run_detectors(str(tree), [("boom", "_boom_for_tests")], [], jobs=1)
    finally:
        sys.path.remove(str(SCRIPTS_DIR))
        boom.unlink()
    assert isinstance(results["boom"], dict), "a crashed category must not be a bare list"
    assert "ValueError" in results["boom"]["error"]


def test_a_healthy_category_stays_a_plain_list(runner, tree):
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        results = runner.run_detectors(str(tree), [("errors", "find_error_handling")], [], jobs=1)
    finally:
        sys.path.remove(str(SCRIPTS_DIR))
    assert isinstance(results["errors"], list)
    assert any(i["smell_type"] == "unwrap_in_fallible_fn" for i in results["errors"])
