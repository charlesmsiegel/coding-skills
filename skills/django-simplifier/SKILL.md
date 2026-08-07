---
name: django-simplifier
description: Review, simplify, upgrade, and write idiomatic Django. Finds N+1 queries and per-row writes, model definition problems, missing object-level authorization, insecure settings, work hidden in templates, form and admin problems, DRF permission gaps, risky migrations, async and transaction-boundary bugs, Django-shaped test smells, and abstractions that never earned their keep. Version-aware across Django 4, 5, and 6 - it detects the project's version and reports what breaks on the way to a target, so it drives a 4-to-5, 4-to-6, or 5-to-6 upgrade. Use when the user asks to simplify, refactor, optimize, review, or upgrade Django code, or mentions models, QuerySets, the ORM, select_related, views, serializers, DRF, forms, admin, signals, middleware, migrations, Celery, or Django settings - and when writing new Django, for which references/django-idioms.md gives one canonical answer per task. For general Python analysis use python-simplifier.
---

# Django Code Simplifier

Django's defaults make several expensive mistakes easy and invisible: a loop that
issues a query per row reads exactly like a loop that doesn't, a template can hit
the database with nothing in the Python to show for it, and an endpoint that
checks you are logged in looks identical to one that checks you own the row. This
skill finds those mechanically, then gives you the judgment to decide what to do.

It does three jobs:

- **Review** — fifteen detectors over an existing project.
- **Upgrade** — what breaks between the Django this project is on and the one you
  want, with the severity derived from the target.
- **Author** — `references/django-idioms.md` is prescriptive: one canonical
  answer per task, version-tagged, for writing Django that does not need this
  skill's other half later.

Its scope is Django. Complexity, duplication, dead code, exception handling, and
resource leaks are not Django questions and this skill does not look for them —
if the **python-simplifier** skill is installed, run it too for that half; if it
is not, say so in the report rather than implying the code was reviewed for it.

## Deterministic detectors

Let `SKILL=/path/to/this/skill` — the directory holding this SKILL.md. Commands run
from the Django project being reviewed, not from the skill directory. Needs **Python
3.11+** and nothing else: the detectors parse source with the stdlib `ast` module and
never import the project or start Django, so no virtualenv, settings module, or
database is required.

```bash
python "$SKILL/scripts/analyze_django.py" /path/to/project      # everything, one report
python "$SKILL/scripts/analyze_django.py" . --format json > report.json
python "$SKILL/scripts/analyze_django.py" . --skip templates,overengineering
python "$SKILL/scripts/analyze_django.py" . --ignore no_default_ordering
python "$SKILL/scripts/analyze_django.py" . --target-version 5.2

# The database
python "$SKILL/scripts/find_query_issues.py" .        # N+1, per-row writes/reads, lost updates, raw SQL
python "$SKILL/scripts/find_model_issues.py" .        # __str__, null on text, on_delete, related_name, save(update_fields)
python "$SKILL/scripts/find_migration_issues.py" .    # historical models, reversibility, NOT NULL adds, leaf conflicts
python "$SKILL/scripts/find_transaction_issues.py" .  # external calls in atomic(), IntegrityError, select_for_update

# The request path
python "$SKILL/scripts/find_view_issues.py" .         # ownership filters, csrf_exempt, open redirects, fat views
python "$SKILL/scripts/find_form_issues.py" .         # fields='__all__', clean() returns, save_m2m, class-scope querysets
python "$SKILL/scripts/find_drf_issues.py" .          # default permissions, unscoped querysets, serializer N+1
python "$SKILL/scripts/find_admin_issues.py" .        # changelist N+1, action permissions, mark_safe in the admin
python "$SKILL/scripts/find_template_issues.py" .     # queries and relation walks, csrf_token, vars in <script>
python "$SKILL/scripts/find_async_issues.py" .        # sync ORM in async, unawaited a*, tasks and on_commit

# The project
python "$SKILL/scripts/find_django_security.py" .     # DEBUG, SECRET_KEY, headers, mark_safe, |safe
python "$SKILL/scripts/find_settings_issues.py" .     # AUTH_USER_MODEL, middleware order, prod backends
python "$SKILL/scripts/find_django_overengineering.py" .  # abstract models, managers, mixins, signals, services
python "$SKILL/scripts/find_test_issues.py" .         # assertNumQueries, on_commit under TestCase, setUpTestData
python "$SKILL/scripts/find_version_issues.py" .      # what breaks on the way to the target — see below
```

Every detector takes `--format text|json` and `--ignore type1,type2`, emits
🔴/🟡/🟢 severities, and produces the same flat findings shape as python-simplifier —
so any of them pipes straight into the bundled reporter:

