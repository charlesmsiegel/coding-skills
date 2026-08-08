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


# One detector per rubric category — what "python-code-doctor ran fully" means.
# Tests that want a graded page have to supply this envelope, because a bare
# JSON list is exactly what a single detector emits and no longer implies a
# full run.
FULL_RUN = ["mutation_hazards", "security", "untested_modules", "complexity",
            "design_smells", "duplicates", "naming_issues", "findings"]


def full_report(findings):
    """An analyze_all.py-shaped report declaring every analyzer ran."""
    return {"meta": {"analyzers_run": FULL_RUN, "analyzer_errors": {}},
            "categories": {"findings": {"issues": findings}}}


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
    report.write_text(json.dumps(full_report(findings)), encoding="utf-8")
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
                           [finding(category="security", line=n) for n in range(40)]))
    assert many["score"] < few["score"]


def test_severity_matters_more_than_count(run_script, sized_repo):
    one_high = read_meta(build(run_script, sized_repo,
                               [finding(category="security", severity="high")]))
    three_low = read_meta(build(run_script, sized_repo,
                                [finding(category="security", severity="low", line=n)
                                 for n in range(3)]))
    assert one_high["score"] < three_low["score"], (
        "high is worth ten lows; three lows must not outweigh one high"
    )


def test_weighting_puts_bugs_above_style(run_script, sized_repo):
    correctness = read_meta(build(run_script, sized_repo,
                                  [finding(category="mutation_hazards", severity="high", line=n)
                                   for n in range(6)]))
    hygiene = read_meta(build(run_script, sized_repo,
                              [finding(category="naming_issues", severity="high", line=n)
                               for n in range(6)]))
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
                           [finding(category="security", severity="high", line=n)
                            for n in range(3)]))
    security = next(row for row in meta["categories"] if row["key"] == "security")
    assert security["findings"] == {"high": 3, "medium": 0, "low": 0, "total": 3}
    assert security["grade"] != "A+"
    design = next(row for row in meta["categories"] if row["key"] == "design")
    assert design["findings"]["total"] == 0


def test_a_doctors_blind_spot_is_ungraded_not_perfect(run_script, sized_repo):
    """Evidence never exceeds what the named doctor can actually detect."""
    meta = read_meta(build(run_script, sized_repo, [finding(smell_type="n_plus_one_query")],
                           "--doctor", "django-code-doctor"))
    assert "duplication" in meta["ungraded"], (
        "the report's envelope claims a duplication analyzer ran, but django-code-doctor "
        "has none — a mislabeled run is not a reason to grade the category"
    )
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
        report.write_text(json.dumps(full_report([
            finding(category="security", severity="high", file=f"src/{package}/m0.py", line=n)
            for n in range(count)])))
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
    assert "which analyzers ran" in result.stderr
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
        [finding(file="src/app/m0.py", category="security", severity="high", line=n)
         for n in range(5)]))
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


def test_a_single_detector_report_grades_nothing(run_script, sized_repo):
    """One detector's output says what it found, never what else was examined."""
    report = sized_repo.path / "one.json"
    report.write_text(json.dumps({"issues": []}))
    out = sized_repo.path / "src/app/docs/health.html"
    result = run_script(BUILD_HEALTH, "--out", out, "--findings", report,
                        "--repo", sized_repo.path, "--name", "app", "--root-dir", "src/app",
                        "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["score"] is None, "an empty find_duplicates run is not an A+ in seven categories"
    assert meta["grade"] == "—"
    assert "single detector" in result.stderr
    assert "--covers" in result.stderr


def test_covers_names_what_was_examined(run_script, sized_repo):
    report = sized_repo.path / "one.json"
    report.write_text(json.dumps({"issues": [finding(smell_type="duplicate_block")]}))
    out = sized_repo.path / "src/app/docs/health.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", report, "--repo", sized_repo.path,
               "--name", "app", "--root-dir", "src/app", "--covers", "duplication")
    meta = read_meta(out)
    assert meta["ungraded"] == [k for k in
                                (row["key"] for row in meta["categories"]) if k != "duplication"]
    duplication = next(r for r in meta["categories"] if r["key"] == "duplication")
    assert duplication["graded"] is True


def test_covers_rejects_an_unknown_category(run_script, sized_repo):
    report = sized_repo.path / "one.json"
    report.write_text("[]")
    result = run_script(BUILD_HEALTH, "--out", sized_repo.path / "h.html", "--findings", report,
                        "--repo", sized_repo.path, "--name", "app", "--covers", "not-a-category",
                        expect_rc=1)
    assert "unknown categories" in result.stderr


