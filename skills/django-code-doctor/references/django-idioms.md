# Writing idiomatic Django

This is the prescriptive half of the skill. The detectors say what is wrong with
code that exists; this says what to write when it does not exist yet.

**One canonical answer per task.** Where Django offers three ways to do
something, this picks one and says why. A codebase that uses one good pattern
everywhere is easier to work in than one that uses the best pattern for each
case, because the reader only has to learn it once.

**Version tags.** Entries marked *(5.0+)*, *(5.2+)*, *(6.0+)* need that version.
Check `find_version_issues.py --list-known` or the project's pin before using
one. Everything unmarked works on 4.2 and up.

---

## Models

The model is the part everything else is built on, and the part migrations make
expensive to change. Spend the extra minute here.

```python
from django.db import models
from django.urls import reverse


class Order(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PAID = "paid", "Paid"
        SHIPPED = "shipped", "Shipped"

    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.PROTECT,          # a deliberate choice, see below
        related_name="orders",             # always name it
    )
    reference = models.CharField(max_length=32, unique=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.DRAFT)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    notes = models.TextField(blank=True, default="")   # never null=True on text
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "pk"]   # unique enough that pagination is stable
        indexes = [models.Index(fields=["status", "-created_at"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total__gte=0),   # `check=` before 5.1
                name="order_total_non_negative",
            ),
        ]

    def __str__(self):
        return self.reference

    def get_absolute_url(self):
        return reverse("orders:detail", kwargs={"pk": self.pk})
```

Rules that earn their place:

| Do | Because |
|---|---|
| `related_name` on every relation | The default `order_set` changes when the model is renamed, silently |
| `on_delete` chosen, not copied | `CASCADE` on a FK to `User` deletes the orders when the account closes. Usually you want `PROTECT` |
| `blank=True, default=""`, never `null=True`, on text | Otherwise "no value" is two states and every query handles both |
| `Meta.ordering` with a tie-breaker | Without one, pagination repeats and skips rows |
| `TextChoices`, not a list of tuples | Gives you `.label`, `get_status_display()`, and a name to grep for |
| `choices=Status` *(5.0+)* | Before 5.0 it is `choices=Status.choices` |
| `Meta.constraints` over validation in `clean()` | The database enforces it; `clean()` only runs when a form does |
| `DecimalField` for money, never `FloatField` | Binary floating point cannot represent 0.10 |

**Set `AUTH_USER_MODEL` on day one**, even if the default looks sufficient:

```python
# accounts/models.py
class User(AbstractUser):
    pass

# settings.py
AUTH_USER_MODEL = "accounts.User"
```

This is the one Django decision with no cheap escape hatch. After the first
migration, changing it means rewriting every historical foreign key to
`auth.User`.

Reference other models by string (`"customers.Customer"`) rather than importing
them — that is what keeps `models.py` from becoming a circular-import hub.

### Newer field types worth knowing

```python
# Computed by the database, not by save() (5.0+)
total_with_tax = models.GeneratedField(
    expression=F("total") * Decimal("1.2"),
    output_field=models.DecimalField(max_digits=10, decimal_places=2),
    db_persist=True,
)

# A default the database applies, so bulk_create and raw inserts get it too (5.0+)
created_at = models.DateTimeField(db_default=Now())

# A genuine composite key (5.2+)
pk = models.CompositePrimaryKey("tenant_id", "reference")
```

`db_default` is the one to reach for most often: an ordinary `default=` is
applied by Python, so anything that writes without going through the ORM misses
it.

---

## QuerySets and managers

**Write a QuerySet, not a Manager.** QuerySet methods chain; manager methods do
not, and the code is the same length.

```python
class OrderQuerySet(models.QuerySet):
    def paid(self):
        return self.filter(status=Order.Status.PAID)

    def for_customer(self, customer):
        return self.filter(customer=customer)

    def with_totals(self):
        return self.annotate(item_count=Count("items"))


class Order(models.Model):
    objects = OrderQuerySet.as_manager()


Order.objects.paid().for_customer(c).with_totals()   # all of these compose
customer.orders.paid()                               # and it works on the reverse side too
```

Write a real `Manager` only to change the *default* queryset (`get_queryset`) or
for something that does not start from one (`create_with_items()`).

### Fetching

```python
# Forward to-one: a JOIN, one query
Order.objects.select_related("customer", "customer__region")

# To-many: a second query
Customer.objects.prefetch_related("orders")

# Control what the prefetch fetches — the tool people miss
Customer.objects.prefetch_related(
    Prefetch("orders", queryset=Order.objects.paid().select_related("region"))
)

# Count in SQL, load nothing
Customer.objects.annotate(order_count=Count("orders"))
```

The rule: **forward-to-one is a JOIN, anything-to-many is a second query.**

### Writing

