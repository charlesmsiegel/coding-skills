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


def test_generated_files_are_skipped(runner, tmp_path):
    """`is_generated` existed and was never called, so bindgen or prost output
    could drown the findings a user can actually act on."""
    root = tmp_path / "crate"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "d"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
    (root / "src" / "lib.rs").write_text(
        "pub fn a(p: &str) -> Result<String, std::io::Error> "
        "{ Ok(std::fs::read_to_string(p).unwrap()) }\n", encoding="utf-8")
    (root / "src" / "bindings.rs").write_text(
        "// @generated by bindgen\npub fn b(p: &str) -> Result<String, std::io::Error> "
        "{ Ok(std::fs::read_to_string(p).unwrap()) }\n", encoding="utf-8")
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        results = runner.run_detectors(str(root), [("errors", "find_error_handling")], [], jobs=1)
    finally:
        sys.path.remove(str(SCRIPTS_DIR))
    files = {Path(i["file"]).name for i in results["errors"]}
    assert files == {"lib.rs"}, f"generated file was analysed: {files}"


def test_candidates_are_counted_separately_and_kept_out_of_the_high_list(tmp_path, load_module, capsys):
    """A candidate is a lead, not a defect: it has its own count and its own
    heading, and never appears under HIGH SEVERITY ISSUES."""
    module = load_module(SCRIPTS_DIR, "analyze_all")
    report = {
        "meta": {"analyzed_path": str(tmp_path), "timestamp": "t",
                 "analyzers_run": ["types"], "analyzers_skipped": [], "analyzer_errors": {}},
        "summary": {"total_issues": 2, "total_candidates": 1,
                    "by_severity": {"high": 2, "medium": 0, "low": 0},
                    "by_category": {"types": 2}},
        "categories": {"types": {"count": 2, "issues": [
            {"file": "a.rs", "line": 1, "smell_type": "unwrap_in_fallible_fn", "description": "a defect",
             "suggestion": "fix it", "severity": "high", "kind": "finding", "category": "types"},
            {"file": "a.rs", "line": 2, "smell_type": "narrowing_cast", "description": "a lead",
             "suggestion": "", "severity": "high", "kind": "candidate", "category": "types",
             "also_caused_by": ["the value was narrowed by a runtime check"]},
        ]}},
    }
    module.print_text_report(report)
    out = capsys.readouterr().out
    high_block = out[out.index("HIGH SEVERITY ISSUES"):out.index("CANDIDATES")]
    assert "a defect" in high_block and "a lead" not in high_block
    assert "a lead" in out[out.index("CANDIDATES"):]
    assert "Total issues found: 2" in out
    assert "of which 1 are candidates" in out, \
        "the summary line claimed the candidates were excluded from the total"


def test_generate_report_counts_candidates_and_rejects_invalid_records(tree, load_module, monkeypatch):
    """The report hop re-validates every record. A candidate with no benign
    explanation is dropped and named, never silently counted as a finding."""
    module = load_module(SCRIPTS_DIR, "analyze_all")

    def fake_run_detectors(path, file_specs, tree_specs, jobs=None):
        return {"types": [
            {"file": "a.rs", "line": 1, "smell_type": "unwrap_in_fallible_fn", "description": "d",
             "suggestion": "fix", "severity": "high", "kind": "finding",
             "also_caused_by": [], "code_snippet": "", "related_lines": []},
            {"file": "a.rs", "line": 2, "smell_type": "narrowing_cast", "description": "d",
             "suggestion": "", "severity": "low", "kind": "candidate",
             "also_caused_by": ["benign"], "code_snippet": "", "related_lines": []},
            {"file": "a.rs", "line": 3, "smell_type": "bogus", "description": "d",
             "suggestion": "", "severity": "low", "kind": "candidate",
             "also_caused_by": [], "code_snippet": "", "related_lines": []},
        ]}

    monkeypatch.setattr(module, "run_detectors", fake_run_detectors)
    monkeypatch.setattr(module, "ANALYZERS", [("types", "find_type_issues", "Types", module.FILE)])
    report = module.generate_report(str(tree))
    assert report["summary"]["total_issues"] == 2
    assert report["summary"]["total_candidates"] == 1
    assert "bogus" in report["meta"]["records_rejected"]["types"]
    # A detector emitting contract-invalid records is a broken detector, and
    # analyzer_errors is what code-overview's coverage logic reads — so the
    # category is graded as ungraded rather than as clean.
    assert "did not satisfy" in report["meta"]["analyzer_errors"]["types"]


