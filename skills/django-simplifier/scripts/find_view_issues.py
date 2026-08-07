#!/usr/bin/env python3
"""View and URL problems, including the object-level authorization gap.

`missing_ownership_filter` is the finding worth the whole file. Fetching a record
by a primary key taken from the URL, with no filter tying it to the requesting
user, is the most common real vulnerability in application code — the endpoint
checks that you are logged in, and then hands you somebody else's row.

The standing caveat: this reads function bodies and class bodies. Authorization
enforced through a base class, a custom mixin, or a permission backend is
invisible here. A clean report means "nothing obvious", not "checked".
"""

import ast
import sys

from django_context import (VIEWSET_BASES, call_name, decorator_name, source_of,
                            string_value)
from django_report import finding, run

FAT_VIEW_LINES = 60
CBV_HOOK_LIMIT = 5
FETCH_CALLS = frozenset({"get", "get_object_or_404", "get_object_or_None", "aget_object_or_404"})
REQUEST_SCOPED = ("request.user", "self.request.user")
URL_KEY_ARGS = frozenset({"pk", "id", "uuid", "slug", "object_id"})

# Decorators that establish who the caller is.
AUTH_DECORATORS = frozenset({"login_required", "permission_required", "user_passes_test",
                             "staff_member_required", "login_not_required"})
AUTH_MIXINS = frozenset({"LoginRequiredMixin", "PermissionRequiredMixin",
                         "UserPassesTestMixin", "AccessMixin"})
# CBV hooks. Overriding one or two is the point of a generic view; overriding six
# means the control flow now lives in Django's source.
CBV_HOOKS = frozenset({"get_queryset", "get_context_data", "get_object", "get_form",
                       "get_form_class", "get_form_kwargs", "form_valid", "form_invalid",
                       "get_success_url", "get_initial", "dispatch", "get", "post",
                       "get_serializer_class", "get_serializer_context", "perform_create",
                       "perform_update", "perform_destroy"})
MUTATING_METHODS = frozenset({"post", "put", "patch", "delete", "create", "update", "destroy"})
WRITE_CALLS = frozenset({"save", "delete", "create", "update", "bulk_create", "bulk_update"})


def _body_length(node):
    if not node.body:
        return 0
    last = node.body[-1]
    return (getattr(last, "end_lineno", last.lineno) or last.lineno) - node.lineno


def _mentions_request_user(text):
    return any(marker in text for marker in REQUEST_SCOPED)


def _takes_a_url_argument(func):
    """A view whose signature carries a lookup key from the URL."""
    names = {a.arg for a in func.args.args} | {a.arg for a in func.args.kwonlyargs}
    return bool(names & URL_KEY_ARGS)


def _decorator_names(node):
    return {d for d in (decorator_name(d) for d in node.decorator_list) if d}


def _writes_to_the_database(func):
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and call_name(node) in WRITE_CALLS:
            return True
    return False


def _looks_like_a_view(func):
    """First parameter is `request` (FBV) or the function sits on a View class."""
    return bool(func.args.args) and func.args.args[0].arg in ("request", "self")


def _filter_kwargs_from_request(node):
    """`filter(**request.GET)` — the caller chooses which fields are queried.

    Not injection: the ORM still parameterises. But a user who controls the key
    can traverse relations they were never meant to reach.
    """
    for kw in node.keywords:
        if kw.arg is not None:
            continue
        # Walk through calls as well as attributes: `**request.GET.dict()` and
        # `**request.GET` are the same finding wearing different clothes.
        chain = []
        current = kw.value
        while isinstance(current, (ast.Attribute, ast.Call)):
            if isinstance(current, ast.Call):
                current = current.func
                continue
            chain.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name) and current.id == "request":
            return True
        if chain and chain[-1] in ("GET", "POST", "data", "query_params"):
            return True
    return False


def _redirect_target_is_user_controlled(node):
    """redirect(request.GET.get('next')) — an open redirect unless validated."""
    for arg in node.args[:1]:
        text = ast.dump(arg)
        if "'next'" in text or '"next"' in text:
            return True
        chain = []
        current = arg
        while isinstance(current, (ast.Attribute, ast.Call)):
            if isinstance(current, ast.Call):
                current = current.func
                continue
            chain.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name) and current.id == "request":
            return True
    return False


