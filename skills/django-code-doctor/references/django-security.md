# Django security review

`find_django_security.py` reads settings, `mark_safe` calls, and template
escaping; `find_view_issues.py` flags the ownership gap, CSRF exemptions, and
open redirects; `find_drf_issues.py` covers the API surface and
`find_admin_issues.py` the admin. Everything below needs a reader.

Run Django's own checker first — it knows about deployment settings this skill
does not: `python manage.py check --deploy`.

## Object-level authorization: the big one

Django authenticates. It does **not** authorize per object, and nothing in the
framework will remind you.

```python
# Authenticated — and hands over any user's order.
@login_required
def order_detail(request, pk):
    order = get_object_or_404(Order, pk=pk)

# Scoped. The filter and the fetch are one query, so there is no gap.
@login_required
def order_detail(request, pk):
    order = get_object_or_404(Order, pk=pk, customer=request.user.customer)
```

This is the most common real vulnerability in Django applications, and it is
invisible in review because the code *looks* protected — there is a decorator
right there.

Where to check:

- Every view taking `pk`/`id`/`uuid`/`slug` from the URL.
- `get_queryset()` in every CBV and DRF ViewSet — this is the right place to
  scope, and its absence is the bug.
- DRF: `permission_classes` covers the endpoint; **`has_object_permission` covers
  the row**, and only runs if `get_object()` is called.
- Nested resources: `/customers/3/orders/9/` must check that order 9 belongs to
  customer 3 *and* that the requester may see customer 3.
- Anything taking an ID in a POST body rather than the URL. Same problem, less
  visible.

The detector's false-negative case: authorization enforced through a base class,
mixin, or overridden `get_queryset`. It only reads the function body. So a clean
report here means "nothing obvious", not "checked".

## Escaping and mark_safe

Django templates autoescape by default; the vulnerabilities come from turning it
off.

```python
mark_safe(f"<b>{name}</b>")                  # XSS if name is user-controlled
format_html("<b>{}</b>", name)               # escapes the argument, trusts the template
format_html_join(", ", "<b>{}</b>", pairs)   # the same for sequences
```

Also disable escaping: `{{ value|safe }}`, `{% autoescape off %}`, and
`json_script`'s alternatives (use `{{ data|json_script:"id" }}`, which is safe).

`mark_safe` on a **literal** is fine and the detector allows it. On anything
computed it is a finding, because the taint may arrive from three calls away.

Beyond templates: a `URLField` or user-supplied string rendered into an `href` can
carry `javascript:`. Validate the scheme, don't just escape.

## Settings

Beyond what the detector reports:

| Setting | Why |
|---|---|
| `SECURE_HSTS_SECONDS` | Unset means no HSTS. Set it once TLS is stable — it is hard to undo |
| `SECURE_SSL_REDIRECT` | Should be True behind TLS |
| `SESSION_COOKIE_HTTPONLY` | Default True; check nothing turned it off |
| `CSRF_TRUSTED_ORIGINS` | Required for cross-origin POSTs since Django 4 |
| `SECURE_PROXY_SSL_HEADER` | Only set it if a proxy actually strips and sets the header — otherwise it is spoofable |
| `X_FRAME_OPTIONS` | Defaults to DENY; loosening it enables clickjacking |
| `FILE_UPLOAD_PERMISSIONS` | A too-permissive mode on uploads |
| `DEFAULT_AUTO_FIELD` | Not security, but its absence dates the project |

Check the *production* settings module, not `settings.py` in development. Split
settings files mean the detector may be reading the wrong one — say so if the
project has `settings/prod.py`.

## The ORM and SQL injection

The ORM parameterizes, so the injection paths are the escapes from it:

- `.raw()` and `cursor.execute()` with a formatted string instead of `params=`.
- `.extra(where=[...])` — no parameterization at all. Never use it.
- `RawSQL()` inside an annotate or filter.
- `order_by(user_input)` — not injection, but a user-chosen field name can expose
  ordering across a relation, and an invalid one raises.

`filter(**user_supplied_dict)` deserves a look: a user who controls the key can
traverse relations (`customer__user__password`) or filter on fields you did not
intend to expose. Allow-list the keys.

## File uploads

- Validate content, not the extension. `ImageField` verifies it is an image;
  `FileField` verifies nothing.
- Never serve user uploads from the same origin as the app if they can be HTML.
- `MEDIA_ROOT` must not be inside a directory served as static.
- A user-controlled filename reaching `os.path.join` is path traversal; Django's
  storage sanitizes, but custom `upload_to` callables often do not.

## The admin

The admin is a full CRUD interface over the database, guarded by one flag.

- `is_staff` gets you in; per-model permissions decide the rest. Custom
  `ModelAdmin` methods and actions bypass those unless they check.
- `list_display` and `readonly_fields` accept callables that render whatever they
  return — with `mark_safe`, that is XSS in the admin.
- `raw_id_fields`/autocomplete endpoints leak the existence and labels of objects
  the user may not otherwise see.
- Consider a non-default admin URL and a second authentication factor. Neither is
  a real control on its own; both cut noise.

## Dependencies

Django and its ecosystem publish security releases regularly, and an unpatched
Django is a more likely route in than anything above.

```bash
python -m pip install pip-audit && pip-audit -r requirements.txt
```

Check the Django version against the supported-release list — `find_version_issues.py`
does this and reports `django_end_of_life`. **As of August 2026 every Django 4
release is end of life**, as are 5.0 and 5.1. A project on one receives no
security fixes at all, which subsumes every other finding in this file. See
`django-upgrade-runbook.md`.

Django 6.0 added first-class Content-Security-Policy support (`SECURE_CSP`,
`ContentSecurityPolicyMiddleware`). A CSP is the control that turns a surviving
XSS into a blocked script rather than a compromise — worth adopting on any
project already on 6.0, starting with `SECURE_CSP_REPORT_ONLY` to find what
breaks.
