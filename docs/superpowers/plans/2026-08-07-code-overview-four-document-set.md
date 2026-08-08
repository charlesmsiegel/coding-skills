# code-overview Four-Document Set Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn code-overview's three-document set into four — summary, code map, health, measurement — with one shell forced on every generator, health rebuilt around code-doctor's merged envelope and its finding/candidate split, and navigation and roll-up extended to the new type.

**Architecture:** code-overview stops choosing doctors. It calls code-doctor once, reads `doctors_run` and `completeness` out of the merged envelope as coverage *evidence*, and grades from that. Its `assets/template.html` becomes the canonical tabbed shell, forced on code-visualization's `assemble.py` and science-investigation's `build_measurement.py` via `--template`, with a CI ratchet pinning the design tokens and letter bands so the four documents cannot drift apart.

**Tech Stack:** Python 3.11+, stdlib only. pytest driving the CLIs as subprocesses.

## Global Constraints

- **Python 3.11+, stdlib only, no network calls.** Nested same-type quotes and backslashes inside f-string expressions are 3.12+ syntax — precompute into a local instead.
- **Source of truth:** `docs/superpowers/specs/2026-08-07-code-overview-companions-design.md`.
- **Prerequisites:** Plan 1 (`2026-08-07-code-doctor-routing.md`) for the merged envelope, Plan 2 (`2026-08-07-science-investigation-measurement-document.md`) for `build_measurement.py` and the science template. Tasks 1, 5 and 6 do not need either and may land first.
- **Candidates never enter a score.** They assert no defect and carry no fix; penalising them grades a repo on its unresolved leads.
- **Ungraded is not zero and not a hundred.** A category nothing measured is dropped from the mean and named on the page.
- **code-doctor alone never claims Correctness.** A merge marker in a git-confirmed unmerged path is the only correctness-class defect its raw layer can prove.
- **Every document is generated, never hand-edited.**
- **No dangling links.** `inject_nav.py --check` exits 0 or the set is not done.
- **CI gates:** `ruff check .`, `python tools/validate_skills.py`, `pytest -q`, the bug-class detector ratchet, and the new shared-asset ratchet from Task 1.

---

### Task 1: The canonical shell and the drift ratchet

