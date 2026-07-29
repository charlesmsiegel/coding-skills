# Does this abstraction earn its keep?

Django supplies a lot of extension points, and the presence of an extension point
is not a reason to use it. Every one of these findings is the same question:
**what would break if this were deleted?** If the answer is "nothing, it would just
be written inline once", delete it.

| Pattern | The complaint | The default fix |
|---|---|---|
| Abstract model with one concrete child | A file to open, no shape shared | Merge the fields into the child |
| Abstract model with no children | Describes something that does not exist | Delete it |
| Manager with no methods | The default manager already does this | Delete it |
| Manager with one method | A QuerySet method chains and composes; this doesn't | `QuerySet.as_manager()` |
| Mixin used once | Indirection without reuse | Inline it |
| Form/serializer 3+ levels deep | Finding its fields means reading 3 files | Flatten; share with composition |
| Middleware with a trivial `__call__` | Runs on every request to do nearly nothing | A decorator on the views that need it |
| Service class that only wraps CRUD | Forwards to the ORM, adds no rule | Call the ORM; or a model method |
| `post_save` signal doing 2 lines | Invisible behavior on every save | Put it in `save()` or an explicit method |

## Managers and QuerySets — the one worth understanding

This is the most common Django over-abstraction and the fix is genuinely better,
not just smaller.

A custom **manager** method does not chain. Once you have two of them you cannot
combine them:

```python
class OrderManager(models.Manager):
    def open(self):
        return self.filter(status="open")

    def recent(self):
        return self.filter(created__gte=...)

Order.objects.open().recent()   # AttributeError — open() returned a QuerySet
```

A custom **QuerySet** method does chain, and `as_manager()` gives you the manager
for free:

```python
class OrderQuerySet(models.QuerySet):
    def open(self):
        return self.filter(status="open")

    def recent(self):
        return self.filter(created__gte=...)


class Order(models.Model):
    objects = OrderQuerySet.as_manager()

Order.objects.open().recent()                       # works
Order.objects.filter(region=r).open().recent()      # also works
customer.orders.open()                              # and on related managers
```

So `thin_manager` is not only "you didn't need this" — it is "the thing you wrote
is worse than the thing that is the same amount of code".

Write a real Manager only when you need to change the *default* queryset for the
model (`get_queryset`), or when the method genuinely does not start from a
queryset (a `create_with_items()` factory, for instance).

## Abstract models

Abstract models are cheap to add and expensive to have, because they change how
every reader finds a field. `TimeStamped` with `created_at`/`updated_at`, used by
twenty models, earns its keep easily. One with a single child does not.

The awkward part: **removing an abstract base does not require a migration** if
the resulting concrete fields are identical, because abstract models create no
table. Verify with `makemigrations --dry-run` before assuming either way.

Do not confuse abstract inheritance with **multi-table inheritance** (a concrete
parent). MTI adds an implicit join to every query on the child and is a genuine
performance decision, not a code-organization one. Prefer a OneToOneField, where
the join is at least visible.

## Mixins

A mixin used once is inlined. A mixin used everywhere is fine. The interesting
case is the middle: three or four users, each overriding a different part.

Ask what happens on the next use. If each new user needs its own override, the
mixin is not capturing a shared rule — it is a template with holes, and the holes
are where the actual behavior lives. That is worse than duplication because the
control flow is now split across the MRO.

Watch for mixins that assume attributes they do not define (`self.model`,
`self.get_queryset()`). Those only work when mixed into the right base, which is a
dependency the code does not state.

## Service layers

Django's ORM already *is* a data-access layer. A service class that wraps it in
`create`/`get`/`update`/`delete` adds a name to learn and nothing else.

`crud_only_service` is identified by class name, not by the graph — there is no
Django base class to inherit from — so it is ranked low and should be confirmed by
reading. A class called `PaymentService` holding a client and retry policy is not
the pattern being complained about.

The version that earns its keep coordinates several things: a transaction, two
models, an external call, an ordering constraint. That usually wants to be a
function (see `django-architecture.md`), and it wants to exist.

## When NOT to remove an abstraction

- **A migration depends on it.** Historical migrations reference model state; an
  abstract base that shaped a past migration cannot always be removed cleanly.
- **A third-party package or downstream project subclasses it.** Your project is
  not the only user of a reusable app's abstract models.
- **It is on a genuinely planned second implementation** — not a hypothetical one,
  a scheduled one.
- **The duplication it would create spans apps that must not import each other.**

And the general rule from python-simplifier applies here too: don't refactor cold
code. An odd abstraction in a module nobody has touched in three years is costing
nothing.
