# Where the logic goes

Django gives you four or five plausible homes for any piece of business logic and
no opinion about which to use. Most Django codebases are messy because that
decision was made ad hoc, file by file, over years.

## The default answer

**Behavior that is about one record belongs on the model.** Behavior that is about
a *process* — several records, an external service, a transaction, an ordering —
belongs in a plain function.

```python
class Order(models.Model):
    def mark_paid(self, at):          # about this record
        self.paid_at = at
        self.status = Status.PAID
        self.save(update_fields=["paid_at", "status"])


def checkout(cart, payment_method):    # about a process
    with transaction.atomic():
        order = Order.from_cart(cart)
        charge = payments.charge(payment_method, order.total)
        order.mark_paid(charge.completed_at)
        email.send_receipt(order)
    return order
```

Note the second one is a **function**, not a `CheckoutService` class. A class with
one public method and no state is a function wearing a costume — the `self` is
never used and the extra name has to be understood by every reader.

A service *class* earns its keep when it holds real state across calls (a client,
a connection, a batch being accumulated) or when it has several methods that
genuinely share it.

## Fat models: what the complaint actually is

"Fat model" is usually misdiagnosed. A model with thirty methods is not
automatically wrong — models are where record behavior belongs, and moving it to a
service just relocates the volume.

The real questions:

- **Does the method need this record, or does it just start from it?**
  `order.total()` needs the record. `order.export_to_accounting_system()` starts
  from it and mostly talks to something else.
- **Would this method exist if the database were a different shape?** If yes, it
  is domain logic and belongs on the model. If it is about serialization,
  transport, or a third-party API, it does not.
- **Does importing this model now drag in six other modules?** That is the real
  cost of a fat model, and it is what makes `models.py` a circular-import hub.

Splitting by *layer* ("all logic goes to services/") almost never helps, because it
puts `order_total` and `order_export` in the same place for no reason. Splitting by
*what the code is about* does.

## Signals: use them across apps, not within one

A signal is invisible at the call site. That is its purpose — the sender does not
know who is listening — and it is exactly why it is the wrong tool inside a single
app.

**Use a signal when** two apps must not import each other, when a third-party app
needs a hook it did not provide, or when several unrelated things react to one
event and the sender genuinely should not know about them.

**Do not use a signal when** you control both sides. Then it is just a method call
you have made untraceable:

```python
# The reader of Order.save() has no way to know this exists.
@receiver(post_save, sender=Order)
def update_inventory(sender, instance, **kwargs):
    instance.product.decrement_stock()

# Explicit: greppable, testable, and ordered.
class Order(models.Model):
    def complete(self):
        self.status = Status.COMPLETE
        self.save()
        self.product.decrement_stock()
```

Signals also fire on **every** save, including fixtures, migrations, `bulk_create`
(no — they *don't* fire there, which is the other half of the trap), and the admin.
Half the "why did this run twice" bugs in Django are signals.

Before deleting one, grep for every sender: `send()`, `save()`, the admin, and any
management command.

## Class-based vs function-based views

Not a style question — a question about how many of the hooks you actually use.

**Function-based** when the view does something specific. It reads top to bottom
and there is no dispatch machinery to hold in your head.

**Class-based generic** when the view is one of the shapes Django already
implements — a list, a detail, a create/update form — and you are overriding one
or two hooks (`get_queryset`, `get_context_data`).

**Neither** when you find yourself overriding five or six methods. At that point
the generic view is fighting you: you have inherited a template method pattern
whose control flow lives in Django's source, and a reader has to reconstruct it
from the base classes. Write the function.

A useful test: can a reader determine what this view returns without opening
Django's source? If not, the inheritance has stopped paying.

## App boundaries

An "app" should be a thing you could plausibly delete. If removing an app would
require edits in six others, it was never a boundary.

Signs the boundaries are wrong:

- Circular imports between apps, "solved" with function-level imports.
- One app's models importing another's constantly — the two are one thing.
- A `core/` or `common/` app that everything imports and that imports everything.
  That is not a boundary; it is a junk drawer that will grow forever.
- Migrations in app A that depend on app B's migrations in both directions.

Splitting apps is expensive once migrations exist. Getting it wrong early and
leaving it is usually cheaper than a heroic reorganization — but stop *adding* to
the junk drawer.

## Middleware

Middleware runs on **every request**, including static files, health checks, and
the ones that error out early. That is the cost, and it is charged whether or not
the request needed anything.

Use it for genuinely cross-cutting concerns: authentication, request IDs,
transactions, locale. Everything else — anything that applies to a subset of views
— should be a decorator or a mixin on those views, where the cost is paid only
where the benefit is.