```python
# Arithmetic in the database — concurrent writers both land
Product.objects.filter(pk=pk).update(stock=F("stock") - 1)

# Read-modify-write across several statements needs a lock
with transaction.atomic():
    product = Product.objects.select_for_update().get(pk=pk)
    product.reserve(quantity)
    product.save(update_fields=["stock", "reserved"])

# Many rows at once
Order.objects.bulk_create(orders, batch_size=1000)
```

`save(update_fields=[...])` is worth the habit: it writes the columns you named
instead of all of them, which turns a full-row update into a narrow one and
stops two concurrent saves clobbering each other's untouched fields.

---

## Views

**Default to a function.** Reach for a generic class-based view when the view is
exactly one of the shapes Django implements and you override one or two hooks.
When you find yourself overriding five, write the function — the inheritance has
stopped paying.

```python
@login_required
def order_detail(request, pk):
    order = get_object_or_404(
        Order.objects.select_related("customer"),
        pk=pk,
        customer=request.user.customer,      # the ownership filter, in the same query
    )
    return render(request, "orders/detail.html", {"order": order})
```

The `customer=request.user.customer` is the whole point. Django authenticates; it
does not authorize per object, and nothing reminds you. Filtering in the same
query means there is no window between the fetch and the check.

For a class-based view, the same rule lives in `get_queryset`:

```python
class OrderList(LoginRequiredMixin, ListView):
    template_name = "orders/list.html"
    paginate_by = 25

    def get_queryset(self):
        return (
            Order.objects
            .for_customer(self.request.user.customer)
            .select_related("customer")
            .with_totals()
        )
```

`LoginRequiredMixin` goes **first** in the bases, so it runs before `dispatch`.

### Forms

```python
class OrderForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ["reference", "notes"]     # named, never "__all__"

    def __init__(self, *args, customer=None, **kwargs):
        super().__init__(*args, **kwargs)
        # In __init__, not the class body: a class-body queryset is evaluated
        # once at import and never sees a row created afterwards.
        self.fields["shipping_address"].queryset = customer.addresses.all()

    def clean_reference(self):
        value = self.cleaned_data["reference"].strip().upper()
        if Order.objects.filter(reference=value).exists():
            raise forms.ValidationError("That reference is already used.")
        return value                        # always return the value
```

In the view:

```python
form = OrderForm(request.POST, customer=request.user.customer)
if form.is_valid():
    order = form.save(commit=False)
    order.customer = request.user.customer
    order.save()
    form.save_m2m()          # commit=False defers this; forgetting it drops every m2m
```

---

## URLs

```python
# orders/urls.py
app_name = "orders"

urlpatterns = [
    path("", views.order_list, name="list"),
    path("<int:pk>/", views.order_detail, name="detail"),
    path("<slug:slug>/", views.order_by_slug, name="by-slug"),
]
```

Every route named, `app_name` set so names are namespaced (`orders:detail`), and
`path` with a converter rather than `re_path` with a regex — `<int:pk>` hands the
view an actual `int`.

In templates it is `{% url 'orders:detail' pk=order.pk %}`, and in Python
`reverse("orders:detail", kwargs={"pk": pk})` — or better, `order.get_absolute_url()`.

*(5.2+)* `reverse()` takes `query=` and `fragment=`:
`reverse("orders:list", query={"status": "paid"})`.

---

## Templates

```django
{% extends "base.html" %}
{% load static %}

{% block content %}
  <form method="post" action="{% url 'orders:create' %}">
    {% csrf_token %}
    {{ form.as_div }}
    <button type="submit">Save</button>
  </form>

  {% for order in orders %}
    <a href="{{ order.get_absolute_url }}">{{ order.reference }}</a>
    <span>{{ order.item_count }}</span>       {# annotated in the view, not counted here #}
  {% empty %}
    <p>No orders yet.</p>
  {% endfor %}

  {{ chart_data|json_script:"chart-data" }}
{% endblock %}
```

- `{% csrf_token %}` in every POST form.
- `{% static %}`, never `{{ STATIC_URL }}` — the tag knows about hashed filenames.
- `{% url %}`, never a literal path.
- `{% empty %}` rather than a separate `{% if %}`.
- `|json_script` to get data into JavaScript. Interpolating a variable directly
  into a `<script>` block is XSS: HTML escaping does not protect a JS string
  context.
- Nothing that queries. `{{ order.items.count }}` runs a query per row, after the
  view has returned, where it cannot be optimized.

*(6.0+)* Template partials replace the small-fragment-per-file habit:

```django
{% partialdef order-row %}
  <tr><td>{{ order.reference }}</td></tr>
{% endpartialdef %}

{% partial order-row %}
```

---

## Settings

Split by environment, share a base:

```
config/settings/
    __init__.py
    base.py          # everything common
    dev.py           # from .base import *  — DEBUG = True lives here
    production.py    # from .base import *  — and the real database
```

