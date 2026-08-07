# django-code-doctor: broaden the detectors, add version awareness, add an upgrade path

**Date:** 2026-08-06
**Status:** approved design, not yet implemented

## Why

`django-code-doctor` today ships six detectors and six judgment guides. They are
good — the class graph in `django_context.py` makes "extended by nobody" a fact
rather than a guess, and the gate keeps the detectors silent outside Django — but
the coverage is narrow. Forms, the admin, DRF, migrations, settings hygiene,
async, transactions, and Django-shaped test smells are all unexamined, and the
skill has no idea which version of Django it is looking at.

That last gap is the expensive one. Django removes on a published schedule, so a
construct is not merely "dated" — it is a future crash with a known date. As of
today **Django 4.2 LTS is end-of-life** (April 2026): every Django 4 project is
receiving no security fixes at all, which subsumes every other finding the skill
could make about it.

Three goals, from the request:

1. Detect a far wider range of bad Django patterns.
2. Work correctly against Django 4, 5, and 6 — version-aware, not version-blind.
3. Drive an upgrade from 4→5, 4→6, or 5→6.

Plus one implied by "an LLM armed with it should write idiomatic Django code
well": the skill must serve **authoring**, not only review. A skill that only
knows what is wrong fires when someone asks for a review; a skill that also knows
what is right fires when someone is writing Django.

## Version landscape (researched 2026-08-06, from docs.djangoproject.com)

| Version | Released | Status |
|---|---|---|
| 4.2 LTS | Apr 2023 | **End of life** (Apr 2026) — no security fixes |
| 5.0, 5.1 | Dec 2023, Aug 2024 | End of life |
| 5.2 LTS | Apr 2025 | Extended support to Apr 2028 |
| 6.0 | Dec 2025 | Mainstream support ended Aug 2026; extended to Apr 2027 |
| 6.1 | Aug 2026 | **Current** |
| 6.2 LTS | Apr 2027 | Next LTS |

The upgrade targets that matter in practice are therefore **5.2 LTS** (the
conservative landing spot) and **6.1** (current).

## Non-goals

- Rewriting code. The skill reports; `django-upgrade` rewrites; the user decides.
  A codemod that is wrong is worse than a report that is right.
- Replicating python-code-doctor. Complexity, duplication, dead code, and
  resource leaks stay out of scope; the report says so when that skill is absent.
- Running the project. Every detector stays stdlib-`ast`-only: no import, no
  settings module, no database, no virtualenv. Things that genuinely require a
  live Django (`makemigrations --check`, `check --deploy`) are delegated to
  `run_external_tools.py`, which shells out only to what is already installed.

## Architecture

Unchanged in shape: **deterministic detectors** for what can be found
mechanically, **judgment guides** in `references/` for what needs a reading
brain. Three additions.

### 1. `scripts/django_versions.py` — the version table

One declarative row per Django change that matters for an upgrade:

```python
Change(
    name="index_together",
    deprecated_in=(4, 2),
    removed_in=(5, 1),
    match={"kind": "meta_option", "name": "index_together"},
    replacement="Meta.indexes = [models.Index(fields=[...])]",
    note="makemigrations generates a RenameIndex operation for existing indexes.",
)
```

`match["kind"]` covers the seven shapes a Django change actually takes, which is
what lets one generic matcher serve the whole table:

| kind | matches | example |
|---|---|---|
| `setting` | a module-level assignment in a settings file | `USE_L10N`, `DEFAULT_FILE_STORAGE` |
| `import` | `from <module> import <name>` | `django.utils.timezone.utc` |
| `call` | a call by last-component name | `url()`, `get_storage_class()` |
| `kwarg` | a keyword on a named call | `CheckConstraint(check=...)` |
| `meta_option` | an assignment inside an inner `Meta` | `index_together` |
| `attribute` | an attribute access by name | `ChoicesMeta` |
| `template_filter` | a filter in a template | `length_is` |

