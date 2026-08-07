#!/usr/bin/env python3
"""Transaction boundaries — what is inside atomic() that should not be.

An `atomic()` block holds a database connection and, for anything it writes,
locks until it commits. The failures follow from that:

- An HTTP call inside the block holds the transaction open for the remote
  service's latency, including its timeout. One slow third party becomes
  connection-pool exhaustion.
- Catching IntegrityError *inside* the same block does not work: the transaction
  is already marked for rollback, and the next query raises
  TransactionManagementError instead of doing what the handler intended. The
  catch has to be outside, or the risky part wrapped in its own inner atomic().
- `select_for_update()` outside a transaction raises. Inside one it is the fix
  for the read-modify-write race the query detector reports.

These are all shapes rather than certainties — a two-line HTTP call to a service
on the same host is not the same problem as a payment gateway — so they are
ranked to be read rather than obeyed.
"""

import ast
import sys

from django_context import call_name
from django_report import finding, run

# Calls that go over the network. Inside a transaction, their latency is the
# transaction's duration.
EXTERNAL_CALLS = frozenset({"post", "get", "put", "patch", "request", "urlopen",
                            "send_mail", "send_messages", "publish", "charge"})
EXTERNAL_MODULES = frozenset({"requests", "httpx", "urllib", "boto3", "stripe"})
WRITE_CALLS = frozenset({"save", "delete", "create", "update", "bulk_create", "bulk_update"})


def _atomic_blocks(tree):
    """Every `with transaction.atomic():` node, and its line range."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            target = item.context_expr
            is_atomic = False
            if isinstance(target, ast.Call) and call_name(target) == "atomic":
                is_atomic = True
            elif isinstance(target, ast.Attribute) and target.attr == "atomic":
                is_atomic = True
            if is_atomic:
                yield node
                break


def _atomic_decorated(tree):
    """Functions wrapped in @transaction.atomic — the whole body is the block."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            name = (call_name(decorator) if isinstance(decorator, ast.Call)
                    else getattr(decorator, "attr", None))
            if name == "atomic":
                yield node
                break


def _inner_atomic_lines(block):
    """Line ranges of atomic() blocks nested inside this one.

    A nested atomic() is a savepoint, which is exactly the correct way to catch
    IntegrityError inside a transaction — so a catch inside one must not be
    reported.
    """
    ranges = []
    for node in ast.walk(block):
        if node is block:
            continue
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            target = item.context_expr
            if ((isinstance(target, ast.Call) and call_name(target) == "atomic")
                    or (isinstance(target, ast.Attribute) and target.attr == "atomic")):
                ranges.append((node.lineno,
                               getattr(node, "end_lineno", node.lineno) or node.lineno))
    return ranges


def _within(ranges, line):
    return any(start <= line <= end for start, end in ranges)


def _root_name(node):
    """The leftmost name of a call chain: requests.post(...) -> 'requests'."""
    current = node
    while isinstance(current, (ast.Call, ast.Attribute)):
        current = current.func if isinstance(current, ast.Call) else current.value
    return current.id if isinstance(current, ast.Name) else None


def _catches(handler, exception_names):
    if handler.type is None:
        return False
    targets = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for target in targets:
        name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
        if name in exception_names:
            return True
    return False


def _line_range(node):
    return node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno


def collect(ctx):
    findings = []

    for path, tree in ctx.python_trees():
        blocks = list(_atomic_blocks(tree)) + list(_atomic_decorated(tree))

        for block in blocks:
            start, end = _line_range(block)
            savepoints = _inner_atomic_lines(block)

            for node in ast.walk(block):
                # A network call holds the transaction open for the other
                # service's latency, including its timeout.
                if isinstance(node, ast.Call) and call_name(node) in EXTERNAL_CALLS:
                    root = _root_name(node)
                    if root in EXTERNAL_MODULES or call_name(node) in ("send_mail", "urlopen"):
                        findings.append(finding(
                            path, node.lineno, "external_call_in_atomic",
                            "a call out to another service sits inside transaction.atomic() "
                            "(opened on line " + str(start) + "), so the transaction — and its "
                            "locks — stay open for however long that service takes, including "
                            "its timeout",
                            "Move it outside the block, or defer it with "
                            "transaction.on_commit(...). If the external call must be undone on "
                            "rollback, that is a compensating action, not a transaction.",
                            "high"))

                # An IntegrityError caught inside the same atomic block cannot be
                # recovered from: the transaction is already marked for rollback.
                if isinstance(node, ast.Try):
                    # The suppression is "the risky statement sits in its own
                    # savepoint", so the question is whether the try *body*
                    # contains an inner atomic() — not whether the try itself
                    # sits inside one.
                    try_start, try_end = _line_range(node)
                    savepointed = any(try_start <= start and end <= try_end
                                      for start, end in savepoints)
                    for handler in node.handlers:
                        if not _catches(handler, {"IntegrityError", "DatabaseError", "DataError"}):
                            continue
                        if savepointed:
                            continue
                        findings.append(finding(
                            path, handler.lineno, "integrity_error_caught_in_atomic",
                            "IntegrityError is caught inside the atomic block opened on line " +
                            str(start) + " — by the time the handler runs the transaction is "
                            "already marked for rollback, so the next query raises "
                            "TransactionManagementError instead of doing what this handler intends",
                            "Put the risky statement in its own inner "
                            "`with transaction.atomic():` (a savepoint) and catch around that, or "
                            "move the try/except outside the outer block entirely.",
                            "high"))

        # select_for_update() outside any transaction raises at runtime.
        atomic_ranges = [_line_range(b) for b in blocks]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and call_name(node) == "select_for_update"):
                continue
            if not _within(atomic_ranges, node.lineno):
                findings.append(finding(
                    path, node.lineno, "select_for_update_outside_atomic",
                    "select_for_update() outside a transaction raises "
                    "TransactionManagementError — there is no transaction for the lock to be "
                    "held by",
                    "Wrap it: with transaction.atomic(): obj = "
                    "Model.objects.select_for_update().get(pk=pk).",
                    "high"))

        # get_or_create races two concurrent requests unless the columns are unique.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and call_name(node) == "get_or_create":
                findings.append(finding(
                    path, node.lineno, "get_or_create_race",
                    "get_or_create() is two statements under the hood, so two concurrent requests "
                    "can both miss the SELECT and both attempt the INSERT",
                    "It is safe when a unique constraint covers the lookup columns — Django then "
                    "catches the IntegrityError and re-fetches. Confirm the constraint exists; "
                    "without one this creates duplicates under load.",
                    "low"))

        # A loop of saves inside one atomic block holds every lock to the end.
        for block in blocks:
            start, _end = _line_range(block)
            for node in ast.walk(block):
                if not isinstance(node, (ast.For, ast.AsyncFor)):
                    continue
                writes = [n for n in ast.walk(node)
                          if isinstance(n, ast.Call) and call_name(n) in WRITE_CALLS]
                if writes:
                    findings.append(finding(
                        path, node.lineno, "atomic_around_loop_of_saves",
                        "a loop of writes runs inside the atomic block opened on line " +
                        str(start) + ", so every row it touches stays locked until the last "
                        "iteration commits — the lock window grows with the data",
                        "Batch it (bulk_update/bulk_create), or commit in chunks with a smaller "
                        "atomic block per chunk. One transaction over a whole table is how a "
                        "nightly job blocks the application.",
                        "medium"))
                    break

    return findings


if __name__ == "__main__":
    sys.exit(run("find_transaction_issues", "Django transaction issues", collect))
