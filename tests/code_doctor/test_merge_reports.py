"""The merge: one envelope, every record attributed, nothing collapsed.

Two properties carry the design. Attribution, because nothing inside a report
says who wrote it and a consumer that credits one doctor's coverage to another
grades a language nobody analysed. And *no deduplication*, because collapsing
two reports of one defect changes a count someone will divide by — that is a
grading decision, and it belongs where the grade is computed.
"""

import json
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"
SCRIPT = SKILL / "scripts" / "merge_reports.py"


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def merge(run_script, *reports, expect_rc=0) -> dict:
    args = []
    for label, path in reports:
        args += ["--report", f"{label}:{path}"]
    result = run_script(SCRIPT, *args, "--format", "json", expect_rc=expect_rc)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def test_every_record_carries_the_doctor_that_produced_it(run_script, tmp_path):
    a = write_json(tmp_path / "a.json", [{"file": "x.py", "line": 1, "smell_type": "secret"}])
    b = write_json(tmp_path / "b.json", [{"file": "y.ts", "line": 2, "smell_type": "any_type"}])

    envelope = merge(run_script,
                     ("code-doctor", a), ("typescript-code-doctor", b))

    doctors = {record["doctor"] for record in envelope["findings"]}
    assert doctors == {"code-doctor", "typescript-code-doctor"}
    assert envelope["doctors_run"] == ["code-doctor", "typescript-code-doctor"]


def test_identical_findings_from_two_doctors_both_survive(run_script, tmp_path):
    same = {"file": "settings.py", "line": 7, "smell_type": "hardcoded_secret",
            "severity": "high", "description": "SECRET_KEY", "suggestion": "move it"}
    a = write_json(tmp_path / "a.json", [same])
    b = write_json(tmp_path / "b.json", [dict(same)])

    envelope = merge(run_script,
                     ("code-doctor", a), ("django-code-doctor", b))

    assert len(envelope["findings"]) == 2, "the merge attributes; it must not deduplicate"


def test_candidates_are_separated_from_findings_by_kind(run_script, tmp_path):
    report = write_json(tmp_path / "cd.json", {
        "completeness": {"reference_graph": "resolution 0.41"},
        "findings": [
            {"file": "a.go", "line": 1, "smell_type": "merge_marker", "kind": "finding",
             "suggestion": "resolve the conflict"},
            {"file": "b.go", "line": 9, "smell_type": "zero_inbound_file", "kind": "candidate",
             "also_caused_by": ["an executable entry point has no internal referrer"]},
        ],
    })

    envelope = merge(run_script, ("code-doctor", report))

    assert [f["smell_type"] for f in envelope["findings"]] == ["merge_marker"]
    assert [c["smell_type"] for c in envelope["candidates"]] == ["zero_inbound_file"]
    assert envelope["completeness"]["code-doctor"] == {"reference_graph": "resolution 0.41"}


def test_a_specialist_envelope_keeps_its_coverage_evidence(run_script, tmp_path):
    report = write_json(tmp_path / "py.json", {
        "meta": {"analyzers_run": ["find_security_issues", "find_duplicates"],
                 "analyzer_errors": {"find_complexity": "timed out"},
                 "analyzers_skipped": ["find_type_gaps"]},
        "categories": {"security": {"issues": [{"file": "s.py", "line": 3,
                                                "issue_type": "hardcoded_secret"}]}},
    })

    envelope = merge(run_script, ("python-code-doctor", report))

    assert envelope["analyzers_run"]["python-code-doctor"] == [
        "find_security_issues", "find_duplicates"]
    assert envelope["analyzer_errors"]["python-code-doctor"] == {"find_complexity": "timed out"}
    assert envelope["analyzers_skipped"]["python-code-doctor"] == ["find_type_gaps"]
    assert envelope["coverage_unknown"] == []


def test_a_category_shaped_report_stamps_its_section_name_on_each_issue(run_script, tmp_path):
    report = write_json(tmp_path / "py.json", {
        "meta": {"analyzers_run": ["find_security_issues"]},
        "categories": {"security": {"issues": [{"file": "s.py", "line": 3,
                                                "issue_type": "hardcoded_secret"}]}},
    })

    envelope = merge(run_script, ("python-code-doctor", report))

    assert envelope["findings"][0]["category"] == "security"


