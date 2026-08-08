"""Scoping and roll-up.

One repo-wide audit produces every package's page, because a script pointed at
src/billing cannot see evals/ and would report a thoroughly-measured pipeline
as having no measurement — a fabricated null that looks exactly like an honest
one. So the rows are gathered once and partitioned here.
"""

import json
import re
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[2] / "skills" / "science-investigation"
SCRIPT = SKILL / "scripts" / "build_measurement.py"


def meta_of(page: Path) -> dict:
    match = re.search(r'id="measurement-meta">(.*?)</script>',
                      page.read_text(encoding="utf-8"), re.S)
    assert match
    return json.loads(match.group(1).replace("<\\/", "</"))


def row(name, evidence, importance=3, credit=1.0, n=100) -> dict:
    return {"name": name, "importance": importance,
            "importance_reason": "gates the release", "credit": credit,
            "credit_reason": "full labelled set", "finding": "", "n": n,
            "n_total": n, "formula": "x", "consumer": "ci.yml",
            "evidence": evidence, "status": "measured", "unmeasurable_reason": ""}


def inventory(tmp_path) -> Path:
    payload = {
        "schema": "measurement-inventory/1", "subject": "repo",
        "rows": [
            row("billing_accuracy", ["src/billing/metrics.py:10"]),
            # Defined in evals/, but it scores billing's output. It belongs to
            # whoever DEFINES it, which is why evidence[0] decides.
            row("billing_judge", ["evals/judge.py:4", "src/billing/api.py:80"]),
            row("search_ndcg", ["src/search/rank.py:22"]),
        ],
        "findings": [], "not_audited": [],
    }
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_root_dir_keeps_only_the_rows_defined_under_it(run_script, tmp_path):
    out = tmp_path / "billing.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "billing",
               "--repo", tmp_path, "--root-dir", "src/billing")

    meta = meta_of(out)
    assert [r["name"] for r in meta["rows"]] == ["billing_accuracy"]
    assert meta["rows_out_of_scope"] == 2


def test_a_row_defined_elsewhere_belongs_to_the_package_that_defines_it(run_script, tmp_path):
    out = tmp_path / "evals.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "evals",
               "--repo", tmp_path, "--root-dir", "evals")

    assert [r["name"] for r in meta_of(out)["rows"]] == ["billing_judge"]


def test_a_package_with_no_rows_scores_null_rather_than_vanishing(run_script, tmp_path):
    out = tmp_path / "utils.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "utils",
               "--repo", tmp_path, "--root-dir", "src/utils")

    meta = meta_of(out)
    assert meta["score"] is None
    assert meta["rows"] == []
    assert "no measurement content" in out.read_text(encoding="utf-8").lower()


def test_scope_can_differ_from_root_dir(run_script, tmp_path):
    out = tmp_path / "wide.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "billing",
               "--repo", tmp_path, "--root-dir", "src/billing", "--scope", "src")

    assert {r["name"] for r in meta_of(out)["rows"]} == {"billing_accuracy", "search_ndcg"}


def test_root_scope_keeps_every_row(run_script, tmp_path):
    out = tmp_path / "root.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "repo",
               "--repo", tmp_path, "--root")

    meta = meta_of(out)
    assert meta["scope"] == "repository"
    assert len(meta["rows"]) == 3


def test_the_root_document_tables_every_package_it_was_given(run_script, tmp_path):
    package = tmp_path / "billing.html"
    run_script(SCRIPT, "--out", package, "--inventory", inventory(tmp_path), "--name", "billing",
               "--repo", tmp_path, "--root-dir", "src/billing")

    out = tmp_path / "root.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "repo",
               "--repo", tmp_path, "--root", "--package", f"billing:{package}")

    packages = meta_of(out)["packages"]
    assert packages[0]["name"] == "billing"
    assert packages[0]["grade"] == "A+"
    assert packages[0]["generated"] is True


def test_a_package_with_no_document_stays_in_the_table_marked_not_generated(run_script, tmp_path):
    out = tmp_path / "root.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "repo",
               "--repo", tmp_path, "--root",
               "--package", f"ghost:{tmp_path / 'absent.html'}")

    row_ = meta_of(out)["packages"][0]
    assert row_["name"] == "ghost"
    assert row_["generated"] is False
    assert row_["score"] is None


def test_a_null_scoring_package_is_listed_as_no_measurement_content(run_script, tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"schema": "measurement-inventory/1", "subject": "utils",
                                 "rows": [], "findings": [], "not_audited": []}),
                     encoding="utf-8")
    package = tmp_path / "utils.html"
    run_script(SCRIPT, "--out", package, "--inventory", empty, "--name", "utils")

    out = tmp_path / "root.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "repo",
               "--repo", tmp_path, "--root", "--package", f"utils:{package}")

    text = out.read_text(encoding="utf-8")
    assert "no measurement content" in text.lower()
    assert meta_of(out)["packages"][0]["score"] is None


def test_package_is_rejected_without_root(run_script, tmp_path):
    result = run_script(SCRIPT, "--out", tmp_path / "x.html", "--inventory", inventory(tmp_path),
                        "--name", "x", "--package", "a:b.html", expect_rc=2)

    assert "--root" in result.stderr
