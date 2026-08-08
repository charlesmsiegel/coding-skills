#!/usr/bin/env python3
"""The code-health rubric: categories, weights, and the score→grade arithmetic.

Pure data and arithmetic — no I/O, no argument parsing — so the grade a document
carries can be recomputed and tested without building a document. Everything a
reader would want to argue with (what counts as a correctness problem, how much
of it is tolerable per KLOC, where B- ends) is a constant in this file rather
than a number buried in a formatter.

The scoring curve is

    score = 100 * 0.5 ** (density / half_life)

where density is severity-weighted findings per 1000 lines. It is monotone,
never leaves [0, 100], and has no cliff: the tenth finding costs less than the
first, which is how the marginal finding actually matters. `half_life` is the
density at which a category scores exactly 50, so each category's tolerance is
stated in the units of the thing being measured.
"""

from __future__ import annotations

# A high finding is worth ten lows. The gap is deliberate: the detectors are
# tuned to report conservatively at "high", so a single one should move a grade.
SEVERITY_WEIGHTS = {"high": 10.0, "medium": 3.0, "low": 1.0}

# key, label, weight (must total 100), half-life in weighted findings per KLOC.
CATEGORIES: tuple[tuple[str, str, float, float], ...] = (
    ("correctness", "Correctness", 25.0, 6.0),
    ("security", "Security", 15.0, 2.0),
    ("tests", "Tests & Safety Net", 15.0, 8.0),
    ("complexity", "Complexity", 15.0, 12.0),
    ("design", "Design & Structure", 12.0, 10.0),
    ("duplication", "Duplication & Dead Code", 10.0, 10.0),
    ("hygiene", "Dependencies & Hygiene", 8.0, 25.0),
)

CATEGORY_KEYS = tuple(key for key, _, _, _ in CATEGORIES)
CATEGORY_LABELS = {key: label for key, label, _, _ in CATEGORIES}
CATEGORY_WEIGHTS = {key: weight for key, _, weight, _ in CATEGORIES}
CATEGORY_HALF_LIVES = {key: half for key, _, _, half in CATEGORIES}

# Where an unmatched finding goes. Deliberately the lowest-weight category: an
# unmapped type should not be able to swing a grade before anyone notices it is
# unmapped, and `unmapped_types` in the metadata is how it gets noticed.
FALLBACK_CATEGORY = "hygiene"

# Detector category → rubric category. These keys are the `category` field
# python-code-doctor and typescript-code-doctor stamp on every finding.
DETECTOR_CATEGORIES = {
    # --- correctness: it computes the wrong answer, leaks, or silently no-ops
    "mutation_hazards": "correctness",
    "exception_issues": "correctness",
    "resource_leaks": "correctness",
    "unawaited_coroutines": "correctness",
    "async_issues": "correctness",
    "global_state": "correctness",
    "return_issues": "correctness",
    "duplicate_definitions": "correctness",
    "ai_scaffolding": "correctness",
    # --- security
    "security": "security",
    "tsconfig": "security",
    # --- tests
    "untested_modules": "tests",
    "test_smells": "tests",
    "coverage": "tests",
    # --- complexity
    "complexity": "complexity",
    "code_smells": "complexity",
    "loop_simplifications": "complexity",
    # --- design & structure
    "design_smells": "design",
    "overengineering": "design",
    "pattern_issues": "design",
    "coupling": "design",
    "parameter_objects": "design",
    "boolean_params": "design",
    "import_cycles": "design",
    "module_issues": "design",
    "local_imports": "design",
    "encapsulation": "design",
    # --- duplication & dead code
    "duplicates": "duplication",
    "dead_code": "duplication",
    "comment_smells": "duplication",
    # --- dependencies & hygiene
    "dependency_issues": "hygiene",
    "outdated_idioms": "hygiene",
    "naming_issues": "hygiene",
    "missing_docstrings": "hygiene",
    "type_gaps": "hygiene",
    "debug_leftovers": "hygiene",
    "unpythonic": "hygiene",
}