def test_a_companion_report_cannot_undo_the_first_reports_skip(run_script, sized_repo):
    """The Python+Django merge the skill recommends, with --skip-duplicates."""
    python = sized_repo.path / "py.json"
    python.write_text(json.dumps({
        "meta": {"analyzers_run": ["security", "complexity"], "analyzers_skipped": ["duplicates"]},
        "categories": {"security": {"issues": [finding(severity="high")]},
                       "complexity": {"issues": []}},
    }))
    django = sized_repo.path / "dj.json"   # flat list: says nothing about coverage
    django.write_text(json.dumps([finding(smell_type="n_plus_one_query", file="src/app/mod0.py")]))
    out = sized_repo.path / "src/app/docs/health.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", python, "--findings", django,
               "--repo", sized_repo.path, "--name", "app", "--root-dir", "src/app",
               "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert "duplication" in meta["ungraded"], (
        "the flat companion report has no duplication detector either; it must not be "
        "read as evidence that duplication was examined"
    )
    assert "security" not in meta["ungraded"]
    assert "complexity" not in meta["ungraded"]


def test_the_same_defect_from_two_doctors_is_charged_once(run_script, sized_repo):
    """Both doctors flag a hardcoded SECRET_KEY at the same line, at different severities."""
    shared = {"file": "src/app/mod0.py", "line": 2, "smell_type": "hardcoded_secret"}
    a = sized_repo.path / "a.json"
    a.write_text(json.dumps([{**shared, "severity": "medium", "description": "python's wording"}]))
    b = sized_repo.path / "b.json"
    b.write_text(json.dumps([{**shared, "severity": "high", "description": "django's wording"}]))
    out = sized_repo.path / "src/app/docs/health.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", a, "--findings", b,
               "--repo", sized_repo.path, "--name", "app", "--root-dir", "src/app",
               "--doctor", "django-code-doctor")
    meta = read_meta(out)
    assert meta["findings_total"] == 1
    assert meta["duplicates_merged"] == 1
    assert meta["findings_by_severity"]["high"] == 1, "the stronger call survives"
    assert meta["findings_by_severity"]["medium"] == 0
    assert "merged" in out.read_text()


def test_the_same_finding_in_two_packages_is_not_merged(run_script, repo):
    """Basename-keyed dedup would collapse these; monorepos produce them constantly."""
    for package in ("a", "b"):
        repo.write(f"src/{package}/models.py", "\n".join(f"x{i}=1" for i in range(400)))
    repo.commit("init")
    report = repo.path / "f.json"
    report.write_text(json.dumps([
        {"file": "src/a/models.py", "line": 3, "smell_type": "fat_model", "severity": "high"},
        {"file": "src/b/models.py", "line": 3, "smell_type": "fat_model", "severity": "high"},
    ]))
    out = repo.path / "docs/health.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", report, "--repo", repo.path,
               "--name", "whole", "--doctor", "django-code-doctor")
    meta = read_meta(out)
    assert meta["findings_total"] == 2
    assert meta["duplicates_merged"] == 0


def test_the_grade_matches_the_score_that_is_published(run_script, sized_repo):
    """A published 93.0 beside an A- would contradict the documented bands."""
    for count in range(0, 40):
        meta = read_meta(build(run_script, sized_repo,
                               [finding(category="security", severity="low", line=n)
                                for n in range(count)]))
        for row in meta["categories"]:
            if row["score"] is not None:
                assert row["grade"] == grade_of(row["score"]), row
        if meta["score"] is not None:
            assert meta["grade"] == grade_of(meta["score"])


BANDS = ((97, "A+"), (93, "A"), (90, "A-"), (87, "B+"), (83, "B"), (80, "B-"),
         (77, "C+"), (73, "C"), (70, "C-"), (67, "D+"), (63, "D"), (60, "D-"))


def grade_of(score):
    for threshold, letter in BANDS:
        if score >= threshold:
            return letter
    return "F"


def test_the_root_keeps_repo_level_configuration_findings(run_script, repo):
    for module in range(4):
        repo.write(f"src/api/m{module}.py", "\n".join(f"x{i}=1" for i in range(300)))
    repo.write("tsconfig.json", "{}\n")
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [{"name": "api", "roots": ["src/api"], "docs": "src/api/docs",
                      "language": "python", "doctor": "python-code-doctor"}],
    }))
    repo.commit("init")
    report = repo.path / "f.json"
    report.write_text(json.dumps([
        finding(file="src/api/m0.py", category="security", severity="high"),
        finding(file="tsconfig.json", line=1, category="security", severity="high"),
        finding(file="legacy/old.py", line=1, category="security", severity="high"),
    ]))
    out = repo.path / "docs/health.html"
    run_script(BUILD_HEALTH, "--root", "--out", out, "--map", repo.path / "docs/code-overview.json",
               "--findings", report, "--repo", repo.path, "--name", "whole-repo",
               "--doctor", "python-code-doctor")
    files = {f["file"] for f in read_meta(out)["top_findings"]}
    assert "tsconfig.json" in files, "repo-level configuration belongs to the repo grade"
    assert "src/api/m0.py" in files
    assert "legacy/old.py" not in files, "an unmapped directory is still out of scope"


