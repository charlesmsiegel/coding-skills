"""The measurement-meta block is a contract between two skills, so it is pinned.

science-investigation *writes* the block into measurement.html;
code-overview's build_summary.py *reads* it back out to put a second grade on
the portal a reader lands on. The two skills are zipped and installed
separately and cannot import one another, so the reader exists twice —
`build_measurement.read_package_grade` and `common.read_meta` — and this repo's
answer to a deliberate cross-skill copy is a test, not trust
(tests/test_shared_assets.py pins four others).

This pair was not pinned, and had already diverged: `read_meta` warned when the
block was present but unparseable, `read_package_grade` returned a blank row in
silence. A blank row is exactly what a package nobody ever generated looks
like, so a corrupted document was tabled as "not generated" with nothing on
stderr — the failure mode this whole document set exists to refuse.

Each reader runs in its own subprocess. The two skills ship same-named modules
(`common`, `rubric`), and importing both into one interpreter is precisely how
a test comes to exercise the other skill's code without saying so.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCIENCE = ROOT / "skills" / "science-investigation" / "scripts"
OVERVIEW = ROOT / "skills" / "code-overview" / "scripts"
BUILD_MEASUREMENT = SCIENCE / "build_measurement.py"

# Two readers, one shape out: {found, score, grade}. Deliberately not importing
# either module into the test process — see the module docstring.
PROBE = '''
import importlib, json, sys
from pathlib import Path

scripts, which, page = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, scripts)

if which == "overview":
    common = importlib.import_module("common")
    meta = common.read_meta(page, common.MEASUREMENT_BLOCK_ID)
    out = {"found": meta is not None,
           "score": None if meta is None else meta.get("score"),
           "grade": None if meta is None else meta.get("grade")}
else:
    build_measurement = importlib.import_module("build_measurement")
    row = build_measurement.read_package_grade("pkg", Path(page))
    out = {"found": row["generated"], "score": row["score"], "grade": row["grade"]}

print(json.dumps(out))
'''


def probe(run_script, tmp_path, which: str, page: Path, expect_rc=0):
    script = tmp_path / "probe.py"
    script.write_text(PROBE, encoding="utf-8")
    scripts = OVERVIEW if which == "overview" else SCIENCE
    result = run_script(script, scripts, which, page, expect_rc=expect_rc)
    return json.loads(result.stdout.strip().splitlines()[-1]), result.stderr


def inventory(tmp_path) -> Path:
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({
        "schema": "measurement-inventory/1", "subject": "billing",
        "rows": [{"name": "judge_accuracy", "importance": 3,
                  "importance_reason": "gates the weekly rollout", "credit": 0.5,
                  "credit_reason": "one medium finding against it", "finding": "unpinned",
                  "n": 400, "n_total": 400, "formula": "mean(judge == gold)",
                  "consumer": "ci.yml", "evidence": ["src/billing/judge.py:10"],
                  "status": "measured", "unmeasurable_reason": ""}],
        "findings": [{"id": "unpinned", "severity": "medium", "title": "Judge is unpinned",
                      "detail": "Built inline.", "evidence": ["src/billing/judge.py:10"],
                      "blast_radius": "the rollout gate"}],
        "not_audited": []}), encoding="utf-8")
    return path


def test_both_skills_read_the_same_grade_out_of_a_real_generated_page(run_script, tmp_path):
    page = tmp_path / "measurement.html"
    run_script(BUILD_MEASUREMENT, "--out", page, "--inventory", inventory(tmp_path),
               "--name", "billing", "--repo", tmp_path, "--root")

    ours, _ = probe(run_script, tmp_path, "science", page)
    theirs, _ = probe(run_script, tmp_path, "overview", page)

    assert ours["found"] and theirs["found"]
    assert ours["score"] is not None and ours["grade"] != "—", (
        "a null-scoring page would let both readers agree on nothing"
    )
    assert ours == theirs, (
        "one document, two readers, one answer — anything else is a portal that "
        "disagrees with the document it links to"
    )


def test_a_script_closing_tag_in_the_payload_does_not_truncate_either_reader(
        run_script, tmp_path):
    # `</` is written as `<\/` so a consumer string containing "</script>"
    # cannot end the block early. Both readers have to undo that the same way,
    # or one of them sees truncated JSON where the other sees a grade.
    path = inventory(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rows"][0]["consumer"] = "</script><script>alert(1)</script>"
    path.write_text(json.dumps(payload), encoding="utf-8")

    page = tmp_path / "measurement.html"
    run_script(BUILD_MEASUREMENT, "--out", page, "--inventory", path,
               "--name", "billing", "--repo", tmp_path, "--root")

    ours, _ = probe(run_script, tmp_path, "science", page)
    theirs, _ = probe(run_script, tmp_path, "overview", page)

    assert ours["found"] and theirs["found"]
    assert ours == theirs


def test_a_corrupt_block_is_refused_and_complained_about_by_both(run_script, tmp_path):
    page = tmp_path / "measurement.html"
    page.write_text('<html><body><script type="application/json" '
                    'id="measurement-meta">{"score": </script></body></html>',
                    encoding="utf-8")

    ours, our_stderr = probe(run_script, tmp_path, "science", page)
    theirs, their_stderr = probe(run_script, tmp_path, "overview", page)

    assert ours["found"] is False and theirs["found"] is False
    assert ours["score"] is None and theirs["score"] is None
    for stderr in (our_stderr, their_stderr):
        assert "measurement-meta" in stderr and "JSON" in stderr, (
            "an unreadable document must not be reported the same way as a document "
            f"that was never built, in silence: {stderr!r}"
        )


def test_a_page_with_no_block_at_all_is_simply_absent_for_both(run_script, tmp_path):
    # The other side of the line: nothing to read is not a corruption, and
    # neither reader should manufacture a complaint about it.
    page = tmp_path / "measurement.html"
    page.write_text("<html><body>no metadata here</body></html>", encoding="utf-8")

    ours, our_stderr = probe(run_script, tmp_path, "science", page)
    theirs, their_stderr = probe(run_script, tmp_path, "overview", page)

    assert ours["found"] is False and theirs["found"] is False
    assert "measurement-meta" not in our_stderr
    assert "measurement-meta" not in their_stderr