# django-code-doctor emits one flat list with no `category` field, so its
# finding types are mapped by name. Grouped by the detector they come from,
# because that is how they are maintained upstream.
SMELL_TYPES = {
    "correctness": (
        # find_query_issues
        "bulk_create_without_batch_size", "deprecated_extra", "index_instead_of_first",
        "len_of_queryset", "n_plus_one_query", "raw_sql", "read_modify_write_race",
        "update_on_sliced_queryset", "update_without_f",
        # find_model_issues — data-shape decisions that lose or corrupt data
        "auto_now_add_with_default", "decimal_without_precision",
        "file_field_without_upload_to", "missing_on_delete", "null_on_text_field",
        "save_ignores_update_fields", "related_name_disabled",
        # find_migration_issues
        "conflicting_leaf_migrations", "non_nullable_without_default",
        "run_python_imports_model", "run_python_without_reverse",
        "run_sql_without_reverse", "schema_and_data_in_one_migration",
        # find_transaction_issues
        "atomic_around_loop_of_saves", "external_call_in_atomic", "get_or_create_race",
        "integrity_error_caught_in_atomic", "select_for_update_outside_atomic",
        # find_async_issues
        "blocking_io_in_async", "enqueue_without_on_commit", "sync_orm_in_async_view",
        "sync_to_async_without_thread_sensitive", "task_takes_model_instance",
        "unawaited_async_orm_call",
        # find_form_issues
        "clean_method_returns_nothing", "cleaned_data_before_validation",
        "commit_false_without_save_m2m", "queryset_at_class_scope",
        "unvalidated_form_use",
        # queries hidden where they are not expected
        "admin_list_display_n_plus_one", "admin_get_queryset_without_super",
        "query_in_context_processor", "query_in_serializer_method_field",
        "query_in_template", "query_in_form_clean", "missing_pagination",
        # settings that break in production rather than merely offend
        "locmem_cache_in_production", "sqlite_in_production", "missing_use_tz",
        "settings_imports_models", "deprecated_storage_setting",
    ),
    "security": (
        # find_django_security
        "autoescape_off", "cors_allow_all", "hardcoded_secret",
        "mark_safe_on_dynamic_value", "missing_csp", "no_password_validators",
        "safe_filter_in_template", "spoofable_proxy_ssl_header", "weak_frame_options",
        "weak_password_hasher", "wildcard_allowed_hosts",
        # authorization — the whole point of the view and DRF detectors
        "csrf_exempt", "missing_ownership_filter", "open_redirect",
        "unauthenticated_mutation", "unfiltered_user_input_lookup",
        "unscoped_get_queryset", "unscoped_viewset_queryset", "permission_allow_any",
        "viewset_default_permission", "serializer_fields_all", "serializer_depth",
        "missing_throttling", "admin_action_without_permission_check",
        "mark_safe_in_admin_display",
        # settings with a security posture
        "authentication", "missing_hsts", "missing_ssl_redirect",
        "missing_security_middleware", "middleware_order", "production",
        # templates
        "missing_csrf_token", "template_var_in_script",
    ),
    "tests": (
        "client_login_over_force_login", "no_query_count_assertions",
        "on_commit_needs_transaction_testcase", "setuptestdata_mutation",
    ),
    "complexity": (
        "fat_model", "fat_view", "cbv_hook_overload", "too_many_fields",
        "deeply_nested_template_loop", "include_in_loop", "relation_walk_in_loop",
    ),
    "design": (
        # find_django_overengineering, wholesale
        "crud_only_service", "deep_form_inheritance", "empty_manager",
        "redundant_template_tag", "save_signal_for_simple_logic",
        "single_impl_abstract_model", "single_use_mixin", "thin_manager",
        "thin_middleware", "unused_abstract_model", "unused_mixin", "work_in_app_ready",
        # modelling decisions that shape the code rather than break it
        "multi_table_inheritance", "inline_choices",
        "unique_together_over_constraints", "missing_get_absolute_url",
    ),
    "hygiene": (
        "django_end_of_life", "django_version_unknown", "missing_default_auto_field",
        "default_user_model", "ospath_join_basedir", "static_url_variable",
        "hardcoded_url", "hardcoded_url_in_template", "url_without_name",
        "missing_str_method", "missing_related_name", "no_default_ordering",
        "redundant_db_index_on_fk", "admin_missing_search_fields",
        "fk_without_raw_id", "list_filter_high_cardinality",
    ),
}

