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
    """The tokens here are python-code-doctor's real `analyzers_run` names.

    They are the keys of its `ANALYZERS` table — `security`, `duplicates`,
    `complexity`, `type_gaps` — not the script filenames. The filenames this
    fixture used to carry (`find_security_issues`, `find_duplicates`) are
    tokens no doctor emits and none that `rubric.DETECTOR_CATEGORIES` resolves,
    so the fixture agreed with nothing on either side of the seam.
    """
    report = write_json(tmp_path / "py.json", {
        "meta": {"analyzers_run": ["security", "duplicates"],
                 "analyzer_errors": {"complexity": "timed out"},
                 "analyzers_skipped": ["type_gaps"]},
        "categories": {"security": {"issues": [{"file": "s.py", "line": 3,
                                                "issue_type": "hardcoded_secret"}]}},
    })

    envelope = merge(run_script, ("python-code-doctor", report))

    assert envelope["analyzers_run"]["python-code-doctor"] == ["security", "duplicates"]
    assert envelope["analyzer_errors"]["python-code-doctor"] == {"complexity": "timed out"}
    assert envelope["analyzers_skipped"]["python-code-doctor"] == ["type_gaps"]
    assert envelope["coverage_unknown"] == []


def test_a_category_shaped_report_stamps_its_section_name_on_each_issue(run_script, tmp_path):
    report = write_json(tmp_path / "py.json", {
        "meta": {"analyzers_run": ["security"]},
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


def test_windows_path_with_drive_letter_is_not_mistaken_for_the_label(load_module):
    """A Windows path like C:\\work\\py.json contains a colon of its own.

    partition(":") on "code-doctor:C:\\work\\py.json" must split on the FIRST
    colon (after the label), leaving the drive letter as part of the path —
    not treat "C" as the label.

    The drive letter is hard-coded rather than taken from `tmp_path`. It only
    happened to be exercised at all because this suite's `tmp_path` starts
    `C:\\` on the machine it was written on; on POSIX CI the argument had no
    colon in it and the test asserted nothing about drive letters. The parser
    is pure string handling, so it is tested directly and the path never has
    to exist.
    """
    module = load_module(SCRIPT.parent, "merge_reports")

    doctor, path = module._parse_report_argument(r"code-doctor:C:\work\py.json")

    assert doctor == "code-doctor"
    assert str(path) == r"C:\work\py.json"


# --------------------------------------------------------------------------
# the seam: analyze_all.py's real output, through merge_reports.py, to the
# rubric that has to resolve its tokens
# --------------------------------------------------------------------------

ANALYZE_ALL = SKILL / "scripts" / "analyze_all.py"
RUBRIC_DIR = (Path(__file__).resolve().parent.parent.parent / "skills" / "code-overview"
              / "scripts")


def test_analyze_alls_own_output_resolves_to_coverage(run_script, repo, tmp_path, load_module):
    """The producer's real bytes, fed to the consumer, must grant coverage.

    Every part of this seam was tested before — with hand-built fixtures on
    both sides — and every part passed while the seam itself was broken end to
    end: analyze_all emitted `categories_run` as a comma-joined string,
    merge_reports required a list, so a real code-doctor report always landed
    in `coverage_unknown` and granted a grader nothing. Nothing hand-built can
    catch that, because a hand-built fixture is written to match whichever side
    the author was looking at.

    The last hop is the rubric's: a token that survives the merge but resolves
    to no rubric category is coverage that still buys nothing, which is how
    `DOCTOR_COVERAGE["code-doctor"]` stayed unreachable.
    """
    repo.write("app.go", "package main\n")
    repo.commit()
    raw = tmp_path / "raw.json"
    raw.write_text(run_script(ANALYZE_ALL, repo.path, "--format", "json").stdout,
                   encoding="utf-8")

    envelope = merge(run_script, ("code-doctor", raw))

    assert envelope["coverage_unknown"] == [], (
        "a real code-doctor report says what ran; it is not a bare list"
    )
    assert envelope["analyzers_run"]["code-doctor"] == ["hygiene", "secrets"]

    rubric = load_module(RUBRIC_DIR, "rubric")
    resolved = {rubric.DETECTOR_CATEGORIES[token]
                for token in envelope["analyzers_run"]["code-doctor"]
                if token in rubric.DETECTOR_CATEGORIES}
    assert resolved == {"hygiene", "security"}, (
        "every token code-doctor emits must resolve to a rubric category, or the coverage "
        "it just proved buys nothing"
    )
    assert resolved <= rubric.DOCTOR_COVERAGE["code-doctor"], (
        "and it must stay inside the profile that caps what the raw layer may claim"
    )
    assert "correctness" not in resolved, (
        "a merge marker is the only correctness-class defect the raw layer can prove"
    )


def test_every_registered_detector_has_a_rubric_category(load_module):
    """A detector added to code-doctor's registry with no rubric row is silent.

    `build_health.py` resolves coverage with a membership test, so an
    unrecognised token is skipped rather than reported: the new detector would
    run, find things, and grant no coverage at all — the category it measures
    coming back ungraded with nothing anywhere saying why.
    """
    analyze_all = load_module(ANALYZE_ALL.parent, "analyze_all")
    rubric = load_module(RUBRIC_DIR, "rubric")

    unmapped = sorted(set(analyze_all.DETECTORS) - set(rubric.DETECTOR_CATEGORIES))

    assert not unmapped, (
        f"{unmapped} are code-doctor detector categories with no row in "
        "rubric.DETECTOR_CATEGORIES, so the coverage they report resolves to nothing"
    )