def test_a_zero_byte_report_grades_nothing(run_script, sized_repo, tmp_path):
    """What a doctor leaves behind when it fails after the shell made the file."""
    empty = tmp_path / "empty.json"
    empty.write_text("")
    out = sized_repo.path / "src/app/docs/health.html"
    result = run_script(BUILD_HEALTH, "--out", out, "--findings", empty,
                        "--repo", sized_repo.path, "--name", "app", "--root-dir", "src/app",
                        "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["score"] is None, "an empty artifact is not a clean run"
    assert meta["grade"] == "—"
    assert "empty" in result.stderr


def test_skipping_one_detector_ungrades_its_whole_category(run_script, sized_repo):
    """Correctness has many detectors; a partial measurement can only miss findings."""
    report = sized_repo.path / "report.json"
    report.write_text(json.dumps({
        "meta": {"analyzers_run": ["mutation_hazards", "security"],
                 "analyzers_skipped": ["exception_issues"]},
        "categories": {"mutation_hazards": {"issues": []}, "security": {"issues": []},
                       "exception_issues": {"issues": []}},
    }), encoding="utf-8")
    out = sized_repo.path / "src/app/docs/health.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", report, "--repo", sized_repo.path,
               "--name", "app", "--root-dir", "src/app", "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert "correctness" in meta["ungraded"], (
        "exception_issues is a correctness detector; skipping it leaves the category "
        "partly measured, and a partial measurement only ever misses findings"
    )
    assert "security" not in meta["ungraded"]


def test_two_findings_on_one_line_from_one_report_both_count(run_script, sized_repo):
    """Django's template detector emits one finding per link on a line."""
    report = sized_repo.path / "one.json"
    shared = {"file": "src/app/mod0.py", "line": 7, "smell_type": "hardcoded_url_in_template",
              "severity": "medium"}
    report.write_text(json.dumps([{**shared, "description": "link to /a"},
                                  {**shared, "description": "link to /b"}]))
    out = sized_repo.path / "src/app/docs/health.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", report, "--repo", sized_repo.path,
               "--name", "app", "--root-dir", "src/app", "--doctor", "django-code-doctor")
    meta = read_meta(out)
    assert meta["findings_total"] == 2, "deduplication is across reports, never within one"
    assert meta["duplicates_merged"] == 0


def test_cross_report_multiplicity_is_the_maximum_not_the_sum(run_script, sized_repo):
    shared = {"file": "src/app/mod0.py", "line": 7, "smell_type": "hardcoded_url_in_template"}
    a = sized_repo.path / "a.json"
    a.write_text(json.dumps([{**shared, "severity": "medium", "description": "one"},
                             {**shared, "severity": "medium", "description": "two"}]))
    b = sized_repo.path / "b.json"
    b.write_text(json.dumps([{**shared, "severity": "high", "description": "the same line"}]))
    out = sized_repo.path / "src/app/docs/health.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", a, "--findings", b,
               "--repo", sized_repo.path, "--name", "app", "--root-dir", "src/app",
               "--doctor", "django-code-doctor")
    meta = read_meta(out)
    assert meta["findings_total"] == 2, "two real defects, one of them also seen by the other doctor"
    assert meta["duplicates_merged"] == 1


def test_a_template_reading_doctor_sizes_templates_either_way(run_script, repo):
    """Django reads templates on every run, so its template lines are always the divisor.

    Sizing off findings alone made the denominator jump the moment a template
    finding appeared — which *raised* the grade. A rule under which discovering
    a bug improves the score is the wrong shape, so the doctor decides.
    """
    for module in range(3):
        repo.write(f"app/m{module}.py", "\n".join(f"x{i}=1" for i in range(200)))
    for page in range(4):
        repo.write(f"app/templates/p{page}.html", "\n".join(f"<p>{i}</p>" for i in range(500)))
    repo.commit("init")

    without = repo.path / "a.json"
    without.write_text(json.dumps([{"file": "app/m0.py", "line": 3, "smell_type": "fat_model",
                                    "severity": "high"}]))
    with_templates = repo.path / "b.json"
    with_templates.write_text(json.dumps([
        {"file": "app/m0.py", "line": 3, "smell_type": "fat_model", "severity": "high"},
        {"file": "app/templates/p0.html", "line": 2, "smell_type": "missing_csrf_token",
         "severity": "high"},
    ]))

    plain = repo.path / "plain.html"
    run_script(BUILD_HEALTH, "--out", plain, "--findings", without, "--repo", repo.path,
               "--name", "app", "--root-dir", "app", "--doctor", "django-code-doctor",
               "--covers", "correctness,security,tests,complexity,design,hygiene")
    templated = repo.path / "templated.html"
    run_script(BUILD_HEALTH, "--out", templated, "--findings", with_templates, "--repo", repo.path,
               "--name", "app", "--root-dir", "app", "--doctor", "django-code-doctor",
               "--covers", "correctness,security,tests,complexity,design,hygiene")

    assert ".html" in read_meta(plain)["sized_extensions"], (
        "clean templates were still analyzed, so they are still in the denominator"
    )
    assert read_meta(plain)["size"]["loc"] == read_meta(templated)["size"]["loc"], (
        "adding a template finding must not change what the grade is divided by"
    )
    assert read_meta(templated)["score"] < read_meta(plain)["score"], (
        "and with the divisor fixed, one more finding can only lower the grade"
    )