**Files:**
- Modify: `skills/code-overview/assets/template.html`
- Create: `skills/code-overview/assets/measurement-body.html`
- Create: `tests/test_shared_assets.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `skills/code-visualization/assets/template.html` (the token source), `skills/science-investigation/assets/measurement-body.html` (Plan 2 Task 2) and `skills/science-investigation/scripts/rubric.py` (Plan 2 Task 1) as the copies to pin.
- Produces: a shell exposing **both** slot families — `DOC_NAV` / `DOC_BODY` for untabbed pages, and `TABS_NAV` / `TABS_PANELS` for code-visualization's contract — so one file serves all four documents.

- [ ] **Step 1: Write the failing test**

Create `tests/test_shared_assets.py`:

```python
"""What must stay identical across skills for the document set to read as one.

Each skill directory is zipped and installed on its own, so shared material is
copied rather than imported. The repo's answer to that is not trust, it is this
kind of test — the same one CI already applies to code-visualization and
pr-visualization's shared scripts.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"

TEMPLATES = (
    SKILLS / "code-overview" / "assets" / "template.html",
    SKILLS / "code-visualization" / "assets" / "template.html",
    SKILLS / "science-investigation" / "assets" / "template.html",
)


def token_blocks(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    root = re.search(r":root\{.*?\n\}", text, re.S)
    light = re.search(r"@media \(prefers-color-scheme: light\)\{.*?\n\s*\}\n\}", text, re.S)
    assert root and light, f"{path.name} is missing its design-token blocks"
    return root.group(0) + "\n" + light.group(0)


def test_every_template_shares_one_palette():
    canonical = token_blocks(TEMPLATES[0])

    for path in TEMPLATES[1:]:
        assert token_blocks(path) == canonical, (
            f"{path.relative_to(ROOT)} has drifted from code-overview's tokens. The four "
            "documents in an overview sit in one nav bar; a divergent palette is visible "
            "the moment two of them are opened side by side."
        )


def test_the_shell_carries_both_slot_families():
    text = TEMPLATES[0].read_text(encoding="utf-8")

    for slot in ("<!--DOC_TITLE-->", "<!--DOC_LABEL-->", "<!--DOC_SUBTITLE-->",
                 "<!--DOC_META-->", "<!--DOC_NAV-->", "<!--DOC_BODY-->",
                 "<!--DOC_FOOTER-->", "<!--TABS_NAV-->", "<!--TABS_PANELS-->"):
        assert slot in text, f"{slot} missing — one shell must serve tabbed and untabbed pages"


def test_an_untabbed_page_hides_the_empty_tab_bar():
    text = TEMPLATES[0].read_text(encoding="utf-8")

    assert "nav.tabs:empty" in text, (
        "summary.html leaves TABS_NAV empty; without this rule the bar renders as a "
        "stray line under the header"
    )


def test_the_measurement_body_scaffold_is_identical_in_both_skills():
    ours = SKILLS / "code-overview" / "assets" / "measurement-body.html"
    theirs = SKILLS / "science-investigation" / "assets" / "measurement-body.html"

    assert ours.read_bytes() == theirs.read_bytes(), (
        "code-overview forces this scaffold on build_measurement.py; if the copies differ, "
        "the page it forces is not the page the skill was tested against"
    )


def test_the_letter_bands_are_identical_in_both_rubrics():
    def bands(path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        match = re.search(r"GRADE_BANDS[^=]*=\s*\((.*?)\n\)", text, re.S)
        assert match, f"{path} has no GRADE_BANDS block"
        return re.sub(r"\s+", "", match.group(1))

    overview = bands(SKILLS / "code-overview" / "scripts" / "rubric.py")
    science = bands(SKILLS / "science-investigation" / "scripts" / "rubric.py")

    assert overview == science, (
        "a B- must mean one score range. The two pages sit side by side in the nav and a "
        "reader compares the letters directly."
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_shared_assets.py -q`
Expected: FAIL — code-overview's template has no `TABS_NAV`, and `measurement-body.html` does not exist there.

- [ ] **Step 3: Add the tab machinery to code-overview's shell**

Edit `skills/code-overview/assets/template.html`. **Add** to the `<style>` block, immediately after the `@media (prefers-reduced-motion: reduce)` line:

```css
/* ---------- tabs: code-visualization's contract, so one shell serves all four
   documents. code-overview forces this template on assemble.py and on
   build_measurement.py, so the markup those two emit has to fit here. ------- */
nav.tabs{
  display:flex;gap:2px;flex-wrap:wrap;max-width:1280px;margin:18px auto 0;
  padding:0 28px;border-bottom:1px solid var(--border);
}
/* summary.html is untabbed and leaves TABS_NAV empty; without this the bar
   renders as a stray rule under the header. */
nav.tabs:empty{display:none;border:none}
nav.tabs button{
  background:none;border:none;border-bottom:2px solid transparent;color:var(--text-dim);
  font-family:var(--sans);font-size:14px;padding:9px 14px;cursor:pointer;
}
nav.tabs button:hover{color:var(--text)}
nav.tabs button[aria-selected="true"]{color:var(--accent);border-bottom-color:var(--accent)}
nav.tabs button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
section.panel{display:none}
section.panel.active{display:block}
```

Add `nav.tabs` to the existing mobile padding rule so it matches the others:

```css
@media(max-width:720px){header.doc,main,nav.tabs,footer.doc{padding-left:16px;padding-right:16px} h1.doc-title{font-size:24px} .gradecard .letter{font-size:48px;min-width:72px}}
```

**Replace** the body region (from `<!--DOC_NAV-->` through `</footer>`) with:

```html
<!--DOC_NAV-->
<nav class="tabs" role="tablist"><!--TABS_NAV--></nav>
<main><!--TABS_PANELS--><!--DOC_BODY--></main>
<footer class="doc"><!--DOC_FOOTER--></footer>
<script>
(function(){
  const btns=[...document.querySelectorAll('nav.tabs button')];
  function activate(id){
    document.querySelectorAll('section.panel').forEach(p=>p.classList.toggle('active',p.id===id));
    btns.forEach(b=>b.setAttribute('aria-selected',b.dataset.tab===id?'true':'false'));
    window.dispatchEvent(new CustomEvent('tab:shown',{detail:{id}}));
  }
  btns.forEach(b=>b.addEventListener('click',()=>activate(b.dataset.tab)));
})();
</script>
```

`common.render` blanks any placeholder it is not given a value for, so
`build_summary.py` and today's `build_health.py` keep working unchanged: their
`TABS_NAV` and `TABS_PANELS` come out empty and the bar hides itself.

- [ ] **Step 4: Copy the measurement scaffold**

```bash
cp skills/science-investigation/assets/measurement-body.html \
   skills/code-overview/assets/measurement-body.html
```

- [ ] **Step 5: Run the tests**

```bash
pytest tests/test_shared_assets.py tests/code_overview -q
```

Expected: PASS. The `code_overview` suite is included because this task edits the shell every existing document renders through — a regression there is the thing most likely to go unnoticed.

- [ ] **Step 6: Add the CI step**

In `.github/workflows/ci.yml`, after the *django-code-doctor's shared files match* step, insert:

```yaml
      # The four documents in a code overview sit in one nav bar. Their design
      # tokens and letter bands are copied between skills (each skill directory
      # has to be installable alone), so the copies are pinned here rather than
      # trusted to stay aligned.
      - name: Document tokens and grade bands stay aligned across skills
        run: pytest tests/test_shared_assets.py -q
```

- [ ] **Step 7: Commit**

```bash
git add skills/code-overview/assets tests/test_shared_assets.py .github/workflows/ci.yml
git commit -m "code-overview: one tabbed shell for all four documents, pinned by CI

The shell now carries both slot families, so it serves an untabbed summary and
a tabbed code map from one file and can be forced on assemble.py and
build_measurement.py. The palette and the letter bands are copied between
skills because each skill directory installs alone; the ratchet makes those
copies an enforced invariant rather than a comment promising one.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: The rubric learns code-doctor

**Files:**
- Modify: `skills/code-overview/scripts/rubric.py`
- Test: `tests/code_overview/test_rubric.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `DETECTOR_CATEGORIES` gains code-doctor's smell types (mapped by name, as django's are)
  - `DOCTOR_COVERAGE["code-doctor"]` — every category **except** `correctness`
  - `COMPLETENESS_GATES: dict[str, str]` — completeness key → the rubric category it gates
  - `ungraded_from_completeness(completeness: dict) -> set[str]` — categories to drop because the evidence behind them was incomplete

- [ ] **Step 1: Write the failing test**

Append to `tests/code_overview/test_rubric.py`:

```python
# --- code-doctor -----------------------------------------------------------

CODE_DOCTOR_TYPES = (
    "private_key_material", "cloud_credential", "hardcoded_secret_assignment",
    "committed_env_file", "merge_conflict_marker", "oversized_file", "oversized_line",
    "todo_inventory", "commented_out_code", "large_committed_binary",
)


@pytest.mark.parametrize("smell_type", CODE_DOCTOR_TYPES)
def test_every_shipped_code_doctor_type_is_mapped_by_name(rubric, smell_type):
    assert smell_type in rubric.SMELL_TYPE_CATEGORIES, (
        f"{smell_type} would fall through to keyword matching or the fallback category, "
        "where a mis-homed finding is invisible until someone reads unmapped_types"
    )


def test_secrets_and_committed_env_files_are_security(rubric):
    assert rubric.categorize({"smell_type": "private_key_material"})[0] == "security"
    assert rubric.categorize({"smell_type": "committed_env_file"})[0] == "security"


def test_a_confirmed_merge_marker_is_correctness(rubric):
    assert rubric.categorize({"smell_type": "merge_conflict_marker"})[0] == "correctness"


def test_oversized_files_are_complexity_not_hygiene(rubric):
    assert rubric.categorize({"smell_type": "oversized_file"})[0] == "complexity"


def test_code_doctor_alone_does_not_claim_correctness(rubric):
    covered = rubric.DOCTOR_COVERAGE["code-doctor"]

    assert "correctness" not in covered, (
        "a merge marker is the only correctness-class defect the raw layer can prove; "
        "crediting the category on that is an A+ from silence"
    )
    assert {"security", "complexity", "duplication", "hygiene", "tests", "design"} <= covered


def test_a_thin_reference_graph_ungrades_design(rubric):
    dropped = rubric.ungraded_from_completeness(
        {"reference_graph": {"adequate": False, "resolution_rate": 0.31}})

    assert "design" in dropped


def test_an_adequate_reference_graph_grades_design(rubric):
    dropped = rubric.ungraded_from_completeness(
        {"reference_graph": {"adequate": True, "resolution_rate": 0.88}})

    assert "design" not in dropped


def test_inconclusive_test_classification_ungrades_tests(rubric):
    dropped = rubric.ungraded_from_completeness(
        {"test_classification": {"adequate": False,
                                 "inconclusive_dirs": ["src/rust-core"]}})

    assert "tests" in dropped


def test_completeness_this_rubric_does_not_recognise_ungrades_nothing(rubric):
    assert rubric.ungraded_from_completeness({"weather": {"adequate": False}}) == set()


def test_an_absent_adequacy_verdict_is_not_read_as_a_failure(rubric):
    # The threshold is code-doctor's to set and report. A missing verdict means
    # the detector said nothing, which is not the same as saying "inadequate" —
    # inventing a cutoff here would silently disagree with the skill that
    # measured it.
    assert rubric.ungraded_from_completeness({"reference_graph": {"resolution_rate": 0.4}}) == set()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/code_overview/test_rubric.py -q`
Expected: FAIL — `ungraded_from_completeness` does not exist and `DOCTOR_COVERAGE` has no `code-doctor` entry.

- [ ] **Step 3: Extend the rubric**

In `skills/code-overview/scripts/rubric.py`, add a `code-doctor` block to `SMELL_TYPES` (it is mapped by name, exactly as django's is — code-doctor emits a flat list with no `category` field):

```python
# code-doctor's own detectors. Language-agnostic and heuristic, which is why so
# few of them land in Correctness: the raw layer can prove a merge marker in a
# path git reports as unmerged, and very little else about whether code computes
# the right answer.
CODE_DOCTOR_SMELLS = {
    "correctness": (
        # Only the git-confirmed unmerged form is a finding; the same marker in
        # a doc example or a conflict-handling fixture is a candidate, and
        # candidates are never scored.
        "merge_conflict_marker",
    ),
    "security": (
        "private_key_material", "cloud_credential", "hardcoded_secret_assignment",
        "committed_env_file",
    ),
    "complexity": (
        "oversized_file", "oversized_line", "decision_density", "nesting_depth",
        "long_function", "high_arity",
    ),
    "duplication": (
        "exact_duplicate", "near_duplicate", "zero_inbound_file",
        "dead_function_candidate", "commented_out_code",
    ),
    "design": (
        "import_cycle", "god_module", "low_directory_cohesion", "change_coupling",
        "changes_with_everything",
    ),
    "tests": (
        "untested_directory", "low_test_ratio", "test_asserts_nothing",
    ),
    "hygiene": (
        "todo_inventory", "large_committed_binary", "single_author_file",
        "departed_author", "hotspot",
    ),
}
```

Then merge it into the existing lookup, immediately after `SMELL_TYPE_CATEGORIES` is built:

```python
SMELL_TYPE_CATEGORIES.update({
    smell: category
    for category, smells in CODE_DOCTOR_SMELLS.items()
    for smell in smells
})
```

Add code-doctor to `DOCTOR_COVERAGE`:

```python
    # code-doctor buys language-independence by giving up parsing. It can prove
    # a merge marker in a path git reports as unmerged and essentially nothing
    # else in the correctness class, so the category is left to a specialist
    # rather than credited from silence. Everything else it genuinely measures.
    "code-doctor": set(CATEGORY_KEYS) - {"correctness"},
```

Add the completeness gates at the end of the file:

```python
# A detector that reported its own evidence incomplete has not measured its
# category, and zero findings from an incomplete look means *unknown*, not
# *clean*. The adequacy verdict is the producing skill's to make: only the
# detector knows what resolution rate its own edges need, and a grader
# inventing a cutoff would silently disagree with the thing that measured it.
COMPLETENESS_GATES: dict[str, str] = {
    "reference_graph": "design",
    "test_classification": "tests",
    "history": "hygiene",
}


def ungraded_from_completeness(completeness: dict | None) -> set[str]:
    """Categories to drop because the evidence behind them was incomplete."""
    dropped: set[str] = set()
    for key, category in COMPLETENESS_GATES.items():
        block = (completeness or {}).get(key)
        if isinstance(block, dict) and block.get("adequate") is False:
            dropped.add(category)
    return dropped
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/code_overview/test_rubric.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/code-overview/scripts/rubric.py tests/code_overview/test_rubric.py
git commit -m "code-overview: map code-doctor's findings, and refuse its Correctness

code-doctor covers six of seven categories. It does not cover Correctness: a
merge marker in a git-confirmed unmerged path is the only correctness-class
defect its raw layer can prove, and crediting the whole category on that is
the 'empty findings list from a doctor that parsed nothing is an A+' lie in a
new costume. Design and Tests are gated the same way, on the adequacy verdict
the producing detector reports — the threshold is its to set, not the grader's
to invent.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `build_health.py` reads the merged envelope

**Files:**
- Modify: `skills/code-overview/scripts/common.py`
- Modify: `skills/code-overview/scripts/build_health.py`
- Test: `tests/code_overview/test_merged_envelope.py`

**Interfaces:**
- Consumes: `code-doctor-merge/1` from Plan 1 Task 2, and `rubric.ungraded_from_completeness` from Task 2.
- Produces:
  - `common.load_merged(path: Path) -> dict` — `{"reports": [...], "candidates": [...], "completeness": {...}, "doctor_errors": {...}, "doctors": [...], "coverage_unknown": [...]}`, where each report is the existing `normalize_findings` record shape with `doctor` filled in
  - `build_health.py --merged FILE` — accepted alongside `--findings`; supplies doctors, coverage, candidates and completeness in one argument
  - metadata gains `candidates_total`, `doctor_errors`, `completeness`, `doctors`

- [ ] **Step 1: Write the failing test**

Create `tests/code_overview/test_merged_envelope.py`:

```python
"""Grading from code-doctor's merged envelope.

