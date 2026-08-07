#!/usr/bin/env python3
"""Async views, the ORM's a* surface, and background tasks.

Django has been async-capable since 3.1 and the surface keeps growing, which
creates a specific hazard: sync and async ORM calls look almost identical, and
mixing them is the difference between an exception and a silently blocked event
loop.

    Order.objects.get(pk=1)         # inside async def -> SynchronousOnlyOperation
    await Order.objects.aget(pk=1)  # correct
    Order.objects.aget(pk=1)        # no await: a coroutine created and discarded,
                                    # so the query never runs and nothing complains

The background-task half is the same class of problem one layer out. A Celery
task that takes a model instance serialises the whole object and then operates on
a snapshot; and a task enqueued inside `transaction.atomic()` can start running
before the transaction commits, so the worker looks for a row that is not there
yet. Both fail intermittently and under load, which is when they are hardest to
diagnose.
"""

import ast
import sys

from django_context import call_name
from django_report import finding, run

# The sync ORM methods with an async twin. Calling these inside async def raises.
SYNC_ORM_CALLS = frozenset({
    "get", "create", "update", "delete", "save", "count", "exists", "first", "last",
    "get_or_create", "update_or_create", "bulk_create", "bulk_update", "in_bulk",
    "aggregate", "latest", "earliest", "refresh_from_db",
})
# Their async equivalents, which must be awaited.
ASYNC_ORM_CALLS = frozenset({
    "aget", "acreate", "aupdate", "adelete", "asave", "acount", "aexists", "afirst",
    "alast", "aget_or_create", "aupdate_or_create", "abulk_create", "abulk_update",
    "ain_bulk", "aaggregate", "alatest", "aearliest", "arefresh_from_db",
})
# Modules whose I/O is synchronous, so a call into them stalls the event loop.
BLOCKING_MODULES = frozenset({"requests", "urllib", "urllib.request", "socket"})

ENQUEUE_CALLS = frozenset({"delay", "apply_async", "enqueue", "send_task"})


def _async_functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            yield node


def _awaited_nodes(func):
    """Every call that sits directly under an `await`."""
    awaited = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Await):
            for sub in ast.walk(node.value):
                awaited.add(id(sub))
    return awaited


def _nested_function_lines(func):
    """Line ranges of *sync* functions defined inside an async one.

    A `def` nested in an `async def` runs synchronously — usually because it is
    about to be handed to sync_to_async — so ORM calls inside it are correct and
    must not be reported.
    """
    ranges = []
    for node in ast.walk(func):
        if isinstance(node, ast.FunctionDef) and node is not func:
            ranges.append((node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno))
    return ranges


def _within(ranges, line):
    return any(start <= line <= end for start, end in ranges)


def _reaches_a_manager(node):
    """Whether a call chain passes through `.objects` or a related manager."""
    names = []
    current = node
    while isinstance(current, (ast.Call, ast.Attribute)):
        if isinstance(current, ast.Call):
            current = current.func
            continue
        names.append(current.attr)
        current = current.value
    return "objects" in names


def _imported_modules(ctx):
    return set(ctx.imports)


def _atomic_ranges(func):
    """Line ranges covered by a `with transaction.atomic():` block."""
    ranges = []
    for node in ast.walk(func):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            target = item.context_expr
            name = call_name(target) if isinstance(target, ast.Call) else None
            if name == "atomic" or (isinstance(target, ast.Attribute) and target.attr == "atomic"):
                ranges.append((node.lineno,
                               getattr(node, "end_lineno", node.lineno) or node.lineno))
    return ranges


def _passes_a_model_instance(node):
    """A task call whose argument looks like a model instance rather than a pk.

    The tell is an argument that is a bare name and is *not* obviously an id:
    `send_receipt.delay(order)` versus `send_receipt.delay(order.pk)`.
    """
    for arg in node.args:
        if isinstance(arg, ast.Name):
            lowered = arg.id.lower()
            if lowered.endswith(("_id", "_pk", "id", "pk", "_ids", "_pks")):
                continue
            return arg
    return None