def test_a_code_only_doctor_leaves_stray_markup_out_of_the_divisor(run_script, repo):
    """A Python package that merely ships an HTML fixture is sized over its code."""
    for module in range(3):
        repo.write(f"app/m{module}.py", "\n".join(f"x{i}=1" for i in range(200)))
    repo.write("app/fixture.html", "\n".join(f"<p>{i}</p>" for i in range(2000)))
    repo.commit("init")
    out = repo.path / "h.html"
    report = repo.path / "f.json"
    report.write_text(json.dumps(full_report(
        [finding(file="app/m0.py", category="security", severity="high", line=3)])))
    run_script(BUILD_HEALTH, "--out", out, "--findings", report, "--repo", repo.path,
               "--name", "app", "--root-dir", "app", "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["sized_extensions"] == []
    assert meta["size"]["loc"] < 1000, "the 2000-line fixture is not code this doctor read"


def test_loc_and_files_overrides_are_independent(run_script, sized_repo):
    measured = read_meta(build(run_script, sized_repo, []))
    only_files = read_meta(build(run_script, sized_repo, [], "--files", "7"))
    assert only_files["size"]["files"] == 7
    assert only_files["size"]["loc"] == measured["size"]["loc"], "--files must not touch loc"

    only_loc = read_meta(build(run_script, sized_repo, [], "--loc", "5000"))
    assert only_loc["size"]["loc"] == 5000
    assert only_loc["size"]["files"] == measured["size"]["files"], "--loc must not zero files"


def test_category_scores_are_shown_at_grading_precision(run_script, sized_repo):
    """A score of 92.6 graded A- must not be rendered as '93'."""
    for count in range(0, 30):
        out = build(run_script, sized_repo,
                    [finding(category="naming_issues", severity="low", line=n)
                     for n in range(count)])
        text = out.read_text()
        for row in read_meta(out)["categories"]:
            if row["score"] is not None:
                assert f'>{row["score"]:.1f}<' in text, (
                    f'{row["key"]} stored {row["score"]} but the page does not show it'
                )


def test_a_package_with_no_health_page_stays_in_the_roll_up(run_script, repo):
    """The documented "codemap only" answer must not vanish from the table."""
    for module in range(3):
        repo.write(f"src/api/m{module}.py", "\n".join(f"x{i}=1" for i in range(300)))
        repo.write(f"src/svc/m{module}.go", "package main\n")
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [
            {"name": "api", "roots": ["src/api"], "docs": "src/api/docs",
             "language": "python", "doctor": "python-code-doctor"},
            {"name": "svc", "roots": ["src/svc"], "docs": "src/svc/docs",
             "language": "go", "doctor": ""},
        ],
    }))
    repo.commit("init")
    report = repo.path / "f.json"
    report.write_text(json.dumps([finding(file="src/api/m0.py", category="security")]))
    run_script(BUILD_HEALTH, "--out", repo.path / "src/api/docs/health.html",
               "--findings", report, "--repo", repo.path, "--name", "api",
               "--root-dir", "src/api", "--doctor", "python-code-doctor")

    out = repo.path / "docs/health.html"
    run_script(BUILD_HEALTH, "--root", "--out", out, "--map", repo.path / "docs/code-overview.json",
               "--findings", report, "--repo", repo.path, "--name", "whole-repo",
               "--doctor", "python-code-doctor")
    rows = {row["package"]: row for row in read_meta(out)["packages"]}
    assert set(rows) == {"api", "svc"}
    assert rows["svc"]["generated"] is False
    assert rows["svc"]["score"] is None
    assert "not generated" in out.read_text()


