# Modernizing dated Django

Django deprecates on a published schedule and removes things two LTS releases
later, so dated idioms are not merely unfashionable — they are a future upgrade
that is already overdue.

**Check the version first.** `django.VERSION`, or `pip show django`. Half of what
follows does not apply to a project on 3.2, and a project on an end-of-life
release has a bigger problem than any of it.

## Routing

```python
# Dated: regex for something that is not a pattern
url(r"^orders/(?P<pk>\d+)/$", views.detail, name="order-detail")

# Current: converters, typed and readable
path("orders/<int:pk>/", views.detail, name="order-detail")

# re_path only where a real regex is needed
re_path(r"^files/(?P<path>.+)$", views.serve, name="file")
```

`django.conf.urls.url` is gone as of Django 4.0. Note that `path` converts
`<int:pk>` to an actual `int`, where the regex gave you a string — a view that
compared it to an integer was already subtly wrong.

## Choices

```python
# Dated: values are bare strings everywhere they are compared
STATUS_CHOICES = [("new", "New"), ("done", "Done")]
status = models.CharField(max_length=20, choices=STATUS_CHOICES)
if order.status == "done":       # a typo here is silent

# Current: named, typed, and greppable
class Status(models.TextChoices):
    NEW = "new", "New"
    DONE = "done", "Done"

status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
if order.status == Status.DONE:
```

`TextChoices`/`IntegerChoices` also give you `get_status_display()`, `.label`, and
membership tests. Changing to them is a code change, not a schema change, as long
as the stored values are identical — check the generated migration says nothing.

## Model and Meta

| Dated | Current |
|---|---|
| `index_together` | `Meta.indexes = [models.Index(fields=[...])]` (removed in 5.1) |
| `unique_together` | `Meta.constraints = [models.UniqueConstraint(...)]` — supports conditions |
| `null=True` on CharField/TextField | `blank=True, default=""` |
| Implicit `id` | `DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"` |
| `JSONField` from a third-party package | `django.db.models.JSONField` |
| A `get_absolute_url` built by string formatting | `reverse()` |
| Validation only in `clean()` | `Meta.constraints` — enforced by the database, not just the form |

`UniqueConstraint` with `condition=` is the one worth knowing: "unique among
non-deleted rows" is expressible now and was not.

## Settings

| Dated | Current |
|---|---|
| `USE_L10N` | Removed in Django 5.0; localization is always on |
| `DEFAULT_FILE_STORAGE`, `STATICFILES_STORAGE` | The `STORAGES` dict (Django 4.2+) |
| `os.path.join(BASE_DIR, ...)` | `BASE_DIR / ...` — `BASE_DIR` is a `Path` |
| `django.utils.timezone.utc` | `datetime.timezone.utc` |
| `ugettext`, `ugettext_lazy` | `gettext`, `gettext_lazy` |
| `providing_args` on a Signal | Removed; document the arguments instead |

## Async

Django has been async-capable since 3.1, and the surface keeps growing: async
views, async ORM methods (`aget`, `acreate`, `afilter`… since 4.1), async
middleware, async signals.

This is an opportunity, not an obligation. Async is worth it where the view spends
its time waiting on the network. It is not worth it for a database-bound view —
and mixing sync ORM calls into an async view raises
`SynchronousOnlyOperation`, or silently blocks the event loop if wrapped in
`sync_to_async` without thought.

If the project is entirely sync and performs fine, leave it. A half-async Django
project is harder to reason about than either whole.

## How to do the upgrade

1. **Find what is already deprecated**, rather than reading the whole release notes:
   ```bash
   python -W error::DeprecationWarning -m pytest
   python manage.py check
   ```
   Django's deprecation warnings name the replacement.
2. **Read the release notes between your version and the target** — one minor
   version at a time. Skipping versions hides the intermediate deprecation that
   would have told you what broke.
3. **Upgrade one minor version at a time**, running the suite each step. The
   deprecation shims exist precisely so this works.
4. **Do the idiom changes separately from the version bump.** A commit that both
   upgrades Django and rewrites every `url()` is unreviewable and unbisectable.
5. **Read every generated migration.** Several of the changes above should produce
   *no* migration; if one appears, something changed that you did not intend.

`python-simplifier`'s `references/typing-and-modernization.md` covers the
Python-level half of the same job — f-strings, builtin generics, `pathlib`.
