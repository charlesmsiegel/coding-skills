# Template Relation-Walk Precision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce false-positive Django template relation-walk findings while fixing and regression-testing the five credible `tg.accounts` N+1 paths.

**Architecture:** Keep the detector textual and conservative, but normalize form-wrapper expressions and suppress two value-access shapes that cannot establish an ORM relation. Fix the application at queryset construction points with `select_related`, and prove those relations are cached after evaluation.

**Tech Stack:** Python 3.11+, pytest, Django ORM/TestCase, PowerShell, Bash installer.

## Global Constraints

- The canonical detector source is `C:\Users\charl\github\coding-skills\skills\django-code-doctor`.
- Preserve the existing uncommitted overview HTML in `C:\Users\charl\github\tg`.
- Do not build a full template type-inference engine or silence arbitrary uncertain chains.
- Use test-first red/green cycles for detector and application behavior.
- Install the verified canonical skill with `install.sh --codex` only after source tests pass.
- Do not regenerate code-overview HTML in this change.

---

### Task 1: Pin the detector's intended relation-walk classification

**Files:**
- Modify: `tests/django_code_doctor/test_template_and_overengineering.py`
- Test: `tests/django_code_doctor/test_template_and_overengineering.py`

**Interfaces:**
- Consumes: `helpers.run_detector(script: str, target: Path) -> list[dict]`
- Produces: Regression coverage for form wrappers, scalar/file values, and genuine relation chains.

- [ ] **Step 1: Add failing accounts-shaped detector tests**

Add helpers that filter `relation_walk_in_loop` findings, then fixtures containing:

```python
def relation_walks(findings):
    return [f for f in findings if f["smell_type"] == "relation_walk_in_loop"]


def test_form_wrappers_only_report_nested_domain_relations(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html":
            "{% for freebie_form in freebie_forms %}\n"
            "{{ freebie_form.character.name }}\n"
            "{{ freebie_form.character.gameline }}\n"
            "{{ freebie_form.character.get_absolute_url }}\n"
            "{{ freebie_form.character.owner.profile.get_absolute_url }}\n"
            "{% endfor %}\n",
    })
    hits = relation_walks(run_detector("find_template_issues.py", project))
    assert [(f["line"], f["suggestion"]) for f in hits] == [
        (5, "select_related/prefetch_related 'owner__profile' on the queryset the view passes in."),
    ]


def test_scalar_and_file_value_accesses_are_not_relations(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html":
            "{% for obj in objects %}\n"
            "{{ obj.type.title }}\n"
            "{{ obj.image.url }}\n"
            "{% endfor %}\n",
    })
    assert relation_walks(run_detector("find_template_issues.py", project)) == []


def test_genuine_nested_model_relations_still_report(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html":
            "{% for obj in objects %}{{ obj.owner.profile.get_absolute_url }}{% endfor %}\n"
            "{% for journal in journals %}{{ journal.character.name }}{% endfor %}\n",
    })
    assert len(relation_walks(run_detector("find_template_issues.py", project))) == 2
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run from `C:\Users\charl\github\coding-skills`:

```powershell
python -m pytest tests/django_code_doctor/test_template_and_overengineering.py -q
```

Expected: the new false-positive tests fail because the current detector reports every three-part chain.

- [ ] **Step 3: Commit only after Task 2 makes these tests green**

The tests and implementation form one detector commit; do not commit a red tree here.

---

### Task 2: Normalize relation-walk expressions

**Files:**
- Modify: `skills/django-code-doctor/scripts/find_template_issues.py`
- Modify: `tests/django_code_doctor/test_template_and_overengineering.py`

**Interfaces:**
- Consumes: tokenized expression parts and active template-loop variables.
- Produces: `_relation_parts(loop_var: str, parts: list[str]) -> list[str]` and `_is_value_access(parts: list[str]) -> bool` used only by `_scan`.

- [ ] **Step 1: Implement the minimal classifier**

Add narrowly named constants/helpers:

```python
_VALUE_ACCESS_SUFFIXES = {("type", "title"), ("image", "url")}


def _relation_parts(loop_var, parts):
    relation_parts = parts[1:]
    if loop_var.endswith("_form") and relation_parts:
        relation_parts = relation_parts[1:]
    return relation_parts


def _is_value_access(parts):
    return len(parts) == 2 and tuple(parts) in _VALUE_ACCESS_SUFFIXES
```

Change the loop rule to normalize `parts`, skip known value access, and report only when at least two normalized components remain. Build the eager-loading suggestion from every normalized component except the terminal display value/method.

- [ ] **Step 2: Run focused tests and verify GREEN**

```powershell
python -m pytest tests/django_code_doctor/test_template_and_overengineering.py tests/django_code_doctor/test_detectors.py -q
```

Expected: all tests pass, including the existing `order.customer.name` positive fixture.

- [ ] **Step 3: Run the complete canonical suite and lint**

```powershell
python -m pytest -q
python -m ruff check skills/django-code-doctor tests/django_code_doctor
```

Expected: both commands exit 0.

- [ ] **Step 4: Commit detector source and tests**

```powershell
git add skills/django-code-doctor/scripts/find_template_issues.py tests/django_code_doctor/test_template_and_overengineering.py
git commit -m "fix: reduce template relation-walk false positives"
```

---

### Task 3: Pin the five `tg.accounts` relations as cached

**Files:**
- Modify: `C:\Users\charl\github\tg\accounts\tests\models\test_models.py`
- Modify: `C:\Users\charl\github\tg\accounts\tests\views\test_views.py`
- Test: those two files

**Interfaces:**
- Consumes: `Profile.characters_to_approve`, `items_to_approve`, `locations_to_approve`, `get_updated_journals`, and `ProfileView.get_context_data`.
- Produces: query-count regressions asserting template relation access causes zero additional queries after queryset/context evaluation.

- [ ] **Step 1: Add failing approval and journal cache tests**

Using existing `User`, `Chronicle`, `Gameline`, `STRelationship`, `Human`, `ItemModel`, and `LocationModel` fixture patterns, add tests equivalent to:

```python
for method_name in ("characters_to_approve", "items_to_approve", "locations_to_approve"):
    rows = list(getattr(self.st_user.profile, method_name)())
    with self.subTest(method=method_name), self.assertNumQueries(0):
        rows[0].owner.profile.pk