def test_a_bare_list_grades_nothing_without_covers(run_script, sized_repo):
    """`find_duplicates.py --format json` prints `[]` — the same shape as a full
    Django run, so a bare list cannot imply a doctor's whole profile."""
    report = sized_repo.path / "one.json"
    report.write_text("[]")
    out = sized_repo.path / "src/app/docs/health.html"
    result = run_script(BUILD_HEALTH, "--out", out, "--findings", report,
                        "--repo", sized_repo.path, "--name", "app", "--root-dir", "src/app",
                        "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["score"] is None, "one empty detector is not an A+ in seven categories"
    assert meta["grade"] == "—"
    assert "which analyzers ran" in result.stderr
    assert "--covers" in result.stderr


def test_a_django_only_run_is_graded_by_declaring_its_coverage(run_script, sized_repo):
    """The documented path for a bare list: say what it covered."""
    report = sized_repo.path / "dj.json"
    report.write_text(json.dumps([finding(smell_type="n_plus_one_query", severity="high")]))
    out = sized_repo.path / "src/app/docs/health.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", report, "--repo", sized_repo.path,
               "--name", "app", "--root-dir", "src/app", "--doctor", "django-code-doctor",
               "--covers", "correctness,security,tests,complexity,design,hygiene")
    meta = read_meta(out)
    assert meta["score"] is not None
    assert meta["ungraded"] == ["duplication"], "exactly django's blind spot, and nothing else"


def test_a_stale_root_is_reported_rather_than_graded(run_script, sized_repo):
    """Map drift: a root deleted between runs measures zero and would grade A+."""
    report = sized_repo.path / "f.json"
    report.write_text(json.dumps(full_report([])))
    out = sized_repo.path / "docs/health.html"
    result = run_script(BUILD_HEALTH, "--out", out, "--findings", report,
                        "--repo", sized_repo.path, "--name", "gone",
                        "--root-dir", "src/vanished", "--doctor", "python-code-doctor")
    assert "do not exist" in result.stderr
    assert read_meta(out)["missing_roots"] == ["src/vanished"]


def test_the_duplicate_count_describes_this_package_only(run_script, repo):
    """Dedup runs repo-wide; the count on a package page must not."""
    for package in ("alpha", "beta"):
        for module in range(3):
            repo.write(f"src/{package}/m{module}.py", "\n".join(f"x{i}=1" for i in range(300)))
    repo.commit("init")
    shared = {"line": 2, "smell_type": "hardcoded_secret", "severity": "high"}
    a = repo.path / "a.json"
    a.write_text(json.dumps([{**shared, "file": f"src/beta/m{n}.py"} for n in range(5)]))
    b = repo.path / "b.json"
    b.write_text(json.dumps([{**shared, "file": f"src/beta/m{n}.py"} for n in range(5)]))

    out = repo.path / "src/alpha/docs/health.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", a, "--findings", b,
               "--repo", repo.path, "--name", "alpha", "--root-dir", "src/alpha",
               "--doctor", "django-code-doctor", "--covers", "security")
    meta = read_meta(out)
    assert meta["findings_total"] == 0
    assert meta["duplicates_merged"] == 0, (
        "the duplicates were all in beta; alpha's page must not claim them"
    )
    assert "were merged" not in out.read_text()