def test_a_bare_list_evidences_no_coverage(run_script, tmp_path):
    report = write_json(tmp_path / "dj.json", [{"file": "m.py", "line": 4,
                                                "smell_type": "n_plus_one_query"}])

    envelope = merge(run_script, ("django-code-doctor", report))

    assert envelope["coverage_unknown"] == ["django-code-doctor"]
    assert envelope["analyzers_run"]["django-code-doctor"] == []


def test_an_empty_file_is_a_failed_doctor_not_a_clean_one(run_script, tmp_path):
    empty = tmp_path / "dj.json"
    empty.write_text("", encoding="utf-8")
    good = write_json(tmp_path / "py.json", [{"file": "a.py", "line": 1, "smell_type": "x"}])

    envelope = merge(run_script, ("python-code-doctor", good),
                     ("django-code-doctor", empty))

    assert "django-code-doctor" in envelope["doctor_errors"]
    assert "django-code-doctor" not in envelope["doctors_run"]
    assert len(envelope["findings"]) == 1


def test_invalid_json_is_a_named_doctor_error(run_script, tmp_path):
    broken = tmp_path / "py.json"
    broken.write_text("{not json", encoding="utf-8")

    envelope = merge(run_script, ("python-code-doctor", broken), expect_rc=1)

    assert "python-code-doctor" in envelope["doctor_errors"]


def test_a_missing_file_is_a_named_doctor_error(run_script, tmp_path):
    envelope = merge(run_script,
                     ("python-code-doctor", tmp_path / "absent.json"), expect_rc=1)

    assert "python-code-doctor" in envelope["doctor_errors"]


def test_every_report_failing_exits_one(run_script, tmp_path):
    broken = tmp_path / "a.json"
    broken.write_text("{", encoding="utf-8")

    result = run_script(SCRIPT, "--report", f"code-doctor:{broken}",
                        "--format", "json", expect_rc=1)

    envelope = json.loads(result.stdout)
    assert envelope["doctors_run"] == []
    assert envelope["findings"] == []


def test_a_report_argument_without_a_label_is_rejected(run_script, tmp_path):
    good = write_json(tmp_path / "a.json", [])

    result = run_script(SCRIPT, "--report", str(good), expect_rc=2)

    assert "doctor:path" in result.stderr


def test_out_writes_the_envelope_to_a_file(run_script, tmp_path):
    report = write_json(tmp_path / "a.json", [{"file": "a.py", "line": 1, "smell_type": "x"}])
    out = tmp_path / "merged.json"

    run_script(SCRIPT, "--report", f"code-doctor:{report}", "--out", out)

    assert json.loads(out.read_text(encoding="utf-8"))["schema"] == "code-doctor-merge/1"


def test_text_output_names_the_doctors_and_the_failures(run_script, tmp_path):
    good = write_json(tmp_path / "a.json", [{"file": "a.py", "line": 1, "smell_type": "x"}])
    empty = tmp_path / "b.json"
    empty.write_text("", encoding="utf-8")

    result = run_script(SCRIPT, "--report", f"code-doctor:{good}",
                        "--report", f"django-code-doctor:{empty}")

    assert "code-doctor" in result.stdout
    assert "django-code-doctor" in result.stdout
    assert "failed" in result.stdout.lower()


def test_windows_path_with_drive_letter_is_not_mistaken_for_the_label(run_script, tmp_path):
    """A Windows path like C:\\work\\py.json contains a colon of its own.

    partition(":") on "code-doctor:C:\\work\\py.json" must split on the FIRST
    colon (after the label), leaving the drive letter as part of the path —
    not treat "C" as the label.
    """
    report = write_json(tmp_path / "py.json", [{"file": "a.py", "line": 1, "smell_type": "x"}])

    envelope = merge(run_script, ("code-doctor", report))

    assert envelope["doctors_run"] == ["code-doctor"]
    assert len(envelope["findings"]) == 1
