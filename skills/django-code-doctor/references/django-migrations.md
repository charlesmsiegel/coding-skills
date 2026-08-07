# Migrations

A migration is the only code in a Django project that runs **once**, against
production data, usually unattended, usually while the previous release is still
serving traffic. Nothing else in the project has that combination, and it is why
migrations need their own rules.

The rule that governs the rest: **a migration is not refactorable.** Once it has
run somewhere you do not control, it is history. Changing it does not change what
happened on that database.

---

## Historical models: the trap that passes review

```python
# WRONG — works today, breaks forever after
from shop.models import Order

def backfill(apps, schema_editor):
    Order.objects.update(status="new")
```

`Order` here is the model as it is **today**, not as it was when this migration
was written. When someone adds a required field next month, this migration —
which runs from zero on every CI build and every new developer's machine — starts
failing, on a line nobody touched.

```python
# RIGHT
def backfill(apps, schema_editor):
    Order = apps.get_model("shop", "Order")
    Order.objects.update(status="new")
```

`apps.get_model` gives the model **as of this point in the migration graph**.

The historical model is not a full model: it has fields and managers, but **no
custom methods, no `save()` override, no signals**. If your backfill needs
`order.recalculate_total()`, that method does not exist on the historical model.
Inline the logic, or accept that the backfill is pinned to a snapshot of it.

---

## Reversibility

```python
migrations.RunPython(backfill, migrations.RunPython.noop)
migrations.RunSQL(forward_sql, reverse_sql)
```

`noop` and a real reverse are both fine. **No second argument is not.** It means
`migrate <app> <previous>` fails, so a bad deploy has to be fixed forward, at
speed, while it is failing.

The distinction that matters: `RunPython.noop` is a decision on the record — "this
cannot be meaningfully undone, and I thought about it". An absent argument is
someone not having thought about it, and the two are indistinguishable later.

---

## Splitting schema from data

One migration should do one kind of thing.

```python
# 0002_add_status.py      — schema. Fast, takes a lock.
migrations.AddField("order", "status", models.CharField(max_length=16, default="new"))

# 0003_backfill_status.py — data. Slow, no DDL lock.
migrations.RunPython(backfill, migrations.RunPython.noop)
```

Mixed together, the DDL lock taken by `AddField` is held for the whole duration
of the backfill. On a large table that is the outage. Split, the backfill can
also be re-run, throttled, or run out-of-hours without re-running the DDL.

---

## Adding a column to a table that has rows

A `NOT NULL` column with no default fails immediately. The three-step version is
also the one that does not hold a long lock:

1. **Add it nullable.** Fast, no table rewrite on modern PostgreSQL.
2. **Backfill in batches**, in a separate migration or a management command.
3. **Make it `NOT NULL`.**

```python
# 0003_backfill.py — batched, so no single long transaction
def backfill(apps, schema_editor):
    Order = apps.get_model("shop", "Order")
    while True:
        batch = list(Order.objects.filter(status__isnull=True).values_list("pk", flat=True)[:1000])
        if not batch:
            break
        Order.objects.filter(pk__in=batch).update(status="new")
```

Set `atomic = False` on a long data migration so it commits per batch instead of
holding one transaction over the whole table:

```python
class Migration(migrations.Migration):
    atomic = False
```

The trade is that a failure halfway leaves it half-applied, so the backfill has
to be **idempotent** — which the loop above is, because it filters on what is
still null.

---

## Zero-downtime: old code and new schema run together

During a rolling deploy, the previous release is still serving requests against
the new schema. Every migration has to be compatible with the code that is
already running.

**Adding is safe. Removing and renaming are not.**

To remove a column:

1. **Release 1** — stop referencing it in code. Deploy. Now no running code
   touches it.
2. **Release 2** — `RemoveField`. Deploy.

To rename a column, the same shape with four steps: add the new one, write to
both, backfill, read from the new one, drop the old. `RenameField` in one
migration is correct only when you can afford for the old code to break.

The same applies to `AlterField` narrowing a type or adding a constraint: the old
code may still write values the new schema rejects.

---

## Conflicting leaf migrations

Two branches each add `0007_*`. Neither depends on the other, so `migrate` refuses
to run — there is no single "latest".

```bash
python manage.py makemigrations --merge
```

Read what it produces. A merge migration is a **claim that the two branches
commute**, and sometimes they do not: two branches that each added an index with
the same name, or that both altered the same field, need a human decision rather
than a merge.

---

## Squashing

Migrations accumulate, and a project with 400 of them takes minutes to build a
test database from scratch.

```bash
python manage.py squashmigrations shop 0001 0087
```

Then: keep the originals until every environment has applied past the squash
point, and only then delete them. Deleting early strands any database that had
not caught up.

Squashing is worth doing when test-database setup is measurably slow. It is not
worth doing for tidiness — the old migrations cost nothing at runtime.

---

## Checking your work

```bash
python manage.py makemigrations --check --dry-run   # is anything unmigrated?
python manage.py sqlmigrate shop 0003               # the actual SQL
python manage.py migrate shop 0002                  # can it roll back?
```

`makemigrations --check` is the one to put in CI. A model change with no
migration passes every test — Django builds the test database from the models,
not the migrations — and then fails on deploy. It is also the one check this
skill's parser structurally cannot do, which is why
`run_external_tools.py --run-migrations-check` exists.

`sqlmigrate` before anything touching a large table. It shows whether you are
about to take a lock.

---

## Migrations and the models this skill flags

Several findings from `find_model_issues.py` are schema changes wearing the
clothes of a refactor. Before acting on one, run `makemigrations --dry-run` and
read the result:

| Finding | Migration? |
|---|---|
| `null_on_text_field` — dropping `null=True` | **Yes**, and it needs a backfill of existing NULLs to `""` first |
| `missing_related_name` — adding one | **No** — the accessor is Python-side. But grep for the old `foo_set` before changing it |
| `inline_choices` → `TextChoices` | **No**, provided the stored values are identical. If a migration appears, they are not |
| `unique_together` → `UniqueConstraint` | **Yes**, but it should drop and add an equivalent index |
| `decimal_without_precision` | **Yes**, and it can truncate stored values |
| Removing an abstract base | **No**, if the resulting concrete fields are identical |
| `multi_table_inheritance` → `OneToOneField` | **Yes**, and it is a data migration, not a schema one |

The general rule: if the change alters what a column *is*, it is a migration and
a deploy, not a refactor.
