"""build_health.py — findings in, a graded document with extractable metadata out.

The metadata block is the contract with everything downstream (the root roll-up,
the summary page, whatever the user greps next quarter), so most of these tests
read the grade back out of the generated HTML rather than trusting stderr.
"""

import json
import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-overview" / "scripts"
BUILD_HEALTH = SCRIPTS / "build_health.py"

META_RE = re.compile(r'id="code-health-meta">(.*?)</script>', re.DOTALL)


def read_meta(path: Path) -> dict:
    match = META_RE.search(path.read_text(encoding="utf-8"))
    assert match, f"{path} carries no code-health metadata block"
    return json.loads(match.group(1).replace("<\\/", "</"))


def finding(**kwargs):
    base = {"file": "src/app/thing.py", "line": 3, "severity": "medium",
            "description": "something", "suggestion": "fix it"}
    return {**base, **kwargs}


@pytest.fixture
def sized_repo(repo):
    """A package with a known, non-trivial size so densities are meaningful."""
    for module in range(6):
        repo.write(f"src/app/mod{module}.py", "\n".join(f"x{i} = {i}" for i in range(400)))
    repo.commit("init")
    return repo


def build(run_script, repo, findings, *args, out="src/app/docs/health.html", **kwargs):
    """Build a package health page. A doctor is named by default — without one
    nothing is graded, which is its own test below rather than the baseline."""
    report = repo.path / "findings.json"
    report.write_text(json.dumps(findings), encoding="utf-8")
    target = repo.path / out
    run_script(BUILD_HEALTH, "--out", target, "--findings", report,
               "--repo", repo.path, "--name", "app", "--root-dir", "src/app",
               "--doctor", "python-code-doctor", *args)
    return target


def test_a_clean_package_scores_top_marks(run_script, sized_repo):
    meta = read_meta(build(run_script, sized_repo, []))
    assert meta["score"] == 100.0
    assert meta["grade"] == "A+"
    assert meta["findings_total"] == 0


def test_more_findings_never_improves_the_grade(run_script, sized_repo):
    few = read_meta(build(run_script, sized_repo, [finding(category="security")]))
    many = read_meta(build(run_script, sized_repo,
                           [finding(category="security") for _ in range(40)]))
    assert many["score"] < few["score"]


def test_severity_matters_more_than_count(run_script, sized_repo):
    one_high = read_meta(build(run_script, sized_repo,
                               [finding(category="security", severity="high")]))
    three_low = read_meta(build(run_script, sized_repo,
                                [finding(category="security", severity="low")] * 3))
    assert one_high["score"] < three_low["score"], (
        "high is worth ten lows; three lows must not outweigh one high"
    )


def test_weighting_puts_bugs_above_style(run_script, sized_repo):
    correctness = read_meta(build(run_script, sized_repo,
                                  [finding(category="mutation_hazards", severity="high")] * 6))
    hygiene = read_meta(build(run_script, sized_repo,
                              [finding(category="naming_issues", severity="high")] * 6))
    assert correctness["score"] < hygiene["score"]


def test_metadata_carries_every_documented_field(run_script, sized_repo):
    meta = read_meta(build(run_script, sized_repo, [finding(category="security")],
                           "--language", "python", "--doctor", "python-code-doctor"))
    for key in ("schema", "scope", "package", "roots", "language", "doctor", "generated",
                "size", "score", "grade", "categories", "ungraded", "unmapped_types",
                "analyzer_errors", "findings_total", "findings_by_severity", "top_findings"):
        assert key in meta, f"{key} missing from the metadata block"
    assert meta["schema"] == "code-health/1"
    assert meta["scope"] == "package"
    assert {row["key"] for row in meta["categories"]}


def test_category_rows_carry_their_own_grade_and_counts(run_script, sized_repo):
    meta = read_meta(build(run_script, sized_repo,
                           [finding(category="security", severity="high")] * 3))
    security = next(row for row in meta["categories"] if row["key"] == "security")
    assert security["findings"] == {"high": 3, "medium": 0, "low": 0, "total": 3}
    assert security["grade"] != "A+"
    design = next(row for row in meta["categories"] if row["key"] == "design")
    assert design["findings"]["total"] == 0


