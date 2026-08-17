"""Every report shape `--findings` can be handed, read as evidence.

The failure these tests exist for was silent. `normalize_findings` knew a bare
list, a `categories` envelope and a single detector's `{"issues": [...]}` — but
not `{"completeness": {...}, "findings": [...]}`, which is what every
code-doctor detector emits and what `merge_reports.py` already read. A real
code-doctor report handed to `--findings code-doctor:raw.json` therefore parsed
fine, matched no branch, and came back with nothing in it: `findings_total: 0`,
**A+ / 100.0**, no warning anywhere on the page, and a Candidates tab reporting
that no candidates were found.

A zero-byte file is loud. An unreadable *shape* was not, and it is the same
amount of evidence: none.

The report under test is produced by running code-doctor for real rather than
hand-built, because a hand-built fixture is written to match whichever side of
the seam its author was looking at — which is exactly how this hole survived
tests on both sides of it.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD_HEALTH = ROOT / "skills" / "code-overview" / "scripts" / "build_health.py"
SCRIPTS = BUILD_HEALTH.parent
ANALYZE_ALL = ROOT / "skills" / "code-doctor" / "scripts" / "analyze_all.py"
PYTHON_ANALYZE_ALL = ROOT / "skills" / "python-code-doctor" / "scripts" / "analyze_all.py"

KEY_BODY = "MIIEowIBAAKCAQEAvQ8Z1kQyBmT3xN0pLrWc7HgYdFvJqR2sKtBnM4eXhAoGBAJ9x\n" * 3


@pytest.fixture
def doctor_report(repo, run_script, tmp_path) -> Path:
    """A genuine code-doctor report over a repo with one defect and one lead."""
    for i in range(12):
        repo.write(f"src/app/mod{i}.py", "def f():\n    return 1\n" * 20)
    repo.write("src/app/deploy.sh",
               f"-----BEGIN RSA PRIVATE KEY-----\n{KEY_BODY}-----END RSA PRIVATE KEY-----\n")
    repo.write("src/app/legacy.py", "def g():\n    return 2\n# h = compute(1)\n")
    repo.commit()

    raw = tmp_path / "raw.json"
    raw.write_text(run_script(ANALYZE_ALL, repo.path, "--format", "json").stdout,
                   encoding="utf-8")
    return raw


def meta_of(page: Path) -> dict:
    match = re.search(r'id="code-health-meta">(.*?)</script>',
                      page.read_text(encoding="utf-8"), re.S)
    assert match
    return json.loads(match.group(1).replace("<\\/", "</"))


def test_a_real_code_doctor_report_is_read_rather_than_graded_from_silence(
        repo, run_script, tmp_path, doctor_report):
    payload = json.loads(doctor_report.read_text(encoding="utf-8"))
    assert payload["findings"], "the fixture must actually contain records to lose"

    # The reported repro, with coverage declared by flag so a grade is computed
    # rather than withheld. Security is named alongside Correctness because
    # that is the category the committed key lands in — with Correctness alone
    # the graded bucket is legitimately empty, and 100 would be honest.
    out = tmp_path / "health.html"
    run_script(BUILD_HEALTH, "--out", out, "--repo", repo.path, "--name", "app",
               "--root-dir", "src/app", "--findings", f"code-doctor:{doctor_report}",
               "--covers", "correctness,security")
    meta = meta_of(out)

    assert meta["findings_total"] > 0, (
        "the whole failure: a report whose shape nothing matched returned zero findings, "
        "and zero findings from a file nobody could read is an A+ from silence"
    )
    assert meta["score"] < 100.0
    assert meta["grade"] != "A+"


def test_the_report_grants_its_own_coverage_and_no_more(
        repo, run_script, tmp_path, doctor_report):
    """Without `--covers`, coverage comes from the report's own inventory.

    `categories_run` names the detectors that ran and completed; `rubric`
    resolves those tokens to rubric categories, and
    `DOCTOR_COVERAGE["code-doctor"]` caps what the raw layer may claim. Both
    halves were unreachable before — the inventory never survived
    `normalize_findings`, and code-doctor's tokens resolved to nothing.
    """
    out = tmp_path / "health.html"
    run_script(BUILD_HEALTH, "--out", out, "--repo", repo.path, "--name", "app",
               "--root-dir", "src/app", "--doctor", "code-doctor",
               "--findings", f"code-doctor:{doctor_report}")
    meta = meta_of(out)

    assert "correctness" in meta["ungraded"], (
        "a merge marker is the only correctness-class defect the raw layer can prove"
    )
    assert "security" not in meta["ungraded"], "the secrets detector ran and completed"
    assert "hygiene" not in meta["ungraded"], "so did the hygiene detector"


def test_candidates_are_kept_out_of_the_findings(load_module, doctor_report):
    """code-doctor emits both kinds in one `findings` array, split only by `kind`.

    Reading the array wholesale would put unverified leads into the score — the
    one thing this skill promises never to do — so the split has to happen at
    the door.
    """
    common = load_module(SCRIPTS, "common")
    payload = json.loads(doctor_report.read_text(encoding="utf-8"))

    report = common.normalize_findings(payload)

    assert report["candidates"], "the fixture contains a commented-out-code lead"
    assert all(record.get("kind") != "candidate" for record in report["findings"])
    kinds = {record.get("kind") for record in report["findings"]}
    assert kinds == {"finding"}


@pytest.fixture
def python_doctor_report(repo, run_script, tmp_path) -> Path:
    """A genuine python-code-doctor report over a repo with one lead in it.

    A dynamic-table PRAGMA is the lead: PRAGMA accepts no bound parameters, so
    interpolation is the only way to write one, and the detector says so by
    emitting `kind: candidate` rather than asserting injection.
    """
    repo.write("src/app/db.py",
               "import sqlite3\n\n\n"
               "def describe(conn, table):\n"
               '    return conn.execute(f"PRAGMA table_info({table})").fetchall()\n')
    repo.commit()

    raw = tmp_path / "python-raw.json"
    raw.write_text(run_script(PYTHON_ANALYZE_ALL, repo.path, "--format", "json",
                              "--skip-duplicates").stdout,
                   encoding="utf-8")
    return raw


def test_a_candidate_inside_a_categories_envelope_is_kept_out_of_the_findings(
        load_module, python_doctor_report):
    """The `categories` shape splits on `kind` too — it is the same promise.

    Only the bare list and code-doctor's own `findings` array were split, and a
    language specialist reports neither: `analyze_all.py` emits a `categories`
    envelope. So every candidate python-code-doctor raised — a PRAGMA that can
    only be written by interpolation, a public function no single file can
    prove dead — was read as an asserted defect and charged to the grade, which
    penalises code for being hard to analyse rather than for being wrong.
    """
    common = load_module(SCRIPTS, "common")
    payload = json.loads(python_doctor_report.read_text(encoding="utf-8"))
    assert payload["categories"]["security"]["issues"], "the fixture must contain the lead"

    report = common.normalize_findings(payload)

    raised = {(record["category"], record.get("smell_type") or record.get("issue_type"))
              for record in report["candidates"]}
    assert ("security", "sql_injection") in raised, "the PRAGMA lead"
    assert ("dead_code", "unused_function") in raised, (
        "one file cannot prove a public function dead — another module may import it"
    )
    assert all(record.get("kind") != "candidate" for record in report["findings"])


def test_a_candidate_inside_a_bare_detector_envelope_is_kept_out_of_the_findings(load_module):
    """`{"issues": [...]}` is one detector's report, and it splits too."""
    common = load_module(SCRIPTS, "common")

    report = common.normalize_findings({"issues": [
        {"file": "a.py", "line": 1, "smell_type": "hardcoded_secret", "severity": "high"},
        {"file": "b.py", "line": 2, "smell_type": "sql_injection", "severity": "high",
         "kind": "candidate"},
    ]})

    assert [record["smell_type"] for record in report["findings"]] == ["hardcoded_secret"]
    assert [record["smell_type"] for record in report["candidates"]] == ["sql_injection"]