```bash
python "$SKILL/scripts/analyze_django.py" . --format json \
  | python "$SKILL/scripts/format_findings.py" --format cards
```

`format_findings.py` is this skill's own copy, so the pipe works whether or not
python-simplifier is installed. `analyze_django.py` builds the project graph and
parses each file **once**, sharing both with all fifteen detectors — which is most
of the runtime on a large project.

### The three findings to read first

- **`missing_ownership_filter`** / **`unscoped_viewset_queryset`** — a view or a
  ViewSet looks up records without scoping them to `request.user`. This is the most
  common real vulnerability in Django application code: the endpoint checks that
  you are logged in and then hands you somebody else's row. Verify each by hand;
  the detectors cannot see authorization enforced through a custom mixin, a
  permission class, or a base class.
- **`django_end_of_life`** — the project is on a Django that receives no security
  fixes. As of August 2026 that is every Django 4 release, plus 5.0 and 5.1. This
  subsumes every other finding in the report.
- **`n_plus_one_query`** — the finding that turns into latency. Reported only when
  the loop iterates a queryset, the body reaches through a real relation on the
  loop variable, and nothing in the chain prefetches it.

### They stay quiet outside Django

Every detector is gated on the project actually being Django (a `manage.py`, or
`django` imports). On anything else they report nothing and **say so on stderr** —
so silence is never mistaken for a clean bill of health. `find_drf_issues.py` is
gated twice, on DRF imports as well.

## Upgrading Django 4 → 5 → 6

```bash
python "$SKILL/scripts/find_version_issues.py" .                 # vs. the current release
python "$SKILL/scripts/find_version_issues.py" . --target 5.2    # the LTS
python "$SKILL/scripts/find_version_issues.py" . --from 4.2 --target 6.1
python "$SKILL/scripts/find_version_issues.py" . --list-known    # what the table covers
```

The version is detected from `pyproject.toml` (PEP 621 and Poetry),
`requirements*.txt`, `setup.cfg`, or `Pipfile`, and the report **names the source**
— a manifest pin and whatever happens to be installed are different claims. When
no version can be determined, nothing version-conditional is reported and the
report says so, because a deprecation warning built on a guessed version is worse
than silence.

Severity is derived, not hand-assigned:

| Condition | | Meaning |
|---|---|---|
| Removed at or before the target | 🔴 | The project will not run on the target |
| Deprecated on the version in use | 🟡 | Works today, on a clock |
| Removal scheduled after the target | 🟢 | Fix it while you are here |

**The change table is a snapshot as of Django 6.1 (August 2026).** When the target
is newer than that, the detector says so on stderr — **web-search the release
notes for the gap** before reporting the check as complete:
`docs.djangoproject.com/en/N/releases/` and `/internals/deprecation/`.

Then follow `references/django-upgrade-runbook.md`. The two rules that matter most:
**one feature release at a time** (the deprecation shims exist so this works), and
**the version bump and the idiom rewrites are separate commits** — one diff that
does both is unreviewable and unbisectable.

## Writing new Django

Load `references/django-idioms.md` before writing models, views, forms, settings,
or tasks. It gives one canonical answer per task rather than surveying the
options, and tags anything that needs 5.0+, 5.2+, or 6.0+.

Two decisions it front-loads because they are expensive later: set
`AUTH_USER_MODEL` on day one, and put the ownership filter in the same query as
the fetch.

## Use the project's own tools when they exist

The detectors are stdlib-only on purpose, but three questions they structurally
**cannot** answer need a live Django:

```bash
python "$SKILL/scripts/run_external_tools.py" .                        # run what is installed
python "$SKILL/scripts/run_external_tools.py" . --run-migrations-check # imports the project
python "$SKILL/scripts/run_external_tools.py" . --tools django-upgrade --fix   # MUTATES
```

| Tool | Answers what parsing cannot |
|---|---|
| `makemigrations --check --dry-run` | Is any model change unmigrated? Only Django can compare against the migration graph — and a missing migration passes every test, then fails on deploy |
| `check --deploy` | What Django itself thinks of the **real** settings module, including environment overrides |
| `pip-audit` | Which advisories apply to the pinned Django. This skill can say a release is past end-of-life; only a database can say which CVEs that means |
| `django-upgrade` | The actual rewrites. This skill reports; that tool applies |

The script **never installs anything**: absent tools are listed under
`missing_tools` with a `pip install` hint. When that list is non-empty and the
tools would help, **ask the user whether to install them** and only install on
confirmation. `--fix` and `--run-migrations-check` are opt-in because they mutate
or execute.

