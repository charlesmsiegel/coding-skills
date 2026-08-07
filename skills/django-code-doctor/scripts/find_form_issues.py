#!/usr/bin/env python3
"""Form and ModelForm problems, mostly about what gets accepted and when.

A form is the boundary between what a user sent and what the database stores, so
the failures here are not stylistic. `fields = "__all__"` means every field
added to the model in future is silently writable from the web — including the
`is_staff` someone adds next year. And a queryset evaluated at class scope is
evaluated once, at import, and then serves stale rows for the life of the
process.
"""

import ast
import sys

from django_context import FORM_BASES, call_name, string_value
from django_report import finding, run


def _returns_something(func):
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and node.value is not None:
            return True
    return False


def _is_form_or_serializer(ctx, name):
    return ctx.derives_from(name, FORM_BASES) or name in ctx.serializers


def _reaches_a_manager(node):
    """Whether an expression is rooted in `<Model>.objects`."""
    names = []
    current = node
    while isinstance(current, (ast.Call, ast.Attribute)):
        if isinstance(current, ast.Call):
            current = current.func
            continue
        names.append(current.attr)
        current = current.value
    return "objects" in names


def _queryset_call_at_class_scope(value):
    """`Model.objects.all()` evaluated in a class body.

    Class bodies execute once, at import. A queryset built there is built with
    the process, so rows created afterwards never appear in the field's choices
    until the worker restarts.

    Usually the queryset is a keyword on the field constructor —
    `ModelChoiceField(queryset=Customer.objects.all())` — so the outer call is
    the field and the manager is one level in.
    """
    if _reaches_a_manager(value):
        return True
    if isinstance(value, ast.Call):
        for kw in value.keywords:
            if kw.arg == "queryset" and _reaches_a_manager(kw.value):
                return True
    return False


def _cleaned_data_before_is_valid(func):
    """cleaned_data read in a body that never checks is_valid() first.

    Django only populates cleaned_data during full_clean(), which is_valid()
    triggers. Reading it first gets an AttributeError or a stale dict.
    """
    saw_is_valid = False
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and call_name(node) in ("is_valid", "ais_valid"):
            saw_is_valid = True
    if saw_is_valid:
        return None
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and node.attr == "cleaned_data":
            # `self.cleaned_data` inside a form method is correct — clean() and
            # clean_<field>() run after validation by definition.
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                continue
            return node
    return None


def _save_commit_false_without_m2m(func):
    """form.save(commit=False) with no later save_m2m().

    commit=False defers the write, and Django puts the many-to-many write behind
    save_m2m(). Forgetting it drops every tag, category, and permission the user
    selected — silently, because the object saves fine.
    """
    commit_false = None
    for node in ast.walk(func):
        if not (isinstance(node, ast.Call) and call_name(node) == "save"):
            continue
        for kw in node.keywords:
            if kw.arg == "commit" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                commit_false = node
    if commit_false is None:
        return None
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and call_name(node) == "save_m2m":
            return None
    return commit_false


def collect(ctx):
    findings = []

    for name, info in sorted(ctx.forms.items(), key=lambda kv: (str(kv[1].file), kv[1].line)):
        if ctx.is_ambiguous(name):
            continue
        path = info.file
        is_serializer = name in ctx.serializers
        smell = "serializer_fields_all" if is_serializer else "form_fields_all"

        fields = info.meta.get("fields")
        if string_value(fields) == "__all__":
            findings.append(finding(
                path, info.line, smell,
                "`" + name + "` sets fields = '__all__', so every column on the model is "
                "writable from the web — including the ones added to the model next year, which "
                "nobody will revisit this form to reconsider",
                "List the fields explicitly. That way adding a field to the model is a decision "
                "about this form rather than a change to it.",
                "high"))

        # `exclude` has the same failure mode one step removed.
        if info.meta.get("exclude") is not None and fields is None:
            findings.append(finding(
                path, info.line, smell,
                "`" + name + "` uses exclude rather than fields, so it opts out of named columns "
                "and opts in to everything else — a field added to the model later is writable "
                "by default",
                "Use fields = [...] and name what this form accepts.",
                "medium"))

        # A queryset built in the class body is built once, at import.
        for attr, value in info.assignments.items():
            if isinstance(value, ast.Call) and _queryset_call_at_class_scope(value):
                findings.append(finding(
                    path, info.line, "queryset_at_class_scope",
                    "`" + name + "." + attr + "` builds its queryset in the class body, which runs "
                    "once at import — rows created after the worker started never appear",
                    "Move it into __init__: self.fields['" + attr + "'].queryset = "
                    "Model.objects.filter(...). That also lets it depend on the request.",
                    "medium"))

        # clean_<field> that falls off the end returns None, which Django stores.
        for method_name, method in info.methods.items():
            if not method_name.startswith("clean_"):
                continue
            if not _returns_something(method):
                findings.append(finding(
                    path, method.lineno, "clean_method_returns_nothing",
                    "`" + name + "." + method_name + "` never returns a value, so Django stores "
                    "None for that field — validation passes and the data is wiped",
                    "Return the cleaned value at the end: return value.",
                    "high"))

    # ---- form usage in views ------------------------------------------------ #
    for path, tree in ctx.python_trees():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            early = _cleaned_data_before_is_valid(node)
            if early is not None:
                findings.append(finding(
                    path, early.lineno, "cleaned_data_before_validation",
                    "cleaned_data is read in `" + node.name + "` without is_valid() having been "
                    "called — Django only populates it during validation, so this reads a stale "
                    "dict or raises AttributeError",
                    "Call is_valid() first and branch on it.",
                    "high"))

            commit_false = _save_commit_false_without_m2m(node)
            if commit_false is not None:
                findings.append(finding(
                    path, commit_false.lineno, "commit_false_without_save_m2m",
                    "`" + node.name + "` calls form.save(commit=False) and never calls "
                    "save_m2m() — every many-to-many selection the user made is dropped, and the "
                    "save itself succeeds so nothing looks wrong",
                    "Call form.save_m2m() after saving the instance.",
                    "medium"))

    # A form whose validity is never checked before use.
    for path, tree in ctx.python_trees():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            builds_form = False
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    target = call_name(sub)
                    if target and target.endswith("Form") and _is_form_or_serializer(ctx, target):
                        builds_form = True
            if not builds_form:
                continue
            checks = any(isinstance(sub, ast.Call) and call_name(sub) in ("is_valid", "ais_valid")
                         for sub in ast.walk(node))
            saves = any(isinstance(sub, ast.Call) and call_name(sub) == "save"
                        for sub in ast.walk(node))
            if saves and not checks:
                findings.append(finding(
                    path, node.lineno, "unvalidated_form_use",
                    "`" + node.name + "` builds a form and saves without calling is_valid(), so "
                    "nothing runs the validators",
                    "Guard the save: if form.is_valid(): form.save(). Without it the field "
                    "validators, clean_<field> methods, and unique checks are all skipped.",
                    "high"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_form_issues", "Django form issues", collect))
