#!/usr/bin/env python3
"""Django abstractions that never earned their keep.

Every finding here is cross-file by nature — "used once" and "extended by nobody"
are not questions a single file can answer. That is why this uses the real class
graph from django_context rather than matching on names: the original version of
this check flagged anything with "Mixin" in its name and anything with "Service"
in its name, which is a guess about intent rather than a fact about use.

A name is still evidence, just weaker than the graph. Where only the name is
available (a service class has no Django base to inherit), the finding says so
and is ranked low.
"""

import ast
import sys

from django_context import call_name
from django_report import finding, run

CRUD_NAMES = frozenset({"create", "get", "update", "delete", "list", "retrieve",
                        "save", "fetch", "remove", "add", "__init__"})
MIDDLEWARE_HOOKS = frozenset({"process_request", "process_response", "process_view",
                              "process_exception", "process_template_response",
                              "__init__", "__call__"})
DEEP_INHERITANCE = 3


def _uses_name(ctx, name):
    """How many classes in the project inherit from ``name``."""
    return len(ctx.subclasses.get(name, []))


def _inheritance_depth(ctx, name, seen=None):
    """Longest chain of project-defined ancestors above ``name``."""
    seen = seen or {name}
    info = ctx.classes.get(name)
    if info is None:
        return 0
    depths = [0]
    for base in info.bases:
        if base in seen or base not in ctx.classes:
            continue
        depths.append(1 + _inheritance_depth(ctx, base, seen | {base}))
    return max(depths)


def _signal_receivers(tree):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and call_name(decorator) == "receiver":
                signals = {a.attr if isinstance(a, ast.Attribute) else
                           a.id if isinstance(a, ast.Name) else None
                           for a in decorator.args}
                yield node, signals


def collect(ctx):
    findings = []
    models = ctx.models

    # ---- abstract models nobody extends, or only one thing extends -------- #
    for name, info in models.items():
        if not ctx.is_abstract(name) or ctx.is_ambiguous(name):
            continue
        children = ctx.concrete_subclasses(name)
        if not children:
            findings.append(finding(
                info.file, info.line, "unused_abstract_model",
                f"abstract model `{name}` is never extended — it describes a shape nothing "
                f"in this project has",
                "Delete it. Bring it back when a second model actually needs the fields.",
                "medium"))
        elif len(children) == 1:
            findings.append(finding(
                info.file, info.line, "single_impl_abstract_model",
                f"abstract model `{name}` has exactly one concrete child, `{children[0]}` — "
                f"the split adds a file to read and changes nothing",
                f"Merge the fields into `{children[0]}` until a second model needs them.",
                "low"))

    # ---- managers that wrap nothing ---------------------------------------- #
    for name, info in ctx.managers.items():
        if ctx.is_ambiguous(name):
            continue
        custom = [m for m in info.methods if not m.startswith("_") and m != "get_queryset"]
        if not custom and "get_queryset" not in info.methods:
            findings.append(finding(
                info.file, info.line, "empty_manager",
                f"manager `{name}` adds no methods and no get_queryset",
                "Delete it; the default manager already does this.",
                "medium"))
        elif len(custom) == 1 and not ctx.subclasses.get(name):
            findings.append(finding(
                info.file, info.line, "thin_manager",
                f"manager `{name}` adds a single method, `{custom[0]}`",
                "A QuerySet method plus .as_manager() chains and composes; a one-method "
                "manager does neither.",
                "low"))

    # ---- mixins used once -------------------------------------------------- #
    for name, info in ctx.classes.items():
        # The suffix is the only signal a mixin gives: it has no Django base class
        # to inherit, so the graph cannot identify one. Convention it is.
        if not name.endswith("Mixin") or ctx.is_ambiguous(name):
            continue
        uses = _uses_name(ctx, name)
        if uses == 0:
            findings.append(finding(
                info.file, info.line, "unused_mixin",
                f"mixin `{name}` is never mixed into anything",
                "Delete it.", "medium"))
        elif uses == 1:
            findings.append(finding(
                info.file, info.line, "single_use_mixin",
                f"mixin `{name}` is used by exactly one class, so it is indirection without "
                f"reuse",
                "Inline it into its single user until a second one appears.",
                "low"))

    # ---- forms and serializers stacked too deep ---------------------------- #
    for name, info in ctx.forms.items():
        if ctx.is_ambiguous(name):
            continue
        depth = _inheritance_depth(ctx, name)
        if depth >= DEEP_INHERITANCE:
            findings.append(finding(
                info.file, info.line, "deep_form_inheritance",
                f"`{name}` sits {depth} levels deep in project-defined classes — working out "
                f"which fields it has means reading {depth} files",
                "Flatten it. Share field definitions with composition or a plain dict rather "
                "than a chain of subclasses.",
                "medium"))

    # ---- middleware and services ------------------------------------------- #
    for name, info in ctx.classes.items():
        if name.endswith("Middleware") and not ctx.is_ambiguous(name):
            hooks = [m for m in info.methods if m in MIDDLEWARE_HOOKS and m not in ("__init__", "__call__")]
            call = info.methods.get("__call__")
            trivial_call = call is not None and len(call.body) <= 2
            if not hooks and trivial_call:
                findings.append(finding(
                    info.file, info.line, "thin_middleware",
                    f"middleware `{name}` runs on every single request to do almost nothing",
                    "A decorator on the views that need it costs nothing on the requests that "
                    "do not.",
                    "low"))

        if name.endswith("Service") and not ctx.is_ambiguous(name):
            method_names = {m.lower() for m in info.methods}
            if method_names and method_names <= CRUD_NAMES:
                findings.append(finding(
                    info.file, info.line, "crud_only_service",
                    f"`{name}` only wraps create/get/update/delete — a layer that forwards to "
                    f"the ORM and adds no rule of its own",
                    "Call the ORM from the caller, or move the behaviour onto the model. "
                    "(Identified by name; confirm it is not doing something subtler.)",
                    "low"))

    # ---- signals doing what save() should ---------------------------------- #
    for path in ctx.files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        for node, signals in _signal_receivers(tree):
            if not signals & {"pre_save", "post_save"}:
                continue
            statements = [s for s in node.body if not isinstance(s, (ast.Pass, ast.Expr))]
            if len(statements) <= 3:
                findings.append(finding(
                    path, node.lineno, "save_signal_for_simple_logic",
                    f"`{node.name}` is a save signal doing {len(statements)} statement(s) — "
                    f"behaviour that fires invisibly from anything that touches the model",
                    "Put it in the model's save(), or in an explicit method the caller names. "
                    "Signals are for decoupling apps, not for shortening a method.",
                    "medium"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_django_overengineering", "Django over-engineering", collect))
