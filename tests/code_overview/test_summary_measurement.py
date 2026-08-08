"""The portal shows both grades — and the caveats behind them — and invents none.

Both grades are read back out of the generated documents, so the page a reader
lands on cannot disagree with the documents it links to. The same obligation
runs the other way: a caveat health.html prints has to reach this page too,
because this is where more people stop reading.
"""

import json
import re
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-overview" / "scripts"
SCRIPT = SCRIPTS / "build_summary.py"
HEALTH_SCRIPT = SCRIPTS / "build_health.py"
MEASUREMENT_SCRIPT = (Path(__file__).resolve().parents[2] / "skills" /
                      "science-investigation" / "scripts" / "build_measurement.py")


def measurement_page(path: Path, score, grade, rows=1) -> Path:
    meta = {"schema": "measurement/1", "scope": "package", "package": path.parent.name,
            "score": score, "grade": grade, "weight_total": 3.0,
            "weight_measured": 0.0 if score is None else 3.0,
            "by_importance": {}, "rows": [{}] * rows, "findings": [], "not_audited": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<html><body><script type="application/json" id="measurement-meta">'
                    + json.dumps(meta) + "</script></body></html>", encoding="utf-8")
    return path


def health_page(path: Path, score, grade, **extra) -> Path:
    meta = {"schema": "code-health/1", "scope": "package", "package": path.parent.name,
            "score": score, "grade": grade, "categories": [], "ungraded": [],
            "size": {"files": 3, "loc": 300}, "findings_total": 2}
    meta.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<html><body><script type="application/json" id="code-health-meta">'
                    + json.dumps(meta) + "</script></body></html>", encoding="utf-8")
    return path


def caveats_of(text: str) -> str:
    """Everything under the Caveats heading — the section, not the document."""
    return text.split("<h2>Caveats</h2>")[1] if "<h2>Caveats</h2>" in text else ""


def test_both_grades_are_read_back_out_of_the_documents(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    health_page(docs / "health.html", 88.0, "B+")
    measurement_page(docs / "measurement.html", 15.0, "F")
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    text = out.read_text(encoding="utf-8")
    assert "B+" in text and "F" in text
    assert "88.0" in text and "15.0" in text


def test_the_measurement_document_is_linked(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    measurement_page(docs / "measurement.html", 40.0, "F")
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    assert "measurement.html" in out.read_text(encoding="utf-8")


def test_a_missing_measurement_document_is_not_linked(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    health_page(docs / "health.html", 88.0, "B+")
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    text = out.read_text(encoding="utf-8")
    assert 'href="measurement.html"' not in text


def test_a_null_measurement_grade_reads_as_no_content_not_as_a_pass(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    measurement_page(docs / "measurement.html", None, "—", rows=0)
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    assert "no measurement content" in out.read_text(encoding="utf-8").lower()


def test_a_failed_doctor_is_named_even_when_it_cost_no_category(repo, run_script, tmp_path):
    """The clean-grade-over-a-crashed-doctor case, which is the dangerous one.

    render_caveats read ungraded, analyzers_skipped, analyzer_errors and
    findings_out_of_scope — never doctor_errors. When a surviving doctor
    happens to cover the crashed one's categories nothing is ungraded, so
    every other caveat stayed silent and the portal showed a clean B+ with no
    hint that a doctor had died. health.html names it either way; its own
    docstring says this page "must say the same thing health.html says".
    """
    docs = repo.path / "src" / "app" / "docs"
    health_page(docs / "health.html", 88.0, "B+", ungraded=[],
                doctor_errors={"django-code-doctor": "crashed reading settings"})
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    text = out.read_text(encoding="utf-8")
    assert "B+" in text, "nothing was ungraded, so the grade still stands"
    caveats = caveats_of(text)
    assert caveats, "a page with a failed doctor has caveats"
    assert "django-code-doctor" in caveats
    assert "crashed reading settings" in caveats


def test_a_page_with_nothing_to_caveat_carries_no_caveats_section(repo, run_script, tmp_path):
    # The negative half: the callout above must be conditional, or its
    # presence proves nothing.
    docs = repo.path / "src" / "app" / "docs"
    health_page(docs / "health.html", 88.0, "B+")
    out = docs / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    assert "<h2>Caveats</h2>" not in out.read_text(encoding="utf-8")


def test_a_real_generated_measurement_page_is_read_back_by_the_summary(
        repo, run_script, tmp_path):
    """End to end across the two skills, with nothing hand-written.

    Every other test in this file writes the metadata block itself, so the
    whole file would stay green if build_measurement.py stopped emitting the
    block in the shape build_summary.py reads — a broken portal with a green
    suite, across a boundary neither skill's own tests cross.
    """
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({
        "schema": "measurement-inventory/1", "subject": "app",
        "rows": [{"name": "judge_accuracy", "importance": 3,
                  "importance_reason": "gates the weekly rollout", "credit": 0.5,
                  "credit_reason": "one medium finding against it",
                  "finding": "unpinned", "n": 400, "n_total": 400,
                  "formula": "mean(judge == gold)", "consumer": "ci.yml",
                  "evidence": ["src/app/judge.py:10"], "status": "measured",
                  "unmeasurable_reason": ""}],
        "findings": [{"id": "unpinned", "severity": "medium", "title": "Judge is unpinned",
                      "detail": "The judge prompt is built inline.",
                      "evidence": ["src/app/judge.py:10"], "blast_radius": "the rollout gate"}],
        "not_audited": []}), encoding="utf-8")

    docs = repo.path / "src" / "app" / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    measurement = docs / "measurement.html"
    run_script(MEASUREMENT_SCRIPT, "--out", measurement, "--inventory", inventory,
               "--name", "app", "--repo", repo.path, "--root")

    generated = json.loads(
        re.search(r'id="measurement-meta">(.*?)</script>',
                  measurement.read_text(encoding="utf-8"), re.S).group(1).replace("<\\/", "</"))
    assert generated["score"] is not None and generated["grade"] != "—", (
        "the fixture must produce a real grade, or reading it back proves nothing"
    )

    out = docs / "summary.html"
    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app")

    text = out.read_text(encoding="utf-8")
    assert 'href="measurement.html"' in text
    assert f'{generated["score"]:.1f} / 100' in text, (
        "the portal shows the score the real document computed, not one it was told"
    )
    assert f'<div class="letter">{generated["grade"]}</div>' in text


def test_the_repo_table_gains_a_measurement_column(repo, run_script, tmp_path):
    for name in ("billing", "search"):
        docs = repo.path / "src" / name / "docs"
        health_page(docs / "health.html", 90.0, "A-")
        measurement_page(docs / "measurement.html", 60.0 if name == "billing" else None,
                         "D-" if name == "billing" else "—",
                         rows=1 if name == "billing" else 0)
    mapping = repo.path / "docs" / "code-overview.json"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text(json.dumps({
        "schema": "code-overview/1",
        "packages": [{"name": n, "roots": [f"src/{n}"], "docs": f"src/{n}/docs",
                      "language": "python", "doctor": "code-doctor"}
                     for n in ("billing", "search")]}), encoding="utf-8")
    out = repo.path / "docs" / "summary.html"

    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "repo",
               "--root", "--map", mapping)

    text = out.read_text(encoding="utf-8")
    assert "Measurement" in text
    assert "D-" in text
    assert "search" in text
    assert "no measurement content" in text.lower()
