#!/usr/bin/env python3
"""Work hidden in templates, where the query cost is invisible from the Python.

A template is the one place a Django project can issue database queries without
any code appearing to do so: `{{ order.customer.name }}` inside a `{% for %}` is
an N+1 that no amount of reading views.py will reveal. Templates are not Python,
so this is a line scanner rather than an AST pass — kept deliberately narrow, and
every finding names the tag it saw.

The escaping checks (|safe, {% autoescape off %}) live in
find_django_security.py rather than here, because they are the same finding as
mark_safe and belong beside it.
"""

import re
import sys

from django_context import template_files
from django_report import finding, run

_FOR_RE = re.compile(r"{%-?\s*for\s+(\w+)\s+in\s+([\w.]+)")
_ENDFOR_RE = re.compile(r"{%-?\s*endfor")
_VAR_RE = re.compile(r"{{\s*([\w.]+)")
_INCLUDE_RE = re.compile(r"{%-?\s*include\s")
_FORM_OPEN_RE = re.compile(r"<form\b[^>]*>", re.IGNORECASE)
_METHOD_POST_RE = re.compile(r"method\s*=\s*[\"']?post", re.IGNORECASE)
_CSRF_RE = re.compile(r"{%-?\s*csrf_token")
_SCRIPT_OPEN_RE = re.compile(r"<script\b", re.IGNORECASE)
_SCRIPT_CLOSE_RE = re.compile(r"</script>", re.IGNORECASE)
_STATIC_URL_VAR_RE = re.compile(r"{{\s*STATIC_URL\s*}}|{{\s*MEDIA_URL\s*}}")
# href/src pointing at an app path rather than a named route.
_HARDCODED_HREF_RE = re.compile(r"""(?:href|action)\s*=\s*["'](/[\w\-/]*)["']""", re.IGNORECASE)
# Callables that hit the database when a template evaluates them.
_QUERY_ACCESSORS = ("all", "count", "first", "last", "exists")
# Attribute pairs whose dotted syntax is value access, not evidence of an ORM
# relation. Keep this deliberately narrow: the scanner has no runtime types.
_VALUE_ACCESS_SUFFIXES = {("type", "title"), ("image", "url")}
# Filters that make a value safe for a script context.
_SCRIPT_SAFE_FILTERS = ("json_script", "escapejs")


def _relation_parts(loop_var, parts):
    """Return the possible model path beneath a template loop variable.

    Django forms commonly expose their bound domain object as an attribute. The
    form and that first object attribute are presentation plumbing, not two ORM
    relations; relation analysis starts beneath the object.
    """
    relation_parts = parts[1:]
    if loop_var.endswith("_form") and relation_parts:
        relation_parts = relation_parts[1:]
    return relation_parts


def _is_value_access(parts):
    return len(parts) == 2 and tuple(parts) in _VALUE_ACCESS_SUFFIXES


