"""The health page's tabs, and the line the Candidates tab has to hold.

A candidate is a lead. Rendering it beside confirmed defects, in a document
whose headline is a grade, is how a reader concludes that a dead-function
candidate is a dead function — and deletes live code.
"""

import json
import re
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "code-overview" /
          "scripts" / "build_health.py")
DJANGO_ANALYZER = (Path(__file__).resolve().parents[2] / "skills" /
                   "django-code-doctor" / "scripts" / "analyze_django.py")
PYTHON_ANALYZER = (Path(__file__).resolve().parents[2] / "skills" /
                   "python-code-doctor" / "scripts" / "analyze_all.py")


def envelope(tmp_path) -> Path:
    payload = {
        "schema": "code-doctor-merge/1",
        "doctors_run": ["code-doctor"],
        "analyzers_run": {"code-doctor": ["hygiene", "secrets"]},
        "analyzers_skipped": {"code-doctor": ["duplication"]},
        "analyzer_errors": {},
        "doctor_errors": {"django-code-doctor": "crashed reading settings"},
        # Two shapes on purpose. code-doctor's own completeness block is a flat
        # dict of *strings* — detectors_failed, merge_state, the unreadable-file
        # accounting — and every one of them was dropped by a renderer that only
        # understood the {"adequate": ...} dict shape. They reached the envelope,
        # survived into the hidden metadata, and appeared nowhere on the tab
        # whose entire job is naming them.
        "completeness": {"code-doctor": {"reference_graph": {"adequate": False,
                                                             "resolution_rate": 0.31},
                                         "detectors_failed": "find_dead_code: exited 1",
                                         "merge_state": "git unavailable — conflict markers "
                                                        "reported as candidates"}},
        "coverage_unknown": [],
        "findings": [{"doctor": "code-doctor", "file": "src/app/settings.py", "line": 4,
                      "smell_type": "hardcoded_secret_assignment", "severity": "high",
                      "description": "SECRET_KEY literal", "suggestion": "read from env",
                      "kind": "finding"}],
        "candidates": [{"doctor": "code-doctor", "file": "src/app/legacy.py", "line": 12,
                        "smell_type": "dead_function_candidate", "severity": "medium",
                        "description": "identifier occurs once in the tree",
                        "kind": "candidate",
                        "also_caused_by": ["a library's public surface has no internal referrer",
                                           "convention-loaded plugins are never named"]}],
    }
    path = tmp_path / "merged.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def page(repo, run_script, tmp_path) -> str:
    for i in range(12):
        repo.write(f"src/app/mod{i}.py", "def f():\n    return 1\n" * 20)
    repo.commit()
    out = tmp_path / "health.html"
    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app",
               "--root-dir", "src/app", "--merged", envelope(tmp_path))
    return out.read_text(encoding="utf-8")


def ungraded_envelope(tmp_path) -> Path:
    """The same fixture data, but code-doctor names no analyzer it ran.

    `page` below used to be the ungraded case by accident: a producer/consumer
    mismatch meant its `analyzers_run` tokens ("hygiene", "secrets") never
    resolved into rubric categories at all, so *every* category came back
    ungraded regardless of what the envelope said. That mismatch is fixed now
    — those tokens resolve, and `page` legitimately grades Hygiene and
    Security. So the ungraded case needs its own envelope, and it needs to be
    ungraded on purpose rather than by bug.

    An empty `analyzers_run` entry for code-doctor is that case, and it is the
    one `common.load_merged` itself documents: nothing distinguishes a doctor
    that ran no analyzers from one that never started, so the report is read
    as SHAPE_PARTIAL and grants no coverage profile at all — see the "bare
    list" comment on `resolve_coverage`. That is a more honest "nothing could
    be graded" than a doctor absent from `rubric.DOCTOR_COVERAGE` (which
    would mean an *unknown* doctor, not a doctor that ran and produced
    nothing) or an envelope whose only doctor failed outright (which is a
    different, already-covered claim: see `doctor_errors` in the base
    `envelope`, exercised by the coverage-tab tests below).
    """
    payload = json.loads(envelope(tmp_path).read_text(encoding="utf-8"))
    payload["analyzers_run"] = {}
    path = tmp_path / "ungraded.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def ungraded_page(repo, run_script, tmp_path) -> str:
    """A page that genuinely earns no grade at all.

    Companion to `graded_page` below: that fixture proves a grade renders when
    one is earned, this one proves the placeholder renders when nothing is.
    """
    for i in range(12):
        repo.write(f"src/app/mod{i}.py", "def f():\n    return 1\n" * 20)
    repo.commit()
    out = tmp_path / "ungraded.html"
    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app",
               "--root-dir", "src/app", "--merged", ungraded_envelope(tmp_path))
    return out.read_text(encoding="utf-8")