## Reviewing a change (diff lens)

For an AI-written feature or a CR, review *what changed*, not the legacy around it:

```bash
python "$SKILL/scripts/analyze_diff.py"                  # vs. merge-base with the default branch
python "$SKILL/scripts/analyze_diff.py" origin/main --path .
```

The context is still built from the whole project — whether a loop is an N+1
depends on models in another file — and only the reporting is narrowed. The five
whole-tree categories it cannot run (over-engineering, settings, migrations, the
suite-wide test check, and the version sweep) are **named in the output**, so
silence about them does not read as a clean bill of health. Run `analyze_django.py`
separately for those.

## What the detectors cannot decide

Deterministic checks find the mechanical problems. Everything below is a judgment
call, and getting it wrong in either direction is expensive — load the guide.

| Load this when… | File |
|---|---|
| **Writing new Django** — one canonical answer per task, version-tagged for 4/5/6 | `references/django-idioms.md` |
| Running an upgrade — the campaign, what changed at each version, what to adopt after | `references/django-upgrade-runbook.md` |
| Deciding how to fix a query problem: select_related vs prefetch_related, what bulk operations skip, when raw SQL is right | `references/django-orm.md` |
| Deciding where logic belongs: fat model vs service layer, signals vs explicit calls, CBV vs FBV, app boundaries | `references/django-architecture.md` |
| Judging whether an abstraction earns its keep — managers, mixins, middleware, service layers | `references/django-overengineering.md` |
| Reviewing security beyond what settings show — object-level authorization, escaping, uploads, the admin | `references/django-security.md` |
| Writing or reviewing a migration, especially a data migration or a zero-downtime deploy | `references/django-migrations.md` |
| Reviewing a DRF API — endpoint vs row permissions, serializer N+1, pagination | `references/django-drf.md` |
| Judging form and admin design — both are security boundaries treated as plumbing | `references/django-forms-and-admin.md` |
| Async views, the `a*` ORM surface, Celery or `django.tasks`, and transaction boundaries | `references/django-async-and-tasks.md` |
| Building the safety net before refactoring — query counts, characterization tests, Django's test traps | `references/django-safety-net.md` |

## Workflow

1. **Run `analyze_django.py`.** Triage the mechanical findings before reading
   anything by hand.
2. **Check the version first.** `django_end_of_life` outranks everything else in
   the report; an unpatched framework is a likelier way in than any finding below it.
3. **Verify every 🔴 by hand.** The authorization findings have real
   false-positive modes — enforcement through a mixin, a permission class, or an
   overridden `get_queryset` is invisible to a parser.
4. **Measure the query findings before and after.** `django-debug-toolbar`,
   `assertNumQueries`, or `CaptureQueriesContext`. A prefetch that doesn't reduce
   the query count is a guess that happened to compile.
5. **Review the hot code with the guides open.** Effort follows churn, not line
   count — `git log --since="1 year ago" --name-only --pretty=format: | grep '\.py$'
   | sort | uniq -c | sort -rn | head -30`.
6. **Pin behavior before refactoring.** Django code is heavily coupled to the
   database; a test that exercises the real query is the only safety net that
   counts. See `references/django-safety-net.md`.
7. **Ratchet.** `assertNumQueries` around the view that had the N+1, and
   `makemigrations --check` in CI, are what stop both coming back.

## Output & ticketing

The deliverable is an **artifact, never a side effect**: a findings list, cards,
JSON, or the full report — returned inline or saved as a file.
`scripts/format_findings.py` renders any detector's JSON into those shapes.

This skill does **not** create tickets in any system on its own. When the user
wants findings filed, **ask which ticket software or MCP to use** and create them
through that tool — never assume or fabricate a tracker.

## Rules

- **Behavior is sacred**, and in Django that includes the *number of queries* and
  the *number of round trips*. A refactor that changes either has changed behavior
  under load even when every test passes.
- **Migrations are not refactorable.** Changing a model field changes the schema,
  and once a migration has run somewhere you do not control, it is history. Never
  "simplify" a field definition without generating and reading the migration.
- **A missing `related_name` cannot be changed freely** once other code uses the
  default accessor. Grep before renaming.
- **Don't remove a signal without finding every sender.** Signals are invisible at
  the call site — that is precisely why they should usually be explicit methods,
  and also why removing one is riskier than it looks.
- **Don't remove a password hasher in the same deploy as an upgrade.** Dropping one
  from `PASSWORD_HASHERS` locks out every user still stored under it.
- **Don't refactor cold code.** An odd abstraction in a module nobody has touched
  in three years is costing nothing.
