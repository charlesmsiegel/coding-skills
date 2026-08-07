# Upgrading Django, and modernizing the idioms on the way

Two jobs that people run together and should not: **moving versions** (which must
not change behaviour) and **modernizing idioms** (which changes code but not
versions). Doing both in one commit produces a diff nobody can review and nobody
can bisect.

---

## Where things stand (August 2026)

| Version | Released | Security support |
|---|---|---|
| 4.2 LTS | Apr 2023 | **ended April 2026** |
| 5.0, 5.1 | Dec 2023, Aug 2024 | ended |
| 5.2 LTS | Apr 2025 | to April 2028 |
| 6.0 | Dec 2025 | to April 2027 |
| 6.1 | Aug 2026 | to December 2027 |
| 6.2 LTS | Apr 2027 | the next long-term target |

**Every Django 4 project is unsupported.** That is not a style finding — an
unpatched framework is a likelier way in than anything else this skill reports,
and it subsumes the rest of the report.

Two sensible destinations:

- **5.2 LTS** — the conservative landing spot. Supported to 2028, and the jump
  from 4.2 is two feature releases rather than four.
- **6.1** — current. Choose this if the project is actively developed and you
  would rather move often in small steps than rarely in large ones.

Do not aim at 6.0: its mainstream support already ended, so it is a stop on the
way to 6.1 rather than a destination.

---

## The campaign

### 1. Find out where you are, honestly

```bash
python "$SKILL/scripts/find_version_issues.py" . --list-known
python "$SKILL/scripts/find_version_issues.py" . --target 5.2
```

The detector reads the pin from `pyproject.toml`/`requirements*.txt` and reports
**where it read it from**. If it says the version is unknown, fix that first —
every version-conditional finding below rests on it.

**If the target is newer than the table knows** (it says so on stderr), read the
release notes for the gap:
`docs.djangoproject.com/en/N/releases/` and `/internals/deprecation/`.

### 2. Build the net before touching anything

Django code is coupled to the database, and an upgrade changes the framework
underneath every query. The suite is the only thing that will tell you.

```bash
pytest                                    # green before you start, or stop here
python "$SKILL/scripts/run_external_tools.py" . --run-migrations-check
```

If the suite is thin, `references/django-safety-net.md` first. An upgrade with no
tests is not an upgrade, it is a deployment with extra steps.

### 3. Let the deprecation warnings tell you what to fix

This is the highest-value step and the one people skip. Django's warnings name
the replacement, and they are specific to the code you actually run:

```bash
python -W error::DeprecationWarning -m pytest
python -W error::PendingDeprecationWarning -m pytest
python manage.py check
```

Turning them into errors makes the suite fail on each one, which turns the
upgrade into a list of failing tests rather than an audit.

### 4. One feature release at a time

4.2 → 5.0 → 5.1 → 5.2. Not 4.2 → 5.2.

The deprecation shims exist precisely so this works: something removed in 5.1 was
deprecated in 4.2, so on 4.2 you get a warning and on 5.1 you get an error.
Skipping the middle version skips the warning that would have told you.

At each step:

```bash
pip install "Django~=5.0.0" && pytest
python "$SKILL/scripts/find_version_issues.py" . --target 5.0
```

Commit each version bump on its own.

### 5. Apply the mechanical rewrites with the tool built for it

`django-upgrade` (Adam Johnson) does the unambiguous rewrites. This skill
*reports*; that tool *applies*:

```bash
pip install django-upgrade
django-upgrade --target-version 5.2 $(git ls-files '*.py')
git diff                              # read it — it is a codemod, not a proof
```

Or through this skill, which passes the detected version:

```bash
python "$SKILL/scripts/run_external_tools.py" . --tools django-upgrade --fix
```

Commit that separately from the version bump. It is a large diff and it should be
reviewable as "mechanical rewrites, no behaviour change".

### 6. Read every generated migration

```bash
python manage.py makemigrations --dry-run --verbose
```