@pytest.fixture
def graded_page(repo, run_script, tmp_path) -> str:
    """A page that actually earns a letter.

    The `page` fixture above is deliberately ungraded: its `analyzers_run`
    names no detector this rubric knows, a doctor crashed, and the reference
    graph is inadequate, so every category is dropped and the grade is
    rubric.UNGRADED. That is the right page for the coverage assertions and
    the wrong one for asking whether a grade renders — which is exactly how
    the grade-card test came to pass with grading deleted.
    """
    for i in range(12):
        repo.write(f"src/app/mod{i}.py", "def f():\n    return 1\n" * 20)
    repo.commit()
    payload = json.loads(envelope(tmp_path).read_text(encoding="utf-8"))
    payload["analyzers_run"] = {"code-doctor": ["security", "debug_leftovers"]}
    payload["analyzers_skipped"] = {}
    payload["doctor_errors"] = {}
    payload["completeness"] = {}
    path = tmp_path / "graded.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "graded.html"
    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app",
               "--root-dir", "src/app", "--merged", path)
    return out.read_text(encoding="utf-8")


def health_meta(page: str) -> dict:
    match = re.search(r'id="code-health-meta">(.*?)</script>', page, re.DOTALL)
    assert match, "the page carries no code-health-meta block"
    return json.loads(match.group(1).replace("<\\/", "</"))


_COVERAGE_PANEL_RE = re.compile(
    r'<section class="panel(?: active)?" id="tab-coverage"[^>]*>(.*?)</section>', re.DOTALL)
_CANDIDATES_PANEL_RE = re.compile(
    r'<section class="panel(?: active)?" id="tab-candidates"[^>]*>(.*?)</section>', re.DOTALL)
_GRADE_PANEL_RE = re.compile(
    r'<section class="panel(?: active)?" id="tab-grade"[^>]*>(.*?)'
    r'(?=<section class="panel|<script type="application/json" id="code-health-meta">)',
    re.DOTALL)


def grade_panel(page: str) -> str:
    """Just the Grade tab's own markup — not the whole document.

    Unlike Coverage, the Grade tab's body itself opens a nested
    `<section class="gradecard ...">` — so the naive "stop at the first
    `</section>`" pattern `coverage_panel` uses stops there too, truncating
    the panel before the placeholder callout that comes after it. Stopping
    instead at the *next* `<section class="panel` (the following tab) or the
    trailing `code-health-meta` script — whichever comes first — captures the
    whole tab regardless of what it nests.

    The `code-health-meta` JSON block sits later in the same page and is
    itself keyed by strings a careless assertion could match — `health_meta`
    exists so score/grade checks go through the parsed JSON instead of a
    substring search. Any prose check (like the placeholder-grade callout)
    needs the same discipline `coverage_panel` documents: scope to the panel
    that is supposed to carry it, not the document as a whole.
    """
    match = _GRADE_PANEL_RE.search(page)
    assert match, "no Grade panel found"
    return match.group(1)


def coverage_panel(page: str) -> str:
    """Just the Coverage tab's own markup — not the whole document.

    `"duplication"` also names the Duplication &amp; Dead Code category, and
    that label is rendered unconditionally in every category row on the Grade
    tab, whether or not duplication was ever skipped. A whole-page substring
    check for it passes on a page where the Coverage tab renders nothing at
    all — this is what actually caught that, four times over in this project.
    Scoping to the panel is what makes the check mean what its name says.
    """
    match = _COVERAGE_PANEL_RE.search(page)
    assert match, "no Coverage panel found"
    return match.group(1)


def candidates_panel(page: str) -> str:
    """Just the Candidates tab's own markup — not the whole document.

    Same reasoning as `coverage_panel`: the `code-health-meta` block embeds the
    raw JSON later in the page, `also_caused_by` text and all, so a whole-page
    substring check would pass even if `render_candidates` produced nothing.
    """
    match = _CANDIDATES_PANEL_RE.search(page)
    assert match, "no Candidates panel found"
    return match.group(1)


