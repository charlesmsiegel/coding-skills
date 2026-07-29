#!/usr/bin/env python3
"""ORM misuse that costs queries: N+1 access, per-row writes, unbounded fetches.

These are the findings that show up as latency. The detector is deliberately
conservative about N+1 in particular, because the shape that looks like an N+1 —
attribute access inside a loop — is also the shape of perfectly fine code, and a
performance detector that fires on every loop teaches people to skip the output.

So an N+1 is only reported when all three hold: the loop iterates a queryset, the
body reaches through a relation on the loop variable, and the queryset has no
select_related/prefetch_related/only/values in its chain.
"""

import ast
import sys

from django_context import FORWARD_RELATIONS, RELATION_FIELDS, call_name
from django_report import finding, run

# Chain members that mean the caller already thought about how much this fetches.
PREFETCH_CALLS = frozenset({"select_related", "prefetch_related", "only", "defer",
                            "values", "values_list", "annotate", "iterator"})
# Calls that produce a queryset from a manager.
QUERYSET_CALLS = frozenset({"all", "filter", "exclude", "order_by", "select_related",
                            "prefetch_related", "annotate", "distinct", "none", "reverse"})
WRITE_CALLS = frozenset({"save", "delete", "create", "update_or_create", "get_or_create"})


def _chain(node):
    """The attribute/method names in a call chain, outermost first."""
    names = []
    current = node
    while True:
        if isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Attribute):
            names.append(current.attr)
            current = current.value
        elif isinstance(current, ast.Name):
            names.append(current.id)
            break
        else:
            break
    return names


def _is_queryset_expr(node):
    """Whether an expression looks like `Model.objects.<something>()`."""
    names = _chain(node)
    return "objects" in names and bool(QUERYSET_CALLS & set(names))


def _loop_var_names(target):
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return {n.id for n in target.elts if isinstance(n, ast.Name)}
    return set()


def _relation_fields(ctx):
    """Every attribute name that is a relation on some model, and its kind."""
    relations = {}
    for info in ctx.models.values():
        for attr, kind in info.fields.items():
            if kind in RELATION_FIELDS:
                relations[attr] = kind
        # A reverse accessor: `related_name="orders"` makes `customer.orders`.
        for stmt in info.node.body:
            if not (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)):
                continue
            if call_name(stmt.value) not in RELATION_FIELDS:
                continue
            for kw in stmt.value.keywords:
                if kw.arg == "related_name" and isinstance(kw.value, ast.Constant):
                    relations[str(kw.value.value)] = "reverse"
    return relations


def _walk_loops(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            yield node


def collect(ctx):
    findings = []
    relations = _relation_fields(ctx)

    for path in ctx.files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue

        for loop in _walk_loops(tree):
            loop_vars = _loop_var_names(loop.target)
            iterates_queryset = _is_queryset_expr(loop.iter)
            already_prefetched = bool(PREFETCH_CALLS & set(_chain(loop.iter)))

            for node in ast.walk(loop):
                # Per-row writes: one query per iteration, always.
                if isinstance(node, ast.Call):
                    name = call_name(node)
                    if name in WRITE_CALLS and isinstance(node.func, ast.Attribute):
                        findings.append(finding(
                            path, node.lineno, f"{name}_in_loop",
                            f".{name}() is called inside a loop — one query per iteration",
                            {"save": "Collect the objects and use bulk_update(), or "
                                     "queryset.update() when the value is uniform.",
                             "create": "Build the instances and use bulk_create().",
                             "delete": "Filter once and delete the queryset in one call.",
                             }.get(name, "Do this in one query outside the loop."),
                            "high"))

                # N+1: reaching through a relation on the loop variable.
                if (iterates_queryset and not already_prefetched
                        and isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id in loop_vars
                        and node.attr in relations):
                    kind = relations[node.attr]
                    helper = ("select_related" if kind in FORWARD_RELATIONS
                              else "prefetch_related")
                    findings.append(finding(
                        path, node.lineno, "n_plus_one_query",
                        f"`{node.value.id}.{node.attr}` reaches through a relation inside a "
                        f"loop over a queryset that does not prefetch it",
                        f"Add .{helper}('{node.attr}') to the queryset on line {loop.iter.lineno}.",
                        "high"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node)

            # .extra() has been discouraged for a decade and is a SQL-injection foot-gun.
            if name == "extra":
                findings.append(finding(
                    path, node.lineno, "deprecated_extra",
                    ".extra() is long-discouraged and bypasses query-parameter safety",
                    "Express it with annotate()/Func()/RawSQL, or use .raw() with params.",
                    "medium"))

            if name == "raw":
                findings.append(finding(
                    path, node.lineno, "raw_sql",
                    ".raw() executes SQL the ORM cannot check",
                    "Confirm every interpolated value is passed via params=[...], never "
                    "formatted into the string.",
                    "medium"))

            # An update whose new value is derived from the old one races other writers.
            if name == "update":
                for kw in node.keywords:
                    if isinstance(kw.value, ast.BinOp) and not _uses_f(kw.value):
                        findings.append(finding(
                            path, node.lineno, "update_without_f",
                            f"update({kw.arg}=...) computes from a value read earlier — "
                            f"a concurrent write between the read and the write is lost",
                            f"Use F('{kw.arg}') so the database does the arithmetic.",
                            "high"))

            # len() of a queryset pulls every row into memory to count them.
            if (name is None and isinstance(node.func, ast.Name) and node.func.id == "len"
                    and node.args and _is_queryset_expr(node.args[0])):
                findings.append(finding(
                    path, node.lineno, "len_of_queryset",
                    "len() on a queryset fetches every row just to count them",
                    "Use .count(), which counts in the database.",
                    "medium"))

    return findings


def _uses_f(node):
    return any(isinstance(sub, ast.Call) and call_name(sub) == "F" for sub in ast.walk(node))


if __name__ == "__main__":
    sys.exit(run("find_query_issues", "Django query issues", collect))