The envelope replaces three things code-overview used to be told: which doctors
ran, what they covered, and which records are defects. Each of those was a place
a wrong answer produced a confident grade, so each gets a test about the failure
rather than the success.
"""

import json
import re
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "code-overview" /
          "scripts" / "build_health.py")


def meta_of(page: Path) -> dict:
    match = re.search(r'id="code-health-meta">(.*?)</script>',
                      page.read_text(encoding="utf-8"), re.S)
    assert match
    return json.loads(match.group(1).replace("<\\/", "</"))


def envelope(tmp_path, **overrides) -> Path:
    payload = {
        "schema": "code-doctor-merge/1",
        "doctors_run": ["code-doctor", "python-code-doctor"],
        "analyzers_run": {"code-doctor": ["hygiene", "secrets"],
                          "python-code-doctor": ["find_security_issues"]},
        "analyzers_skipped": {"code-doctor": [], "python-code-doctor": []},
        "analyzer_errors": {},
        "doctor_errors": {},
        "completeness": {},
        "coverage_unknown": [],
        "findings": [
            {"doctor": "code-doctor", "file": "src/app/settings.py", "line": 4,
             "smell_type": "hardcoded_secret_assignment", "severity": "high",
             "description": "SECRET_KEY assigned a literal", "suggestion": "read it from env",
             "kind": "finding"},
        ],
        "candidates": [
            {"doctor": "code-doctor", "file": "src/app/legacy.py", "line": 12,
             "smell_type": "dead_function_candidate", "severity": "medium",
             "description": "identifier occurs once in the tree", "kind": "candidate",
             "also_caused_by": ["a library's public surface has no internal referrer"]},
        ],
    }
    payload.update(overrides)
    path = tmp_path / "merged.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def build(repo, run_script, tmp_path, merged: Path, *extra, expect_rc=0) -> Path:
    out = tmp_path / "health.html"
    run_script(SCRIPT, "--out", out, "--repo", repo.path, "--name", "app",
               "--root-dir", "src/app", "--merged", merged, *extra, expect_rc=expect_rc)
    return out


@pytest.fixture
def repo_with_code(repo):
    for i in range(12):
        repo.write(f"src/app/mod{i}.py", "def f():\n    return 1\n" * 20)
    repo.commit()
    return repo


def test_candidates_are_counted_but_never_scored(repo_with_code, run_script, tmp_path):
    with_candidates = meta_of(build(repo_with_code, run_script, tmp_path,
                                    envelope(tmp_path)))

    without = meta_of(build(repo_with_code, run_script, tmp_path,
                            envelope(tmp_path, candidates=[])))

    assert with_candidates["candidates_total"] == 1
    assert without["candidates_total"] == 0
    assert with_candidates["score"] == pytest.approx(without["score"]), (
        "a candidate asserts no defect; charging one to the grade punishes honesty"
    )


def test_coverage_comes_from_doctors_run_not_a_flag(repo_with_code, run_script, tmp_path):
    meta = meta_of(build(repo_with_code, run_script, tmp_path, envelope(tmp_path)))

    assert meta["doctors"] == ["code-doctor", "python-code-doctor"]
    assert "correctness" not in meta["ungraded"], (
        "python-code-doctor ran, so Correctness is measured"
    )


def test_code_doctor_alone_leaves_correctness_ungraded(repo_with_code, run_script, tmp_path):
    solo = envelope(tmp_path, doctors_run=["code-doctor"],
                    analyzers_run={"code-doctor": ["hygiene", "secrets"]})

    meta = meta_of(build(repo_with_code, run_script, tmp_path, solo))

    assert "correctness" in meta["ungraded"]


def test_a_failed_doctor_ungrades_what_it_covered(repo_with_code, run_script, tmp_path):
    broken = envelope(tmp_path, doctors_run=["code-doctor"],
                      analyzers_run={"code-doctor": ["hygiene"]},
                      doctor_errors={"python-code-doctor": "crashed reading settings"})

    meta = meta_of(build(repo_with_code, run_script, tmp_path, broken))

    assert meta["doctor_errors"] == {"python-code-doctor": "crashed reading settings"}
    assert "correctness" in meta["ungraded"], (
        "the surviving report must not score categories the failed doctor was measuring"
    )


def test_an_inadequate_reference_graph_ungrades_design(repo_with_code, run_script, tmp_path):
    thin = envelope(tmp_path, completeness={
        "code-doctor": {"reference_graph": {"adequate": False, "resolution_rate": 0.3}}})

    meta = meta_of(build(repo_with_code, run_script, tmp_path, thin))

    assert "design" in meta["ungraded"]
    assert meta["completeness"]["code-doctor"]["reference_graph"]["resolution_rate"] == 0.3


def test_a_doctor_with_no_coverage_evidence_grants_nothing(repo_with_code, run_script, tmp_path):
    unknown = envelope(tmp_path, doctors_run=["django-code-doctor"],
                       analyzers_run={"django-code-doctor": []},
                       coverage_unknown=["django-code-doctor"])

    meta = meta_of(build(repo_with_code, run_script, tmp_path, unknown))

    assert meta["score"] is None, "a bare list says nothing about what was examined"


def test_an_envelope_with_no_doctors_grades_nothing(repo_with_code, run_script, tmp_path):
    empty = envelope(tmp_path, doctors_run=[], analyzers_run={}, findings=[],
                     candidates=[], doctor_errors={"code-doctor": "empty report"})

    meta = meta_of(build(repo_with_code, run_script, tmp_path, empty))

    assert meta["score"] is None
    assert meta["grade"] == "—"


def test_out_of_scope_candidates_are_dropped_with_the_findings(repo_with_code, run_script,
                                                               tmp_path):
    elsewhere = envelope(tmp_path, candidates=[
        {"doctor": "code-doctor", "file": "src/other/x.py", "line": 1,
         "smell_type": "zero_inbound_file", "severity": "low", "kind": "candidate",
         "description": "no inbound edges", "also_caused_by": ["an entry point"]}])

    meta = meta_of(build(repo_with_code, run_script, tmp_path, elsewhere))

    assert meta["candidates_total"] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/code_overview/test_merged_envelope.py -q`
Expected: FAIL — `unrecognized arguments: --merged`.

- [ ] **Step 3: Add `load_merged` to `common.py`**

Append to `skills/code-overview/scripts/common.py`:

```python
MERGE_SCHEMA = "code-doctor-merge/1"


def load_merged(path) -> dict:
    """Unpack code-doctor's merged envelope into this skill's report records.

    The envelope replaces three things this skill used to be told by flag: which
    doctors ran, what they covered, and which records assert a defect. Every one
    of those was a place where a wrong answer produced a *confident* grade, so
    each is read as evidence here rather than declared.
    """
    path = Path(path)
    blank = {"reports": [], "candidates": [], "completeness": {}, "doctor_errors": {},
             "doctors": [], "coverage_unknown": []}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        warn(f"{path}: {exc} — nothing from this envelope is graded")
        return blank
    if not text.strip():
        # Identical to the zero-byte findings rule: a doctor that produced no
        # output failed, and failure is not a clean bill of health.
        warn(f"{path} is empty — a merge that produced nothing failed; nothing is graded")
        return blank
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        warn(f"{path} is not valid JSON: {exc} — nothing from this envelope is graded")
        return blank
    if not isinstance(data, dict) or data.get("schema") != MERGE_SCHEMA:
        warn(f"{path} is not a {MERGE_SCHEMA} envelope — nothing from it is graded")
        return blank

    doctors = [str(name) for name in (data.get("doctors_run") or [])]
    analyzers = data.get("analyzers_run") or {}
    skipped = data.get("analyzers_skipped") or {}
    errors = data.get("analyzer_errors") or {}
    unknown = {str(name) for name in (data.get("coverage_unknown") or [])}

    by_doctor: dict[str, dict] = {}
    for doctor in doctors:
        ran = {str(name) for name in (analyzers.get(doctor) or [])}
        by_doctor[doctor] = {
            "findings": [],
            "errors": {str(k): str(v) for k, v in (errors.get(doctor) or {}).items()},
            "ran": ran,
            "skipped": {str(name) for name in (skipped.get(doctor) or [])},
            # A doctor listed with no analyzers_run evidence is exactly the bare
            # list case: nothing in it distinguishes a full run from one detector
            # that found nothing, so it grants no coverage profile.
            "shape": SHAPE_PARTIAL if (doctor in unknown or not ran) else SHAPE_FULL,
            "empty_artifact": False,
            "doctor": doctor,
        }

    for record in data.get("findings") or []:
        if isinstance(record, dict):
            record["severity"] = normalize_severity(record.get("severity"))
            report = by_doctor.get(str(record.get("doctor")))
            if report is not None:
                report["findings"].append(record)

    candidates = [record for record in (data.get("candidates") or [])
                  if isinstance(record, dict)]
    for record in candidates:
        record["severity"] = normalize_severity(record.get("severity"))

    return {
        "reports": list(by_doctor.values()),
        "candidates": candidates,
        "completeness": data.get("completeness") or {},
        "doctor_errors": {str(k): str(v) for k, v in (data.get("doctor_errors") or {}).items()},
        "doctors": doctors,
        "coverage_unknown": sorted(unknown),
    }
```

- [ ] **Step 4: Wire it into `build_health.py`**

Add the argument beside `--findings`:

```python
    parser.add_argument("--merged", type=Path, default=None,
                        help="code-doctor's merged envelope (code-doctor-merge/1); "
                             "supplies doctors, coverage, candidates and completeness at once")
```

After the existing reports are loaded, fold the envelope in:

```python
    merged = common.load_merged(args.merged) if args.merged else None
    candidates: list[dict] = []
    doctor_errors: dict[str, str] = {}
    completeness: dict = {}
    doctors: list[str] = []
    if merged:
        reports.extend(merged["reports"])
        candidates = merged["candidates"]
        doctor_errors = merged["doctor_errors"]
        completeness = merged["completeness"]
        doctors = merged["doctors"]
```

Scope the candidates with the findings, using the same predicate — the block
that computes `kept` for each report already defines `in_scope`:

```python
    candidates = [record for record in candidates if in_scope(record)]
```

Compute the covered set from the doctors that actually ran, and subtract what
the evidence says was not measured. Replace the existing `covered` derivation
(the `rubric.DOCTOR_COVERAGE` lookup keyed on `args.doctor`) with:

```python
    if doctors:
        # Coverage is the union of what the doctors that ran can speak to —
        # read from the envelope, not declared by --doctor. A doctor with no
        # analyzers_run evidence contributes nothing, which is why `shape`
        # gates it here rather than the doctor's name doing so.
        covered = set()
        for report in reports:
            if report["shape"] == common.SHAPE_FULL:
                covered |= rubric.DOCTOR_COVERAGE.get(report["doctor"], set())

    # A doctor that crashed measured nothing. Only the categories it *alone*
    # covered become unknown — where a surviving doctor covers the same ground,
    # that ground was still measured.
    surviving: set[str] = set()
    for name in doctors:
        surviving |= rubric.DOCTOR_COVERAGE.get(name, set())
    for failed in doctor_errors:
        covered -= rubric.DOCTOR_COVERAGE.get(failed, set()) - surviving

    for block in completeness.values():
        covered -= rubric.ungraded_from_completeness(block)
```

Add the new metadata fields beside the existing ones:

```python
        "doctors": doctors,
        "doctor_errors": doctor_errors,
        "completeness": completeness,
        "candidates_total": len(candidates),
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
pytest tests/code_overview -q
```

Expected: PASS, including the pre-existing `test_build_health.py` — `--findings`
must keep working exactly as before for hand-assembled input.

- [ ] **Step 6: Commit**

```bash
git add skills/code-overview/scripts/common.py skills/code-overview/scripts/build_health.py tests/code_overview/test_merged_envelope.py
git commit -m "code-overview: grade from code-doctor's merged envelope

Which doctors ran, what they covered, and which records assert a defect were
all things this skill used to be told by flag, and each was a place a wrong
answer produced a confident grade. They are now read as evidence: coverage
from doctors_run, defects from the findings list with candidates held out of
the arithmetic entirely, and a failed doctor's exclusive categories dropped
rather than scored from the survivor's silence.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `health.html` grows its four tabs

**Files:**
- Modify: `skills/code-overview/assets/health-body.html`
- Modify: `skills/code-overview/scripts/build_health.py`
- Test: `tests/code_overview/test_health_tabs.py`

**Interfaces:**
- Consumes: Task 1's shell, Task 3's candidates and completeness.
- Produces: `render_candidates(candidates: list[dict]) -> str`, `render_coverage(meta: dict) -> str`, and `panels(fragments: list[str]) -> tuple[str, str]` — the same tab-markup helper `build_measurement.py` uses.

- [ ] **Step 1: Write the failing test**

Create `tests/code_overview/test_health_tabs.py`:

```python
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


def envelope(tmp_path) -> Path:
    payload = {
        "schema": "code-doctor-merge/1",
        "doctors_run": ["code-doctor"],
        "analyzers_run": {"code-doctor": ["hygiene", "secrets"]},
        "analyzers_skipped": {"code-doctor": ["duplication"]},
        "analyzer_errors": {},
        "doctor_errors": {"django-code-doctor": "crashed reading settings"},
        "completeness": {"code-doctor": {"reference_graph": {"adequate": False,
                                                             "resolution_rate": 0.31}}},
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


def test_all_four_tabs_are_present(page):
    for title in ("Grade", "Findings", "Candidates", "Coverage"):
        assert f">{title}<" in page, f"the {title} tab is missing"


def test_the_grade_tab_is_the_one_that_opens(page):
    first = re.search(r'<section class="panel active" id="([^"]+)"', page)
    assert first and first.group(1) == "tab-grade"


def test_the_candidates_tab_says_it_did_not_affect_the_grade(page):
    assert "did not affect the grade" in page.lower()


def test_a_candidate_carries_the_benign_explanations(page):
    assert "convention-loaded plugins are never named" in page


def test_the_coverage_tab_names_the_failed_doctor(page):
    assert "django-code-doctor" in page
    assert "crashed reading settings" in page


def test_the_coverage_tab_reports_the_thin_graph_rather_than_hiding_it(page):
    assert "0.31" in page or "31" in page
    assert "design" in page.lower()


def test_the_coverage_tab_lists_the_skipped_analyzer(page):
    assert "duplication" in page.lower()


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/code_overview/test_health_tabs.py -q`
Expected: FAIL — the page has no tabs.

- [ ] **Step 3: Restructure `health-body.html` into four fragments**

Replace `skills/code-overview/assets/health-body.html` with:

```html
<!-- Body layout for health.html, filled by build_health.py. Four tabs, and the
     split between the first two and the third is the point: a finding asserts a
     defect and moves the grade, a candidate is an unverified lead that moves
     nothing. A page that mixes them is how a reader concludes a dead-function
     candidate is a dead function. -->
<!-- tab: Grade -->
<section class="gradecard <!--GRADE_CLASS-->">
  <div>
    <div class="letter"><!--GRADE--></div>
    <div class="score"><!--SCORE--> / 100</div>
  </div>
  <div class="what">
    <h2><!--SUBJECT--></h2>
    <p class="dim"><!--SUBJECT_DETAIL--></p>
    <div><!--HEADLINE_BADGES--></div>
  </div>
</section>
<!--UNGRADED_NOTE-->
<div class="catrows"><!--CATEGORY_ROWS--></div>
<!--PACKAGE_TABLE-->

<!-- tab: Findings -->
<!--FINDINGS_SUMMARY-->
<!--TOP_FINDINGS-->
<!--BY_TYPE-->

<!-- tab: Candidates -->
<!--CANDIDATES-->

<!-- tab: Coverage -->
<!--COVERAGE-->
<!--CAVEATS-->
```

- [ ] **Step 4: Add the two renderers and the tab assembly**

Add to `skills/code-overview/scripts/build_health.py`:

```python
def render_candidates(candidates: list[dict]) -> str:
    """Leads, rendered so nobody mistakes one for a defect.

    A candidate carries no fix and asserts nothing. The two things that keep it
    honest on a page whose headline is a grade are the statement that it did not
    affect that grade, and `also_caused_by` — the specific ways a healthy
    codebase produces the same observation, so the reader can rule them out
    instead of taking the tool's word for it.
    """
    note = ('<div class="callout warn"><strong>These did not affect the grade.</strong> '
            "A candidate is an unverified lead, not a defect: it names what was observed "
            "and the ways healthy code produces the same observation, and it deliberately "
            "carries no fix. Confirm one before acting on it.</div>")
    if not candidates:
        return (note + '<p class="dim">No candidates were reported for this unit.</p>')

    rows = []
    for item in sorted(candidates, key=lambda c: (SEVERITY_ORDER.index(c.get("severity", "medium"))
                                                  if c.get("severity") in SEVERITY_ORDER else 1,
                                                  str(c.get("file")), line_number(c))):
        benign = "".join(f"<li>{esc(reason)}</li>"
                         for reason in item.get("also_caused_by") or [])
        location = f'{item.get("file", "")}:{line_number(item)}'
        rows.append(
            f'<tr><td><code class="ftype">{esc(item.get("smell_type", "candidate"))}</code></td>'
            f'<td><code class="floc">{esc(location)}</code></td>'
            f'<td>{esc(item.get("description", ""))}'
            f'<div class="faint">Also caused by:<ul>{benign}</ul></div></td>'
            f'<td>{esc(item.get("doctor", ""))}</td></tr>'
        )
    return (note + '<div class="tbl-wrap"><table><thead><tr><th>Type</th><th>Location</th>'
            "<th>Observed · and what else produces it</th><th>Reported by</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def render_coverage(meta: dict) -> str:
    """What was measured, what was not, and why — beside the grade that used it."""
    parts = []

    doctors = meta.get("doctors") or []
    if doctors:
        chips = "".join(f'<span class="badge accent">{esc(name)}</span> ' for name in doctors)
        parts.append(f"<h3>Doctors that ran</h3><p>{chips}</p>")
    else:
        parts.append('<div class="callout bad">No doctor contributed to this page, so '
                     "nothing on it was measured. The grade is a placeholder.</div>")

    failures = meta.get("doctor_errors") or {}
    if failures:
        items = "".join(f"<li><strong>{esc(name)}</strong>: {esc(reason)}</li>"
                        for name, reason in failures.items())
        parts.append('<div class="callout bad"><strong>A doctor failed.</strong> Whatever it '
                     "alone covered is unknown, not clean — those categories are ungraded "
                     f"rather than scored from the surviving report.<ul>{items}</ul></div>")

    for doctor, block in (meta.get("completeness") or {}).items():
        rows = []
        for key, detail in (block or {}).items():
            if not isinstance(detail, dict):
                continue
            verdict = detail.get("adequate")
            state = "adequate" if verdict is True else ("incomplete" if verdict is False
                                                        else "not stated")
            numbers = ", ".join(f"{k}: {v}" for k, v in detail.items() if k != "adequate")
            rows.append(f"<tr><td>{esc(key)}</td><td>{esc(state)}</td>"
                        f"<td>{esc(numbers)}</td></tr>")
        if rows:
            parts.append(f"<h3>Evidence completeness · {esc(doctor)}</h3>"
                         '<div class="tbl-wrap"><table><thead><tr><th>Evidence</th>'
                         "<th>Verdict</th><th>Detail</th></tr></thead>"
                         f"<tbody>{''.join(rows)}</tbody></table></div>")

    ungraded = meta.get("ungraded") or []
    if ungraded:
        names = ", ".join(esc(rubric.CATEGORY_LABELS.get(key, key)) for key in ungraded)
        parts.append('<div class="callout warn"><strong>Ungraded: </strong>'
                     f"{names}. Nothing measured these, so they are dropped from the mean "
                     "rather than counted as zero or as a hundred.</div>")

    return "".join(parts)


def panels(fragments: list[str]) -> tuple[str, str]:
    """Turn `<!-- tab: Title -->` fragments into code-visualization's markup."""
    nav, sections = [], []
    for fragment in fragments:
        header, _, body = fragment.partition("\n")
        title = header.removeprefix("<!-- tab:").removesuffix("-->").strip()
        tab_id = "tab-" + title.lower().replace(" ", "-")
        selected = "true" if not nav else "false"
        active = " active" if not sections else ""
        nav.append(f'<button role="tab" data-tab="{tab_id}" aria-selected="{selected}" '
                   f'aria-controls="{tab_id}">{esc(title)}</button>')
        sections.append(f'<section class="panel{active}" id="{tab_id}" role="tabpanel">\n'
                        f"{body}\n</section>")
    return "\n".join(nav), "\n".join(sections)
