# django-simplifier Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Grow `django-simplifier` from six detectors to fifteen, make every detector version-aware across Django 4/5/6, add a 4→5→6 upgrade detector driven by a declarative change table, and add a prescriptive idioms guide so the skill serves authoring as well as review.

**Architecture:** Unchanged in shape — deterministic stdlib-`ast` detectors plus judgment guides loaded on demand. Three additions: `django_versions.py` (a declarative table of Django changes with a generic matcher), version detection from the project's dependency manifests, and `run_external_tools.py` to drive the tools that require a live Django.

**Tech Stack:** Python 3.11+, stdlib only (`ast`, `re`, `tomllib`, `pathlib`, `argparse`, `json`). No runtime dependencies.

## Global Constraints

Copied verbatim from the spec and the repo's `pyproject.toml`:

- **Python 3.11+, stdlib only.** No runtime dependencies. Detectors never import the project, never start Django, never need a settings module or a database.
- **No PEP 701 f-strings** — no nested same-quote expressions, no backslashes inside the expression part. A standalone install runs on whatever interpreter is present.
- **`tools/validate_skills.py` must pass:** flat frontmatter; `description` ≤ 1024 chars with no unquoted `#`; every SKILL.md invocation written `python "$SKILL/scripts/..."`; `evals/django-simplifier/evals.json` present, unique ids, `expected_output` per case.
- **`ruff` clean** under `select = ["E4","E7","E9","F"]`, `line-length = 120`.
- **Every detector keeps the shared contract:** `--format text|json`, `--ignore type1,type2`, 🔴/🟡/🟢, findings shaped `{file, line, smell_type, description, suggestion, severity}`, and `collect(ctx) -> [finding]` + `sys.exit(run(name, description, collect))`.
- **Silence outside Django is explained on stderr**, never presented as a clean bill of health.
- **Conservative by design:** false negatives over false positives. A detector that fires on correct Django is worse than no detector.

---

## File Structure

### New scripts

| File | Responsibility |
|---|---|
| `django_versions.py` | The `Change` record, the version table (Django 4.0 → 6.1), release/EOL metadata, and `matches(change, ...)` helpers. Data, not logic. |
| `django_detect_version.py` | Resolve the project's Django version from manifests; return `(version, source)` or `(None, reason)`. |
| `find_version_issues.py` | Walk the tree against the version table; derive severity from `--from`/`--target`. |
| `find_form_issues.py` | Forms and ModelForms. |
| `find_admin_issues.py` | `ModelAdmin` classes. |
| `find_drf_issues.py` | DRF serializers, viewsets, and API views. |
| `find_migration_issues.py` | Files under `migrations/`. |
| `find_settings_issues.py` | Settings modules and project layout. |
| `find_async_issues.py` | `async def`, the `a*` ORM surface, and background tasks. |
| `find_transaction_issues.py` | `atomic`, `select_for_update`, `on_commit`. |
| `find_test_issues.py` | Django-shaped test smells. |
| `analyze_diff.py` | The diff lens over changed files only. |
| `run_external_tools.py` | Drive installed tools; never install. |

### Modified scripts

| File | Change |
|---|---|
| `django_context.py` | Add admin/serializer/migration/settings/urls discovery, decorator index, `ctx.version`, `ctx.template_files`, and `ctx.parsed(path)` caching so fifteen detectors parse each file once. |
| `analyze_django.py` | Register the nine new categories; add `--target-version`; report the detected version in stats. |
| `find_query_issues.py`, `find_model_issues.py`, `find_view_issues.py`, `find_django_security.py`, `find_django_overengineering.py`, `find_template_issues.py` | Hardened per the smell inventory below. |

### References

Updated: `django-orm.md`, `django-architecture.md`, `django-overengineering.md`, `django-security.md`, `django-safety-net.md`.
Absorbed and deleted: `django-modernization.md` → `django-upgrade-runbook.md`.
New: `django-idioms.md`, `django-upgrade-runbook.md`, `django-forms-and-admin.md`, `django-drf.md`, `django-migrations.md`, `django-async-and-tasks.md`.

### Tests and evals

`tests/django_simplifier/test_detectors.py` (extend), `test_analyze_django.py` (extend), plus new `test_versions.py` and `test_external_tools.py`. `evals/django-simplifier/evals.json` grows to cover upgrade, authoring, DRF, and migration cases.