Adding Django 6.2 or 7.0 later is a data edit, not a code edit. That is the whole
point of the split.

The table is seeded from the release notes and the deprecation timeline for
4.0 → 6.1, prioritising what actually appears in application code (settings,
model Meta options, template filters, view and form APIs) over what appears only
in database-backend subclasses.

### 2. Version detection

`detect_django_version(root)` returns `(version_tuple | None, source: str)`,
resolving in order:

1. `pyproject.toml` — PEP 621 `dependencies`, and Poetry's `tool.poetry.dependencies`
2. `requirements*.txt` / `constraints*.txt`
3. `setup.cfg`, `setup.py`
4. `Pipfile`
5. `pip show django` in the current environment (labelled as such — it is a
   different claim from what the manifest pins)

When none of these answer, it returns `None` and **says so**. A version-conditional
finding built on a guessed version is worse than no finding, so detectors that
need a version stay quiet and the report names the gap.

### 3. Freshness

The table is a snapshot as of Django 6.1 (August 2026). SKILL.md instructs
fetching `docs.djangoproject.com/en/N/releases/` and `/internals/deprecation/`
before targeting anything newer, and `find_version_issues.py --list-known` prints
the table's high-water mark so the gap is visible rather than silent.

## Detectors

Fifteen, sharing the existing contract without change: `--format text|json`,
`--ignore type1,type2`, 🔴/🟡/🟢 severities, the flat findings shape
(`file, line, smell_type, description, suggestion, severity`), and silence-with-a-
stderr-note outside Django. `analyze_django.py` builds the context once and shares
it with all of them.

### Existing six, hardened

| Script | Added checks |
|---|---|
| `find_query_issues` | `.count()`/`.exists()` in a loop; `queryset[0]` where `.first()` is meant; `bulk_create` without `batch_size`; read-modify-write without `select_for_update`; `.update()` on a sliced queryset; unbounded `.all()` rendered by a view |
| `find_model_issues` | `save()` override that drops `update_fields` (the 4.2 `update_or_create` trap); `DecimalField` without `max_digits`/`decimal_places`; `unique_together` → `constraints`; concrete (multi-table) inheritance; `related_name="+"`; redundant `db_index=True` on a `ForeignKey`; missing `get_absolute_url` on a model with a detail route |
| `find_view_issues` | `csrf_exempt`; open redirect via an unvalidated `?next=`; `request.GET` fed straight into `filter(**...)`; CBV/ViewSet with no `get_queryset` scoping; a mutating view with no auth decorator; five or more overridden CBV hooks |
| `find_django_security` | `SECURE_HSTS_SECONDS`, `SECURE_SSL_REDIRECT`, `X_FRAME_OPTIONS`, `CSRF_TRUSTED_ORIGINS`, `SECURE_PROXY_SSL_HEADER`, `SESSION_COOKIE_SAMESITE`; empty `AUTH_PASSWORD_VALIDATORS`; weak `PASSWORD_HASHERS`; absent `SECURE_CSP` on 6.0+; `\|safe` and `{% autoescape off %}` in templates |
| `find_django_overengineering` | `AppConfig.ready()` doing work; context processors that query; custom template tags duplicating built-ins |
| `find_template_issues` | Missing `{% csrf_token %}` in a POST form; a hardcoded path where `{% url %}` belongs; `{{ STATIC_URL }}` instead of `{% static %}`; a template variable inside an inline `<script>`; `{% include %}` inside a loop |

### Nine new