```

Add the two new slots to the existing `render(read_asset("health-body.html"), {...})` call:

```python
        "CANDIDATES": render_candidates(candidates),
        "COVERAGE": render_coverage(meta),
```

and remove `"META_JSON": json_block(meta)` from that dict — the block now travels
with the panels. Then replace the final page render with:

```python
    fragments = [f"<!-- tab:{part}" for part in body.split("<!-- tab:") if part.strip()]
    nav, sections = panels(fragments)
    sections += (f'\n<script type="application/json" id="code-health-meta">'
                 f"{json_block(meta)}</script>")

    scope = "repository" if args.root else "package"
    page = render(read_asset("template.html"), {
        "DOC_TITLE": esc(f"{args.name} — Code Health"),
        "DOC_LABEL": "CODE HEALTH",
        "DOC_SUBTITLE": esc(args.subtitle or f"Graded health of the {args.name} {scope}."),
        "DOC_META": esc(" · ".join(part for part in (
            f"generated {meta['generated']}",
            meta["commit"],
            f"{size['files']} files, {size['loc']} lines",
            f"{len(findings)} findings, {len(candidates)} candidates",
            ", ".join(meta.get("doctors") or []) or args.doctor,
        ) if part)),
        "TABS_NAV": nav,
        "TABS_PANELS": sections,
        "DOC_FOOTER": ("Generated by code-overview. The grade is a density of detectable "
                       "problems, not a verdict on the design — read the code map beside "
                       "it. Candidates are leads and are excluded from the score."),
    })