def _scan(path, text):
    findings = []
    # (loop variable, the expression iterated, depth at entry)
    stack = []
    in_script = False
    # (line where the <form> opened, whether a csrf_token was seen since)
    open_post_form = None

    for number, line in enumerate(text.splitlines(), start=1):
        # Loop-sensitive tokens must be handled in source order. Django permits
        # an entire loop on one line; collecting every endfor before inspecting
        # variables would otherwise make the body look as though it were outside
        # the loop.
        events = []
        events.extend((match.start(), "for", match) for match in _FOR_RE.finditer(line))
        events.extend((match.start(), "endfor", match) for match in _ENDFOR_RE.finditer(line))
        events.extend((match.start(), "include", match) for match in _INCLUDE_RE.finditer(line))
        events.extend((match.start(), "variable", match) for match in _VAR_RE.finditer(line))

        for _, kind, match in sorted(events, key=lambda event: event[0]):
            if kind == "for":
                stack.append((match.group(1), match.group(2), number))
                if len(stack) >= 3:
                    findings.append(finding(
                        path, number, "deeply_nested_template_loop",
                        str(len(stack)) + " nested {% for %} loops — the innermost body runs the "
                        "product of all three",
                        "Flatten the data in the view, or paginate; a template is a bad place to "
                        "discover a quadratic.",
                        "medium"))
                continue

            if kind == "endfor":
                if stack:
                    stack.pop()
                continue

            if kind == "include":
                if stack:
                    findings.append(finding(
                        path, number, "include_in_loop",
                        "{% include %} inside a loop re-resolves and re-renders the included "
                        "template once per row",
                        "Use {% include ... only %} to cut the context it copies, or inline the "
                        "fragment. For a fragment used on every row, an inclusion tag caches "
                        "better.",
                        "low"))
                continue

            expression = match.group(1)
            parts = expression.split(".")
            if len(parts) < 2:
                continue

            if parts[-1] in _QUERY_ACCESSORS:
                findings.append(finding(
                    path, number, "query_in_template",
                    "`{{ " + expression + " }}` calls ." + parts[-1] + "() during rendering, so "
                    "the query happens after the view has returned and cannot be optimised there",
                    "Do it in the view and pass the result in; annotate() for a count.",
                    "high" if stack else "medium"))
                continue

            # Reaching through a possible relation beneath a loop variable is the
            # classic N+1. Normalize form wrappers and known value-only access
            # before deciding whether the dotted shape is evidence of a relation.
            if stack and parts[0] in {var for var, _, _ in stack}:
                loop_var = parts[0]
                relation_parts = _relation_parts(loop_var, parts)
                if len(relation_parts) < 2 or _is_value_access(relation_parts):
                    continue
                loop_line = next(ln for var, _, ln in stack if var == loop_var)
                findings.append(finding(
                    path, number, "relation_walk_in_loop",
                    "`{{ " + expression + " }}` walks " + str(len(relation_parts))
                    + " attributes inside "
                    "the loop opened on line " + str(loop_line) + " — one query per row unless it "
                    "is prefetched",
                    "select_related/prefetch_related '" + "__".join(relation_parts[:-1]) + "' on "
                    "the queryset the view passes in.",
                    "high"))

        # ---- forms and CSRF ------------------------------------------------ #
        form_match = _FORM_OPEN_RE.search(line)
        if form_match and _METHOD_POST_RE.search(form_match.group(0)):
            open_post_form = number
        if open_post_form is not None and _CSRF_RE.search(line):
            open_post_form = None
        if open_post_form is not None and "</form>" in line.lower():
            findings.append(finding(
                path, open_post_form, "missing_csrf_token",
                "a POST form with no {% csrf_token %} — Django rejects the submission with a 403, "
                "and the usual 'fix' is to reach for csrf_exempt, which removes the protection "
                "instead of satisfying it",
                "Add {% csrf_token %} as the first thing inside the <form>.",
                "high"))
            open_post_form = None

        # ---- scripts -------------------------------------------------------- #
        if _SCRIPT_OPEN_RE.search(line):
            in_script = True
        if in_script:
            for match in _VAR_RE.finditer(line):
                expression = match.group(1)
                tail = line[match.end():match.end() + 60]
                if not any(f in tail for f in _SCRIPT_SAFE_FILTERS):
                    findings.append(finding(
                        path, number, "template_var_in_script",
                        "`" + expression + "` is interpolated into a <script> block, where HTML "
                        "escaping does not protect you — a value containing </script> or a quote "
                        "breaks out into executable JavaScript",
                        "Use {{ value|json_script:'id' }} and read it from JavaScript with "
                        "JSON.parse(document.getElementById('id').textContent).",
                        "high"))
        if _SCRIPT_CLOSE_RE.search(line):
            in_script = False

        # ---- URLs and static ------------------------------------------------ #
        if _STATIC_URL_VAR_RE.search(line):
            findings.append(finding(
                path, number, "static_url_variable",
                "{{ STATIC_URL }} builds the path by string concatenation, so it misses the hashed "
                "filename that ManifestStaticFilesStorage produces and serves a stale asset",
                "Use {% load static %} and {% static 'app/style.css' %}.",
                "medium"))

        for match in _HARDCODED_HREF_RE.finditer(line):
            target = match.group(1)
            if target in ("/", "#"):
                continue
            findings.append(finding(
                path, number, "hardcoded_url_in_template",
                "the literal path '" + target + "' is written into the template, so renaming the "
                "route breaks this link silently",
                "Use {% url 'route-name' %}, or the model's get_absolute_url.",
                "low"))

    return findings


def collect(ctx):
    findings = []
    for path in template_files(ctx):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        findings.extend(_scan(path, text))
    return findings


if __name__ == "__main__":
    sys.exit(run("find_template_issues", "Django template issues", collect))
