"""Scoping and roll-up.

One repo-wide audit produces every package's page, because a script pointed at
src/billing cannot see evals/ and would report a thoroughly-measured pipeline
as having no measurement — a fabricated null that looks exactly like an honest
one. So the rows are gathered once and partitioned here.
"""

import json
import re
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skills" / "science-investigation"
SCRIPT = SKILL / "scripts" / "build_measurement.py"


def meta_of(page: Path) -> dict:
    match = re.search(r'id="measurement-meta">(.*?)</script>',
                      page.read_text(encoding="utf-8"), re.S)
    assert match
    return json.loads(match.group(1).replace("<\\/", "</"))


def panel_of(page: Path, tab_id: str) -> str:
    """One rendered panel's own markup, so 'on the Score tab' can be asserted."""
    # Split on the panel boundary rather than the next </section>: the grade
    # card is itself a <section>, so a lazy match to </section> stops short.
    # Then cut at this panel's own end — panels() writes each body followed by
    # a newline and </section> — because the last panel's chunk otherwise runs
    # to end-of-file and swallows the measurement-meta script, which carries
    # the entire inventory as text and makes any substring check vacuous.
    text = page.read_text(encoding="utf-8")
    for chunk in text.split('<section class="panel')[1:]:
        if re.match(rf'[^>]*id="{tab_id}"', chunk):
            return chunk.split("\n</section>")[0]
    raise AssertionError(f"no {tab_id} panel on the page")


def state_cells(panel: str) -> dict[str, str]:
    """The Packages roll-up table as {package name: the State cell it shows}."""
    assert "<h3>Packages</h3>" in panel, "the Score tab carries no Packages table"
    table = panel.split("<h3>Packages</h3>")[1]
    return {cells[0]: cells[-1]
            for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)
            if len(cells := re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)) == 5}


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


def test_dropped_rows_are_named_on_the_page_not_only_on_stderr(run_script, tmp_path):
    # A page that drops rows and says nothing is how a partial audit renders as
    # a perfect score. The count belongs beside the KPI it qualifies, not in
    # the hidden metadata block a reader never opens.
    out = tmp_path / "billing.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "billing",
               "--repo", tmp_path, "--root-dir", "src/billing")

    score_panel = panel_of(out, "tab-score")
    assert "2 further rows" in score_panel
    assert "defined outside this unit" in score_panel


def test_a_package_build_with_no_scope_at_all_says_it_took_everything(run_script, tmp_path):
    # An empty scope list means "everything" — right for --root, silently wrong
    # for a package, where it scores the whole repository's inventory as this
    # one package's and then counts the same rows again under every other
    # package. The audit is repo-wide by design, so this is the easy mistake,
    # and it produces a plausible number rather than a crash.
    out = tmp_path / "billing.html"
    result = run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path),
                        "--name", "billing", "--repo", tmp_path)

    assert len(meta_of(out)["rows"]) == 3, "it really did take every row"
    assert "warning" in result.stderr.lower()
    assert "--root-dir" in result.stderr and "--scope" in result.stderr


def test_a_scoped_package_build_is_not_warned_at(run_script, tmp_path):
    out = tmp_path / "billing.html"
    result = run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path),
                        "--name", "billing", "--repo", tmp_path, "--root-dir", "src/billing")

    assert "neither --scope nor --root-dir" not in result.stderr


def test_the_repository_roll_up_is_not_warned_at_for_covering_everything(run_script, tmp_path):
    # --root *is* the declaration that everything belongs here.
    out = tmp_path / "root.html"
    result = run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path),
                        "--name", "repo", "--repo", tmp_path, "--root")

    assert "neither --scope nor --root-dir" not in result.stderr


def test_a_page_that_dropped_nothing_carries_no_scope_note(run_script, tmp_path):
    out = tmp_path / "root.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "repo",
               "--repo", tmp_path, "--root")

    assert "defined outside this unit" not in panel_of(out, "tab-score")