Several of the changes below should produce **no migration**. If one appears,
something changed that you did not intend — most often a field default or a
`choices` value that is not identical to what was there.

### 7. Check the deployment settings

```bash
python manage.py check --deploy
```

Against the *production* settings module, not `dev.py`.

---

## What changes, by version

### Removed in 5.0 (deprecated in 4.0/4.1)

| Gone | Replacement | Note |
|---|---|---|
| `USE_L10N` | delete it | Localization is always on |
| `USE_TZ` defaulting to `False` | now defaults to `True` | **Behaviour change.** A project relying on the old default gets aware datetimes |
| `pytz`, `USE_DEPRECATED_PYTZ` | `zoneinfo` | `pytz.localize()` code needs rewriting, not just re-importing |
| `is_dst=` on `make_aware`, `Trunc*` | drop it | Ambiguous times resolve by `fold=` |
| `django.utils.timezone.utc` | `datetime.timezone.utc` | |
| `PickleSerializer` for sessions | `JSONSerializer` | Pickled sessions execute code if the signing key leaks — a security fix |
| `CSRF_COOKIE_MASKED` | delete it | |
| `django.utils.baseconv`, `datetime_safe` | stdlib | |

Also 5.0: form rendering became div-based by default, which **changes rendered
HTML**. If templates or CSS depend on the old table/paragraph output, that is a
visual regression the test suite will not catch.

### Removed in 5.1 (deprecated in 4.2)

| Gone | Replacement |
|---|---|
| `Meta.index_together` | `Meta.indexes = [models.Index(fields=[...])]` |
| `DEFAULT_FILE_STORAGE`, `STATICFILES_STORAGE` | the `STORAGES` dict |
| `get_storage_class()` | `django.core.files.storage.storages["default"]` |
| `length_is` template filter | `{% if value|length == 4 %}` |
| `BaseUserManager.make_random_password()` | `secrets` / `get_random_string()` |
| `CICharField`, `CIEmailField`, `CITextField` | `db_collation=` |
| `SHA1PasswordHasher`, `UnsaltedMD5PasswordHasher`, … | PBKDF2 or Argon2 |
| `assertFormsetError`, `assertQuerysetEqual` | capital-S spellings |

**The password hasher removal needs a plan.** Dropping a hasher from
`PASSWORD_HASHERS` locks out every user whose password is still stored under it.
Either keep the hasher until they have all logged in once (Django upgrades the
hash on login), or force a reset. Removing it in the same deploy as the upgrade
is how a release turns into an incident.

`index_together` → `indexes` generates a `RenameIndex` operation for indexes that
already exist. Read it; it should rename, not drop and recreate.

### Removed in 6.0 (deprecated in 5.0/5.1)

| Gone | Replacement |
|---|---|
| `CheckConstraint(check=...)` | `CheckConstraint(condition=...)` |
| `ChoicesMeta` | `ChoicesType` |
| `DjangoDivFormRenderer`, `Jinja2DivFormRenderer` | `DjangoTemplates` — div is the default |
| `get_prefetch_queryset()` | `get_prefetch_querysets()` |
| positional args to `Model.save()` | keyword-only |
| `FORMS_URLFIELD_ASSUME_HTTPS` | delete it — https is the default now |
| `itercompat.is_iterable()` | `isinstance(x, collections.abc.Iterable)` |
| `cx_Oracle` | `oracledb` |

Also 6.0:

- **Python 3.12 is the floor.** This is often the real blocker: the Python
  upgrade has to happen first, and it has its own dependency fallout.
- `DEFAULT_AUTO_FIELD` now defaults to `BigAutoField`. Projects that relied on
  the old `AutoField` default must set it explicitly.
- Custom ORM expressions: `as_sql()` must return params as a **tuple**, not a
  list. This one is silent until it isn't.
- `Field.pre_save()` may be called more than once, so it has to be idempotent.

### Removed in 6.1 (deprecated in 5.2)

