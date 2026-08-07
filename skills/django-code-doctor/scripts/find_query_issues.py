#!/usr/bin/env python3
"""ORM misuse that costs queries: N+1 access, per-row writes, unbounded fetches.

These are the findings that show up as latency. The detector is deliberately
conservative about N+1 in particular, because the shape that looks like an N+1 —
attribute access inside a loop — is also the shape of perfectly fine code, and a
performance detector that fires on every loop teaches people to skip the output.

So an N+1 is only reported when all three hold: the loop iterates a queryset, the
body reaches through a relation on the loop variable, and the queryset has no
select_related/prefetch_related/only/values in its chain.

One finding here is not about speed at all. `read_modify_write_race` and
`update_without_f` are correctness: they describe a lost update that appears only
under concurrency, so they pass every test and surface as numbers that do not add
up.
"""

import ast
import sys

from django_context import (FORWARD_RELATIONS, RELATION_FIELDS, attribute_chain,
                            call_name, keyword)
from django_report import finding, run

# Chain members that mean the caller already thought about how much this fetches.
PREFETCH_CALLS = frozenset({"select_related", "prefetch_related", "only", "defer",
                            "values", "values_list", "annotate", "iterator"})
# Calls that produce a queryset from a manager.
QUERYSET_CALLS = frozenset({"all", "filter", "exclude", "order_by", "select_related",
                            "prefetch_related", "annotate", "distinct", "none", "reverse"})
WRITE_CALLS = frozenset({"save", "delete", "create", "update_or_create", "get_or_create"})
# Per-iteration reads. Cheaper than a write, still one round trip each.
READ_CALLS = frozenset({"count", "exists", "first", "last", "get"})

# Methods that end a queryset chain by evaluating it.
TERMINAL_CALLS = frozenset({"count", "exists", "first", "last", "get", "aggregate",
                            "latest", "earliest", "in_bulk", "update", "delete"})


def _chain(node):
    """The attribute/method names in a call chain, outermost first."""
    return attribute_chain(node)


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
            related = keyword(stmt.value, "related_name")
            if isinstance(related, ast.Constant):
                relations[str(related.value)] = "reverse"
    return relations


def _walk_loops(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            yield node


def _uses_f(node):
    return any(isinstance(sub, ast.Call) and call_name(sub) == "F" for sub in ast.walk(node))


def _attribute_assignments_then_save(node):
    """`obj.field = <expr involving obj.field>` followed by obj.save().

    The lost-update shape: read a number into Python, do arithmetic on it, write
    it back. Two writers interleave and one increment vanishes.
    """
    hits = []
    for statement in ast.walk(node):
        if not isinstance(statement, (ast.Assign, ast.AugAssign)):
            continue
        target = statement.targets[0] if isinstance(statement, ast.Assign) else statement.target
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)):
            continue
        value = statement.value
        if isinstance(statement, ast.AugAssign):
            hits.append((statement, target))
            continue
        # A plain assignment only races when the new value derives from the old.
        if isinstance(value, ast.BinOp):
            reads_itself = any(
                isinstance(sub, ast.Attribute) and sub.attr == target.attr
                for sub in ast.walk(value))
            if reads_itself:
                hits.append((statement, target))
    return hits