```

Also delete `<!--META_JSON-->` from `health-body.html` if it survived Step 3.

- [ ] **Step 5: Run the tests**

```bash
pytest tests/code_overview -q
```

Expected: PASS, including `test_build_health.py` — its metadata assertions read
the same block, which now lives after the panels rather than inside the body.

- [ ] **Step 6: Commit**

```bash
git add skills/code-overview/assets/health-body.html skills/code-overview/scripts/build_health.py tests/code_overview/test_health_tabs.py
git commit -m "code-overview: give health.html its Candidates and Coverage tabs

The split is the point. A finding asserts a defect and moves the grade; a
candidate is an unverified lead that moves nothing and carries no fix. Mixed
into one list under a letter grade, a dead-function candidate reads as a dead
function, and the tool talks someone into deleting live code. The Candidates
tab says on its face that nothing in it touched the score, and shows the
benign explanations so a reader can rule them out.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `build_summary.py` carries two grades

**Files:**
- Modify: `skills/code-overview/assets/summary-body.html`
- Modify: `skills/code-overview/scripts/common.py`
- Modify: `skills/code-overview/scripts/build_summary.py`
- Test: `tests/code_overview/test_summary_measurement.py`

**Interfaces:**
- Consumes: `measurement/1` metadata written by Plan 2's `build_measurement.py`.
- Produces:
  - `common.read_meta(path, block_id: str = META_BLOCK_ID) -> dict | None` — the existing reader, now able to pull either block
  - `common.MEASUREMENT_BLOCK_ID = "measurement-meta"`
  - a second grade card, a `Measurement` doclink, and the repo table's measurement column plus its *no measurement content* list