def test_the_diff_lens_marks_a_candidate_as_a_lead_not_a_ranked_defect(load_module, capsys):
    """No diff-safe Rust detector emits a candidate yet, so the renderer is
    exercised directly — the shape it must print the day one does.

    A severity icon on a lead reads as a verdict the syntax never proved, and a
    bare arrow reads as a fix that was forgotten. What the record has instead is
    the benign readings the reader must rule out first.
    """
    module = load_module(SCRIPTS_DIR, "analyze_diff")
    findings = [
        {"file": "a.rs", "line": 1, "smell_type": "unwrap_in_fallible_fn",
         "description": "a defect", "suggestion": "fix it", "severity": "high",
         "kind": "finding"},
        {"file": "a.rs", "line": 2, "smell_type": "narrowing_cast",
         "description": "a lead", "suggestion": "", "severity": "high",
         "kind": "candidate",
         "also_caused_by": ["the range was checked by the caller"]},
    ]
    module.print_text(["a.rs"], findings, "0123456789abcdef")
    out = capsys.readouterr().out

    assert "[CANDIDATE]" in out
    assert "? also caused by: the range was checked by the caller" in out
    marked = [line for line in out.splitlines() if "[CANDIDATE]" in line]
    assert marked and not any(icon in line for line in marked for icon in "🔴🟡🟢"), \
        "a candidate was given a severity icon it did not earn"
    assert [line for line in out.splitlines() if line.strip() == "→"] == [], \
        "a candidate rendered a fix arrow with nothing after it"
    assert "1 finding(s), 1 candidate(s)" in out, \
        "the count line still calls every record a finding"


def test_a_candidate_only_category_earns_no_recommendation(tmp_path, load_module, capsys):
    """RECOMMENDATIONS tells the reader to change code. A category whose only
    record is an unverified lead has not earned an instruction."""
    module = load_module(SCRIPTS_DIR, "analyze_all")
    report = {
        "meta": {"analyzed_path": str(tmp_path), "timestamp": "t",
                 "analyzers_run": ["type_issues"], "analyzers_skipped": [], "analyzer_errors": {}},
        "summary": {"total_issues": 1, "total_candidates": 1,
                    "by_severity": {"high": 0, "medium": 0, "low": 1},
                    "by_category": {"type_issues": 1}},
        "categories": {"type_issues": {"count": 1, "issues": [
            {"file": "a.rs", "line": 2, "smell_type": "narrowing_cast", "description": "a lead",
             "suggestion": "", "severity": "low", "kind": "candidate", "category": "type_issues",
             "also_caused_by": ["the range was checked by the caller"]},
        ]}},
    }
    module.print_text_report(report)
    out = capsys.readouterr().out
    assert module.RECOMMENDATIONS["type_issues"] not in out, \
        "a lead bought advice about a defect nothing found"


def test_the_report_hop_keeps_a_record_carrying_an_extra_key(tree, load_module, monkeypatch):
    """The hop validates the contract's fields; a detector that also attaches
    something of its own has not broken the contract. Dropping the record loses
    a real finding over a key the validator simply did not know about."""
    module = load_module(SCRIPTS_DIR, "analyze_all")

    def fake_run_detectors(path, file_specs, tree_specs, jobs=None):
        return {"types": [
            {"file": "a.rs", "line": 1, "smell_type": "unwrap_in_fallible_fn", "description": "d",
             "suggestion": "fix", "severity": "high", "kind": "finding",
             "also_caused_by": [], "code_snippet": "", "related_lines": [], "lines": 5},
        ]}

    monkeypatch.setattr(module, "run_detectors", fake_run_detectors)
    monkeypatch.setattr(module, "ANALYZERS", [("types", "find_type_issues", "Types", module.FILE)])
    report = module.generate_report(str(tree))
    assert [i["smell_type"] for i in report["categories"]["types"]["issues"]] == ["unwrap_in_fallible_fn"]
    assert not report["meta"].get("records_rejected")