---

## Smell inventory

The complete list of `smell_type` values. This is the contract between tasks: a detector emits exactly these names, tests assert exactly these names, and SKILL.md documents exactly these names. **No detector invents a name not on this list.**

### `find_query_issues` (existing + new)
`n_plus_one_query`, `save_in_loop`, `create_in_loop`, `delete_in_loop`, `update_without_f`, `len_of_queryset`, `raw_sql`, `deprecated_extra`, **`count_in_loop`**, **`exists_in_loop`**, **`index_instead_of_first`**, **`bulk_create_without_batch_size`**, **`read_modify_write_race`**, **`update_on_sliced_queryset`**, **`unbounded_queryset_in_view`**

### `find_model_issues`
`missing_str_method`, `too_many_fields`, `fat_model`, `no_default_ordering`, `null_on_text_field`, `missing_related_name`, `missing_on_delete`, `inline_choices`, **`save_ignores_update_fields`**, **`decimal_without_precision`**, **`unique_together_over_constraints`**, **`multi_table_inheritance`**, **`related_name_disabled`**, **`redundant_db_index_on_fk`**, **`missing_get_absolute_url`**, **`auto_now_add_with_default`**, **`file_field_without_upload_to`**

### `find_view_issues`
`fat_view`, `missing_ownership_filter`, `query_in_form_clean`, `hardcoded_url`, `url_without_name`, **`csrf_exempt`**, **`open_redirect`**, **`unfiltered_user_input_lookup`**, **`unscoped_get_queryset`**, **`unauthenticated_mutation`**, **`cbv_hook_overload`**

### `find_django_security`
`debug_true`, `debug_propagate`, `insecure_session_cookie`, `insecure_csrf_cookie`, `hardcoded_secret`, `wildcard_allowed_hosts`, `cors_allow_all`, `mark_safe_on_dynamic_value`, **`missing_hsts`**, **`missing_ssl_redirect`**, **`weak_frame_options`**, **`missing_csrf_trusted_origins`**, **`spoofable_proxy_ssl_header`**, **`missing_samesite`**, **`no_password_validators`**, **`weak_password_hasher`**, **`missing_csp`** (6.0+ only), **`safe_filter_in_template`**, **`autoescape_off`**

### `find_django_overengineering`
`unused_abstract_model`, `single_impl_abstract_model`, `empty_manager`, `thin_manager`, `unused_mixin`, `single_use_mixin`, `deep_form_inheritance`, `thin_middleware`, `crud_only_service`, `save_signal_for_simple_logic`, **`work_in_app_ready`**, **`query_in_context_processor`**, **`redundant_template_tag`**

### `find_template_issues`
`query_in_template`, `relation_walk_in_loop`, `deeply_nested_template_loop`, **`missing_csrf_token`**, **`hardcoded_url_in_template`**, **`static_url_variable`**, **`template_var_in_script`**, **`include_in_loop`**

### `find_form_issues` (new)
`form_fields_all`, `cleaned_data_before_validation`, `commit_false_without_save_m2m`, `queryset_at_class_scope`, `clean_method_returns_nothing`, `unvalidated_form_use`

### `find_admin_issues` (new)
`admin_list_display_n_plus_one`, `list_filter_high_cardinality`, `fk_without_raw_id`, `admin_action_without_permission_check`, `admin_get_queryset_without_super`, `mark_safe_in_admin_display`, `admin_missing_search_fields`

### `find_drf_issues` (new)
`serializer_fields_all`, `viewset_default_permission`, `unscoped_viewset_queryset`, `query_in_serializer_method_field`, `serializer_depth`, `missing_pagination`, `missing_throttling`, `permission_allow_any`

### `find_migration_issues` (new)
`run_python_without_reverse`, `run_python_imports_model`, `run_sql_without_reverse`, `schema_and_data_in_one_migration`, `non_nullable_without_default`, `conflicting_leaf_migrations`

### `find_settings_issues` (new)
`default_user_model`, `missing_security_middleware`, `middleware_order`, `missing_default_auto_field`, `deprecated_storage_setting`, `sqlite_in_production`, `locmem_cache_in_production`, `ospath_join_basedir`, `settings_imports_models`, `missing_use_tz`

