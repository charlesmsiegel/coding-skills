# Fixing query problems without breaking semantics

The detector tells you a query problem exists. Which fix is right is a judgment
call, and several of the obvious ones quietly change what the code does.

## select_related vs prefetch_related

They are not interchangeable, and picking by guess produces a slower query.

| | `select_related` | `prefetch_related` |
|---|---|---|
| Relations | ForeignKey, OneToOne (forward) | ManyToMany, reverse FK, GenericForeignKey |
| Mechanism | SQL JOIN — one query | A second query plus a join in Python |
| Cost | Row duplication: a JOIN across two multi-valued relations multiplies rows | An extra round trip, and the whole related set in memory |
| Chaining | `select_related('order__customer__region')` | `prefetch_related('orders__items')` |

The rule: **forward-to-one is a JOIN, anything-to-many is a second query.** Using
`select_related` across two to-many relations is how a page that was slow becomes
a page that returns a million rows.

```python
# One query, joined
Order.objects.select_related("customer")

# Two queries; the second fetches all items for the orders in the first
Customer.objects.prefetch_related("orders")

# Control the inner queryset — filter, order, or nest a select_related in it
from django.db.models import Prefetch
Customer.objects.prefetch_related(
    Prefetch("orders", queryset=Order.objects.filter(status="open").select_related("region"))
)
```

`Prefetch` is the tool people miss. When a prefetch pulls back thousands of rows
to display ten, filter the inner queryset rather than filtering in the template.

### When the answer is neither

Prefetching a relation only to compute an aggregate loads every row to count
them. Push it into the database:

```python
# Loads every order for every customer
Customer.objects.prefetch_related("orders")   # then customer.orders.count() in the template

# Counts in SQL, loads nothing
from django.db.models import Count
Customer.objects.annotate(order_count=Count("orders"))
```

## Bulk operations, and what they skip

The bulk methods are fast because they **bypass the ORM's per-row machinery.**
That is the whole trade, and it is the part that bites.

| | Skipped |
|---|---|
| `bulk_create` | `save()`, `pre_save`/`post_save` signals, and on some backends the returned primary keys |
| `bulk_update` | `save()`, `pre_save`/`post_save`, `auto_now` fields |
| `queryset.update()` | `save()`, signals, `auto_now`, and any validation in `save()` |
| `queryset.delete()` | `delete()` on the model — though it *does* run `pre_delete`/`post_delete` and cascade |

So before converting a loop:

1. Does the model override `save()`? If it does, `update()` will not run it. Move
   that logic somewhere the bulk path also reaches, or don't convert.
2. Is there a `post_save` receiver? It will not fire. If something downstream
   depends on it (a cache invalidation, a search index), the bulk version silently
   breaks it — and nothing in the test suite necessarily notices.
3. Is there an `auto_now` field? It will not update.

A per-row loop with a comment saying *why* is better than a bulk call that skips a
signal nobody remembered.

## F() expressions and the lost update

```python
# Read, compute, write. Another writer between the read and the write is lost.
product = Product.objects.get(pk=1)
product.stock -= 1
product.save()

# The database computes it; concurrent decrements both land.
Product.objects.filter(pk=1).update(stock=F("stock") - 1)
```

This is a correctness fix, not a performance one — which is why
`update_without_f` is ranked 🔴. The bug appears only under concurrency, so it
passes every test and shows up as inventory that doesn't add up.

`F()` returns an expression, not a value: after `update()`, the in-memory instance
still holds the old number. `refresh_from_db()` if you need it.

For anything that must be atomic across multiple statements, `F()` is not enough —
use `select_for_update()` inside a transaction. `django-async-and-tasks.md`
covers what else belongs inside that block, and what must not.

## When raw SQL is the right answer

The ORM is not always the tool. Reach for raw SQL when:

- The query needs a window function, a recursive CTE, or a database-specific
  feature the ORM does not express.
- The generated SQL is measurably wrong for the data — a query planner problem
  the ORM cannot be talked out of.
- It is a one-off report or migration, not part of the application's read path.

When you do:

```python
# Parameters go in params=, never into the string. This is not style.
Order.objects.raw("SELECT * FROM shop_order WHERE status = %s", [status])

with connection.cursor() as cursor:
    cursor.execute("SELECT ... WHERE id = %s", [order_id])
```

Never `.extra()` — it is long-discouraged, and its `where=` argument takes SQL
fragments with no parameterization at all.

## only(), defer(), and values()

`only()` and `defer()` are deferred loading, not exclusion. Touching a deferred
field triggers **another query, per instance** — so `only()` applied to a queryset
whose template renders a deferred field is strictly worse than not using it.

`values()` and `values_list()` return dicts and tuples, not model instances. No
model methods, no properties, no `__str__`. That is a real interface change, not
an optimization detail, and it is where "just add values()" breaks a template.

## Measure it

Every claim in this file is conditional on your data. Confirm the fix:

```python
from django.test.utils import CaptureQueriesContext
from django.db import connection

with CaptureQueriesContext(connection) as ctx:
    render_the_page()
print(len(ctx.captured_queries))

# In a test, make it a ratchet:
with self.assertNumQueries(3):
    self.client.get("/orders/")
```

`assertNumQueries` around the view that had the N+1 is what stops it returning on
the next feature. Add it as part of the fix, not afterwards.