def test_a_doctors_blind_spot_is_ungraded_not_perfect(run_script, sized_repo):
    meta = read_meta(build(run_script, sized_repo, [finding(smell_type="n_plus_one_query")],
                           "--doctor", "django-code-doctor"))
    assert "duplication" in meta["ungraded"]
    duplication = next(row for row in meta["categories"] if row["key"] == "duplication")
    assert duplication["graded"] is False
    assert duplication["score"] is None
    assert "Ungraded" in (sized_repo.path / "src/app/docs/health.html").read_text()


def test_a_crashed_detector_makes_its_category_ungraded(run_script, sized_repo):
    report = sized_repo.path / "report.json"
    report.write_text(json.dumps({
        "meta": {"analyzer_errors": {"duplicates": "boom"}},
        "categories": {"security": {"issues": [finding(severity="high")]}},
    }), encoding="utf-8")
    out = sized_repo.path / "src/app/docs/health.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", report, "--repo", sized_repo.path,
               "--name", "app", "--root-dir", "src/app", "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["analyzer_errors"] == {"duplicates": "boom"}
    assert "duplication" in meta["ungraded"], (
        "a zero count from a detector that crashed means unknown, not clean"
    )


def test_unmapped_finding_types_are_recorded_and_surfaced(run_script, sized_repo):
    out = build(run_script, sized_repo, [finding(smell_type="zzz_unknown_thing")])
    meta = read_meta(out)
    assert meta["unmapped_types"] == ["zzz_unknown_thing"]
    assert "zzz_unknown_thing" in out.read_text(), "the reader has to be able to see the gap"


def test_paths_are_relative_to_the_repo(run_script, sized_repo):
    absolute = str(sized_repo.path / "src/app/mod0.py")
    out = build(run_script, sized_repo, [finding(
        file=absolute, description=f"Module {absolute!r} has no docstring")])
    text = out.read_text()
    assert str(sized_repo.path) not in text, (
        "these documents get committed; an absolute path from the generating machine "
        "is noise at best"
    )
    assert "src/app/mod0.py" in text


def test_findings_are_html_escaped(run_script, sized_repo):
    out = build(run_script, sized_repo,
                [finding(description="<script>alert(1)</script>", smell_type="x<y")])
    text = out.read_text()
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_the_metadata_block_survives_a_script_tag_in_a_finding(run_script, sized_repo):
    """A literal </script> inside the JSON would end the block early."""
    out = build(run_script, sized_repo, [finding(description="close it </script> now")])
    meta = read_meta(out)
    assert "</script>" in meta["top_findings"][0]["description"]


def test_the_root_document_rolls_up_every_package(run_script, repo):
    for package in ("alpha", "beta"):
        for module in range(4):
            repo.write(f"src/{package}/m{module}.py", "\n".join(f"x{i}=1" for i in range(300)))
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [
            {"name": "alpha", "roots": ["src/alpha"], "docs": "src/alpha/docs",
             "language": "python", "doctor": "python-code-doctor"},
            {"name": "beta", "roots": ["src/beta"], "docs": "src/beta/docs",
             "language": "python", "doctor": "python-code-doctor"},
        ],
    }))
    repo.commit("init")

    reports = {}
    for package, count in (("alpha", 30), ("beta", 1)):
        report = repo.path / f"{package}.json"
        report.write_text(json.dumps([
            finding(category="security", severity="high", file=f"src/{package}/m0.py")
            for _ in range(count)]))
        reports[package] = report
        run_script(BUILD_HEALTH, "--out", repo.path / f"src/{package}/docs/health.html",
                   "--findings", report, "--repo", repo.path, "--name", package,
                   "--root-dir", f"src/{package}", "--doctor", "python-code-doctor")

    out = repo.path / "docs/health.html"
    run_script(BUILD_HEALTH, "--root", "--out", out, "--map", repo.path / "docs/code-overview.json",
               "--findings", reports["alpha"], "--findings", reports["beta"],
               "--repo", repo.path, "--name", "whole-repo", "--doctor", "python-code-doctor")

    meta = read_meta(out)
    assert meta["scope"] == "repository"
    assert {p["package"] for p in meta["packages"]} == {"alpha", "beta"}
    alpha = next(p for p in meta["packages"] if p["package"] == "alpha")
    beta = next(p for p in meta["packages"] if p["package"] == "beta")
    assert alpha["score"] < beta["score"]
    assert meta["findings_total"] == 31, "the root grade is recomputed from the union"
    assert "alpha" in out.read_text() and "beta" in out.read_text()