journals = list(self.st_user.profile.get_updated_journals())
with self.assertNumQueries(0):
    journals[0].character.name
```

Give pending item/location rows owners, and create a `Journal` with an unanswered `JournalEntry` so every assertion exercises a real row.

- [ ] **Step 2: Add a failing freebie-view cache test**

Use `RequestFactory` or the existing profile-view fixture to call `ProfileView.get_context_data` without rendering the template. Extract the created `freebie_forms`, then assert accessing `form.character.owner.profile.pk` performs zero additional queries.

- [ ] **Step 3: Run focused tests and verify RED**

```powershell
.\.venv\Scripts\python.exe manage.py test accounts.tests.models.test_models accounts.tests.views.test_views --keepdb --verbosity 1
```

Expected: new cache assertions fail with one additional profile/character query per affected row on the unmodified querysets. If the repository's known test-runner stall recurs, run the individual new test methods and record the timeout separately from assertion results.

---

### Task 4: Eager-load the five application relations

**Files:**
- Modify: `C:\Users\charl\github\tg\core\models.py:49-61`
- Modify: `C:\Users\charl\github\tg\accounts\models.py:337-344`
- Modify: `C:\Users\charl\github\tg\accounts\views.py:235-240`
- Test: `C:\Users\charl\github\tg\accounts\tests\models\test_models.py`
- Test: `C:\Users\charl\github\tg\accounts\tests\views\test_views.py`

**Interfaces:**
- Consumes: Django `QuerySet.select_related(*fields)`.
- Produces: cached `owner.profile` for approval/freebie rows and cached `journal.character` for journal rows.

- [ ] **Step 1: Add the minimal eager loading**

Implement exactly:

```python
# core.models.ModelQuerySet.pending_approval_for_user
.select_related("polymorphic_ctype", "chronicle", "owner", "owner__profile")

# accounts.models.Profile.get_updated_journals
return Journal.objects.filter(entries__st_message="").select_related("character").distinct()

# accounts.views.ProfileView.get_context_data freebie queryset
.select_related("polymorphic_ctype", "owner", "owner__profile", "chronicle")
```

- [ ] **Step 2: Run focused tests and verify GREEN**

Run the exact new methods first, then both containing modules:

```powershell
.\.venv\Scripts\python.exe manage.py test accounts.tests.models.test_models accounts.tests.views.test_views --keepdb --verbosity 1
```

Expected: new query-cache tests pass and existing tests in both modules remain green.

- [ ] **Step 3: Run static and Django checks**

```powershell
.\.venv\Scripts\ruff.exe check core/models.py accounts/models.py accounts/views.py accounts/tests/models/test_models.py accounts/tests/views/test_views.py
.\.venv\Scripts\python.exe manage.py check
```

Expected: both commands exit 0.

- [ ] **Step 4: Commit only application source and tests**

```powershell
git add core/models.py accounts/models.py accounts/views.py accounts/tests/models/test_models.py accounts/tests/views/test_views.py
git commit -m "perf: eager-load account approval relations"
```

Do not stage the existing generated HTML changes.

---

### Task 5: Validate the canonical detector against `tg` and install it

**Files:**
- Read: `C:\Users\charl\github\coding-skills\skills\django-code-doctor\scripts\find_template_issues.py`
- Replace through installer: `C:\Users\charl\.codex\skills\django-code-doctor`

**Interfaces:**
- Consumes: canonical detector and `install.sh --codex`.
- Produces: installed detector identical to canonical source.

- [ ] **Step 1: Run the canonical detector against `tg`**

```powershell
python C:\Users\charl\github\coding-skills\skills\django-code-doctor\scripts\find_template_issues.py C:\Users\charl\github\tg --format json
```

Filter `accounts` `relation_walk_in_loop` findings. Expected: exactly the five credible expressions remain; the former form/scalar/file false positives are absent. The five remain as static candidates because this textual detector deliberately does not inspect view querysets; Task 4's query-count tests prove they are handled.

- [ ] **Step 2: Install the verified skill**

Run from `C:\Users\charl\github\coding-skills`:

```powershell
bash ./install.sh --codex
```

Expected: the installer reports replacement of the Codex skill directory and exits 0.

- [ ] **Step 3: Compare canonical and installed trees**

```powershell
git diff --no-index -- skills/django-code-doctor C:\Users\charl\.codex\skills\django-code-doctor
```

Expected: exit 0 and no diff.

- [ ] **Step 4: Run the installed detector against `tg`**

```powershell
python C:\Users\charl\.codex\skills\django-code-doctor\scripts\find_template_issues.py C:\Users\charl\github\tg --format json
```

Expected: the installed result matches the canonical result.

- [ ] **Step 5: Final scope verification**

In `coding-skills`, verify only the planned detector commits exist and the worktree is clean. In `tg`, verify the new commit contains only application source/tests and that the pre-existing 42 HTML modifications remain unstaged and otherwise untouched.