### `find_async_issues` (new)
`sync_orm_in_async_view`, `unawaited_async_orm_call`, `blocking_io_in_async`, `task_takes_model_instance`, `enqueue_without_on_commit`, `sync_to_async_without_thread_sensitive`

### `find_transaction_issues` (new)
`external_call_in_atomic`, `select_for_update_outside_atomic`, `integrity_error_caught_in_atomic`, `get_or_create_race`, `atomic_around_loop_of_saves`

### `find_test_issues` (new)
`no_query_count_assertions`, `on_commit_needs_transaction_testcase`, `setuptestdata_mutation`, `client_login_over_force_login`, `no_unauthorized_path_test`

### `find_version_issues` (new)
`django_end_of_life`, `removed_in_target`, `deprecated_api`, `future_removal`, `django_version_unknown`

---

## Tasks

### Task 1: The version table and version detection

**Files:** Create `skills/django-simplifier/scripts/django_versions.py`, `skills/django-simplifier/scripts/django_detect_version.py`; create `tests/django_simplifier/test_versions.py`.

**Interfaces produced:**
```python
# django_versions.py
LATEST_KNOWN: tuple[int, int]                     # (6, 1)
SUPPORTED: dict[tuple[int,int], dict]             # {(5,2): {"lts": True, "security_until": "2028-04"}}
class Change:                                     # frozen dataclass
    name: str
    match: dict                                   # {"kind": ..., ...}
    replacement: str
    deprecated_in: tuple[int,int] | None
    removed_in: tuple[int,int] | None
    note: str = ""
CHANGES: list[Change]
MATCH_KINDS = {"setting","import","call","kwarg","meta_option","attribute","template_filter"}
def parse_version(text: str) -> tuple[int,int] | None
def is_end_of_life(version) -> bool

# django_detect_version.py
def detect_django_version(root) -> tuple[tuple[int,int] | None, str]   # (version, source_description)
```

- [ ] Write `test_versions.py`: `parse_version` handles `"5.2"`, `"Django>=4.2,<5.0"`, `"~=6.1.0"`, and rejects junk; every `CHANGES` entry has a known `match["kind"]` and at least one of `deprecated_in`/`removed_in`; `detect_django_version` reads pyproject PEP 621, Poetry, `requirements.txt`, and returns `(None, reason)` on a bare tree.
- [ ] Run: `pytest tests/django_simplifier/test_versions.py -v` → FAIL (no modules).
- [ ] Implement `django_versions.py` — seed `CHANGES` from the researched release notes for 4.0 → 6.1, prioritising application-level constructs over database-backend internals.
- [ ] Implement `django_detect_version.py`.
- [ ] Run tests → PASS. Commit.

### Task 2: Extend the shared project context

**Files:** Modify `skills/django-simplifier/scripts/django_context.py`; extend `tests/django_simplifier/test_analyze_django.py`.

**Interfaces produced:** on `DjangoContext` —
```python
ctx.version: tuple[int,int] | None
ctx.version_source: str
ctx.parsed(path) -> ast.Module | None       # cached; 15 detectors parse each file once
ctx.admins -> dict[str, ClassInfo]          # ModelAdmin subclasses
ctx.serializers -> dict[str, ClassInfo]
ctx.viewsets -> dict[str, ClassInfo]
ctx.migration_files -> list[Path]
ctx.urls_files -> list[Path]
ctx.test_files -> list[Path]
ctx.production_settings -> list[Path]       # settings modules that are not obviously dev
ctx.at_least(major, minor) -> bool          # False when the version is unknown
```

- [ ] Add tests: the cache returns the same object twice; `at_least` is False when version is unknown; `production_settings` excludes `dev.py`/`local.py`; migrations and tests are discovered.
- [ ] Run → FAIL. Implement. Run → PASS. Commit.

### Task 3: `find_version_issues.py`

**Files:** Create the script; extend `test_versions.py`.

- [ ] Tests: `index_together` is 🟡 against `--target 4.2` and 🔴 against `--target 5.1`; `USE_L10N` is 🔴 against 5.0+; a 4.2 project reports `django_end_of_life`; an undetectable version reports `django_version_unknown` and nothing version-conditional; `--list-known` prints `LATEST_KNOWN`.
- [ ] Run → FAIL. Implement the generic matcher over `CHANGES`. Run → PASS. Commit.

### Task 4: Harden `find_query_issues` and `find_model_issues`