def test_the_completeness_block_is_coverage_evidence(load_module, doctor_report):
    common = load_module(SCRIPTS, "common")

    report = common.normalize_findings(json.loads(doctor_report.read_text(encoding="utf-8")))

    assert report["shape"] == common.SHAPE_FULL, (
        "`categories_run` says what looked; that is what SHAPE_FULL means"
    )
    assert report["ran"] == {"hygiene", "secrets"}
    assert report["completeness"]["categories_run"] == ["hygiene", "secrets"]


def test_a_crashed_category_is_not_credited_as_run(load_module):
    """`categories_run` means ran *and completed*, and `categories_failed` says
    which run lost what. A category listed in both would be graded from a zero
    its detector never produced."""
    common = load_module(SCRIPTS, "common")

    report = common.normalize_findings({
        "completeness": {"categories_run": ["secrets"],
                         "categories_failed": {"hygiene": "find_hygiene_issues.py: exited 3"}},
        "findings": [],
    })

    assert report["ran"] == {"secrets"}
    assert report["errors"] == {"hygiene": "find_hygiene_issues.py: exited 3"}


def test_an_unrecognised_shape_warns_instead_of_returning_empty_quietly(load_module, capsys):
    common = load_module(SCRIPTS, "common")

    report = common.normalize_findings({"schema": "code-doctor-merge/1", "records": []},
                                       source="raw.json")

    assert report["findings"] == []
    warning = capsys.readouterr().err
    assert "raw.json" in warning
    assert "no known report shape" in warning


