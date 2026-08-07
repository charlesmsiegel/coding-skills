#!/usr/bin/env python3
"""ModelAdmin problems — mostly performance, and one authorization gap.

The admin is the part of a Django project that gets the least review and the
most privilege: a full CRUD interface over the database behind a single boolean.
Two things go wrong there repeatedly.

Performance: the changelist renders one row per record and evaluates
`list_display` per row. A callable that walks a relation is therefore an N+1 that
only appears on the page nobody load-tests. `list_select_related` exists for
exactly this and is almost never set.

Authorization: a custom admin action operates on a queryset the user selected,
and Django does not check per-object permissions for it. An action that deletes
or approves without a permission check is a privilege escalation for any staff
user.
"""

import ast
import sys

from django_context import (RELATION_FIELDS, call_name, decorator_name, source_of,
                            string_value)
from django_report import finding, run

# A changelist with more than this many rows per page is where the N+1 hurts.
DEFAULT_LIST_PER_PAGE = 100
# Field types whose distinct values are effectively unbounded, so a list_filter
# on them renders a sidebar with one entry per row.
HIGH_CARDINALITY_FIELDS = frozenset({
    "CharField", "TextField", "EmailField", "URLField", "SlugField", "UUIDField",
    "DateTimeField", "DecimalField", "FloatField", "IntegerField", "BigIntegerField",
})
# Filters that are fine on any field, because they bucket rather than enumerate.
BUCKETING_FILTERS = frozenset({"DateFieldListFilter", "BooleanFieldListFilter"})

ACTION_WRITE_CALLS = frozenset({"delete", "update", "save", "bulk_update", "create"})
PERMISSION_MARKERS = ("has_perm", "has_permission", "permission_required",
                      "user.is_superuser", "raise PermissionDenied")