- [ ] Positive + negative test per new smell (see inventory). Negative cases: a `.count()` outside a loop; a `save()` override that *does* extend `update_fields`; a `DecimalField` that *does* declare precision.
- [ ] Run → FAIL. Implement. Run → PASS. Commit.

### Task 5: Harden `find_view_issues` and `find_django_security`

- [ ] Tests including the version gate: `missing_csp` fires only when `ctx.at_least(6, 0)`.
- [ ] Run → FAIL. Implement. Run → PASS. Commit.

### Task 6: Harden `find_template_issues` and `find_django_overengineering`

- [ ] Tests. Negative: a GET form needs no `{% csrf_token %}`; `{% static %}` is correct.
- [ ] Run → FAIL. Implement. Run → PASS. Commit.

### Task 7: `find_form_issues` and `find_admin_issues`

- [ ] Tests. Negative: an explicit `fields = [...]`; a `list_display` that only names local fields.
- [ ] Run → FAIL. Implement. Run → PASS. Commit.

### Task 8: `find_drf_issues`

- [ ] Tests. Silent when the project has no DRF import at all.
- [ ] Run → FAIL. Implement. Run → PASS. Commit.

### Task 9: `find_migration_issues` and `find_settings_issues`

- [ ] Tests. Negative: `RunPython(forward, backward)`; a settings module that does set `AUTH_USER_MODEL`.
- [ ] Run → FAIL. Implement. Run → PASS. Commit.

### Task 10: `find_async_issues`, `find_transaction_issues`, `find_test_issues`

- [ ] Tests. Negative: `await Model.objects.aget(...)`; `select_for_update()` inside `with transaction.atomic():`.
- [ ] Run → FAIL. Implement. Run → PASS. Commit.

### Task 11: Orchestrator, diff lens, external tools

**Files:** Modify `analyze_django.py`; create `analyze_diff.py`, `run_external_tools.py`; create `tests/django_simplifier/test_external_tools.py`.

- [ ] Tests: all fifteen categories register; `--skip` drops a new category; the diff lens only reports changed files; `run_external_tools` lists a missing tool under `missing_tools` rather than failing, and never installs.
- [ ] Run → FAIL. Implement. Run → PASS. Commit.

### Task 12: References

- [ ] Write `django-idioms.md`, `django-upgrade-runbook.md`, `django-forms-and-admin.md`, `django-drf.md`, `django-migrations.md`, `django-async-and-tasks.md`; update the five kept guides; delete `django-modernization.md`. Commit.

### Task 13: SKILL.md and evals

- [ ] Rewrite SKILL.md: the fifteen detectors, the version/upgrade workflow, the authoring entry point, the reference index, the web-search-for-newer-versions instruction. Keep `description` ≤ 1024 chars.
- [ ] Extend `evals/django-simplifier/evals.json` with upgrade, authoring, DRF, and migration cases.
- [ ] Run `python tools/validate_skills.py` → PASS. Commit.

### Task 14: Full verification

- [ ] `pytest tests/django_simplifier -v` → all pass.
- [ ] `pytest tests/ -q` → no regression in other skills.
- [ ] `ruff check skills/django-simplifier` → clean.
- [ ] `python tools/validate_skills.py` → exit 0.
- [ ] Run `analyze_django.py` over a synthetic multi-app project end to end; confirm findings are worst-first and the version line appears in the report.
- [ ] Commit.

---

## Self-review against the spec

**Spec coverage:** version table → Task 1; version detection → Task 1; freshness/`--list-known` → Tasks 3 and 13; six hardened detectors → Tasks 4–6; nine new detectors → Tasks 3, 7–10; `analyze_diff`/`run_external_tools` → Task 11; eleven references incl. absorbing `django-modernization.md` → Task 12; authoring guide → Task 12; tests both halves → every task; evals → Task 13; repo constraints → Global Constraints + Task 14.

**Type consistency:** `ctx.at_least(major, minor)` is the single version gate used by Tasks 5, 9, and 10. `detect_django_version` returns `(version, source)` in Task 1 and is consumed unchanged in Tasks 2 and 3. Every `smell_type` a task emits appears in the inventory above.

**Known deviation from the spec:** the spec named `find_version_issues` as one of "nine new" detectors and also listed it separately; the count is fifteen detectors total either way.
