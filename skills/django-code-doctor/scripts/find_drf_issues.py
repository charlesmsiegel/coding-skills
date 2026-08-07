#!/usr/bin/env python3
"""Django REST Framework problems, mostly about who can see which rows.

DRF's defaults are permissive in a way that reads as configured. `AllowAny` is
the default permission class unless a project sets DEFAULT_PERMISSION_CLASSES,
so an endpoint with no `permission_classes` is public — and it looks exactly
like an endpoint whose permissions are handled elsewhere.

The other half is the shape of an API response. A serializer that walks a
relation per row is an N+1 that scales with the page size, and `depth = 1` is a
one-line way to do it to every relation at once.

Gated twice over: the Django gate in django_context, plus `ctx.uses_drf`. Firing
DRF rules at a project with no DRF is the same failure as firing Django rules at
Flask, one level down.
"""

import ast
import sys

from django_context import (API_VIEW_BASES, VIEWSET_BASES, call_name, source_of,
                            string_value)
from django_report import finding, run

# Permission classes that grant access to everyone.
OPEN_PERMISSIONS = frozenset({"AllowAny"})
# Views that return a collection, and therefore need pagination.
LIST_BASES = frozenset({"ListAPIView", "ListCreateAPIView", "ModelViewSet",
                        "ReadOnlyModelViewSet", "GenericViewSet"})
REQUEST_SCOPED = ("request.user", "self.request.user")


def _names_in(node):
    """Every bare/dotted name in a list literal: [IsAuthenticated] -> {'IsAuthenticated'}."""
    found = set()
    if not isinstance(node, (ast.List, ast.Tuple)):
        return found
    for element in node.elts:
        if isinstance(element, ast.Name):
            found.add(element.id)
        elif isinstance(element, ast.Attribute):
            found.add(element.attr)
        elif isinstance(element, ast.Call):
            target = call_name(element)
            if target:
                found.add(target)
    return found