```python
# base.py
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]        # fail loudly if unset
DEBUG = False                                        # the safe default
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "").split(",")

AUTH_USER_MODEL = "accounts.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"   # implied from 6.0
USE_TZ = True                                          # the default from 5.0

STORAGES = {                                           # 4.2+; the old settings are gone
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# production.py
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CSP = {"default-src": ["'self'"]}               # 6.0+
```

`SECRET_KEY = os.environ["..."]` rather than `.get(...)` with a fallback: a
deployment missing its secret should fail at startup, not run with a default key
that is in the repository.

---

## Where the logic goes

**Behavior about one record → a model method. Behavior about a process → a plain
function.**

```python
class Order(models.Model):
    def mark_paid(self, at):                    # about this record
        self.paid_at = at
        self.status = self.Status.PAID
        self.save(update_fields=["paid_at", "status"])


def checkout(cart, payment_method):             # about a process
    with transaction.atomic():
        order = Order.from_cart(cart)
        charge = payments.charge(payment_method, order.total)
        order.mark_paid(charge.completed_at)
        transaction.on_commit(lambda: send_receipt.delay(order.pk))
    return order
```

A **function**, not a `CheckoutService` class. A class with one public method and
no state is a function wearing a costume.

Two things in `checkout` are load-bearing:

- The payment call is inside `atomic()` here only because the example is short.
  In real code, move a slow external call outside the block — the transaction
  holds its locks for the gateway's whole timeout.
- `transaction.on_commit` around the task. Enqueue directly and the worker can
  start before the commit, then query for a row that does not exist yet.

**Signals:** use them across apps that must not import each other. Inside one
app, they are a method call you have made untraceable. And note the asymmetry
that catches people — `post_save` does not fire for `bulk_create`.

---

## Background tasks

```python
@shared_task(bind=True, max_retries=3)
def send_receipt(self, order_id):          # a pk, never the instance
    order = Order.objects.get(pk=order_id)
    ...
```

Pass the primary key. An instance is serialised whole, so the worker operates on
a snapshot taken at enqueue time — and the task is not safely retryable.

*(6.0+)* Django ships its own task framework, which is worth preferring for
simple cases over adding Celery:

```python
from django.tasks import task

@task
def send_receipt(order_id): ...

send_receipt.enqueue(order.pk)
```

---

## Async

Async is worth it where a view **waits on the network**. It is not worth it for a
database-bound view, and a half-async project is harder to reason about than
either whole.

```python
async def dashboard(request):
    order = await Order.objects.aget(pk=1)      # the a* twin, awaited
    async for item in order.items.all():
        ...
```

Every sync ORM method has an `a`-prefixed twin. Calling the sync one inside
`async def` raises `SynchronousOnlyOperation`; calling the async one without
`await` does nothing at all and raises nothing.

For sync code with no twin: `await sync_to_async(fn, thread_sensitive=True)()`.
`thread_sensitive=True` keeps it on the connection-owning thread, which is what
lets it see the caller's transaction.

---

## Tests

```python
class OrderViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user("alice")
        cls.order = Order.objects.create(customer=cls.alice.customer, total=10)

    def test_list_is_two_queries(self):
        self.client.force_login(self.alice)     # not .login() — that hashes a password
        with self.assertNumQueries(2):          # 1 page + 1 prefetch
            self.client.get(reverse("orders:list"))

    def test_another_users_order_is_404(self):
        self.client.force_login(self.mallory)
        response = self.client.get(reverse("orders:detail", kwargs={"pk": self.order.pk}))
        self.assertEqual(response.status_code, 404)
```

Three habits:

- **`assertNumQueries` on anything that lists.** Query count is behaviour in
  Django and nothing else in the suite notices when it changes.
- **Test the unauthorized path.** A test that only exercises the owner cannot
  tell a working ownership filter from a missing one.
- **At least two rows** in any test meant to catch an N+1. One row hides it.

`setUpTestData` gives each test method a deepcopy — mutate one and the database
still holds the original, so `save()` when the change is meant to be visible.
And `TestCase` rolls back, so `transaction.on_commit` callbacks never run: use
`captureOnCommitCallbacks(execute=True)`, or `TransactionTestCase`.

---

## Further reading

The four books this skill drew on, by topic. Where a book and the current
release notes disagree, **the release notes win** — these span 2020 to 2025.

| Topic | Where |
|---|---|
| A full project end to end, on Django 5 | Mele, *Django 5 by Example* — blog (ch. 1–3), auth and profiles (4–5), shop and Celery (8–10), i18n (11), DRF (16) |
| Recipes for specific problems | Bendoraitis, *Django Cookbook* — templates, forms, admin customisation, deployment |
| The framework's own reasoning | Holovaty & Kaplan-Moss, *The Definitive Guide to Django* — dated on APIs, still the clearest account of *why* the ORM and template layers are shaped as they are |
| Working through the layers | Shaw, *Web Development with Django* |

Authoritative for anything version-specific:
`docs.djangoproject.com/en/stable/releases/` and `/internals/deprecation/`.
