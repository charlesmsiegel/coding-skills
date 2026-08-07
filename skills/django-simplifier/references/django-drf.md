# Reviewing a DRF API

Two things go wrong in Django REST Framework APIs, repeatedly, and both are
invisible in review because the code looks configured.

**Permissions default open.** With no `DEFAULT_PERMISSION_CLASSES`, DRF falls
back to `AllowAny`. A viewset with no `permission_classes` is public — and it
looks exactly like one whose permissions are handled somewhere else.

**Serializers hide queries.** Every field is evaluated per object, so a
`SerializerMethodField` that queries costs one round trip per row of every list
response, and nothing in the code reads as a loop.

---

## Endpoint permission vs. row permission

These are different questions and DRF answers them in different places.

```python
class OrderViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]     # may you call this endpoint?
    serializer_class = OrderSerializer

    def get_queryset(self):                    # which rows exist, for you?
        return Order.objects.filter(customer=self.request.user.customer)
```

`permission_classes` guards the endpoint. **Only the queryset guards the row.**

A `ModelViewSet` with `queryset = Order.objects.all()` and `IsAuthenticated`
serves every order to every logged-in user, and hands over any id that is asked
for on the detail route. It is the DRF spelling of the
`missing_ownership_filter` finding, and it is the most common real vulnerability
in DRF code.

### `has_object_permission` covers less than people think

```python
class IsOwner(BasePermission):
    def has_object_permission(self, request, view, obj):
        return obj.customer == request.user.customer
```

This runs **only when `get_object()` is called** — so on detail, update, and
destroy. It does **not** run on list. An API that relies on it for row-level
access leaks the whole table through the list endpoint.

Scope in `get_queryset`. Use `has_object_permission` for rules a queryset filter
cannot express, not as the primary control.

### Set the default the safe way round

```python
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.UserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": "1000/day", "anon": "100/day"},
}
```

Then opt individual endpoints **out** with `permission_classes = [AllowAny]`. An
endpoint that is public by omission is a mistake; one that is public by
declaration is a decision, and it shows up in review.

---

## Serializers

### Name the fields

```python
class OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = ["id", "reference", "status", "total"]
        read_only_fields = ["id", "status"]
```

`fields = "__all__"` exposes every column, including the ones added next year.
An API response is a contract; `"__all__"` means the contract is whatever the
schema happens to be today.

`read_only_fields` matters as much: without it, a field that appears in `fields`
is **writable**, so a PATCH can set `status` directly and skip whatever
transition rules the application has.

### The N+1s

```python
# One query per row of every list response.
class OrderSerializer(serializers.ModelSerializer):
    item_count = serializers.SerializerMethodField()

    def get_item_count(self, obj):
        return obj.items.count()

# Counted in SQL, read off the instance.
class OrderViewSet(ModelViewSet):
    def get_queryset(self):
        return (Order.objects
                .filter(customer=self.request.user.customer)
                .annotate(item_count=Count("items")))

class OrderSerializer(serializers.ModelSerializer):
    item_count = serializers.IntegerField(read_only=True)
```

A nested serializer is the same problem:

```python
class OrderSerializer(serializers.ModelSerializer):
    items = ItemSerializer(many=True, read_only=True)   # N+1 without a prefetch

    # in the viewset:
    def get_queryset(self):
        return Order.objects.prefetch_related("items").select_related("customer")
```

**`depth = 1` does this to every relation at once.** It is a debugging
convenience, not an API design: it expands relations you did not choose, exposes
their fields wholesale, and prefetches none of them.

### Validation belongs on the serializer

```python
def validate_reference(self, value):
    if Order.objects.filter(reference=value).exists():
        raise serializers.ValidationError("Already used.")
    return value                     # always return it

def validate(self, attrs):           # cross-field rules
    if attrs["ends_at"] <= attrs["starts_at"]:
        raise serializers.ValidationError("ends_at must be after starts_at.")
    return attrs
```

A `validate_<field>` that falls off the end returns `None`, which is then what
gets saved. Same trap as a form's `clean_<field>`.

---

## Pagination

An unpaginated list endpoint works in development and times out in production,
because the response grows with the table. Set `DEFAULT_PAGINATION_CLASS`
project-wide.

For large offsets, `PageNumberPagination` gets slower the deeper you page —
`OFFSET 100000` makes the database walk 100,000 rows to discard them. Use
`CursorPagination` for anything that pages deeply; it needs a stable ordering,
which is another reason for `Meta.ordering` with a tie-breaker.

---

## Where DRF stops being worth it

DRF earns its keep for a real API: many endpoints, content negotiation,
browsable docs, consistent errors, schema generation.

It does not earn its keep for three JSON endpoints consumed by your own
front-end. There, a plain view returning `JsonResponse` is less machinery, and
`django-ninja` is a lighter fit if you want validation and schemas.

If the project has one `ModelViewSet` and a router, that is DRF being used as a
JSON serializer with a lot of ceremony attached.

---

## Review checklist

- [ ] `DEFAULT_PERMISSION_CLASSES` set to something closed, with explicit opt-outs.
- [ ] Every viewset scopes `get_queryset()` to the caller — including list.
- [ ] `fields` named explicitly; `read_only_fields` covers anything the client must not set.
- [ ] No `depth`. Nested serializers have a matching `prefetch_related`.
- [ ] No query inside a `SerializerMethodField`.
- [ ] Pagination configured; cursor pagination anywhere that pages deeply.
- [ ] Throttling on anything that sends email, costs money, or authenticates.
- [ ] `assertNumQueries` around the list endpoint, with at least two rows in the fixture.
- [ ] A test that another user's object returns 404, not 200 — on both list and detail.
