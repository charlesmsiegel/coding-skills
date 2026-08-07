#!/usr/bin/env python3
"""Django-shaped test smells — the ones about the database, not about assertions.

python-code-doctor's find_test_smells.py covers tests that assert nothing and
tests that over-mock. This covers the traps that exist because Django's test
tooling does something helpful that is occasionally wrong:

- `TestCase` wraps each test in a transaction and rolls it back. Code that
  depends on `transaction.on_commit` therefore never runs, and the test passes
  by not exercising the thing it was written for.
- `setUpTestData` hands each test method a deepcopy. Mutating one and then
  asserting through a view compares a local copy against an untouched row.
- Query count is behaviour in Django, and nothing in an ordinary test suite
  notices when it changes. `assertNumQueries` is the only assertion that does.

The query-count check is deliberately whole-suite rather than per-test: "this
one test has no assertNumQueries" is noise, and "nothing in this project ever
asserts a query count" is a fact worth one finding.
"""

import ast
import sys

from django_context import call_name, source_of
from django_report import finding, run

DJANGO_TEST_BASES = frozenset({"TestCase", "TransactionTestCase", "SimpleTestCase",
                               "LiveServerTestCase", "APITestCase", "APITransactionTestCase"})
# The base classes that roll back rather than commit, so on_commit never fires.
ROLLBACK_BASES = frozenset({"TestCase", "APITestCase"})


def _test_classes(ctx):
    for name, info in ctx.classes.items():
        if ctx.is_ambiguous(name):
            continue
        if set(info.bases) & DJANGO_TEST_BASES or ctx.derives_from(name, DJANGO_TEST_BASES):
            yield name, info


def _self_attribute_assignments(func):
    """`self.order.total = 0` — mutating an object the fixture set up."""
    hits = []
    for node in ast.walk(func):
        target = None
        if isinstance(node, ast.Assign) and node.targets:
            target = node.targets[0]
        elif isinstance(node, ast.AugAssign):
            target = node.target
        if not isinstance(target, ast.Attribute):
            continue
        inner = target.value
        # Two hops: self.<fixture>.<field>
        if (isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name)
                and inner.value.id == "self"):
            hits.append((node, inner.attr, target.attr))
    return hits


def _saves_after(func, line):
    for node in ast.walk(func):
        if (isinstance(node, ast.Call) and call_name(node) in ("save", "refresh_from_db")
                and node.lineno >= line):
            return True
    return False


def collect(ctx):
    findings = []
    test_classes = list(_test_classes(ctx))
    if not test_classes:
        return findings

    asserts_query_counts = False

    for name, info in sorted(test_classes, key=lambda kv: (str(kv[1].file), kv[1].line)):
        path = info.file
        bases = set(info.bases)

        for method_name, method in info.methods.items():
            body = source_of(ctx, path, method)
            if "assertNumQueries" in body:
                asserts_query_counts = True

            # on_commit under TestCase never fires: the transaction rolls back.
            if "on_commit" in body and (bases & ROLLBACK_BASES):
                findings.append(finding(
                    path, method.lineno, "on_commit_needs_transaction_testcase",
                    "`" + name + "." + method_name + "` exercises transaction.on_commit under "
                    "TestCase, which wraps each test in a transaction and rolls it back — so the "
                    "callback never runs and this test passes without testing anything",
                    "Use TransactionTestCase (slower, commits for real), or "
                    "django.test.utils.captureOnCommitCallbacks(execute=True) to run the "
                    "callbacks inside the wrapped transaction.",
                    "high"))

            # self.client.login() where force_login is meant.
            for node in ast.walk(method):
                if (isinstance(node, ast.Call) and call_name(node) == "login"
                        and any(isinstance(n, ast.Attribute) and n.attr == "client"
                                for n in ast.walk(node))):
                    findings.append(finding(
                        path, node.lineno, "client_login_over_force_login",
                        "self.client.login() runs the full authentication backend, including "
                        "password hashing, on every test that calls it — which is the single "
                        "biggest cost in most Django suites",
                        "Use self.client.force_login(user) unless the login flow itself is what "
                        "this test is about.",
                        "low"))

        # setUpTestData objects are deepcopied per test method; mutating one
        # does not reach the database.
        setup = info.methods.get("setUpTestData")
        if setup is not None:
            fixture_names = set()
            for node in ast.walk(setup):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (isinstance(target, ast.Attribute)
                                and isinstance(target.value, ast.Name)
                                and target.value.id == "cls"):
                            fixture_names.add(target.attr)

            for method_name, method in info.methods.items():
                if method_name == "setUpTestData":
                    continue
                for node, fixture, field in _self_attribute_assignments(method):
                    if fixture not in fixture_names:
                        continue
                    if _saves_after(method, node.lineno):
                        continue
                    findings.append(finding(
                        path, node.lineno, "setuptestdata_mutation",
                        "`self." + fixture + "." + field + "` is changed on an object that came "
                        "from setUpTestData, which hands every test method a deepcopy — the "
                        "change never reaches the database, so anything asserting through a view "
                        "or a fresh query is comparing against the untouched row",
                        "Call save() when the change is meant to be visible, or "
                        "refresh_from_db() when you want the row rather than the copy. Build "
                        "objects in setUp() instead when each test needs its own.",
                        "high"))

    # ---- one finding for the whole suite ------------------------------------- #
    if not asserts_query_counts:
        anchor = test_classes[0][1].file
        findings.append(finding(
            anchor, 1, "no_query_count_assertions",
            "nothing in this test suite calls assertNumQueries, so the number of database round "
            "trips is unpinned — an N+1 introduced by the next feature passes every test",
            "Add assertNumQueries around the views that matter, at the count they currently "
            "produce. That is what turns a prefetch from a guess into a measurement, and what "
            "stops the N+1 coming back.",
            "medium"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_test_issues", "Django test issues", collect))
