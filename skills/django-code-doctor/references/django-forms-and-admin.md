# Forms and the admin

Two layers people treat as plumbing, both of which are actually security
boundaries: a form decides what a user may write, and the admin is a full CRUD
interface over the database behind a single boolean.

---

## Forms

### What the form accepts is the whole point

```python
class OrderForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ["reference", "notes"]      # named, always
```

`fields = "__all__"` means every column is writable from the web — including the
`is_approved` someone adds next year, because nobody revisits this form when
adding a model field.

`exclude` is the same failure one step removed: it opts out of the columns you
thought of and opts in to everything else. Use `fields`. Then adding a model
field is a *decision* about this form rather than a silent change to it.

The fields a user must never set do not belong in the form at all:

```python
order = form.save(commit=False)
order.customer = request.user.customer     # from the request, not from the POST
order.save()
form.save_m2m()
```

### `commit=False` and the m2m write

`save(commit=False)` defers the write, and Django puts the many-to-many part
behind `save_m2m()`. Forget it and every tag, category, and permission the user
selected is dropped — silently, because the object itself saves fine.

If you are not setting a field, do not use `commit=False`. `form.save()` handles
the m2m itself.

### Validation

```python
def clean_reference(self):
    value = self.cleaned_data["reference"].strip().upper()
    if not value.startswith("ORD-"):
        raise forms.ValidationError("References start with ORD-.")
    return value                     # a method that falls off the end saves None

def clean(self):
    cleaned = super().clean()        # cross-field rules go here
    if cleaned.get("ends_at") <= cleaned.get("starts_at"):
        raise forms.ValidationError("ends_at must be after starts_at.")
    return cleaned
```

Three rules:

- **`clean_<field>` must return the value.** Falling off the end returns `None`,
  which Django then stores. Validation passes and the data is wiped.
- **`cleaned_data` does not exist until `is_valid()` has run.** Reading it first
  gets `AttributeError` or a stale dict.
- **Don't query in `clean()` for something the database can enforce.** A
  uniqueness check in `clean()` is a race — two requests both pass it. Add a
  `UniqueConstraint` and let the database be the authority; the form check is
  then only for the error message.

### Querysets belong in `__init__`, not the class body

```python
# WRONG — evaluated once, at import
class OrderForm(forms.Form):
    address = forms.ModelChoiceField(queryset=Address.objects.filter(active=True))

# RIGHT — per instantiation, and it can depend on the request
class OrderForm(forms.Form):
    address = forms.ModelChoiceField(queryset=Address.objects.none())

    def __init__(self, *args, customer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["address"].queryset = customer.addresses.filter(active=True)
```

A class body runs once, when the module is imported. A queryset built there is
built with the process — so rows created afterwards never appear until the worker
restarts. It is also how a form ends up offering every customer's addresses to
every customer.

### Rendering

`{{ form.as_div }}` since 4.1, and the div renderer is the only one from 6.0.
*(5.0+)* `{{ field.as_field_group }}` renders label, widget, help text, and errors
together, which is what most custom rendering loops were reimplementing.

---

## The admin

The admin is a full CRUD interface over your database, guarded by `is_staff`. It
gets the least review of anything in a Django project and the most privilege.

### The changelist N+1

```python
@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ["reference", "customer", "total"]
    list_select_related = ["customer"]        # without this, one query per row
    search_fields = ["reference", "customer__name"]
    autocomplete_fields = ["customer"]
    list_filter = ["status", ("created_at", admin.DateFieldListFilter)]
```

`list_display` is evaluated once per row, and the changelist renders 100 rows per
page by default. A relation in `list_display` with no `list_select_related` is
therefore 100 extra queries per page view. It is the most common performance
problem in Django admins and it never shows up in a load test, because nobody
load-tests the admin.

For anything more complex, override the queryset:

```python
def get_queryset(self, request):
    return super().get_queryset(request).select_related("customer").annotate(
        item_count=Count("items"))
```

**Start from `super()`.** A `get_queryset` that builds its own discards the
admin's ordering and any scoping a parent class set up.

### The other two performance traps

- **`list_filter` on a high-cardinality column** renders one sidebar link per
  distinct value. On a `CharField` over a large table that is the whole table, in
  a sidebar. Filter on a status, a boolean, or a FK with few rows; use
  `SimpleListFilter` for fixed buckets.
- **A `ForeignKey` with the default widget** renders a `<select>` containing every
  row of the related table. Fine at a hundred rows, a timeout at a million. Use
  `autocomplete_fields` (needs `search_fields` on the *related* admin) or
  `raw_id_fields`.

### Admin actions are a permission gap

```python
@admin.action(description="Approve selected orders")
def approve(modeladmin, request, queryset):
    if not request.user.has_perm("shop.change_order"):
        raise PermissionDenied
    queryset.update(status=Order.Status.APPROVED)
```

Django checks the model permission for *viewing the changelist*. It does not
check anything for what a custom action does. Without an explicit check, any
staff user who can see the list can run every action on it.

Two more things about that action: `queryset.update()` skips `save()`, signals,
and `auto_now`, so if approval has side effects they do not happen. And it
operates on whatever the user ticked, which may include rows a scoped
`get_queryset` would have hidden — check both.

### `mark_safe` in the admin is XSS at maximum privilege

```python
# WRONG — executes in a session that can edit anything
@admin.display(description="Badge")
def badge(self, obj):
    return mark_safe("<b>" + obj.reference + "</b>")

# RIGHT — escapes the argument, trusts only the template string
@admin.display(description="Badge")
def badge(self, obj):
    return format_html("<b>{}</b>", obj.reference)
```

The admin is the highest-privilege browser context in the project. A stored XSS
that fires there is a full compromise, not a defacement.

### Scoping the admin per user

`get_queryset` is also where multi-tenant scoping goes:

```python
def get_queryset(self, request):
    qs = super().get_queryset(request)
    if request.user.is_superuser:
        return qs
    return qs.filter(tenant=request.user.tenant)
```

But note what this does *not* cover: `raw_id_fields` and autocomplete endpoints
have their own views, and they will happily reveal the existence and labels of
objects this filter hides. If the admin is exposed to anyone who is not fully
trusted, that is a real leak.

### When not to use the admin

The admin is a database editor with a nice skin. It is excellent for developers
and operations staff, and a poor fit for a business workflow — because it has no
concept of one. If you find yourself adding custom views, custom templates, and
permission logic to a `ModelAdmin`, you are building an application inside a tool
that was built for something else. Build the application.

---

## Review checklist

**Forms**
- [ ] `fields` named; no `"__all__"`, no `exclude`.
- [ ] Fields the user must not set are absent from the form and assigned in the view.
- [ ] Every `clean_<field>` returns a value.
- [ ] `cleaned_data` only read after `is_valid()`.
- [ ] `save_m2m()` after every `save(commit=False)`.
- [ ] Querysets assigned in `__init__`, not the class body.
- [ ] Uniqueness enforced by a constraint, not only by `clean()`.

**Admin**
- [ ] `list_select_related` (or a `get_queryset` override) wherever `list_display` walks a relation.
- [ ] `search_fields` set; `autocomplete_fields`/`raw_id_fields` on FKs to large tables.
- [ ] `list_filter` only on low-cardinality columns.
- [ ] Every custom action checks a permission.
- [ ] `format_html`, never `mark_safe`, in display callables.
- [ ] `get_queryset` starts from `super()`.