| Script | Covers |
|---|---|
| `find_form_issues` | `fields = "__all__"`; `cleaned_data` touched before `is_valid()`; `save(commit=False)` without `save_m2m()`; a `queryset=` evaluated at class scope; a `clean_*` that returns nothing |
| `find_admin_issues` | `list_display` walking a relation without `list_select_related` (the admin N+1); `list_filter` on a high-cardinality field; a `ForeignKey` with no `raw_id_fields`/`autocomplete_fields`; actions with no permission check; `get_queryset` overridden without `super()`; `mark_safe` in a `list_display` callable |
| `find_drf_issues` | A ViewSet leaning on the default permission class; `fields = "__all__"` on a serializer; a `queryset` class attribute with no user scoping; a `SerializerMethodField` issuing a query; `depth = 1`; no pagination; no throttling |
| `find_migration_issues` | `RunPython` without a reverse; `RunPython` importing models directly instead of `apps.get_model`; schema and data in one migration; a non-nullable column added without a default; conflicting leaf migrations; `RunSQL` without a reverse |
| `find_settings_issues` | `AUTH_USER_MODEL` not custom; missing or misordered middleware; absent `DEFAULT_AUTO_FIELD`; deprecated storage settings; SQLite or `LocMemCache` in a production settings module; `os.path.join(BASE_DIR, ...)`; settings importing models |
| `find_async_issues` | Sync ORM inside `async def`; a missing `await` on an `a*` ORM method; blocking I/O in async; a Celery task taking a model instance instead of a pk; an enqueue inside `atomic()` with no `on_commit` |
| `find_transaction_issues` | An external HTTP call inside `atomic()`; `select_for_update()` outside a transaction; `IntegrityError` caught inside the same atomic block; `get_or_create` races |
| `find_test_issues` | No `assertNumQueries` anywhere in a suite whose project this skill just flagged N+1s in; `TestCase` where `on_commit` needs `TransactionTestCase`; `setUpTestData` mutation; no test for the *unauthorized* path |
| `find_version_issues` | The upgrade detector — see below |

### Supporting scripts

- `analyze_django.py` — orchestrator, extended to the new categories. Interface
  unchanged.
- `analyze_diff.py` — new. The diff lens, mirroring python-code-doctor: run the
  file-level detectors against only the changed files, for reviewing an
  AI-written Django feature or CR. Whole-project detectors (over-engineering,
  version, migrations) still need the full tree and say so.
- `run_external_tools.py` — new. See below.

## The upgrade capability

### `find_version_issues.py`

Answers one question: **what in this project breaks between where it is and where
you want it?**

```bash
python "$SKILL/scripts/find_version_issues.py" .                 # vs. latest known
python "$SKILL/scripts/find_version_issues.py" . --target 5.2
python "$SKILL/scripts/find_version_issues.py" . --from 4.2 --target 6.0
python "$SKILL/scripts/find_version_issues.py" . --list-known
```

Severity is **derived, not hand-assigned**:

| Condition | Severity | Meaning |
|---|---|---|
| Removed at or before the target | 🔴 | The project will not run on the target |
| Currently deprecated | 🟡 | Works today, scheduled to break |
| Scheduled for removal after the target | 🟢 | Fix it while you are here |

Plus a standing 🔴 `django_end_of_life` when the detected version no longer
receives security fixes — which today is every Django 4 project, and every 5.0/5.1
project.

### `run_external_tools.py`

Drives what a stdlib AST detector structurally *cannot*, and only what is already
installed:

| Tool | Answers what the detectors cannot |
|---|---|
| `django-upgrade` | The actual rewrite. The skill reports; this applies. Never run without asking. |
| `manage.py check --deploy` | Django's own deployment checks, against the real settings module |
| `manage.py makemigrations --check --dry-run` | "There are model changes with no migration" — unanswerable by parsing |
| `djlint` | Template linting |
| `mypy` + `django-stubs` | Type errors through Django's own stubs |
| `bandit`, `pip-audit` | Language-level security; advisories against the pinned Django |

It **never installs anything**. Absent tools are listed under `missing_tools`
with a `pip install` hint, and the skill asks before installing — the same policy
python-code-doctor already applies. `--fix` and `--run-migrations-check` are
opt-in because they mutate or execute.

## References — eleven, loaded on demand

Kept and updated: `django-orm.md`, `django-architecture.md`,
`django-overengineering.md`, `django-security.md`, `django-safety-net.md`.