def collect(ctx):
    findings = []
    imported = _imported_modules(ctx)
    uses_blocking_http = bool(imported & BLOCKING_MODULES)

    for path, tree in ctx.python_trees():
        for func in _async_functions(tree):
            awaited = _awaited_nodes(func)
            sync_ranges = _nested_function_lines(func)

            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                if _within(sync_ranges, node.lineno):
                    continue
                name = call_name(node)

                # A sync ORM call inside async def raises SynchronousOnlyOperation.
                if name in SYNC_ORM_CALLS and _reaches_a_manager(node):
                    findings.append(finding(
                        path, node.lineno, "sync_orm_in_async_view",
                        "`" + str(name) + "()` is a synchronous ORM call inside the async function "
                        "`" + func.name + "` — Django raises SynchronousOnlyOperation rather than "
                        "silently blocking",
                        "Use the async twin and await it: await ....a" + str(name) + "(...). "
                        "Where no twin exists, wrap the sync work in "
                        "sync_to_async(fn, thread_sensitive=True).",
                        "high"))

                # An async ORM call that is never awaited does nothing at all.
                if name in ASYNC_ORM_CALLS and id(node) not in awaited:
                    findings.append(finding(
                        path, node.lineno, "unawaited_async_orm_call",
                        "`" + str(name) + "()` returns a coroutine that is never awaited, so the "
                        "query never runs — and nothing raises, so the code reads as if it worked",
                        "await it. If the result is genuinely not needed now, hold the coroutine "
                        "and await it later, or wrap it in asyncio.create_task().",
                        "high"))

                # Blocking I/O stalls every request on the worker, not just this one.
                if name == "sleep" and not _within(sync_ranges, node.lineno):
                    if id(node) not in awaited:
                        findings.append(finding(
                            path, node.lineno, "blocking_io_in_async",
                            "time.sleep() inside `" + func.name + "` blocks the event loop, so "
                            "every other request this worker is serving waits too",
                            "await asyncio.sleep(...).",
                            "high"))
                if uses_blocking_http and name in ("post", "put", "patch") and id(node) not in awaited:
                    chain = []
                    current = node.func
                    while isinstance(current, ast.Attribute):
                        chain.append(current.attr)
                        current = current.value
                    if isinstance(current, ast.Name) and current.id in BLOCKING_MODULES:
                        findings.append(finding(
                            path, node.lineno, "blocking_io_in_async",
                            "a synchronous HTTP call inside `" + func.name + "` blocks the event "
                            "loop for the duration of the request to the other service",
                            "Use httpx.AsyncClient and await it. An async view that blocks on "
                            "the network is slower than the sync view it replaced.",
                            "high"))

                # sync_to_async without thread_sensitive gets a fresh thread, which
                # means a different database connection and a different transaction.
                if name == "sync_to_async":
                    has_flag = any(kw.arg == "thread_sensitive" for kw in node.keywords)
                    if not has_flag:
                        findings.append(finding(
                            path, node.lineno, "sync_to_async_without_thread_sensitive",
                            "sync_to_async() without thread_sensitive — the default is True, but "
                            "stating it matters here: with False the wrapped code runs in a "
                            "fresh thread, so it gets a different database connection and cannot "
                            "see the caller's open transaction",
                            "Pass thread_sensitive=True explicitly for anything touching the ORM.",
                            "low"))

    # ---- background tasks ----------------------------------------------------- #
    for path, tree in ctx.python_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node)
            if name not in ENQUEUE_CALLS:
                continue

            instance = _passes_a_model_instance(node)
            if instance is not None:
                findings.append(finding(
                    path, node.lineno, "task_takes_model_instance",
                    "`" + str(instance.id) + "` is passed to a background task, which serialises "
                    "the whole object — the worker then operates on a snapshot taken at enqueue "
                    "time, so any change made in between is silently overwritten or lost",
                    "Pass the primary key and re-fetch inside the task. The task is also then "
                    "retryable, which an embedded object is not.",
                    "medium"))

        # An enqueue inside atomic() can run before the commit it depends on.
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            atomic = _atomic_ranges(func)
            if not atomic:
                continue
            uses_on_commit = any(
                isinstance(n, ast.Call) and call_name(n) == "on_commit" for n in ast.walk(func))
            if uses_on_commit:
                continue
            for node in ast.walk(func):
                if (isinstance(node, ast.Call) and call_name(node) in ENQUEUE_CALLS
                        and _within(atomic, node.lineno)):
                    findings.append(finding(
                        path, node.lineno, "enqueue_without_on_commit",
                        "a task is enqueued inside transaction.atomic(), so the worker can pick it "
                        "up before the transaction commits — and then it queries for a row that "
                        "does not exist yet. It fails intermittently, and more often under load",
                        "transaction.on_commit(lambda: task.delay(obj.pk)) — which also means the "
                        "task is never enqueued at all if the transaction rolls back.",
                        "high"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_async_issues", "Django async and background-task issues", collect))