def test_the_root_table_survives_a_package_with_no_health_page(run_script, repo):
    repo.write("src/alpha/m.py", "x = 1\n")
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [{"name": "alpha", "roots": ["src/alpha"], "docs": "src/alpha/docs"}],
    }))
    repo.commit("init")
    report = repo.path / "f.json"
    report.write_text("[]")
    out = repo.path / "docs/health.html"
    result = run_script(BUILD_HEALTH, "--root", "--out", out,
                        "--map", repo.path / "docs/code-overview.json", "--findings", report,
                        "--repo", repo.path, "--name", "whole-repo",
                        "--doctor", "python-code-doctor")
    assert out.is_file(), "a missing package page is a warning, not a failure"
    assert "alpha" in result.stderr


def test_an_empty_findings_file_is_not_a_crash(run_script, sized_repo, tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("")
    out = sized_repo.path / "src/app/docs/health.html"
    result = run_script(BUILD_HEALTH, "--out", out, "--findings", empty,
                        "--repo", sized_repo.path, "--name", "app", "--root-dir", "src/app",
                        "--doctor", "python-code-doctor")
    assert "empty" in result.stderr
    assert read_meta(out)["findings_total"] == 0


def test_no_recognized_doctor_grades_nothing(run_script, sized_repo):
    """The one output this must never produce is an A+ for an unread language."""
    report = sized_repo.path / "findings.json"
    report.write_text("[]")
    out = sized_repo.path / "src/app/docs/health.html"
    result = run_script(BUILD_HEALTH, "--out", out, "--findings", report,
                        "--repo", sized_repo.path, "--name", "app", "--root-dir", "src/app",
                        "--language", "go")
    meta = read_meta(out)
    assert meta["score"] is None
    assert meta["grade"] == "—"
    assert set(meta["ungraded"]) == {row["key"] for row in meta["categories"]}
    assert "no coverage profile" in result.stderr
    assert "--assume-full-coverage" in result.stderr, "the warning has to name the override"


def test_assume_full_coverage_is_the_deliberate_override(run_script, sized_repo):
    report = sized_repo.path / "findings.json"
    report.write_text("[]")
    out = sized_repo.path / "src/app/docs/health.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", report, "--repo", sized_repo.path,
               "--name", "app", "--root-dir", "src/app", "--assume-full-coverage")
    meta = read_meta(out)
    assert meta["grade"] == "A+"
    assert meta["ungraded"] == []


def test_a_skipped_analyzer_is_ungraded_not_clean(run_script, sized_repo):
    """--skip-duplicates is documented by the doctors, so this report is normal."""
    report = sized_repo.path / "report.json"
    report.write_text(json.dumps({
        "meta": {"analyzers_skipped": ["duplicates"], "analyzer_errors": {}},
        "categories": {"security": {"issues": [finding(severity="high")]}},
    }), encoding="utf-8")
    out = sized_repo.path / "src/app/docs/health.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", report, "--repo", sized_repo.path,
               "--name", "app", "--root-dir", "src/app", "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["analyzers_skipped"] == ["duplicates"]
    assert "duplication" in meta["ungraded"]
    assert "Detectors that were not run" in out.read_text()


def test_an_analyzer_missing_from_analyzers_run_counts_as_skipped(run_script, sized_repo):
    report = sized_repo.path / "report.json"
    report.write_text(json.dumps({
        "meta": {"analyzers_run": ["security"]},
        "categories": {"security": {"issues": []}, "duplicates": {"issues": []}},
    }), encoding="utf-8")
    out = sized_repo.path / "src/app/docs/health.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", report, "--repo", sized_repo.path,
               "--name", "app", "--root-dir", "src/app", "--doctor", "python-code-doctor")
    assert "duplication" in read_meta(out)["ungraded"]


def test_findings_outside_the_package_are_dropped_and_counted(run_script, sized_repo):
    """The doctor runs from the repo root, so its report covers the whole tree."""
    out = build(run_script, sized_repo, [
        finding(file="src/app/mod0.py", category="security", severity="high"),
        finding(file="src/other/thing.py", category="security", severity="high"),
        finding(file=str(sized_repo.path / "src/app/mod1.py"), category="security"),
    ])
    meta = read_meta(out)
    assert meta["findings_total"] == 2, "an absolute path inside the package still counts"
    assert meta["findings_out_of_scope"] == 1
    text = out.read_text()
    assert "src/other/thing.py" not in text
    assert "1 finding(s) in the report were about code outside" in text, (
        "a dropped finding has to be visible on the page, not only in the metadata"
    )