- [ ] **Step 1: Write the failing test**

Create `tests/code_overview/test_summary_measurement.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/code_overview/test_summary_measurement.py -q`
Expected: FAIL — the summary knows nothing about measurement.

- [ ] **Step 3: Teach `read_meta` the second block**

In `skills/code-overview/scripts/common.py`, replace the fixed `_META_RE` usage:

```python
MEASUREMENT_BLOCK_ID = "measurement-meta"


def _meta_re(block_id: str) -> re.Pattern:
    return re.compile(r'<script[^>]*id="' + re.escape(block_id) + r'"[^>]*>(.*?)</script>',
                      re.DOTALL)


def read_meta(path, block_id: str = META_BLOCK_ID) -> dict | None:
    """Pull a generated document's metadata block back out of it.

    Both document types embed the same way and escape `</` the same way, so one
    reader serves both — which is what keeps the portal from having to be told a
    grade it could read.
    """
    path = Path(path)
    if not path.is_file():
        return None
    match = _meta_re(block_id).search(path.read_text(encoding="utf-8"))
    if not match:
        return None
    try:
        return json.loads(match.group(1).replace("<\\/", "</"))
    except json.JSONDecodeError as exc:
        warn(f"{path} has a {block_id} block that is not valid JSON: {exc}")
        return None
```

- [ ] **Step 4: Add the second card and the column**

In `skills/code-overview/assets/summary-body.html`, add after the existing
`<section class="gradecard …>` block:

```html
<!--MEASUREMENT_CARD-->
```

In `build_summary.py`, read the sibling document and render the card:

```python
def render_measurement_card(meta: dict | None) -> str:
    """The second grade, read from measurement.html rather than passed in."""
    if meta is None:
        return ""
    score = meta.get("score")
    if score is None:
        return ('<div class="callout"><strong>Measurement: no measurement content.</strong> '
                "Nothing in this unit produces a quality or accuracy number, so there is "
                "nothing to grade. That is a null, not a pass.</div>")
    grade = str(meta.get("grade", "—"))
    return (f'<section class="gradecard {grade_class(grade)}">'
            f'<div><div class="letter">{esc(grade)}</div>'
            f'<div class="score">{score:.1f} / 100</div></div>'
            '<div class="what"><h2>Measurement coverage</h2>'
            '<p class="dim">How much of what matters here is actually measured — '
            "importance-weighted measured things over measurable things. A different "
            "question from the health grade, and often a more uncomfortable one.</p>"
            "</div></section>")
```

In `build(args)`, beside the existing `meta = read_meta(health_path) or {}`, read
the sibling document and add the two slots:

```python
    measurement_path = Path(args.out).parent / "measurement.html"
    measurement = read_meta(measurement_path, common.MEASUREMENT_BLOCK_ID)
```

Add to the `render(read_asset("summary-body.html"), {...})` dict:

```python
        "MEASUREMENT_CARD": render_measurement_card(measurement),
```

and append one more entry to the `links` string built for `DOC_LINKS`, using the
same `doc_link` helper the other three use so the label and the disabled state
stay consistent:

```python
        doc_link("measurement", "measurement.html",
                 "Can the numbers this unit reports be believed? Importance-weighted "
                 "measurement coverage, with the inventory it was computed from.",
                 measurement_path.is_file()),
```

For the root table, `render_package_links` already reads each package's health
metadata at `read_meta(doc_path(repo, package, "health"))`. Read the measurement
document beside it and render the two extra columns:

```python
def measurement_cell(meta: dict | None) -> tuple[str, str]:
    """(score, state) as displayed, for one package's measurement document.

    Three outcomes, deliberately distinct: no document, a document that found
    nothing measurable, and a graded one. Collapsing the first two into a dash
    is how a package nobody audited comes to look like a package with nothing
    to audit.
    """
    if meta is None:
        return "—", "not generated"
    score = meta.get("score")
    if score is None:
        return "—", "no measurement content"
    return f"{score:.1f}", str(meta.get("grade", "—"))
```

Call it inside the per-package loop with
`read_meta(doc_path(repo, package, "measurement"), common.MEASUREMENT_BLOCK_ID)`
and add a `<th>Measurement</th>` header plus the two `<td>` cells to that
function's table.

- [ ] **Step 5: Run the tests**

```bash
pytest tests/code_overview -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add skills/code-overview/assets/summary-body.html skills/code-overview/scripts/common.py skills/code-overview/scripts/build_summary.py tests/code_overview/test_summary_measurement.py
git commit -m "code-overview: the portal carries both grades, neither passed in

Health and measurement are read back out of the documents they describe, so
the page a reader lands on cannot disagree with the pages it links to. A
package with nothing measurable shows 'no measurement content' rather than a
dash that could be mistaken for a pass.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Navigation gains the fourth type

**Files:**
- Modify: `skills/code-overview/scripts/common.py`
- Modify: `skills/code-overview/scripts/inject_nav.py`
- Test: `tests/code_overview/test_navigation.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `DOC_KINDS = ("summary", "codemap", "health", "measurement")`, matching `DOC_TITLES` and `HOME_LABELS` entries.

- [ ] **Step 1: Write the failing test**

Append to `tests/code_overview/test_navigation.py`:

```python
# --- the measurement document ---------------------------------------------

def test_the_across_row_links_all_four_documents(repo, run_script, tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    docs.mkdir(parents=True)
    for kind in ("summary", "codemap", "health", "measurement"):
        (docs / f"{kind}.html").write_text("<html><body><header></header></body></html>",
                                           encoding="utf-8")
    mapping = repo.path / "docs" / "code-overview.json"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text(json.dumps({"schema": "code-overview/1", "packages": [
        {"name": "app", "roots": ["src/app"], "docs": "src/app/docs",
         "language": "python", "doctor": "code-doctor"}]}), encoding="utf-8")

    run_script(SCRIPT, "--map", mapping, "--repo", repo.path)

    text = (docs / "summary.html").read_text(encoding="utf-8")
    assert "measurement.html" in text
    assert "Measurement" in text


def test_a_package_without_a_measurement_document_gets_no_dangling_link(repo, run_script,
                                                                        tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    docs.mkdir(parents=True)
    for kind in ("summary", "health"):
        (docs / f"{kind}.html").write_text("<html><body><header></header></body></html>",
                                           encoding="utf-8")
    mapping = repo.path / "docs" / "code-overview.json"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text(json.dumps({"schema": "code-overview/1", "packages": [
        {"name": "app", "roots": ["src/app"], "docs": "src/app/docs",
         "language": "python", "doctor": "code-doctor"}]}), encoding="utf-8")

    run_script(SCRIPT, "--map", mapping, "--repo", repo.path)

    assert 'href="measurement.html"' not in (docs / "summary.html").read_text(encoding="utf-8")


def test_check_fails_on_a_measurement_document_deleted_after_injection(repo, run_script,
                                                                        tmp_path):
    docs = repo.path / "src" / "app" / "docs"
    docs.mkdir(parents=True)
    for kind in ("summary", "measurement"):
        (docs / f"{kind}.html").write_text("<html><body><header></header></body></html>",
                                           encoding="utf-8")
    mapping = repo.path / "docs" / "code-overview.json"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text(json.dumps({"schema": "code-overview/1", "packages": [
        {"name": "app", "roots": ["src/app"], "docs": "src/app/docs",
         "language": "python", "doctor": "code-doctor"}]}), encoding="utf-8")
    run_script(SCRIPT, "--map", mapping, "--repo", repo.path)

    (docs / "measurement.html").unlink()
    result = run_script(SCRIPT, "--map", mapping, "--repo", repo.path, "--check",
                        expect_rc=1)

    assert "BROKEN LINK" in result.stderr
```