def test_all_four_tabs_are_present(page):
    # Exactly four, not "at least four": the preamble of a body scaffold has
    # already once been split into a fifth, bogus tab that rendered *active* and
    # hid the grade card. Counting the buttons is what catches that; asserting a
    # title appears somewhere in the HTML does not.
    assert page.count('<button role="tab"') == 4
    for title in ("Grade", "Findings", "Candidates", "Coverage"):
        assert f">{title}<" in page, f"the {title} tab is missing"


def test_the_visible_grade_card_carries_the_grade(graded_page):
    # This used to accept any letter that was neither empty nor the word
    # "None" — and rubric.UNGRADED is "—", which is neither. Deleting the
    # grading arithmetic outright (weighted_overall returning None) failed 21
    # tests elsewhere and every one in this file still passed. So: a real band
    # letter, and the one the metadata says was computed.
    meta = health_meta(graded_page)
    assert meta["score"] is not None, "this fixture must produce a graded page"
    assert re.fullmatch(r"[A-F][+-]?", str(meta["grade"])), (
        f"{meta['grade']!r} is not a letter grade — nothing was actually graded"
    )

    card = re.search(r'<div class="letter">([^<]*)</div>', graded_page)
    assert card, "no grade card rendered"
    assert card.group(1).strip() == meta["grade"], (
        "the letter a reader sees and the letter the metadata carries are the same number"
    )


def test_an_ungraded_page_shows_the_dash_and_says_the_grade_is_a_placeholder(ungraded_page):
    # The other half of the same claim as test_the_visible_grade_card_carries_
    # the_grade: when nothing could be graded, the card must not quietly
    # render something that reads like a grade. `page` cannot stand in for
    # that anymore — its code-doctor entry now genuinely resolves Hygiene and
    # Security coverage (see `ungraded_envelope`'s docstring) — so this uses
    # the dedicated `ungraded_page` fixture instead.
    assert health_meta(ungraded_page)["score"] is None
    card = re.search(r'<div class="letter">([^<]*)</div>', ungraded_page)
    assert card and card.group(1).strip() == "—"
    # Scoped to the Grade panel: the placeholder callout is only meaningful
    # where the grade card itself lives, not merely present somewhere in the
    # document.
    assert "the grade shown is a placeholder" in grade_panel(ungraded_page)


def test_the_grade_tab_is_the_one_that_opens(page):
    first = re.search(r'<section class="panel active" id="([^"]+)"', page)
    assert first and first.group(1) == "tab-grade"


def test_the_candidates_tab_says_it_did_not_affect_the_grade(page):
    assert "did not affect the grade" in page.lower()


def test_a_candidate_carries_the_benign_explanations(page):
    assert "convention-loaded plugins are never named" in page


def test_the_coverage_tab_names_the_failed_doctor(page):
    panel = coverage_panel(page)
    assert "django-code-doctor" in panel
    assert "crashed reading settings" in panel


def test_the_coverage_tab_reports_the_thin_graph_rather_than_hiding_it(page):
    # "design" alone would prove nothing: every category row on the Grade tab
    # names all seven categories regardless of coverage, "Design" among them,
    # so that word is on the page even with this whole callout deleted. What
    # is specific to the completeness evidence is the (evidence, verdict,
    # detail) row: reference_graph's resolution_rate, called out as
    # incomplete, inside the Coverage panel.
    panel = coverage_panel(page)
    assert "reference_graph" in panel
    assert "incomplete" in panel
    assert "resolution_rate: 0.31" in panel


def test_the_coverage_tab_shows_the_string_valued_caveats_too(page):
    # Every value code-doctor writes into its completeness block is a plain
    # string, and the renderer skipped anything that was not a dict. So the
    # detector that crashed, and "git unavailable — conflict markers reported
    # as candidates", reached the envelope, were copied into the hidden JSON
    # metadata, and were shown to nobody — on the one tab that exists to name
    # what was not measured. A caveat only in the metadata is a caveat that
    # was not made.
    panel = coverage_panel(page)

    assert "detectors_failed" in panel
    assert "find_dead_code: exited 1" in panel
    assert "merge_state" in panel
    assert "conflict markers reported as candidates" in panel