def _string_list(node):
    """The string constants in a list/tuple literal."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    return [s for s in (string_value(e) for e in node.elts) if s is not None]


def _admin_model(ctx, info):
    """The model a ModelAdmin administers, from @register(Model) or Meta."""
    for decorator in info.node.decorator_list:
        if isinstance(decorator, ast.Call) and decorator_name(decorator) == "register":
            for arg in decorator.args:
                name = arg.id if isinstance(arg, ast.Name) else getattr(arg, "attr", None)
                if name and name in ctx.models:
                    return ctx.models[name]
    model = info.assignments.get("model")
    if model is not None:
        name = model.id if isinstance(model, ast.Name) else getattr(model, "attr", None)
        if name and name in ctx.models:
            return ctx.models[name]
    return None


def _relation_names(ctx, model_info):
    """Relation field names on this model, so a list_display entry can be judged."""
    if model_info is None:
        return set()
    return {attr for attr, kind in model_info.fields.items() if kind in RELATION_FIELDS}


def _walks_a_relation(entry, relations):
    """`customer__name`, or a bare relation field rendered as a row value."""
    if "__" in entry:
        return True
    return entry in relations


def _method_walks_a_relation(info, entry, relations):
    """A list_display callable whose body reaches through a relation.

    Narrow on purpose: `obj.customer.name` is the finding, `self.model` is not.
    The middle component has to be a relation on the model, and the whole chain
    has to start from a plain name rather than from self.
    """
    method = info.methods.get(entry)
    if method is None:
        return False
    for node in ast.walk(method):
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)):
            continue
        middle = node.value
        if not isinstance(middle.value, ast.Name) or middle.value.id == "self":
            continue
        if middle.attr in relations:
            return True
    return False


def _checks_permission(ctx, path, func):
    body = source_of(ctx, path, func)
    return any(marker in body for marker in PERMISSION_MARKERS)


def _is_admin_action(info, method_name, method):
    """Registered with @admin.action, or listed in `actions`."""
    if any(decorator_name(d) == "action" for d in method.decorator_list):
        return True
    actions = info.assignments.get("actions")
    return method_name in _string_list(actions)


def collect(ctx):
    findings = []

    for name, info in sorted(ctx.admins.items(), key=lambda kv: (str(kv[1].file), kv[1].line)):
        if ctx.is_ambiguous(name):
            continue
        path = info.file
        model_info = _admin_model(ctx, info)
        relations = _relation_names(ctx, model_info)

        list_display = _string_list(info.assignments.get("list_display"))
        select_related = info.assignments.get("list_select_related")

        # The changelist N+1: a relation walked per row, with no join set up.
        if list_display and select_related is None:
            walkers = [e for e in list_display
                       if _walks_a_relation(e, relations) or _method_walks_a_relation(info, e, relations)]
            if walkers:
                findings.append(finding(
                    path, info.line, "admin_list_display_n_plus_one",
                    "`" + name + ".list_display` renders " + ", ".join(walkers[:3]) + ", which "
                    "reaches through a relation, and list_select_related is not set — the "
                    "changelist issues one extra query per row, " + str(DEFAULT_LIST_PER_PAGE) +
                    " times per page by default",
                    "Set list_select_related = [" +
                    ", ".join("'" + w.split("__")[0] + "'" for w in sorted(set(walkers))[:3]) +
                    "], or override get_queryset with the select_related the page needs.",
                    "medium"))

        # list_filter on a free-text or continuous column renders one sidebar
        # entry per distinct value, which on a large table is the whole table.
        for entry in _string_list(info.assignments.get("list_filter")):
            if entry in BUCKETING_FILTERS or "__" in entry:
                continue
            kind = (model_info.fields.get(entry) if model_info else None)
            if kind in HIGH_CARDINALITY_FIELDS:
                findings.append(finding(
                    path, info.line, "list_filter_high_cardinality",
                    "`" + name + ".list_filter` includes '" + entry + "', a " + kind + " — the "
                    "sidebar renders one link per distinct value, so this scales with the table",
                    "Filter on a low-cardinality column (a status, a boolean, a FK with few rows), "
                    "or use a custom SimpleListFilter that offers fixed buckets. For dates, "
                    "('" + entry + "', DateFieldListFilter).",
                    "medium"))

        # A FK rendered as a dropdown loads every row of the target table.
        if model_info is not None and relations:
            raw_id = set(_string_list(info.assignments.get("raw_id_fields")))
            autocomplete = set(_string_list(info.assignments.get("autocomplete_fields")))
            handled = raw_id | autocomplete
            unhandled = sorted(relations - handled)
            if unhandled and (list_display or "fields" in info.assignments):
                findings.append(finding(
                    path, info.line, "fk_without_raw_id",
                    "`" + name + "` edits the relation(s) " + ", ".join(unhandled[:3]) + " with the "
                    "default widget, which renders a <select> containing every row of the related "
                    "table — fine at a hundred rows, a timeout at a million",
                    "Add autocomplete_fields = [" +
                    ", ".join("'" + f + "'" for f in unhandled[:3]) +
                    "] (needs search_fields on the related admin), or raw_id_fields for a plain "
                    "id input.",
                    "low"))

        # A changelist with no search on a model that has text to search.
        if list_display and "search_fields" not in info.assignments:
            findings.append(finding(
                path, info.line, "admin_missing_search_fields",
                "`" + name + "` sets list_display but no search_fields, so the only way to find a "
                "row is to page through the changelist",
                "Add search_fields for the columns a human would search by. It is also what "
                "autocomplete_fields on other admins requires.",
                "low"))

        # get_queryset that drops the base queryset entirely.
        get_queryset = info.methods.get("get_queryset")
        if get_queryset is not None:
            calls_super = any(isinstance(n, ast.Call) and call_name(n) == "get_queryset"
                              for n in ast.walk(get_queryset))
            if not calls_super:
                findings.append(finding(
                    path, get_queryset.lineno, "admin_get_queryset_without_super",
                    "`" + name + ".get_queryset()` builds a queryset without calling super(), so "
                    "it discards the admin's own ordering and any queryset a parent class set up",
                    "Start from super().get_queryset(request) and refine it.",
                    "medium"))

        for method_name, method in info.methods.items():
            # An action writes to whatever the staff user ticked. Django checks
            # the model permission for the changelist, not for what the action does.
            if _is_admin_action(info, method_name, method):
                writes = any(isinstance(n, ast.Call) and call_name(n) in ACTION_WRITE_CALLS
                             for n in ast.walk(method))
                if writes and not _checks_permission(ctx, path, method):
                    findings.append(finding(
                        path, method.lineno, "admin_action_without_permission_check",
                        "admin action `" + method_name + "` writes to every selected row and "
                        "checks no permission — any staff user who can see the changelist can "
                        "run it",
                        "Check explicitly: if not request.user.has_perm('app.change_model'): "
                        "raise PermissionDenied. The changelist permission is not the action's "
                        "permission.",
                        "high"))

            # mark_safe in a display callable is XSS inside the admin.
            if method_name in list_display or method_name.endswith("_display"):
                for node in ast.walk(method):
                    if isinstance(node, ast.Call) and call_name(node) in ("mark_safe", "SafeString"):
                        findings.append(finding(
                            path, node.lineno, "mark_safe_in_admin_display",
                            "`" + name + "." + method_name + "` renders with mark_safe, so any "
                            "user-supplied value it interpolates is executed in an admin session "
                            "— the highest-privilege browser context in the project",
                            "Use format_html('<b>{}</b>', value), which escapes the arguments.",
                            "high"))
                        break

    return findings


if __name__ == "__main__":
    sys.exit(run("find_admin_issues", "Django admin issues", collect))