(If `SCRIPT` and `json` are not already bound at the top of that file, they are —
the existing navigation tests use both.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/code_overview/test_navigation.py -q`
Expected: FAIL — measurement is not a document kind.

- [ ] **Step 3: Add the kind**

In `skills/code-overview/scripts/common.py`:

```python
DOC_KINDS = ("summary", "codemap", "health", "measurement")
DOC_TITLES = {"summary": "Summary", "codemap": "Code Map", "health": "Health",
              "measurement": "Measurement"}
```

In `skills/code-overview/scripts/inject_nav.py`:

```python
HOME_LABELS = {"summary": "Overall Summary", "codemap": "Overall Code Map",
               "health": "Overall Health", "measurement": "Overall Measurement"}
```

Update the module docstring's "across this package's three documents" to "four".

- [ ] **Step 4: Run the tests**

```bash
pytest tests/code_overview -q
```

Expected: PASS. Everything else in `inject_nav.py` is driven by `DOC_KINDS`, so
the existence checks, the percent-encoding and `--check` extend on their own.

- [ ] **Step 5: Commit**

```bash
git add skills/code-overview/scripts/common.py skills/code-overview/scripts/inject_nav.py tests/code_overview/test_navigation.py
git commit -m "code-overview: navigate the fourth document type

Everything in inject_nav.py is driven by DOC_KINDS, so adding measurement
extends the existence checks, the percent-encoding and the --check gate on
their own. The test that matters is the deleted-document one: a measurement
page removed after injection must break --check rather than leave a nav that
looks complete.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: The workflow, the references, and an end-to-end gate

**Files:**
- Modify: `skills/code-overview/SKILL.md`
- Modify: `skills/code-overview/references/doc-layout.md`
- Modify: `skills/code-overview/references/scoring.md`
- Modify: `evals/code-overview/evals.json`
- Modify: `README.md`
- Test: `tests/code_overview/test_end_to_end.py` (append)

**Interfaces:**
- Consumes: everything above.
- Produces: no code.

- [ ] **Step 1: Write the failing test**

Append to `tests/code_overview/test_end_to_end.py`:

```python
def test_a_four_document_set_survives_the_link_gate(repo, run_script, tmp_path):
    """The whole point of the set: every document exists and every link resolves."""
    import json as _json

    docs = repo.path / "src" / "app" / "docs"
    docs.mkdir(parents=True)
    for kind in ("summary", "codemap", "health", "measurement"):
        (docs / f"{kind}.html").write_text("<html><body><header></header></body></html>",
                                           encoding="utf-8")
    root_docs = repo.path / "docs"
    root_docs.mkdir(parents=True, exist_ok=True)
    for kind in ("summary", "codemap", "health", "measurement"):
        (root_docs / f"{kind}.html").write_text("<html><body><header></header></body></html>",
                                                encoding="utf-8")
    mapping = root_docs / "code-overview.json"
    mapping.write_text(_json.dumps({"schema": "code-overview/1", "packages": [
        {"name": "app", "roots": ["src/app"], "docs": "src/app/docs",
         "language": "python", "doctor": "code-doctor"}]}), encoding="utf-8")

    inject = SCRIPTS / "inject_nav.py"
    run_script(inject, "--map", mapping, "--repo", repo.path)
    run_script(inject, "--map", mapping, "--repo", repo.path, "--check", expect_rc=0)

    text = (docs / "measurement.html").read_text(encoding="utf-8")
    assert "Overall Measurement" in text
    assert text.count("<!-- code-overview:nav -->") == 1


def test_the_skill_documents_all_four_documents():
    text = (SCRIPTS.parent / "SKILL.md").read_text(encoding="utf-8")

    assert "measurement.html" in text
    assert "build_measurement.py" in text
    assert "merge_reports.py" in text
    assert "candidates" in text.lower()
```

(`SCRIPTS` is already defined at the top of that file.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/code_overview/test_end_to_end.py -q`
Expected: the SKILL.md test FAILS; the link-gate test passes once Task 6 has landed.

- [ ] **Step 3: Rewrite the workflow in `SKILL.md`**

Replace section **2. Run the doctors once, from the repo root** with:

```markdown
## 2. Run code-doctor once, from the repo root

**One doctor call.** code-doctor detects the languages and frameworks the
repository's manifests declare, names the specialists that justifies, and merges
every report into one envelope.

```bash
python "$DOCTOR/scripts/route.py" <repo> --format json
# load and run each named specialist over <repo>, then:
python "$DOCTOR/scripts/merge_reports.py" \
  --report code-doctor:$WORK/raw.json \
  --report python-code-doctor:$WORK/py.json \
  --out $WORK/merged.json --format json
```

**Not once per package directory.** This is the step that is easy to get wrong
and expensive to get wrong, because the failure is silent. Pointed at
`src/billing` instead, a doctor cannot see the repo's `pyproject.toml` or its
top-level `tests/`, so it reports a missing dependency manifest and "no test
files were found" — two fabricated findings about a project that has both.

The envelope replaces three things this skill used to be told:

- **Attribution.** Every record carries the doctor that produced it, so
  `--findings <doctor>:<path>` is no longer needed on the normal path. It stays
  for hand-assembled input.
- **Coverage.** `doctors_run` and `analyzers_run` are the evidence; `--doctor`
  no longer grants a coverage profile. A doctor in `coverage_unknown` — a bare
  JSON list, which `analyze_django.py` and a single detector both emit — grants
  nothing, because nothing in that shape distinguishes a full run from one
  detector that found nothing.
- **Defect vs lead.** Findings are scored; **candidates are not**. A candidate
  asserts no defect and carries no fix, and charging one to the grade would
  punish a doctor for being honest about what it could not prove.

**Failures are named, not absorbed.** `doctor_errors` ungrades the categories
the failed doctor covered rather than letting the surviving report score them.
`completeness` does the same per evidence class: a reference graph the detector
reports as inadequate ungrades Design, and inconclusive test classification
ungrades Tests. **code-doctor alone never grades Correctness** — a merge marker
in a git-confirmed unmerged path is the only correctness-class defect its raw
layer can prove.
```

Add a new section after **3. Per package**'s atlas paragraph:

```markdown
**The measurement audit.** Run `science-investigation`'s scripts **once from the
repo root** — pointed at a package, `find_metrics.py` cannot see `evals/` and
reports a thoroughly-measured pipeline as having no measurement. Write the
inventory once, then build each package's page from it:

```bash
python "$SCIENCE/scripts/build_measurement.py" --out <DOCS>/measurement.html \
  --inventory $WORK/inventory.json --name <pkg> --repo <repo> \
  --root-dir <root> --template "$SKILL/assets/template.html" \
  --body "$SKILL/assets/measurement-body.html" --intro-file $WORK/<pkg>-measure.html
```

**Every package gets one.** A package with nothing measurable is a short page
scoring `null` — not zero, not A+ — and appears in the repo document's *no
measurement content* list. Most packages in a typical repo are that page, and
that is the correct output.

Pass `--template "$SKILL/assets/template.html"` to `code-visualization`'s
`assemble.py` too. That one flag is what makes the four documents read as one
artifact instead of three tools' output.
```

Extend the *What gets built* block and the *Quality bar* with the measurement
document, and add to the reference index:

```markdown
| A measurement grade looks wrong; explaining what the coverage ratio divides | `references/scoring.md` |
```

- [ ] **Step 4: Update the references**

In `references/doc-layout.md`: add `measurement.html` to both layout blocks and
to the *Across* row of the navigation table, replace the rebuild-order list with
the six steps from the spec, and append this section after *The metadata block*:

```markdown
## The measurement metadata block

`measurement.html` embeds its numbers the same way `health.html` does, with the
same `</` → `<\/` escaping, so one extraction snippet reads both:

```html
<script type="application/json" id="measurement-meta"> … </script>
```

| Field | Meaning |
|---|---|
| `schema` | `measurement/1` |
| `scope` | `package` or `repository` |
| `package`, `generated`, `commit` | which unit, when, against which sha |
| `score`, `grade` | 0–100 and a letter; `score` is null when nothing measurable was found |
| `weight_total`, `weight_measured` | the two sides of the ratio |
| `by_importance` | per level: `total`, `measured`, `share`, `rows` — `["3"]["share"]` is the ship-gate cut |
| `rows[]` | every measurable thing: `name`, `importance`, `importance_reason`, `credit`, `credit_reason`, `finding`, `n`, `n_total`, `formula`, `consumer`, `evidence[]`, `status`, `unmeasurable_reason` |
| `findings[]` | `id`, `severity`, `title`, `detail`, `evidence[]`, `blast_radius` |
| `not_audited[]` | subsystems the audit could not reach |
| `rows_out_of_scope` | rows dropped as defined outside this unit |
| `packages[]` | root scope only: `name`, `score`, `grade`, `rows`, `generated` |

`score: null` with `rows: []` is the documented "no measurement content" page.
It is neither a pass nor a failure, and the roll-up says so in words rather
than showing a dash that could be read either way.
```

In `references/scoring.md`, append:

```markdown
## The measurement grade

A different question from the health grade, and it divides a different thing:

    score = 100 × Σ(importance × credit) / Σ(importance)     over all measurable things

Health asks *how dense are the detectable defects*. Measurement asks *how much of
what matters is actually measured* — so its denominator is not lines of code but
the set of things that could be measured, whether or not anyone measured them.

| Importance | |
|---|---|
| 3 | gates a ship / release / rollout decision |
| 2 | informs a decision someone actually makes |
| 1 | informational |

| Credit | |
|---|---|
| 1.0 | measured, nothing found against it |
| 0.5 | measured, one confirmed medium finding |
| 0.25 | measured, one confirmed high finding |
| 0.0 | not measured, or structurally unmeasurable with today's data |

**Structurally unmeasurable things stay in the denominator.** Recall with no gold
set, calibration with no outcomes, causal effect with no control arm — dropping
those rows would let a system that measures one easy thing perfectly score 100,
which is precisely how silence gets read as success.

**The letter bands are the health grade's, exactly.** A B- means the same score
range on both pages, because they sit side by side in the nav and a reader
compares them directly. What differs is what each divides — say which when you
present them together. The rubric itself lives in
`science-investigation/scripts/rubric.py`; a CI test pins the band table
identical to this skill's.
```

- [ ] **Step 5: Append the eval cases**

Add to `evals/code-overview/evals.json`:

```json
{
  "id": "candidates-do-not-move-the-grade",
  "prompt": "The health page for our Go service shows 40 candidates and a B. Should we be deleting those dead functions?",
  "expected_output": "Explains that candidates are unverified leads that carried no weight in the B, and that the dead-function candidates specifically rest on a heuristic reference graph that under-resolves package-style imports. Points at the benign explanations on the page — entry points, convention-loaded plugins, a library's public surface — and says to confirm each before deleting anything."
},
{
  "id": "every-package-gets-a-measurement-page",
  "prompt": "Build the overview. Only the evals package actually computes any metrics — skip measurement for the other six.",
  "expected_output": "Builds a measurement page for all seven, explaining that a package with nothing measurable scores null and says so in one line, and that the repo document lists them as having no measurement content. Does not skip the pages, and does not manufacture metrics to fill them."
}
```

- [ ] **Step 6: Update the README**

Extend the `code-overview` row of the skill table to name the four document types
and the single code-doctor call.

- [ ] **Step 7: Verify everything**

```bash
pytest -q
python tools/validate_skills.py
ruff check .
```

Expected: the whole suite green. `validate_skills.py` enforces the ≤1024-character
description — the frontmatter will need trimming to fit the fourth document.

- [ ] **Step 8: Commit**

```bash
git add skills/code-overview evals/code-overview/evals.json README.md tests/code_overview/test_end_to_end.py
git commit -m "code-overview: document the four-document set and the single doctor call

One code-doctor call replaces per-package doctor selection, and its envelope
replaces three things this skill used to be told by flag — attribution,
coverage, and which records assert a defect. The workflow says so where
someone would otherwise reach for the old flags, and the eval cases pin the
two answers most likely to go wrong: that candidates did not move the grade,
and that a package with no metrics still gets a page.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Self-review notes

| Spec requirement | Task |
|---|---|
| Four document types, health tabbed with Candidates | 1, 4 |
| Every package gets a measurement page; null for the empty ones | 5, 7 |
| One shell forced via `--template`; companions keep defaults | 1, 7 |
| Drift ratchet on tokens **and** letter bands | 1 |
| `measurement-body.html` carried by code-overview | 1 (byte-identical copy, pinned) |
| code-doctor's types mapped; no Correctness claim | 2 |
| Coverage read from `doctors_run`, not `--doctor` | 3 |
| `doctor_errors` ungrades the failed doctor's categories | 3 |
| `completeness` ungrades Design and Tests | 2, 3 |
| Candidates never scored | 3, 4 |
| Two grade cards, both read back out | 5 |
| Repo table's measurement column and no-content list | 5 |
| Nav across-row of four; `--check` gate | 6 |
| Rebuild order with both analysis skills run from the root | 7 |

**Known deviation, carried from Plan 2:** the canonical `measurement-body.html`
lives in `science-investigation` and code-overview carries a byte-identical copy
pinned by CI, rather than each owning an independent file. Same observable
behaviour, one source of truth.

**Not covered here, by design:** code-doctor's remaining detectors, and the
`--covers` escape hatch, which keeps working unchanged for hand-assembled input.