def _redirect_validating_ranges(ctx, path, tree):
    """Line ranges of functions that check a redirect target before using it.

    The validation call sits several lines above the redirect, so asking whether
    the redirect *line* mentions it would report every correctly-written login
    view. The unit that has to be checked is the function.
    """
    ranges = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = source_of(ctx, path, node)
        if "url_has_allowed_host_and_scheme" in body or "is_safe_url" in body:
            ranges.append((node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno))
    return ranges


def _within(ranges, line):
    return any(start <= line <= end for start, end in ranges)


def collect(ctx):
    findings = []

    for path, tree in ctx.python_trees():
        is_urlconf = path in set(ctx.urls_files)
        validated = _redirect_validating_ranges(ctx, path, tree)

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decorators = _decorator_names(node)
                length = _body_length(node)

                if _looks_like_a_view(node) and length > FAT_VIEW_LINES:
                    findings.append(finding(
                        path, node.lineno, "fat_view",
                        "`" + node.name + "` runs " + str(length) + " lines — a view this size is "
                        "doing the work as well as handling the request",
                        "Move the business logic to a model method or a service function and "
                        "leave the view to parse input and choose a response.",
                        "medium"))

                # Object-level authorization.
                if _takes_a_url_argument(node):
                    body_text = source_of(ctx, path, node)
                    fetches = [
                        sub for sub in ast.walk(node)
                        if isinstance(sub, ast.Call) and call_name(sub) in FETCH_CALLS
                    ]
                    if fetches and not _mentions_request_user(body_text):
                        findings.append(finding(
                            path, fetches[0].lineno, "missing_ownership_filter",
                            "`" + node.name + "` looks up a record by a key from the URL and never "
                            "mentions request.user — any authenticated user can read another "
                            "user's row by changing the id",
                            "Filter by the owner in the same query: "
                            "get_object_or_404(Model, pk=pk, owner=request.user).",
                            "high"))

                if "csrf_exempt" in decorators:
                    findings.append(finding(
                        path, node.lineno, "csrf_exempt",
                        "`" + node.name + "` turns off CSRF protection, so any site can make a "
                        "logged-in browser submit this endpoint",
                        "Remove it. If this is a machine-to-machine webhook, authenticate the "
                        "caller by signature instead — csrf_exempt is not authentication.",
                        "high"))

                # A view that writes and names no authentication.
                if (_looks_like_a_view(node) and _writes_to_the_database(node)
                        and node.args.args[0].arg == "request"
                        and not (decorators & AUTH_DECORATORS)):
                    body_text = source_of(ctx, path, node)
                    if "is_authenticated" not in body_text and "permission" not in body_text.lower():
                        findings.append(finding(
                            path, node.lineno, "unauthenticated_mutation",
                            "`" + node.name + "` writes to the database and carries no "
                            "authentication decorator or check",
                            "Add @login_required (or the permission the write requires). If it is "
                            "deliberately public, say so in a comment — the next reader cannot "
                            "tell the difference.",
                            "medium"))

                if node.name == "clean" or node.name.startswith("clean_"):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Attribute) and sub.attr == "objects":
                            findings.append(finding(
                                path, sub.lineno, "query_in_form_clean",
                                "`" + node.name + "` queries the database during validation, so "
                                "the query runs once per field validated and per re-render",
                                "Do the lookup once in __init__ or in the view, and validate "
                                "against the result.",
                                "medium"))
                            break

            if isinstance(node, ast.Call):
                name = call_name(node)

                # A literal URL breaks the moment a route is renamed.
                if name in ("redirect", "HttpResponseRedirect"):
                    for arg in node.args[:1]:
                        literal = string_value(arg)
                        if literal and literal.startswith("/"):
                            findings.append(finding(
                                path, node.lineno, "hardcoded_url",
                                "redirect to the literal path '" + literal + "'",
                                "Use the route's name — redirect('order-detail', pk=...) — so "
                                "changing the URL does not break the redirect.",
                                "medium"))

                if (name in ("redirect", "HttpResponseRedirect")
                        and _redirect_target_is_user_controlled(node)
                        and not _within(validated, node.lineno)):
                    findings.append(finding(
                        path, node.lineno, "open_redirect",
                        "the redirect target comes from the request, so a crafted ?next= sends "
                        "the user to another site while looking like your login flow",
                        "Validate it: url_has_allowed_host_and_scheme(url, "
                        "allowed_hosts={request.get_host()}) before redirecting, and fall back "
                        "to a known-safe default.",
                        "high"))

                if name in ("filter", "exclude", "get") and _filter_kwargs_from_request(node):
                    findings.append(finding(
                        path, node.lineno, "unfiltered_user_input_lookup",
                        "query keywords are expanded straight from the request, so the caller "
                        "chooses which fields are filtered — including relations "
                        "(customer__user__email) that were never meant to be queryable",
                        "Allow-list the keys: {k: v for k, v in request.GET.items() if k in ALLOWED}.",
                        "high"))

                if is_urlconf and name in ("path", "re_path"):
                    if not any(kw.arg == "name" for kw in node.keywords):
                        findings.append(finding(
                            path, node.lineno, "url_without_name",
                            "route declared without name=, so nothing can reverse() it",
                            "Add name='...' and refer to the route by name everywhere else.",
                            "medium"))

    # ---- class-based views ------------------------------------------------- #
    for name, info in ctx.views.items():
        if ctx.is_ambiguous(name):
            continue

        overridden = sorted(m for m in info.methods if m in CBV_HOOKS)
        if len(overridden) >= CBV_HOOK_LIMIT:
            findings.append(finding(
                info.file, info.line, "cbv_hook_overload",
                "`" + name + "` overrides " + str(len(overridden)) + " generic-view hooks (" +
                ", ".join(overridden[:6]) + ") — the control flow now lives in Django's source and "
                "a reader has to reconstruct it from the base classes",
                "Write the function. A generic view earns its keep at one or two overrides; past "
                "that it is a template method pattern you are fighting.",
                "medium"))

        # A viewset or detail view that never scopes its queryset to the caller.
        is_viewset = bool(set(info.bases) & VIEWSET_BASES) or ctx.derives_from(name, VIEWSET_BASES)
        has_queryset_attr = "queryset" in info.assignments
        get_queryset = info.methods.get("get_queryset")

        if has_queryset_attr and get_queryset is None:
            findings.append(finding(
                info.file, info.line, "unscoped_get_queryset",
                "`" + name + "` sets a class-level queryset and never overrides get_queryset, so "
                "every authenticated caller sees every row",
                "Override get_queryset and filter by the caller: "
                "return super().get_queryset().filter(owner=self.request.user). "
                "Confirm by reading — a permission class or a base view may already scope it.",
                "high"))
        elif get_queryset is not None:
            body = source_of(ctx, info.file, get_queryset)
            if not _mentions_request_user(body) and "filter" not in body:
                findings.append(finding(
                    info.file, get_queryset.lineno, "unscoped_get_queryset",
                    "`" + name + ".get_queryset()` neither filters nor mentions request.user",
                    "This is the right place to scope a queryset to its owner; its absence is "
                    "usually the bug. Confirm by reading before acting.",
                    "medium"))

        # A CBV that mutates and mixes in no auth.
        mutating = [m for m in info.methods if m in MUTATING_METHODS]
        mixins = set(info.bases)
        if mutating and not (mixins & AUTH_MIXINS):
            has_permission_attr = any(k in info.assignments
                                      for k in ("permission_classes", "permission_required",
                                                "raise_exception"))
            decorated = bool(set(info.decorators) & AUTH_DECORATORS)
            if not has_permission_attr and not decorated and not is_viewset:
                findings.append(finding(
                    info.file, info.line, "unauthenticated_mutation",
                    "`" + name + "` handles " + ", ".join(sorted(mutating)) + " and mixes in no "
                    "authentication",
                    "Add LoginRequiredMixin (first in the bases, so it runs before dispatch), or "
                    "the permission mixin the write requires.",
                    "medium"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_view_issues", "Django view and URL issues", collect))
