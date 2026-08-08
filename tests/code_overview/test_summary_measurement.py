"""The portal shows both grades, and neither is passed in.

Both are read back out of the generated documents, so the page a reader lands on
cannot disagree with the documents it links to.
"""

import json
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-overview" / "scripts"
SCRIPT = SCRIPTS / "build_summary.py"


def measurement_page(path: Path, score, grade, rows=1) -> Path:
    meta = {"schema": "measurement/1", "scope": "package", "package": path.parent.name,
            "score": score, "grade": grade, "weight_total": 3.0,
            "weight_measured": 0.0 if score is None else 3.0,
            "by_importance": {}, "rows": [{}] * rows, "findings": [], "not_audited": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<html><body><script type="application/json" id="measurement-meta">'
                    + json.dumps(meta) + "</script></body></html>", encoding="utf-8")
    return path


def health_page(path: Path, score, grade) -> Path:
    meta = {"schema": "code-health/1", "scope": "package", "package": path.parent.name,
            "score": score, "grade": grade, "categories": [], "ungraded": [],
            "size": {"files": 3, "loc": 300}, "findings_total": 2}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<html><body><script type="application/json" id="code-health-meta">'
                    + json.dumps(meta) + "</script></body></html>", encoding="utf-8")
    return path


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
