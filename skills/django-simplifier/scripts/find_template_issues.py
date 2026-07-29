#!/usr/bin/env python3
"""Work hidden in templates, where the query cost is invisible from the Python.

A template is the one place a Django project can issue database queries without
any code appearing to do so: `{{ order.customer.name }}` inside a `{% for %}` is
an N+1 that no amount of reading views.py will reveal. Templates are not Python,
so this is a line scanner rather than an AST pass — kept deliberately narrow, and
every finding names the tag it saw.
"""

import re
import sys

from django_context import template_files
from django_report import finding, run

_FOR_RE = re.compile(r"{%-?\s*for\s+(\w+)\s+in\s+([\w.]+)")
_ENDFOR_RE = re.compile(r"{%-?\s*endfor")
_VAR_RE = re.compile(r"{{\s*([\w.]+)")
# Callables that hit the database when a template evaluates them.
_QUERY_ACCESSORS = ("all", "count", "first", "last", "exists")


def _scan(path, text):
    findings = []
    # (loop variable, the expression iterated, depth at entry)
    stack = []

    for number, line in enumerate(text.splitlines(), start=1):
        for match in _FOR_RE.finditer(line):
            stack.append((match.group(1), match.group(2), number))
            if len(stack) >= 3:
                findings.append(finding(
                    path, number, "deeply_nested_template_loop",
                    f"{len(stack)} nested {{% for %}} loops — the innermost body runs the "
                    f"product of all three",
                    "Flatten the data in the view, or paginate; a template is a bad place to "
                    "discover a quadratic.",
                    "medium"))

        for _ in _ENDFOR_RE.finditer(line):
            if stack:
                stack.pop()

        for match in _VAR_RE.finditer(line):
            expression = match.group(1)
            parts = expression.split(".")
            if len(parts) < 2:
                continue

            if parts[-1] in _QUERY_ACCESSORS:
                findings.append(finding(
                    path, number, "query_in_template",
                    f"`{{{{ {expression} }}}}` calls .{parts[-1]}() during rendering, so the "
                    f"query happens after the view has returned and cannot be optimised there",
                    "Do it in the view and pass the result in; annotate() for a count.",
                    "high" if stack else "medium"))
                continue

            # Reaching two levels deep off a loop variable is the classic N+1.
            if stack and parts[0] in {var for var, _, _ in stack} and len(parts) >= 3:
                loop_line = next(ln for var, _, ln in stack if var == parts[0])
                findings.append(finding(
                    path, number, "relation_walk_in_loop",
                    f"`{{{{ {expression} }}}}` walks {len(parts) - 1} relations inside the "
                    f"loop opened on line {loop_line} — one query per row unless it is "
                    f"prefetched",
                    f"select_related/prefetch_related '{'__'.join(parts[1:-1])}' on the "
                    f"queryset the view passes in.",
                    "high"))

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