# --------------------------------------------------------------------------
# the merged envelope: what it drops, and what it knows but never said
# --------------------------------------------------------------------------

def merged_envelope(tmp_path, **overrides) -> Path:
    payload = {
        "schema": "code-doctor-merge/1",
        "doctors_run": ["code-doctor"],
        "analyzers_run": {"code-doctor": ["hygiene", "secrets"]},
        "analyzers_skipped": {"code-doctor": []},
        "analyzer_errors": {},
        "doctor_errors": {},
        "completeness": {},
        "coverage_unknown": [],
        "findings": [],
        "candidates": [],
    }
    payload.update(overrides)
    path = tmp_path / "merged.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_finding_naming_an_unlisted_doctor_is_counted_and_warned(
        load_module, tmp_path, capsys):
    """Dropping it silently shrank the numerator against an unchanged denominator.

    The finding vanished from the grade while the lines it points at stayed in
    the divisor, so a mis-attributed record made the code score *better*. That
    is the one direction a grader must never be wrong in by accident.
    """
    common = load_module(SCRIPTS, "common")
    path = merged_envelope(tmp_path, findings=[
        {"doctor": "code-doctor", "file": "a.py", "line": 1, "smell_type": "todo_inventory"},
        {"doctor": "ghost-doctor", "file": "b.py", "line": 2, "smell_type": "hardcoded_secret"},
    ])

    merged = common.load_merged(path)

    graded = [f for report in merged["reports"] for f in report["findings"]]
    assert len(graded) == 1
    warning = capsys.readouterr().err
    assert "1 finding(s)" in warning
    assert "ghost-doctor" in warning
    assert "denominator" in warning, "say which way the grade is wrong, not just that it is"


def test_a_doctor_with_no_coverage_evidence_gets_a_caveat(load_module, tmp_path, capsys):
    """`coverage_unknown` used to be returned by `load_merged` and read by nobody.

    It is consumed here instead of carried: the doctor's categories come back
    ungraded either way, but a reader was given no reason why. Returning the
    list as well would just re-create the unread field.
    """
    common = load_module(SCRIPTS, "common")
    path = merged_envelope(tmp_path,
                           doctors_run=["django-code-doctor"],
                           analyzers_run={"django-code-doctor": []},
                           analyzers_skipped={"django-code-doctor": []},
                           coverage_unknown=["django-code-doctor"])

    merged = common.load_merged(path)

    assert "coverage_unknown" not in merged, "a returned field nobody reads is not a consumer"
    warning = capsys.readouterr().err
    assert "django-code-doctor" in warning
    assert "ungraded" in warning