def test_the_root_grade_excludes_packages_no_doctor_reviews(run_script, repo):
    """Go lines must not dilute Python findings they cannot contribute to."""
    for module in range(3):
        repo.write(f"src/api/m{module}.py", "\n".join(f"x{i}=1" for i in range(200)))
    for module in range(20):
        repo.write(f"src/svc/m{module}.go", "\n".join(f"var x{i} = {i}" for i in range(300)))
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [
            {"name": "api", "roots": ["src/api"], "docs": "src/api/docs",
             "language": "python", "doctor": "python-code-doctor"},
            {"name": "svc", "roots": ["src/svc"], "docs": "src/svc/docs",
             "language": "go", "doctor": ""},
        ],
    }))
    repo.commit("init")
    report = repo.path / "f.json"
    report.write_text(json.dumps(full_report(
        [finding(file="src/api/m0.py", category="security", severity="high", line=n)
         for n in range(5)])))
    out = repo.path / "docs/health.html"
    result = run_script(BUILD_HEALTH, "--root", "--out", out,
                        "--map", repo.path / "docs/code-overview.json", "--findings", report,
                        "--repo", repo.path, "--name", "whole-repo",
                        "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["roots"] == ["src/api"], "the Go package is not part of what was analyzed"
    assert meta["size"]["loc"] < 1000, "and its 6000 lines are not in the denominator"
    assert "no doctor" in result.stderr


def test_malformed_findings_fail_loudly(run_script, sized_repo, tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    result = run_script(BUILD_HEALTH, "--out", sized_repo.path / "h.html", "--findings", broken,
                        "--repo", sized_repo.path, "--name", "app", expect_rc=1)
    assert "not valid JSON" in result.stderr


def test_a_missing_findings_file_fails_cleanly(run_script, sized_repo, tmp_path):
    result = run_script(BUILD_HEALTH, "--out", sized_repo.path / "h.html",
                        "--findings", tmp_path / "nope.json", "--repo", sized_repo.path,
                        "--name", "app", expect_rc=1)
    assert "cannot read findings file" in result.stderr
    assert "Traceback" not in result.stderr


def test_a_crashed_companion_ungrades_everything(run_script, sized_repo, tmp_path):
    """A doctor that died leaves a zero-byte file beside a full, clean report.

    The Python report really did run; the point is that Django is what sees
    N+1 queries, missing CSRF and insecure settings, so the categories the
    Python report "covered" were half-measured — and nothing in either file
    says which half. Grading them produced an A+ built on a crash.
    """
    good = tmp_path / "py.json"
    good.write_text(json.dumps(full_report([])))
    crashed = tmp_path / "django.json"
    crashed.write_text("")
    out = sized_repo.path / "h.html"
    result = run_script(BUILD_HEALTH, "--out", out, "--findings", good, "--findings", crashed,
                        "--repo", sized_repo.path, "--name", "app", "--root-dir", "src/app",
                        "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["score"] is None, "a crashed companion must not leave an A+ standing"
    assert len(meta["ungraded"]) == 7, "every category, since the gap cannot be attributed"
    assert "a doctor failed" in result.stderr
    assert "--covers" in result.stderr


def test_naming_the_coverage_overrides_a_crashed_companion(run_script, sized_repo, tmp_path):
    """--covers is the documented way to say what the surviving reports examined."""
    good = tmp_path / "py.json"
    good.write_text(json.dumps(full_report([])))
    crashed = tmp_path / "django.json"
    crashed.write_text("")
    out = sized_repo.path / "h.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", good, "--findings", crashed,
               "--repo", sized_repo.path, "--name", "app", "--root-dir", "src/app",
               "--doctor", "python-code-doctor", "--covers", "correctness,security")
    meta = read_meta(out)
    assert meta["score"] is not None
    assert set(meta["ungraded"]) == {"tests", "complexity", "design", "duplication", "hygiene"}


def test_a_null_line_does_not_abort_the_page(run_script, sized_repo):
    """`--covers` invites tools this skill has never seen; their fields vary."""
    out = build(run_script, sized_repo,
                [finding(line=3, severity="high"), finding(line=None, severity="high")])
    meta = read_meta(out)
    assert meta["findings_total"] == 2
    assert {item["line"] for item in meta["top_findings"]} == {0, 3}


def test_the_root_grade_excludes_packages_whose_report_never_arrived(run_script, repo):
    """A doctor named in the map is an intention; a graded page is the evidence."""
    for module in range(2):
        repo.write(f"src/api/m{module}.py", "\n".join(f"x{i} = {i}" for i in range(300)))
    for module in range(10):
        repo.write(f"web/c{module}.ts", "\n".join(f"const b{i} = {i};" for i in range(600)))
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [
            {"name": "api", "roots": ["src/api"], "docs": "src/api/docs",
             "language": "python", "doctor": "python-code-doctor"},
            {"name": "web", "roots": ["web"], "docs": "web/docs",
             "language": "typescript", "doctor": "typescript-code-doctor"},
        ],
    }))
    repo.commit("init")
    report = repo.path / "f.json"
    report.write_text(json.dumps(full_report(
        [finding(file="src/api/m0.py", category="security", severity="high", line=3)])))
    # Only the Python package's health page is ever built.
    run_script(BUILD_HEALTH, "--out", repo.path / "src/api/docs/health.html",
               "--findings", report, "--repo", repo.path, "--name", "api",
               "--root-dir", "src/api", "--doctor", "python-code-doctor")
    out = repo.path / "docs/health.html"
    result = run_script(BUILD_HEALTH, "--root", "--out", out,
                        "--map", repo.path / "docs/code-overview.json", "--findings", report,
                        "--repo", repo.path, "--name", "whole", "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["roots"] == ["src/api"], "web was never analyzed, so it is not measured"
    assert meta["size"]["loc"] < 1000, "6000 unexamined TypeScript lines stay out"
    assert "no graded health page" in result.stderr


def test_whole_project_findings_survive_root_scoping(run_script, repo):
    """Findings with no single file to blame are reported against the directory.

    `find_untested_modules.py` and `find_dependency_issues.py` emit
    `no_tests_in_repo` and `no_dependency_manifest` with `file=str(root)`.
    Requiring a *file* dropped both — high severity, and about the whole tree —
    so a repo with no tests and no manifest rolled up to a clean A+.
    """
    for module in range(3):
        repo.write(f"src/api/m{module}.py", "\n".join(f"x{i} = {i}" for i in range(300)))
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [{"name": "api", "roots": ["src/api"], "docs": "src/api/docs",
                      "language": "python", "doctor": "python-code-doctor"}],
    }))
    repo.commit("init")
    report = repo.path / "f.json"
    report.write_text(json.dumps(full_report([
        {"file": str(repo.path), "line": 1, "severity": "high",
         "smell_type": "no_tests_in_repo", "description": "no tests"},
        {"file": str(repo.path), "line": 1, "severity": "high",
         "smell_type": "no_dependency_manifest", "description": "no manifest"},
    ])))
    out = repo.path / "docs/health.html"
    run_script(BUILD_HEALTH, "--root", "--out", out,
               "--map", repo.path / "docs/code-overview.json", "--findings", report,
               "--repo", repo.path, "--name", "whole", "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["findings_total"] == 2, "both whole-project findings are in scope"
    assert meta["findings_out_of_scope"] == 0
    assert meta["grade"] != "A+", "a repo with no tests and no manifest is not flawless"


def test_a_companions_crashed_detector_does_not_ungrade_this_doctor(run_script, sized_repo,
                                                                    tmp_path):
    """A TypeScript failure must not ungrade a Python package's Security."""
    python = tmp_path / "py.json"
    python.write_text(json.dumps(full_report([])))
    typescript = tmp_path / "ts.json"
    typescript.write_text(json.dumps({
        "meta": {"analyzers_run": ["tsconfig", "complexity"],
                 "analyzer_errors": {"tsconfig": "crashed"}, "analyzers_skipped": []},
        "categories": {"tsconfig": {"issues": []}, "complexity": {"issues": []}},
    }))
    out = sized_repo.path / "h.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", python, "--findings", typescript,
               "--repo", sized_repo.path, "--name", "app", "--root-dir", "src/app",
               "--doctor", "python-code-doctor")
    assert read_meta(out)["ungraded"] == [], (
        "the Python security detector completed; another language's crash is not its gap"
    )