def test_an_unattached_finding_survives_scoping_rather_than_vanishing(run_script, tmp_path):
    # Nothing requires a finding to name a row. One about the measurement setup
    # generally — no seeds anywhere, no version pinning — belongs to no row, so
    # no scope can be out of scope for it. It used to be dropped, after which
    # the page announced "no confirmed findings".
    payload = {
        "schema": "measurement-inventory/1", "subject": "repo",
        "rows": [row("billing_accuracy", ["src/billing/metrics.py:10"]),
                 {**row("search_ndcg", ["src/search/rank.py:22"], credit=0.25),
                  "finding": "search_small_n"}],
        "findings": [
            {"id": "unpinned_judge", "severity": "high",
             "title": "No judge prompt is version-pinned anywhere",
             "detail": "Every judge prompt is built inline from an f-string.",
             "evidence": ["evals/judge.py:9"], "blast_radius": "every score in the repo"},
            {"id": "search_small_n", "severity": "medium", "title": "search n=4",
             "detail": "4 graded queries.", "evidence": ["src/search/rank.py:22"],
             "blast_radius": "the ranking roadmap"},
        ],
        "not_audited": [],
    }
    inv = tmp_path / "unattached.json"
    inv.write_text(json.dumps(payload), encoding="utf-8")

    out = tmp_path / "billing.html"
    run_script(SCRIPT, "--out", out, "--inventory", inv, "--name", "billing",
               "--repo", tmp_path, "--root-dir", "src/billing")

    # Every finding — id, title, detail — is also serialized into the
    # measurement-meta block, so both of these assertions passed against a page
    # with the Findings tab rendered as an empty paragraph. The claim is that a
    # reader still sees the finding, so it is read out of the Findings panel.
    panel = panel_of(out, "tab-findings")
    assert [f["id"] for f in meta_of(out)["findings"]] == ["unpinned_judge"], (
        "the unattached finding stays; the one attached to a dropped row goes with it"
    )
    assert "No judge prompt is version-pinned anywhere" in panel
    assert "every score in the repo" in panel, "its blast radius, rendered"
    assert "search n=4" not in panel, "the finding that went with its dropped row"
    assert "No confirmed findings" not in panel


def test_not_audited_is_labelled_repository_wide_on_a_scoped_page(run_script, tmp_path):
    # Labelled, not filtered: dropping these would hide a real gap, but
    # reprinting them unlabelled reads as this package's own gap.
    payload = json.loads(inventory(tmp_path).read_text(encoding="utf-8"))
    payload["not_audited"] = ["the analytics dashboard — no access"]
    inv = tmp_path / "gaps.json"
    inv.write_text(json.dumps(payload), encoding="utf-8")

    scoped = tmp_path / "billing.html"
    run_script(SCRIPT, "--out", scoped, "--inventory", inv, "--name", "billing",
               "--repo", tmp_path, "--root-dir", "src/billing")
    root = tmp_path / "root.html"
    run_script(SCRIPT, "--out", root, "--inventory", inv, "--name", "repo",
               "--repo", tmp_path, "--root")

    # `not_audited` is copied verbatim into the metadata block, so the "still
    # listed" half of this passed with the gap list deleted from the rendering.
    scoped_panel = panel_of(scoped, "tab-score")
    assert "the analytics dashboard" in scoped_panel, "still listed, never filtered"
    assert "Not audited (whole repository)" in scoped_panel
    assert "Not audited (whole repository)" not in panel_of(root, "tab-score")
    assert "the analytics dashboard" in panel_of(root, "tab-score"), (
        "the root page lists it too — labelled differently, never dropped"
    )


def test_a_row_defined_elsewhere_belongs_to_the_package_that_defines_it(run_script, tmp_path):
    out = tmp_path / "evals.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "evals",
               "--repo", tmp_path, "--root-dir", "evals")

    assert [r["name"] for r in meta_of(out)["rows"]] == ["billing_judge"]


def test_defining_path_without_a_line_number_is_not_truncated(load_module, tmp_path):
    # Regression: a naive split on the first ":" truncates a Windows absolute
    # path at its drive letter. A plain relative path with no line number
    # must come back untouched.
    build_measurement = load_module(SKILL / "scripts", "build_measurement")
    entry = {"evidence": ["src/a.py"]}
    assert build_measurement.defining_path(entry, tmp_path) == "src/a.py"


