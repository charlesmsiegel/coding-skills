#!/usr/bin/env python3
"""View and URL problems, including the object-level authorization gap.

`missing_ownership_filter` is the finding worth the whole file. Fetching a record
by a primary key taken from the URL, with no filter tying it to the requesting
user, is the most common real vulnerability in application code — the endpoint
checks that you are logged in, and then hands you somebody else's row.
"""

import ast
import sys

from django_context import call_name
from django_report import finding, run

FAT_VIEW_LINES = 60
FETCH_CALLS = frozenset({"get", "get_object_or_404", "get_object_or_None"})
REQUEST_SCOPED = ("request.user", "self.request.user")


def _source_segment(source_lines, node):
    try:
        return "\n".join(source_lines[node.lineno - 1:(node.end_lineno or node.lineno)])
    except (IndexError, TypeError):
        return ""


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
    return bool(names & {"pk", "id", "uuid", "slug", "object_id"})


def collect(ctx):
    findings = []

    for path in ctx.files:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (SyntaxError, ValueError, OSError):
            continue
        lines = source.splitlines()
        is_urlconf = path.name == "urls.py"

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = _body_length(node)
                takes_request = bool(node.args.args) and node.args.args[0].arg in ("request", "self")
                if takes_request and length > FAT_VIEW_LINES:
                    findings.append(finding(
                        path, node.lineno, "fat_view",
                        f"`{node.name}` runs {length} lines — a view this size is doing the "
                        f"work as well as handling the request",
                        "Move the business logic to a model method or a service function and "
                        "leave the view to parse input and choose a response.",
                        "medium"))

                # Object-level authorization.
                if _takes_a_url_argument(node):
                    body_text = _source_segment(lines, node)
                    fetches = [
                        sub for sub in ast.walk(node)
                        if isinstance(sub, ast.Call) and call_name(sub) in FETCH_CALLS
                    ]
                    if fetches and not _mentions_request_user(body_text):
                        findings.append(finding(
                            path, fetches[0].lineno, "missing_ownership_filter",
                            f"`{node.name}` looks up a record by a key from the URL and never "
                            f"mentions request.user — any authenticated user can read another "
                            f"user's row by changing the id",
                            "Filter by the owner in the same query: "
                            "get_object_or_404(Model, pk=pk, owner=request.user).",
                            "high"))

                if node.name == "clean" or node.name.startswith("clean_"):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Attribute) and sub.attr == "objects":
                            findings.append(finding(
                                path, sub.lineno, "query_in_form_clean",
                                f"`{node.name}` queries the database during validation, so the "
                                f"query runs once per field validated and per re-render",
                                "Do the lookup once in __init__ or in the view, and validate "
                                "against the result.",
                                "medium"))
                            break

            if isinstance(node, ast.Call):
                name = call_name(node)

                # A literal URL breaks the moment a route is renamed.
                if name in ("redirect", "HttpResponseRedirect"):
                    for arg in node.args[:1]:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                                and arg.value.startswith("/"):
                            findings.append(finding(
                                path, node.lineno, "hardcoded_url",
                                f"redirect to the literal path '{arg.value}'",
                                "Use the route's name — redirect('order-detail', pk=...) — so "
                                "changing the URL does not break the redirect.",
                                "medium"))

                if is_urlconf and name in ("path", "re_path"):
                    named = any(kw.arg == "name" for kw in node.keywords)
                    if not named:
                        findings.append(finding(
                            path, node.lineno, "url_without_name",
                            "route declared without name=, so nothing can reverse() it",
                            "Add name='...' and refer to the route by name everywhere else.",
                            "medium"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_view_issues", "Django view and URL issues", collect))