def test_the_coverage_tab_lists_the_skipped_analyzer(page):
    # A skipped analyzer named with no doctor beside it tells a reader
    # something was not run, not who to go re-run — so the analyzer name has
    # to appear attributed to the doctor whose report skipped it, not merely
    # co-occur somewhere on the page (see coverage_panel's docstring: the
    # bare substring "duplication" is also the Duplication & Dead Code
    # category label, printed on the Grade tab unconditionally).
    panel = coverage_panel(page)
    match = re.search(r"Detectors that were not run:.*?</div>", panel, re.DOTALL)
    assert match, "no 'detectors that were not run' callout in the Coverage panel"
    assert "code-doctor" in match.group()
    assert "duplication" in match.group()


def test_a_page_with_no_candidates_says_so_rather_than_showing_an_empty_table(
        repo, run_script, tmp_path):
    repo.write("src/app/mod.py", "x = 1\n")
    repo.commit()
    payload = json.loads(envelope(tmp_path).read_text(encoding="utf-8"))
    payload["candidates"] = []
    path = tmp_path / "m2.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "health.html"
    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app",
               "--root-dir", "src/app", "--merged", path)

    assert "no candidates" in out.read_text(encoding="utf-8").lower()


# --------------------------------------------------------------------------
# `--findings`: code-doctor's own {"completeness": ..., "findings": [...]}
# shape, carried straight in (no --merged envelope). normalize_findings
# already reads candidates and a completeness block out of this shape; the
# bug under test was that main() only ever sourced those two things from
# --merged, so a --findings-only code-doctor report graded correctly but its
# Candidates tab rendered empty and its completeness caveats never reached
# the Coverage tab.
# --------------------------------------------------------------------------

def doctor_report(tmp_path, *, with_candidate: bool) -> Path:
    """A hand-built code-doctor report, in code-doctor's own report shape —
    not code-doctor-merge/1. One asserted defect (kept in both variants, so
    the score has something to compute), plus one candidate lead that is
    present only when `with_candidate` is True.
    """
    findings = [{"doctor": "code-doctor", "file": "src/app/settings.py", "line": 4,
                "smell_type": "hardcoded_secret_assignment", "severity": "high",
                "description": "SECRET_KEY literal", "suggestion": "read from env",
                "kind": "finding"}]
    if with_candidate:
        findings.append({"doctor": "code-doctor", "file": "src/app/legacy.py", "line": 12,
                         "smell_type": "dead_function_candidate", "severity": "medium",
                         "description": "identifier occurs once in the tree",
                         "kind": "candidate",
                         "also_caused_by": ["a library's public surface has no internal referrer",
                                            "convention-loaded plugins are never named"]})
    payload = {
        "completeness": {"categories_run": ["hygiene", "secrets"], "categories_skipped": [],
                         "reference_graph": {"adequate": False, "resolution_rate": 0.31}},
        "findings": findings,
    }
    name = "doctor_with_candidate.json" if with_candidate else "doctor_no_candidate.json"
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _findings_path_page(repo, run_script, tmp_path, out_name: str, *, with_candidate: bool) -> str:
    out = tmp_path / out_name
    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app",
               "--root-dir", "src/app", "--doctor", "code-doctor",
               "--findings", f"code-doctor:{doctor_report(tmp_path, with_candidate=with_candidate)}")
    return out.read_text(encoding="utf-8")


def test_a_findings_path_report_renders_its_candidates_without_moving_the_score(
        repo, run_script, tmp_path):
    for i in range(12):
        repo.write(f"src/app/mod{i}.py", "def f():\n    return 1\n" * 20)
    repo.commit()

    with_page = _findings_path_page(repo, run_script, tmp_path, "with.html",
                                    with_candidate=True)
    without_page = _findings_path_page(repo, run_script, tmp_path, "without.html",
                                       with_candidate=False)

    panel = candidates_panel(with_page)
    assert "dead_function_candidate" in panel
    assert "convention-loaded plugins are never named" in panel

    with_meta = health_meta(with_page)
    without_meta = health_meta(without_page)
    assert with_meta["score"] is not None, "the fixture's own finding must be graded"
    assert with_meta["score"] == without_meta["score"], (
        "a candidate carried by a --findings report must not move the score, exactly like "
        "one carried by --merged"
    )