def test_a_skipped_detector_still_ungrades_within_its_own_report(run_script, sized_repo,
                                                                 tmp_path):
    """Per-report resolution must not weaken the same-report subtraction."""
    report = tmp_path / "py.json"
    report.write_text(json.dumps({
        "meta": {"analyzers_run": [a for a in FULL_RUN if a != "security"],
                 "analyzer_errors": {}, "analyzers_skipped": ["security"]},
        "categories": {"findings": {"issues": []}},
    }))
    out = sized_repo.path / "h.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", report, "--repo", sized_repo.path,
               "--name", "app", "--root-dir", "src/app", "--doctor", "python-code-doctor")
    assert "security" in read_meta(out)["ungraded"]


def test_an_empty_tree_is_ungraded_rather_than_flawless(run_script, repo):
    """The LOC floor turns nothing into 1000 lines, and a clean report into an A+."""
    repo.write("src/app/thing.py", "x = 1\n")
    repo.commit("init")
    report = repo.path / "f.json"
    report.write_text(json.dumps(full_report([])))
    out = repo.path / "h.html"
    result = run_script(BUILD_HEALTH, "--out", out, "--findings", report,
                        "--repo", repo.path, "--name", "gone", "--root-dir", "src/vanished",
                        "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["score"] is None, "a package that is not there cannot be graded"
    assert meta["missing_roots"] == ["src/vanished"]
    assert "no longer describe the same code" in result.stderr


def test_an_ungraded_page_can_be_built_with_no_findings_at_all(run_script, sized_repo):
    """The documented answer for Go or Rust: a health page with nothing graded.

    There is no findings artifact for a language no doctor covers, and the
    skill says not to invent one — so requiring --findings made the documented
    option impossible to follow.
    """
    out = sized_repo.path / "h.html"
    result = run_script(BUILD_HEALTH, "--out", out, "--repo", sized_repo.path,
                        "--name", "svc", "--root-dir", "src/app", "--language", "go")
    meta = read_meta(out)
    assert meta["score"] is None
    assert len(meta["ungraded"]) == 7
    assert meta["findings_total"] == 0
    assert "no findings were supplied" in result.stderr


def test_a_foreign_doctors_report_grants_no_coverage(run_script, sized_repo, tmp_path):
    """A successful foreign report is as wrong a source of coverage as a failed one.

    With only a TypeScript report present, a Python package graded Security 100
    off `tsconfig`. The doctor-profile cap cannot catch this — both doctors
    cover the same rubric categories — so the report has to say who wrote it.
    """
    typescript = tmp_path / "ts.json"
    typescript.write_text(json.dumps({
        "meta": {"analyzers_run": ["tsconfig", "complexity"],
                 "analyzer_errors": {}, "analyzers_skipped": []},
        "categories": {"tsconfig": {"issues": []}, "complexity": {"issues": []}},
    }))
    out = sized_repo.path / "h.html"
    result = run_script(BUILD_HEALTH, "--out", out,
                        "--findings", f"typescript-code-doctor:{typescript}",
                        "--repo", sized_repo.path, "--name", "app", "--root-dir", "src/app",
                        "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["score"] is None, "no Python analyzer examined this package"
    assert len(meta["ungraded"]) == 7
    assert "different doctor" in result.stderr


def test_an_unlabelled_report_is_still_attributed_to_the_named_doctor(run_script, sized_repo):
    """The ordinary single-doctor run must not need the label."""
    meta = read_meta(build(run_script, sized_repo, []))
    assert meta["score"] == 100.0


def test_a_doctor_label_does_not_swallow_a_path(run_script, sized_repo, tmp_path):
    """Only a prefix naming a known doctor is a label; a path stays a path."""
    weird = tmp_path / "notadoctor.json"
    weird.write_text(json.dumps(full_report([])))
    out = sized_repo.path / "h.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", weird, "--repo", sized_repo.path,
               "--name", "app", "--root-dir", "src/app", "--doctor", "python-code-doctor")
    assert read_meta(out)["score"] == 100.0


def test_the_django_merge_sizes_templates_from_the_companion_report(run_script, repo):
    """--doctor caps coverage; it does not say which doctors contributed.

    The recommended merge names python-code-doctor while a Django report sits
    beside it carrying the templates that belong in the denominator.
    """
    repo.write("app/views.py", "\n".join(f"v{i}=1" for i in range(400)))
    for page in range(8):
        repo.write(f"app/templates/t{page}.html", "\n".join(f"<p>{i}</p>" for i in range(400)))
    repo.commit("init")
    python = repo.path / "py.json"
    python.write_text(json.dumps(full_report(
        [finding(file="app/views.py", category="security", severity="high", line=3)])))
    django = repo.path / "dj.json"
    django.write_text(json.dumps([]))          # a clean Django run is a bare list
    out = repo.path / "h.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", python,
               "--findings", f"django-code-doctor:{django}", "--repo", repo.path,
               "--name", "app", "--root-dir", "app", "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert ".html" in meta["sized_extensions"]
    assert meta["size"]["loc"] > 3000, "the analyzed templates are in the divisor"


def test_any_missing_root_ungrades_a_multi_root_package(run_script, repo):
    """One surviving root still measures files, so a zero-files test missed this."""
    for module in range(4):
        repo.write(f"src/app/m{module}.py", "\n".join(f"x{i} = {i}" for i in range(300)))
    repo.commit("init")
    report = repo.path / "f.json"
    report.write_text(json.dumps(full_report([])))
    out = repo.path / "h.html"
    run_script(BUILD_HEALTH, "--out", out, "--findings", report, "--repo", repo.path,
               "--name", "app", "--root-dir", "src/app", "--root-dir", "shared/types",
               "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["missing_roots"] == ["shared/types"]
    assert meta["score"] is None, (
        "half the package was sized while all of it was searched for findings"
    )


def test_unassigned_root_level_source_is_out_of_the_root_grade(run_script, repo):
    """Its lines are excluded from the denominator, so its findings must go too.

    The repo-wide exception is for configuration that belongs to no package —
    not for source the user deliberately left out of the map.
    """
    for module in range(4):
        repo.write(f"src/app/m{module}.py", "\n".join(f"x{i} = {i}" for i in range(300)))
    repo.write("loose.py", "\n".join(f"y{i} = {i}" for i in range(200)))
    repo.write("tsconfig.json", "{}\n")
    repo.write("docs/code-overview.json", json.dumps({
        "schema": "code-overview/1",
        "packages": [{"name": "app", "roots": ["src/app"], "docs": "src/app/docs",
                      "language": "python", "doctor": "python-code-doctor"}],
    }))
    repo.commit("init")
    report = repo.path / "f.json"
    report.write_text(json.dumps(full_report([
        finding(file="loose.py", category="security", severity="high", line=3),
        finding(file="tsconfig.json", category="security", severity="high", line=1),
    ])))
    out = repo.path / "docs/health.html"
    run_script(BUILD_HEALTH, "--root", "--out", out,
               "--map", repo.path / "docs/code-overview.json", "--findings", report,
               "--repo", repo.path, "--name", "whole", "--doctor", "python-code-doctor")
    meta = read_meta(out)
    assert meta["roots"] == ["src/app"]
    assert meta["findings_total"] == 1, "the config finding stays, the loose source goes"
    assert meta["top_findings"][0]["file"] == "tsconfig.json"