def collect(ctx):
    findings = []
    relations = _relation_fields(ctx)

    for path, tree in ctx.python_trees():
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
                            path, node.lineno, name + "_in_loop",
                            "." + name + "() is called inside a loop — one query per iteration",
                            {"save": "Collect the objects and use bulk_update(), or "
                                     "queryset.update() when the value is uniform.",
                             "create": "Build the instances and use bulk_create().",
                             "delete": "Filter once and delete the queryset in one call.",
                             }.get(name, "Do this in one query outside the loop."),
                            "high"))

                    # Per-row reads. A count() per row is the N+1 nobody looks for,
                    # because nothing is being written and the code reads as cheap.
                    if (name in ("count", "exists") and isinstance(node.func, ast.Attribute)
                            and not node.args):
                        chain = _chain(node)
                        touches_loop_var = bool(loop_vars & set(chain))
                        if touches_loop_var or "objects" in chain:
                            findings.append(finding(
                                path, node.lineno, name + "_in_loop",
                                "." + name + "() runs once per iteration, so the loop costs one "
                                "query per row on top of the query that produced the rows",
                                "Annotate the count onto the outer queryset — "
                                "annotate(n=Count('items')) — and read it off each row."
                                if name == "count" else
                                "Fetch the ids once outside the loop and test membership in Python.",
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
                        "`" + node.value.id + "." + node.attr + "` reaches through a relation "
                        "inside a loop over a queryset that does not prefetch it",
                        "Add ." + helper + "('" + node.attr + "') to the queryset on line " +
                        str(loop.iter.lineno) + ".",
                        "high"))

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Read-modify-write on a model attribute, then save. Distinct from
                # update_without_f: that one is already inside an update() call.
                saves = [c for c in ast.walk(node)
                         if isinstance(c, ast.Call) and call_name(c) == "save"]
                if saves:
                    for statement, target in _attribute_assignments_then_save(node):
                        findings.append(finding(
                            path, statement.lineno, "read_modify_write_race",
                            "`" + target.attr + "` is read into Python, changed, and written back "
                            "— two writers between the read and the save lose one of the changes",
                            "Do the arithmetic in the database: "
                            "Model.objects.filter(pk=obj.pk).update(" + target.attr +
                            "=F('" + target.attr + "') + 1). For anything spanning several "
                            "statements, select_for_update() inside a transaction.",
                            "high"))

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
                            "update(" + str(kw.arg) + "=...) computes from a value read earlier — "
                            "a concurrent write between the read and the write is lost",
                            "Use F('" + str(kw.arg) + "') so the database does the arithmetic.",
                            "high"))

                # `qs[:10].update(...)` raises at runtime; the slice has to go.
                if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Subscript):
                    findings.append(finding(
                        path, node.lineno, "update_on_sliced_queryset",
                        "update() on a sliced queryset raises TypeError — Django cannot write "
                        "through a LIMIT",
                        "Select the primary keys first, then update on a filter over them: "
                        "Model.objects.filter(pk__in=qs[:10].values('pk')).update(...).",
                        "high"))

            # bulk_create with no batch_size sends every row in one statement, which
            # is where a large import meets the database's parameter limit.
            if name in ("bulk_create", "bulk_update") and keyword(node, "batch_size") is None:
                findings.append(finding(
                    path, node.lineno, "bulk_create_without_batch_size",
                    "." + name + "() without batch_size builds a single statement for every row, "
                    "which fails on a large list once the driver's parameter limit is hit",
                    "Pass batch_size=1000 (or whatever the backend tolerates).",
                    "low"))

            # len() of a queryset pulls every row into memory to count them.
            if (name is None and isinstance(node.func, ast.Name) and node.func.id == "len"
                    and node.args and _is_queryset_expr(node.args[0])):
                findings.append(finding(
                    path, node.lineno, "len_of_queryset",
                    "len() on a queryset fetches every row just to count them",
                    "Use .count(), which counts in the database.",
                    "medium"))

        # qs[0] where .first() is meant: the index raises IndexError on an empty
        # queryset, and the two are not interchangeable at the call site.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Subscript):
                continue
            if not _is_queryset_expr(node.value):
                continue
            index = node.slice
            if isinstance(index, ast.Constant) and index.value == 0:
                findings.append(finding(
                    path, node.lineno, "index_instead_of_first",
                    "queryset[0] raises IndexError when nothing matches, where .first() returns None",
                    "Use .first() and handle None, unless the IndexError is deliberate.",
                    "low"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_query_issues", "Django query issues", collect))