| Gone | Replacement |
|---|---|
| `staticfiles.finders.find(all=...)` | `find_all=` |
| `ordering=` on `ArrayAgg`/`StringAgg`/`JSONBAgg` | `order_by=` |
| `RemoteUserMiddleware` overriding only `process_request()` | add `aprocess_request()` |

### Deprecated now, gone in 7.0

Worth fixing while you are in the file:

- **The whole `EMAIL_*` family** moves to `MAILERS`, the way `DATABASES` and
  `STORAGES` already work. `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, and the
  rest are all on the clock.
- `select_related()` with **no arguments** — it joins every non-null FK, which is
  rarely what anyone meant.
- `values_list(flat=True)` with no field named.
- `transaction.savepoint()` → `savepoint_create()`.
- `ADMINS`/`MANAGERS` as `(name, address)` tuples → plain address strings.
- `urlize` defaulting to `http` → will assume `https`.

---

## New capabilities worth adopting after the upgrade

Not required — but these are why the upgrade is worth more than "staying
supported".

**5.0:** `db_default=` (a default the *database* applies, so bulk and raw inserts
get it too), `GeneratedField`, `choices=SomeTextChoices` without `.choices`, and
the async auth surface.

**5.1:** `LoginRequiredMiddleware` — flips authentication from opt-in to opt-out
across the whole site, with `@login_not_required` for the exceptions. That is a
structurally better default than remembering `@login_required` on every view.
Also `{% querystring %}`, which deletes a lot of pagination template code.

**5.2:** `CompositePrimaryKey`, automatic model imports in `shell`, and `query=`/
`fragment=` on `reverse()`.

**6.0:** built-in **Content-Security-Policy** (`SECURE_CSP`,
`ContentSecurityPolicyMiddleware`) — the control that turns a surviving XSS into
a blocked script, and previously a third-party package. Also template partials
and the built-in **background task framework**, which is worth preferring over
adding Celery for simple cases.

**6.1:** `QuerySet.fetch_mode()` with `FETCH_PEERS` (fetches a deferred field for
every instance in the queryset rather than one at a time — an N+1 fix at the ORM
level) and `FETCH_RAISE` (turns an accidental query into an exception, which is a
very good thing to turn on in tests). Also `on_delete=DB_CASCADE`, which pushes
the cascade into the database.

---

## Modernizing idioms — separate commits, any time

These are not tied to a version bump. Do them when you are already in the file.

| Dated | Current |
|---|---|
| `url(r"^orders/(?P<pk>\d+)/$", ...)` | `path("orders/<int:pk>/", ...)` |
| `STATUS_CHOICES = [("new", "New")]` | `class Status(models.TextChoices)` |
| `null=True` on `CharField`/`TextField` | `blank=True, default=""` |
| `unique_together` | `Meta.constraints = [UniqueConstraint(...)]` — takes a condition |
| `os.path.join(BASE_DIR, ...)` | `BASE_DIR / ...` |
| validation only in `clean()` | `Meta.constraints` — the database enforces it |
| a `get_absolute_url` built by string formatting | `reverse()` |
| a one-method custom `Manager` | `QuerySet` + `.as_manager()` |
| `ugettext`, `ugettext_lazy` | `gettext`, `gettext_lazy` |

The Python-level half — f-strings, builtin generics, `pathlib` — is out of scope
here. If **python-code-doctor** is installed, its typing-and-modernization guide
covers it.

---

## The order that works

1. Suite green, on the current version.
2. `-W error::DeprecationWarning` — fix what it names.
3. Bump one feature release. Suite green. **Commit.**
4. `django-upgrade --target-version N`. Read the diff. **Commit separately.**
5. `makemigrations --dry-run` — read every migration, expect most to be empty.
6. Repeat 2–5 for the next release.
7. `check --deploy` against production settings.
8. Only then, adopt the new features.

Each numbered step is a commit that can be reverted on its own. That is the whole
point: when something breaks in production three weeks later, you want to be able
to bisect to "the 5.1 bump" rather than to "the upgrade".