def _settings_default(ctx, key):
    """Whether REST_FRAMEWORK in settings sets ``key``."""
    for path in ctx.settings_files:
        tree = ctx.parsed(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "REST_FRAMEWORK" not in targets or not isinstance(node.value, ast.Dict):
                continue
            for entry in node.value.keys:
                if string_value(entry) == key:
                    return True
    return False


def _is_list_view(ctx, name, info):
    return bool(set(info.bases) & LIST_BASES) or ctx.derives_from(name, LIST_BASES)


def _method_field_queries(info, ctx):
    """SerializerMethodField callables that hit the database per row."""
    hits = []
    method_fields = {attr for attr, value in info.assignments.items()
                     if isinstance(value, ast.Call) and call_name(value) == "SerializerMethodField"}
    for attr in sorted(method_fields):
        method = info.methods.get("get_" + attr)
        if method is None:
            continue
        for node in ast.walk(method):
            manager = isinstance(node, ast.Attribute) and node.attr == "objects"
            aggregate = isinstance(node, ast.Call) and call_name(node) in (
                "count", "filter", "exclude", "aggregate", "all", "exists", "first")
            if manager or aggregate:
                hits.append((attr, method))
                break
    return hits


def collect(ctx):
    findings = []
    if not ctx.uses_drf:
        return findings

    has_default_permission = _settings_default(ctx, "DEFAULT_PERMISSION_CLASSES")
    has_default_pagination = _settings_default(ctx, "DEFAULT_PAGINATION_CLASS")
    has_default_throttle = _settings_default(ctx, "DEFAULT_THROTTLE_CLASSES")

    # ---- serializers -------------------------------------------------------- #
    for name, info in sorted(ctx.serializers.items(), key=lambda kv: (str(kv[1].file), kv[1].line)):
        if ctx.is_ambiguous(name):
            continue
        path = info.file

        if string_value(info.meta.get("fields")) == "__all__":
            findings.append(finding(
                path, info.line, "serializer_fields_all",
                "`" + name + "` serialises fields = '__all__', so every column is exposed and "
                "every column added later is exposed too — including the internal flag or the "
                "hashed token someone adds next year",
                "List the fields. An API response is a contract; '__all__' means the contract is "
                "whatever the schema happens to be today.",
                "high"))

        depth = info.meta.get("depth")
        if isinstance(depth, ast.Constant) and isinstance(depth.value, int) and depth.value >= 1:
            findings.append(finding(
                path, info.line, "serializer_depth",
                "`" + name + "` sets depth = " + str(depth.value) + ", which expands every "
                "relation on every row — an N+1 across the whole page, and it exposes the related "
                "models' fields wholesale",
                "Declare the nested serializers you actually want, and prefetch them in "
                "get_queryset. depth= is a debugging convenience, not an API design.",
                "medium"))

        for attr, method in _method_field_queries(info, ctx):
            findings.append(finding(
                path, method.lineno, "query_in_serializer_method_field",
                "`" + name + ".get_" + attr + "` queries the database, and a "
                "SerializerMethodField runs once per object — so this is one query per row of "
                "every list response",
                "Annotate the value onto the queryset in get_queryset "
                "(annotate(" + attr + "=Count(...))) and read it off the instance, or prefetch "
                "the relation it walks.",
                "high"))

    # ---- viewsets and API views --------------------------------------------- #
    for name, info in sorted(ctx.viewsets.items(), key=lambda kv: (str(kv[1].file), kv[1].line)):
        if ctx.is_ambiguous(name):
            continue
        path = info.file
        permissions = info.assignments.get("permission_classes")

        if permissions is None and not has_default_permission:
            findings.append(finding(
                path, info.line, "viewset_default_permission",
                "`" + name + "` sets no permission_classes and the project sets no "
                "DEFAULT_PERMISSION_CLASSES, so DRF falls back to AllowAny — this endpoint is "
                "public, and looks exactly like one whose permissions are handled elsewhere",
                "Set DEFAULT_PERMISSION_CLASSES to IsAuthenticated in REST_FRAMEWORK and opt "
                "individual endpoints out, rather than opting each one in.",
                "high"))
        elif permissions is not None and (_names_in(permissions) & OPEN_PERMISSIONS):
            findings.append(finding(
                path, info.line, "permission_allow_any",
                "`" + name + "` sets permission_classes = [AllowAny], making it public",
                "If that is deliberate, keep it and say why in a comment. If it was copied from "
                "a public endpoint, it is now the hole.",
                "medium"))

        # A class-level queryset with no scoping serves every row to every caller.
        queryset = info.assignments.get("queryset")
        get_queryset = info.methods.get("get_queryset")
        if queryset is not None and get_queryset is None:
            findings.append(finding(
                path, info.line, "unscoped_viewset_queryset",
                "`" + name + "` exposes a class-level queryset with no get_queryset override, so "
                "every authenticated caller sees every row — and the detail route hands over any "
                "id that is asked for",
                "Override get_queryset and scope it: "
                "return super().get_queryset().filter(owner=self.request.user). "
                "permission_classes guards the endpoint; only the queryset guards the row.",
                "high"))
        elif get_queryset is not None:
            body = source_of(ctx, path, get_queryset)
            scoped = any(marker in body for marker in REQUEST_SCOPED)
            if not scoped and "filter" not in body:
                findings.append(finding(
                    path, get_queryset.lineno, "unscoped_viewset_queryset",
                    "`" + name + ".get_queryset()` neither filters nor mentions request.user",
                    "This is where row-level access is decided. Confirm by reading — a permission "
                    "class implementing has_object_permission may cover the detail route, though "
                    "it does not cover the list route.",
                    "medium"))

        if _is_list_view(ctx, name, info):
            if "pagination_class" not in info.assignments and not has_default_pagination:
                findings.append(finding(
                    path, info.line, "missing_pagination",
                    "`" + name + "` returns a collection and nothing paginates it — the response "
                    "grows with the table, so the endpoint works in development and times out in "
                    "production",
                    "Set DEFAULT_PAGINATION_CLASS and PAGE_SIZE in REST_FRAMEWORK, or a "
                    "pagination_class here.",
                    "medium"))

        if "throttle_classes" not in info.assignments and not has_default_throttle:
            is_api_view = bool(set(info.bases) & (API_VIEW_BASES | VIEWSET_BASES))
            if is_api_view:
                findings.append(finding(
                    path, info.line, "missing_throttling",
                    "`" + name + "` has no throttling and the project sets no "
                    "DEFAULT_THROTTLE_CLASSES, so a client can call it as fast as it likes",
                    "Set DEFAULT_THROTTLE_CLASSES and DEFAULT_THROTTLE_RATES in REST_FRAMEWORK. "
                    "This matters most on anything that sends email, costs money, or authenticates.",
                    "low"))

    # ---- @api_view functions -------------------------------------------------- #
    for path, tree in ctx.python_trees():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = {call_name(d) if isinstance(d, ast.Call) else
                          (d.attr if isinstance(d, ast.Attribute) else
                           d.id if isinstance(d, ast.Name) else None)
                          for d in node.decorator_list}
            if "api_view" not in decorators:
                continue
            if "permission_classes" not in decorators and not has_default_permission:
                findings.append(finding(
                    path, node.lineno, "viewset_default_permission",
                    "`" + node.name + "` is an @api_view with no @permission_classes and the "
                    "project sets no DEFAULT_PERMISSION_CLASSES — it is public",
                    "Add @permission_classes([IsAuthenticated]), or set the project default.",
                    "high"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_drf_issues", "Django REST Framework issues", collect))