SMELL_TYPE_CATEGORIES = {
    smell: category for category, smells in SMELL_TYPES.items() for smell in smells
}

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

SMELL_TYPE_CATEGORIES.update({
    smell: category
    for category, smells in CODE_DOCTOR_SMELLS.items()
    for smell in smells
})

# Last resort before the fallback. Matched against the finding's type token at
# word boundaries — see `keyword_matches`. Ordering is by specificity within a
# category, but ordering alone cannot keep keywords apart: `test` sits before
# `dependency`, so a plain substring match sent `latest_dependency` to Tests
# and reported it as a successful match, hiding the mistake from the
# unmapped-type caveat while swapping a 15%-weight category for an 8% one.
TYPE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("n_plus_one", "correctness"), ("race", "correctness"), ("leak", "correctness"),
    ("unawaited", "correctness"), ("await", "correctness"), ("mutation", "correctness"),
    ("exception", "correctness"), ("swallow", "correctness"),
    ("secret", "security"), ("csrf", "security"), ("xss", "security"),
    ("injection", "security"), ("permission", "security"), ("auth", "security"),
    ("insecure", "security"), ("security", "security"), ("unsafe", "security"),
    ("test", "tests"), ("coverage", "tests"), ("assert", "tests"), ("mock", "tests"),
    ("complex", "complexity"), ("nesting", "complexity"), ("long_", "complexity"),
    ("god_", "complexity"), ("fat_", "complexity"), ("too_many", "complexity"),
    ("duplicate", "duplication"), ("dead_", "duplication"), ("unused", "duplication"),
    ("commented", "duplication"),
    ("cycle", "design"), ("coupling", "design"), ("envy", "design"),
    ("overengineer", "design"), ("abstract", "design"), ("wrapper", "design"),
    ("factory", "design"), ("singleton", "design"),
    ("deprecat", "hygiene"), ("idiom", "hygiene"), ("naming", "hygiene"),
    ("docstring", "hygiene"), ("dependency", "hygiene"), ("annotation", "hygiene"),
)

# The standard US letter scale. Kept explicit rather than computed so the
# boundary anyone will ask about ("what is a B-?") is readable as data.
GRADE_BANDS: tuple[tuple[float, str], ...] = (
    (97.0, "A+"), (93.0, "A"), (90.0, "A-"),
    (87.0, "B+"), (83.0, "B"), (80.0, "B-"),
    (77.0, "C+"), (73.0, "C"), (70.0, "C-"),
    (67.0, "D+"), (63.0, "D"), (60.0, "D-"),
)

UNGRADED = "—"

# Which rubric categories a doctor is capable of speaking to. A category no
# detector covers must come back ungraded rather than as a free 100 — the
# difference between "clean" and "not looked at" is the whole point.
DOCTOR_COVERAGE = {
    "python-code-doctor": set(CATEGORY_KEYS),
    "typescript-code-doctor": set(CATEGORY_KEYS),
    # django-code-doctor is a companion to python-code-doctor, not a replacement:
    # it has no general duplication or dead-code detector of its own.
    "django-code-doctor": {"correctness", "security", "tests", "complexity", "design", "hygiene"},
    # code-doctor buys language-independence by giving up parsing. It can prove
    # a merge marker in a path git reports as unmerged and essentially nothing
    # else in the correctness class, so the category is left to a specialist
    # rather than credited from silence. Everything else it genuinely measures.
    "code-doctor": set(CATEGORY_KEYS) - {"correctness"},
}

