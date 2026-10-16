#!/usr/bin/env python3
"""Form and ModelForm problems, mostly about what gets accepted and when.

A form is the boundary between what a user sent and what the database stores, so
the failures here are not stylistic. `fields = "__all__"` means every field
added to the model in future is silently writable from the web — including the
`is_staff` someone adds next year. A collection materialized from a queryset
at class scope is a snapshot taken at import. Ordinary lazy querysets on Django
model choice fields are valid and are cloned by the fields.
"""

import ast
import sys

from django_context import FORM_BASES, call_name, string_value
from django_report import candidate, finding, run


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


_LAZY_QUERYSET_METHODS = {
    "all", "none", "filter", "exclude", "order_by", "reverse", "distinct",
    "select_related", "prefetch_related", "only", "defer", "annotate", "alias",
    "values", "values_list", "using", "select_for_update", "union",
    "intersection", "difference", "extra",
}


def _known_lazy_queryset(value):
    if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
        return False
    if value.func.attr not in _LAZY_QUERYSET_METHODS:
        return False
    receiver = value.func.value
    return (isinstance(receiver, ast.Attribute) and receiver.attr == "objects"
            or _known_lazy_queryset(receiver))


def _queryset_call_at_class_scope(value):
    """Distinguish eager collections from normal, lazy form field querysets."""
    if _known_lazy_queryset(value):
        return None
    if isinstance(value, ast.Call):
        for kw in value.keywords:
            if kw.arg in {"queryset", "choices"}:
                result = _queryset_call_at_class_scope(kw.value)
                if result:
                    return result
        if (isinstance(value.func, ast.Name) and value.func.id in {"list", "tuple", "set", "dict", "sorted"}
                and value.args and _known_lazy_queryset(value.args[0])):
            return "finding"
    if isinstance(value, (ast.ListComp, ast.SetComp, ast.DictComp)):
        if any(_known_lazy_queryset(gen.iter) for gen in value.generators):
            return "finding"
    if _reaches_a_manager(value):
        return "candidate"
    return None


def _guarded_cleaned_data_reads(func):
    """Recognize the narrow short-circuit membership guard used by form helpers."""
    guarded = set()
    for expr in ast.walk(func):
        if not isinstance(expr, ast.BoolOp) or not isinstance(expr.op, ast.And):
            continue
        receivers = set()
        for value in expr.values:
            for read in ast.walk(value):
                if (isinstance(read, ast.Attribute) and read.attr == "cleaned_data"
                        and ast.dump(read.value) in receivers):
                    guarded.add(read)
            # A successful membership test against getattr(..., {}) proves the
            # attribute exists; a bare truthy getattr default would not.
            if not (isinstance(value, ast.Compare) and len(value.ops) == 1
                    and isinstance(value.ops[0], ast.In)):
                continue
            call = value.comparators[0]
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "getattr" and len(call.args) == 3
                    and string_value(call.args[1]) == "cleaned_data"
                    and isinstance(call.args[2], ast.Dict) and not call.args[2].keys):
                receivers.add(ast.dump(call.args[0]))
    return guarded


def _cleaned_data_before_is_valid(func, validation_hook=False):
    """Find reads needing review when validation is not visible locally.

    Callers and alternate validation entry points remain unknown, so a returned
    read is a candidate rather than proof of invalid access.
    """
    saw_is_valid = False
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and call_name(node) in ("is_valid", "ais_valid"):
            saw_is_valid = True
    if saw_is_valid:
        return None
    guarded = _guarded_cleaned_data_reads(func)
    if validation_hook:
        # Formset.clean runs after its child forms have been cleaned. Limit
        # this exemption to comprehensions directly over self.forms.
        for comp in ast.walk(func):
            if not isinstance(comp, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                continue
            for gen in comp.generators:
                if (isinstance(gen.target, ast.Name) and isinstance(gen.iter, ast.Attribute)
                        and gen.iter.attr == "forms" and isinstance(gen.iter.value, ast.Name)
                        and gen.iter.value.id == "self"):
                    guarded.update(read for read in ast.walk(comp)
                                   if isinstance(read, ast.Attribute) and read.attr == "cleaned_data"
                                   and isinstance(read.value, ast.Name) and read.value.id == gen.target.id)
    for node in ast.walk(func):
        if node in guarded:
            continue
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

        # Ordinary QuerySets are lazy and Django fields clone them. Only
        # materialized collections establish a class-level snapshot.
        for attr, value in info.assignments.items():
            result = _queryset_call_at_class_scope(value)
            if result == "finding":
                findings.append(finding(
                    path, getattr(value, "lineno", info.line), "queryset_at_class_scope",
                    "`" + name + "." + attr + "` materializes database rows in the class body; "
                    "this collection is a snapshot taken at import time",
                    "Build the collection per form instance, or pass a lazy QuerySet to a "
                    "ModelChoiceField or ModelMultipleChoiceField.",
                    "medium"))
            elif result == "candidate":
                findings.append(candidate(
                    path, getattr(value, "lineno", info.line), "queryset_at_class_scope",
                    "`" + name + "." + attr + "` calls a manager method at class scope; "
                    "check whether it materializes rows at import time",
                    ["A custom manager method may return an unevaluated QuerySet, "
                     "which is valid for Django model choice fields."],
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

    validation_hooks = set()
    formset_bases = {"BaseFormSet", "BaseModelFormSet", "BaseInlineFormSet"}
    for name, info in ctx.classes.items():
        if not ctx.is_ambiguous(name) and ctx.derives_from(name, formset_bases):
            method = info.methods.get("clean")
            if method is not None:
                validation_hooks.add((info.file, method.lineno))

    # ---- form usage in views ------------------------------------------------ #
    for path, tree in ctx.python_trees():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            early = _cleaned_data_before_is_valid(node, (path, node.lineno) in validation_hooks)
            if early is not None:
                findings.append(candidate(
                    path, early.lineno, "cleaned_data_before_validation",
                    "cleaned_data is read in `" + node.name + "` without a local is_valid() call; "
                    "check whether validation has already run for this receiver",
                    ["The caller may have validated the form before passing it to this helper.",
                     "Accessing errors or calling full_clean() also triggers validation."],
                    "high"))

            commit_false = _save_commit_false_without_m2m(node)
            if commit_false is not None:
                findings.append(candidate(
                    path, commit_false.lineno, "commit_false_without_save_m2m",
                    "`" + node.name + "` calls save(commit=False) without a local save_m2m(); "
                    "check whether this form includes many-to-many fields that need saving",
                    ["The form may have no many-to-many fields.",
                     "The caller may perform the deferred many-to-many save."],
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