def test_a_findings_path_reports_completeness_caveats_on_the_coverage_tab(
        repo, run_script, tmp_path):
    for i in range(12):
        repo.write(f"src/app/mod{i}.py", "def f():\n    return 1\n" * 20)
    repo.commit()
    page = _findings_path_page(repo, run_script, tmp_path, "health.html", with_candidate=True)
    panel = coverage_panel(page)
    assert "reference_graph" in panel
    assert "incomplete" in panel
    assert "resolution_rate: 0.31" in panel


def test_raw_django_report_keeps_template_relation_walks_out_of_the_grade(
        repo, run_script, tmp_path):
    repo.write("manage.py", "import django\n")
    repo.write(
        "app/templates/app/list.html",
        "{% for obj in objects %}{{ obj.owner.profile.name }}{% endfor %}\n",
    )
    repo.write("app/views.py", "def list_view(request):\n    return None\n")
    repo.commit()

    raw = tmp_path / "django.json"
    analyzed = run_script(DJANGO_ANALYZER, repo.path, "--format", "json")
    raw.write_text(analyzed.stdout, encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")

    def build(report: Path, name: str) -> str:
        out = tmp_path / name
        run_script(
            SCRIPT, "--out", out, "--repo", repo.path, "--name", "app",
            "--root-dir", "app", "--findings", f"django-code-doctor:{report}",
            "--covers", "complexity",
        )
        return out.read_text(encoding="utf-8")

    with_candidate = build(raw, "with.html")
    without_candidate = build(empty, "without.html")
    assert "relation_walk_in_loop" in candidates_panel(with_candidate)
    assert health_meta(with_candidate)["score"] == health_meta(without_candidate)["score"]


def test_raw_python_report_keeps_a_pragma_out_of_the_grade(repo, run_script, tmp_path):
    """The same promise as the Django case, for the shape a specialist emits.

    `analyze_all.py` reports `categories`, not a bare list, and that branch did
    not split on `kind` — so python-code-doctor's leads were graded as defects.
    A dynamic-table PRAGMA is the sharpest example: PRAGMA accepts no bound
    parameters, so interpolation is the only way to write one, and grading it as
    SQL injection charges the score for a query that cannot be written any other
    way.
    """
    repo.write("app/db.py",
               "import sqlite3\n\n\n"
               "def describe(conn, table):\n"
               '    return conn.execute(f"PRAGMA table_info({table})").fetchall()\n')
    repo.commit()

    raw = tmp_path / "python.json"
    raw.write_text(run_script(PYTHON_ANALYZER, repo.path, "--format", "json",
                              "--skip-duplicates").stdout, encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")

    def build(report: Path, name: str) -> str:
        out = tmp_path / name
        run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app",
                   "--root-dir", "app", "--findings", f"python-code-doctor:{report}",
                   "--covers", "complexity")
        return out.read_text(encoding="utf-8")

    with_candidate = build(raw, "with.html")
    without_candidate = build(empty, "without.html")

    panel = candidates_panel(with_candidate)
    assert "sql_injection" in panel
    assert health_meta(with_candidate)["score"] == health_meta(without_candidate)["score"]


def test_a_candidates_location_is_repo_relative_like_a_findings(repo, run_script, tmp_path):
    """`top_findings` relativizes; the Candidates tab did not.

    A doctor is run with an absolute path and echoes it back, so the location
    column filled up with the path of the machine that generated the document —
    in a document meant to be committed.
    """
    repo.write("app/db.py",
               "import sqlite3\n\n\n"
               "def describe(conn, table):\n"
               '    return conn.execute(f"PRAGMA table_info({table})").fetchall()\n')
    repo.commit()

    raw = tmp_path / "python.json"
    raw.write_text(run_script(PYTHON_ANALYZER, repo.path, "--format", "json",
                              "--skip-duplicates").stdout, encoding="utf-8")
    out = tmp_path / "health.html"
    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app",
               "--root-dir", "app", "--findings", f"python-code-doctor:{raw}",
               "--covers", "complexity")

    panel = candidates_panel(out.read_text(encoding="utf-8"))
    locations = re.findall(r'<code class="floc">([^<]+)</code>', panel)
    assert locations, "the fixture raises at least one candidate"
    assert all(location.startswith("app/") for location in locations), locations
    assert str(repo.path) not in panel