# Doctors that parse markup as well as code. Their template lines belong in the
# denominator whenever they run, not only when a template happened to produce a
# finding: sizing off findings alone leaves a package full of *clean* templates
# measured over its Python lines only, and then the first template finding
# added drops thousands of lines into the divisor and *improves* the grade.
# A discontinuity that rewards finding a bug is the wrong shape entirely.
DOCTORS_ANALYZING_TEMPLATES = frozenset({"django-code-doctor"})


def finding_type(finding: dict) -> str:
    """The type token, under whichever key the emitting detector used."""
    for key in ("smell_type", "issue_type", "pattern_type", "type"):
        value = finding.get(key)
        if value:
            return str(value)
    return "issue"


def keyword_matches(needle: str, token: str) -> bool:
    """Does `needle` begin a word inside `token`?

    Finding types are snake_case, so a word begins at the start of the string
    or after a non-alphanumeric character. Matching there rather than anywhere
    keeps `test` out of `latest_dependency`, `greatest_hits` and `protest_*`
    while still letting a keyword cover a whole family by prefix — `complex`
    reaches `complexity`, `auth` reaches `authentication`, `deprecat` reaches
    both `deprecated` and `deprecation`. Keywords that are themselves
    multi-word (`n_plus_one`, `too_many`) match as written.
    """
    start = 0
    while True:
        index = token.find(needle, start)
        if index < 0:
            return False
        if index == 0 or not token[index - 1].isalnum():
            return True
        start = index + 1


def categorize(finding: dict) -> tuple[str, bool]:
    """Map one finding to a rubric category. Returns (category, matched).

    `matched` is False when nothing but the fallback applied — the caller
    records those types so a rubric gap is visible in the output instead of
    silently landing in hygiene.
    """
    detector = finding.get("category")
    if detector and detector in DETECTOR_CATEGORIES:
        return DETECTOR_CATEGORIES[detector], True

    token = finding_type(finding)
    if token in SMELL_TYPE_CATEGORIES:
        return SMELL_TYPE_CATEGORIES[token], True

    lowered = token.lower()
    for needle, category in TYPE_KEYWORDS:
        if keyword_matches(needle, lowered):
            return category, True

    # A detector category we do not know, but which names itself usefully.
    if detector:
        lowered_detector = str(detector).lower()
        for needle, category in TYPE_KEYWORDS:
            if keyword_matches(needle, lowered_detector):
                return category, True

    return FALLBACK_CATEGORY, False


def severity_weight(severity: str) -> float:
    return SEVERITY_WEIGHTS.get(str(severity).lower(), SEVERITY_WEIGHTS["medium"])


def density(weighted: float, loc: int) -> float:
    """Weighted findings per 1000 lines. Small units are floored at 1 KLOC.

    Without the floor a 40-line package with one medium finding scores 0, which
    says more about the divisor than about the code.
    """
    return weighted / max(loc, 1000) * 1000.0


def score_from_density(value: float, half_life: float) -> float:
    if value <= 0:
        return 100.0
    return 100.0 * (0.5 ** (value / half_life))


def grade_for(score: float | None) -> str:
    if score is None:
        return UNGRADED
    for threshold, letter in GRADE_BANDS:
        if score >= threshold:
            return letter
    return "F"


def weighted_overall(scores: dict[str, float | None]) -> float | None:
    """Weighted mean over graded categories only, weights renormalized.

    Passing None for a category means "not measured". Dropping it and
    renormalizing is the only honest option: scoring it 0 punishes a repo for a
    missing coverage artifact, and scoring it 100 rewards it for one.
    """
    total_weight = 0.0
    total = 0.0
    for key, _, weight, _ in CATEGORIES:
        value = scores.get(key)
        if value is None:
            continue
        total_weight += weight
        total += weight * value
    if total_weight <= 0:
        return None
    return total / total_weight


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
