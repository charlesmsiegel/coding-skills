# The safety net before a Django refactor

Refactoring without tests is editing. This is the Django-shaped version of that
rule: what has to be pinned here is not only *what the code returns* but *how many
times it talks to the database*, because the ORM makes that count invisible at the
call site and nothing else in the suite will notice when it changes.

## Pin the query count, not just the result

`assertNumQueries` is the one assertion this skill's findings actually need. It is
what turns "I added `select_related` and it looks faster" into a fact:

```python
def test_order_list_is_one_query(self):
    with self.assertNumQueries(2):          # 1 for the page, 1 for the prefetch
        self.client.get("/orders/")
```

Write it **before** the fix, at the count the code currently produces. The test then
fails on the fix, and you change the number in the same commit that earns it — so
the diff records the improvement instead of asserting it. A prefetch that leaves the
number unchanged is a guess that happened to compile.

For finer control than the test-client boundary:

```python
from django.test.utils import CaptureQueriesContext
from django.db import connection

with CaptureQueriesContext(connection) as ctx:
    list(Order.objects.for_dashboard())
print(len(ctx.captured_queries))            # and ctx.captured_queries[i]["sql"]
```

`CaptureQueriesContext` gives you the SQL itself, which is how you tell a genuine
N+1 from a loop that issues one query and iterates a cached result.

## Characterization tests for views you do not understand

When the behavior is unclear — the usual case in inherited code — pin what it does
now rather than what it should do. Assert the status code, the redirect target, the
context keys the template actually reads, and the query count. Name it for what it
is (`test_characterizes_...`) so the next reader knows it records behavior rather
than specifying it.

Do this for the *unauthorized* path too. Every `missing_ownership_filter` fix is a
change to who can read a row, and a test that only exercises the owner cannot tell a
successful fix from a broken one:

```python
def test_other_users_order_is_404(self):
    self.client.force_login(self.mallory)
    assert self.client.get(f"/orders/{self.alice_order.pk}/").status_code == 404
```

## Traps specific to Django's test tooling

- **`TestCase` wraps each test in a transaction and rolls back.** Code that depends
  on `transaction.on_commit`, or on a second connection seeing the row, silently
  does nothing. Use `TransactionTestCase` for those — it is much slower, so use it
  only where the transaction boundary is the thing under test.
- **Query counts shift with the fixture, not only with the code.** A test that
  creates one related object hides an N+1 that appears at two. Create at least two
  rows in any test whose purpose is to detect a per-row query.
- **`setUpTestData` objects are in-memory copies, not live rows.** Since Django 3.2
  each attribute it assigns is handed to every test method as a `deepcopy`, so
  mutating `self.order` does *not* leak into the next test — but it also does not
  reach the database. A test that sets `self.order.total = 0` and then asserts
  through a view or a fresh `Order.objects.get(...)` is comparing its local copy
  against the untouched row. Call `save()` when the change is meant to be visible,
  or `refresh_from_db()` when you want the row rather than the copy. (On Django
  before 3.2 the objects really were shared across tests and mutation did leak;
  that version is long unsupported, so do not carry the old workaround forward.)
- **`transaction.on_commit` callbacks never fire under `TestCase`.** The wrapping
  transaction rolls back, so the callback is discarded — and the test passes
  without exercising the thing it was written for. Use
  `captureOnCommitCallbacks(execute=True)` to run them inside the wrapped
  transaction, or `TransactionTestCase` when the commit itself is under test.
  This is the same trap as the bullet above, one layer out, and it is why
  `enqueue_without_on_commit` bugs survive a green suite.
- **`self.client.login()` runs the real authentication backend**, password
  hashing included, on every test that calls it. `force_login()` skips it and is
  usually the single largest win available in a slow Django suite. Use `login()`
  only when the login flow is what the test is about.
- **Migrations run once for the test database.** A model-field change is a schema
  change, and no test in this file protects you from it — see
  `django-migrations.md`, and `makemigrations --check` in CI.

## Where the net is not worth building

Cold code, code being deleted next sprint, and admin glue nobody calls. Effort
follows churn. If a view has not changed in two years and is not in a finding, a
characterization test for it is inventory, not safety.