def test_absolute_evidence_inside_the_repo_scopes_to_the_right_package(run_script, tmp_path):
    repo_dir = tmp_path / "repo"
    (repo_dir / "src" / "billing").mkdir(parents=True)
    abs_citation = repo_dir / "src" / "billing" / "metrics.py"

    payload = {
        "schema": "measurement-inventory/1", "subject": "repo",
        "rows": [row("billing_accuracy", [f"{abs_citation}:10"])],
        "findings": [], "not_audited": [],
    }
    inv = tmp_path / "abs-inside.json"
    inv.write_text(json.dumps(payload), encoding="utf-8")

    out = tmp_path / "billing-abs.html"
    run_script(SCRIPT, "--out", out, "--inventory", inv, "--name", "billing",
               "--repo", repo_dir, "--root-dir", "src/billing")

    assert [r["name"] for r in meta_of(out)["rows"]] == ["billing_accuracy"]


def test_absolute_evidence_outside_the_repo_matches_no_scope(run_script, tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    abs_citation = outside_dir / "metrics.py"

    payload = {
        "schema": "measurement-inventory/1", "subject": "repo",
        "rows": [row("outside_metric", [f"{abs_citation}:10"])],
        "findings": [], "not_audited": [],
    }
    inv = tmp_path / "abs-outside.json"
    inv.write_text(json.dumps(payload), encoding="utf-8")

    out = tmp_path / "billing-abs-outside.html"
    run_script(SCRIPT, "--out", out, "--inventory", inv, "--name", "billing",
               "--repo", repo_dir, "--root-dir", "src/billing")

    meta = meta_of(out)
    assert meta["rows"] == []
    assert meta["rows_out_of_scope"] == 1


def test_backslash_relative_evidence_scopes_to_its_forward_slash_package(run_script, tmp_path):
    payload = {
        "schema": "measurement-inventory/1", "subject": "repo",
        "rows": [row("billing_accuracy", ["src\\billing\\metrics.py:10"])],
        "findings": [], "not_audited": [],
    }
    inv = tmp_path / "backslash.json"
    inv.write_text(json.dumps(payload), encoding="utf-8")

    out = tmp_path / "billing-backslash.html"
    run_script(SCRIPT, "--out", out, "--inventory", inv, "--name", "billing",
               "--repo", tmp_path, "--root-dir", "src/billing")

    assert [r["name"] for r in meta_of(out)["rows"]] == ["billing_accuracy"]


def test_a_package_with_no_rows_scores_null_rather_than_vanishing(run_script, tmp_path):
    out = tmp_path / "utils.html"
    run_script(SCRIPT, "--out", out, "--inventory", inventory(tmp_path), "--name", "utils",
               "--repo", tmp_path, "--root-dir", "src/utils")

    meta = meta_of(out)
    assert meta["score"] is None
    assert meta["rows"] == []
    assert "no measurement content" in panel_of(out, "tab-score").lower()


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
    # "stays in the table" is a claim about the table, and the metadata alone
    # satisfied it. A package nobody built and a package with nothing to
    # measure must not collapse into the same cell.
    assert state_cells(panel_of(out, "tab-score")) == {"ghost": "not generated"}


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

    # Not `"no measurement content" in text`: render_package_table's lead
    # paragraph says that phrase unconditionally, so the check was satisfied by
    # a static sentence rather than by the state it names. The claim is that
    # *utils* is listed in that state, which is one cell of one row.
    assert state_cells(panel_of(out, "tab-score")) == {"utils": "no measurement content"}
    assert meta_of(out)["packages"][0]["score"] is None


def test_package_is_rejected_without_root(run_script, tmp_path):
    result = run_script(SCRIPT, "--out", tmp_path / "x.html", "--inventory", inventory(tmp_path),
                        "--name", "x", "--package", "a:b.html", expect_rc=2)

    assert "--root" in result.stderr