def test_scope_can_be_named_separately_from_the_measured_roots(run_script, sized_repo):
    out = build(run_script, sized_repo,
                [finding(file="src/app/mod0.py"), finding(file="src/other/x.py")],
                "--scope", "src")
    assert read_meta(out)["findings_out_of_scope"] == 0


def test_the_root_grade_is_sized_over_the_mapped_packages_only(run_script, repo):
    """Unassigned code must not pad the denominator it contributes no findings to."""
    for module in range(3):
        repo.write(f"src/app/m{module}.py", "\n".join(f"x{i}=1" for i in range(300)))
    for module in range(20):
        repo.write(f"legacy/old{module}.py", "\n".join(f"y{i}=1" for i in range(300)))
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [{"name": "app", "roots": ["src/app"], "docs": "src/app/docs",
                      "language": "python", "doctor": "python-code-doctor"}],
    }))
    repo.commit("init")
    report = repo.path / "f.json"
    report.write_text(json.dumps(
        [finding(file="src/app/m0.py", category="security", severity="high")] * 5))
    out = repo.path / "docs/health.html"
    run_script(BUILD_HEALTH, "--root", "--out", out, "--map", repo.path / "docs/code-overview.json",
               "--findings", report, "--repo", repo.path, "--name", "whole-repo",
               "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["roots"] == ["src/app"]
    whole_repo_loc = 23 * 300
    assert meta["size"]["loc"] < whole_repo_loc / 2, (
        "the unmapped legacy/ tree would otherwise dilute the density it never contributed to"
    )


def test_the_by_type_table_agrees_with_the_category_scores(run_script, sized_repo):
    """`bare_except` comes from code_smells → Complexity, and re-deriving it from
    the type token alone would land it in Hygiene."""
    out = build(run_script, sized_repo,
                [finding(category="code_smells", smell_type="bare_except", severity="high")])
    text = out.read_text()
    table = text.split("Findings by type", 1)[1]
    assert "bare_except" in table
    row = table.split("bare_except", 1)[1][:300]
    assert "Complexity" in row
    assert "Dependencies" not in row
    complexity = next(r for r in read_meta(out)["categories"] if r["key"] == "complexity")
    assert complexity["findings"]["total"] == 1


def test_a_root_collapsed_package_is_not_listed_as_its_own(run_script, repo):
    repo.write("m.py", "x = 1\n")
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [{"name": "whole", "roots": ["."], "docs": "docs"}],
    }))
    repo.commit("init")
    report = repo.path / "f.json"
    report.write_text("[]")
    out = repo.path / "docs/health.html"
    run_script(BUILD_HEALTH, "--root", "--out", out, "--map", repo.path / "docs/code-overview.json",
               "--findings", report, "--repo", repo.path, "--name", "whole-repo",
               "--doctor", "python-code-doctor")
    assert read_meta(out).get("packages") is None, (
        "the package's documents are the repo's; a table row would link this page to itself"
    )


def test_duplicate_package_names_are_rejected(run_script, repo):
    repo.write("m.py", "x = 1\n")
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [
            {"name": "api", "roots": ["services/a"], "docs": "services/a/docs"},
            {"name": "api", "roots": ["services/b"], "docs": "services/b/docs"},
        ],
    }))
    repo.commit("init")
    report = repo.path / "f.json"
    report.write_text("[]")
    result = run_script(BUILD_HEALTH, "--root", "--out", repo.path / "docs/health.html",
                        "--map", repo.path / "docs/code-overview.json", "--findings", report,
                        "--repo", repo.path, "--name", "whole-repo", expect_rc=1)
    assert "two packages named 'api'" in result.stderr
    assert "unique" in result.stderr


def test_malformed_findings_fail_loudly(run_script, sized_repo, tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    result = run_script(BUILD_HEALTH, "--out", sized_repo.path / "h.html", "--findings", broken,
                        "--repo", sized_repo.path, "--name", "app", expect_rc=1)
    assert "not valid JSON" in result.stderr
