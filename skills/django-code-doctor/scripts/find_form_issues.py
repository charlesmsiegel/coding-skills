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


def _has_unvalidated_form_save(func, ctx):
    """Match a save to its constructed form within this function's scope.

    Track simple aliases and rebinding in source order. This is deliberately a
    local receiver check, not a claim that validation dominates every branch.
    Nested functions have their own scope and are inspected separately.
    """
    class Usage(ast.NodeVisitor):
        def __init__(self):
            self.bindings = {}
            self.validated = set()
            self.unvalidated = False

        def receiver_key(self, expr):
            if isinstance(expr, ast.Name):
                return (expr.id,)
            if isinstance(expr, ast.Attribute):
                root = self.receiver_key(expr.value)
                if root is not None:
                    return (*root, expr.attr)
            return None

        def receiver(self, expr):
            if isinstance(expr, ast.Call):
                target = call_name(expr)
                if target and target.endswith("Form") and _is_form_or_serializer(ctx, target):
                    return expr
            if isinstance(expr, (ast.Name, ast.Attribute)):
                return self.bindings.get(self.receiver_key(expr))
            return None

        def bind(self, target, form):
            if isinstance(target, (ast.Name, ast.Attribute)):
                key = self.receiver_key(target)
                if key is None:
                    return
                # Replacing a root or intermediate attribute also replaces
                # receivers beneath it, such as holder.form or self.box.form.
                for existing in list(self.bindings):
                    if existing[:len(key)] == key:
                        del self.bindings[existing]
                self.bindings[key] = form
            elif isinstance(target, (ast.Tuple, ast.List)):
                # Unpacking is unsupported: forget every target instead of
                # retaining an instance from an earlier assignment.
                for item in target.elts:
                    self.bind(item, None)
            elif isinstance(target, ast.Starred):
                self.bind(target.value, None)

        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.bind(node, None)

        def visit_Attribute(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.bind(node, None)
            self.visit(node.value)

        def visit_For(self, node):
            self.visit(node.iter)
            self.bind(node.target, None)
            for statement in [*node.body, *node.orelse]:
                self.visit(statement)

        visit_AsyncFor = visit_For

        def visit_MatchAs(self, node):
            if node.pattern is not None:
                self.visit(node.pattern)
            if node.name:
                self.bind(ast.Name(id=node.name, ctx=ast.Load()), None)

        def visit_MatchStar(self, node):
            if node.name:
                self.bind(ast.Name(id=node.name, ctx=ast.Load()), None)

        def visit_MatchMapping(self, node):
            self.generic_visit(node)
            if node.rest:
                self.bind(ast.Name(id=node.rest, ctx=ast.Load()), None)

        def visit_comprehension_scope(self, node):
            # Comprehension targets live in their own scope. Preserve bindings
            # for those roots while still visiting iterables and expressions.
            roots = {target.id for gen in node.generators for target in ast.walk(gen.target)
                     if isinstance(target, ast.Name)}
            saved = {key: value for key, value in self.bindings.items() if key[0] in roots}
            for gen in node.generators:
                self.visit(gen.iter)
                self.bind(gen.target, None)
                for condition in gen.ifs:
                    self.visit(condition)
            if isinstance(node, ast.DictComp):
                self.visit(node.key)
                self.visit(node.value)
            else:
                self.visit(node.elt)
            for key in list(self.bindings):
                if key[0] in roots:
                    del self.bindings[key]
            self.bindings.update(saved)

        visit_ListComp = visit_comprehension_scope
        visit_SetComp = visit_comprehension_scope
        visit_DictComp = visit_comprehension_scope
        visit_GeneratorExp = visit_comprehension_scope

        def visit_ExceptHandler(self, node):
            if node.type is not None:
                self.visit(node.type)
            if node.name:
                self.bind(ast.Name(id=node.name, ctx=ast.Load()), None)
            for statement in node.body:
                self.visit(statement)
            if node.name:
                self.bind(ast.Name(id=node.name, ctx=ast.Load()), None)

        def visit_Import(self, node):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                self.bind(ast.Name(id=name, ctx=ast.Load()), None)

        def visit_ImportFrom(self, node):
            for alias in node.names:
                self.bind(ast.Name(id=alias.asname or alias.name, ctx=ast.Load()), None)

        def visit_Assign(self, node):
            self.visit(node.value)
            form = self.receiver(node.value)
            for target in node.targets:
                self.bind(target, form)

        def visit_AnnAssign(self, node):
            if node.value is not None:
                self.visit(node.value)
                self.bind(node.target, self.receiver(node.value))

        def visit_NamedExpr(self, node):
            self.visit(node.value)
            self.bind(node.target, self.receiver(node.value))

        def visit_AugAssign(self, node):
            self.visit(node.value)
            self.bind(node.target, None)

        def visit_Call(self, node):
            self.generic_visit(node)
            if not isinstance(node.func, ast.Attribute):
                return
            form = self.receiver(node.func.value)
            if form is None:
                return
            if node.func.attr in {"is_valid", "ais_valid"}:
                self.validated.add(form)
            elif node.func.attr == "save" and form not in self.validated:
                self.unvalidated = True

        def visit_FunctionDef(self, node):
            self.bind(ast.Name(id=node.name, ctx=ast.Load()), None)

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef

        def visit_Lambda(self, node):
            pass

    usage = Usage()
    for statement in func.body:
        usage.visit(statement)
    return usage.unvalidated


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

    # A local form save without an explicit validation call needs review:
    # ModelForm.save() validates through errors, and custom saves may validate too.
    for path, tree in ctx.python_trees():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _has_unvalidated_form_save(node, ctx):
                findings.append(candidate(
                    path, node.lineno, "unvalidated_form_use",
                    "`" + node.name + "` saves a locally constructed form without an earlier "
                    "explicit is_valid() call on that receiver; check its save implementation "
                    "and whether validation is required for this operation",
                    ["Django ModelForm.save() accesses errors, which triggers full_clean() "
                     "when needed and rejects invalid forms.",
                     "A custom save() may validate internally, or intentionally return an "
                     "instance without consuming submitted data."],
                    "high"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_form_issues", "Django form issues", collect))