`django-modernization.md` is **absorbed** into `django-upgrade-runbook.md` — the
two overlap almost entirely, and one file that covers both idiom modernization
and the version campaign is easier to keep honest than two that drift.

New:

| File | Load when |
|---|---|
| `django-idioms.md` | **Writing** Django, not reviewing it. One canonical answer per common task — models, views, urls, forms, settings, auth, admin, tasks — version-tagged for 4/5/6. This is the file that makes the skill useful before the code exists. |
| `django-upgrade-runbook.md` | Running a 4→5, 4→6, or 5→6 upgrade. The campaign: one minor version at a time, `-W error::DeprecationWarning` first, idiom changes in a commit separate from the version bump, read every generated migration. |
| `django-forms-and-admin.md` | Judging form and admin design |
| `django-drf.md` | Reviewing a DRF API — permissions vs. object permissions, serializer N+1, pagination |
| `django-migrations.md` | Writing or reviewing a migration, especially a data migration or a zero-downtime deploy |
| `django-async-and-tasks.md` | Async views, the ORM's `a*` methods, Celery / `django.tasks`, and transaction boundaries around both |

### The books

The four supplied books (Shaw, Bendoraitis, Holovaty, Mele) are strongest as
**topic coverage and further reading** — canonical URLs, generic relations,
context processors, Celery, i18n, DRF, deployment. They span 2020–2025, so where
a book and the 6.1 release notes disagree, **the release notes win**, and the
reference says which. They are cited by chapter in the relevant guide rather than
treated as the version authority.

## Testing

Extends the repo's existing convention (`tests/django_code_doctor/`, subprocess-
driven, fixtures written to `tmp_path` at runtime so the deliberately-bad Django
never trips the repo's own linters).

Fifteen detectors is too many to ship on "it imported cleanly". Every detector
gets both halves:

- **A positive case** — the bad pattern, asserting its `smell_type` fires.
- **A negative case** — the *correct* form of the same thing, asserting silence.
  This half matters more. A Django detector that fires on correct code is worse
  than no detector: it trains people to skip the output, and the real N+1 goes out
  with it.

Plus, extending the existing shared-contract tests to all fifteen: the findings
shape, `--ignore`, `--skip`, worst-first ordering, the non-Django gate, and the
stderr message that distinguishes "not Django" from "clean".

Version-specific tests pin the derived severity: the same project reports
`index_together` as 🟡 against a 4.2 target and 🔴 against 5.1+, and reports
nothing at all when the version cannot be determined.

`evals/django-code-doctor/evals.json` grows from four cases to cover the new
surface: an upgrade request, an authoring request, a DRF review, and a migration
review.

## Constraints inherited from the repo

- Python 3.11+, stdlib only, no runtime dependencies.
- No PEP 701 f-strings (nested same-quote expressions, backslashes inside the
  expression part) — a skill installed standalone runs on whatever interpreter is
  present.
- `tools/validate_skills.py` must pass: frontmatter flat, `description` ≤ 1024
  characters and free of unquoted `#`, every `python` invocation in SKILL.md
  written as `python "$SKILL/scripts/..."`, and `evals/django-code-doctor/evals.json`
  present with unique ids and an `expected_output` per case.
- `ruff` clean under the repo's `E4,E7,E9,F` selection at line-length 120.

## Risks

- **Detector count vs. signal.** Fifteen detectors can produce a wall of output
  that nobody reads. Mitigation: severity discipline (most new checks are 🟡/🟢),
  the existing `--skip`/`--ignore`, and SKILL.md continuing to name the two
  findings to read first.
- **False positives on DRF and admin**, where much of the real behaviour lives in
  base classes the AST cannot see. Mitigation: the same policy already used for
  `crud_only_service` — rank low, say in the finding that it was identified
  structurally and needs confirmation by reading.
- **The version table going stale.** Mitigation: `--list-known`, and an explicit
  SKILL.md instruction to web-search the release notes when the target exceeds
  what the table knows.
