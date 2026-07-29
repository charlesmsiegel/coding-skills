---
name: django-simplifier
description: Review and simplify Django code — find N+1 queries and per-row writes, model definition problems, fat views, missing object-level authorization, insecure settings, work hidden in templates, and abstractions (abstract models, managers, mixins, signals, service layers) that never earned their keep. Use when the user asks to simplify, refactor, optimize, or review Django code, or mentions models, QuerySets, the ORM, select_related/prefetch_related, views, serializers, forms, signals, middleware, admin, or Django settings. Detectors build one whole-project class graph, so "extended by nobody" and "used once" are facts rather than guesses, and they stay silent on trees that are not Django projects. For general Python analysis use python-simplifier — this skill's findings share its format and pipe into its format_findings.py.
---

# Django Code Simplifier

Django's defaults make several expensive mistakes easy and invisible: a loop that
issues a query per row reads exactly like a loop that doesn't, and a template can
hit the database with nothing in the Python to show for it. This skill finds those
mechanically, then gives you the judgment to decide what to do.

It is the Django-specific companion to **python-simplifier**. Run that one too —
complexity, duplication, dead code, exception handling, and resource leaks are not
Django questions, and this skill does not look for them.

## Deterministic detectors

```bash
python scripts/analyze_django.py /path/to/project          # everything, one report
python scripts/analyze_django.py . --format json > report.json
python scripts/analyze_django.py . --skip templates,overengineering
python scripts/analyze_django.py . --ignore no_default_ordering

python scripts/find_query_issues.py .            # N+1, per-row writes, update-without-F, raw SQL
python scripts/find_model_issues.py .            # __str__, null on text fields, on_delete, related_name, ordering
python scripts/find_view_issues.py .             # fat views, hardcoded URLs, unnamed routes, ownership checks
python scripts/find_django_security.py .         # DEBUG, SECRET_KEY, ALLOWED_HOSTS, mark_safe
python scripts/find_django_overengineering.py .  # abstract models, managers, mixins, signals, services
python scripts/find_template_issues.py .         # queries and relation walks inside templates
```

Every detector takes `--format text|json` and `--ignore type1,type2`, emits
🔴/🟡/🟢 severities, and produces the same flat findings shape as
python-simplifier — so any of them pipes straight into its reporting tools:

```bash
python scripts/analyze_django.py . --format json \
  | python ../python-simplifier/scripts/format_findings.py --format cards
```

`analyze_django.py` builds the project graph **once** and shares it with all six,
which is most of the runtime on a large project.

### The two findings to read first

- **`missing_ownership_filter`** — a view looks up a record by a key from the URL
  and never mentions `request.user`. This is the most common real vulnerability in
  Django application code: the endpoint checks that you are logged in and then
  hands you somebody else's row. Verify each one by hand; the detector cannot see
  authorization enforced through a custom mixin or a queryset override.
- **`n_plus_one_query`** — the finding that turns into latency. Reported only when
  the loop iterates a queryset, the body reaches through a real relation on the
  loop variable, and nothing in the chain prefetches it.

### They stay quiet outside Django

Every detector is gated on the project actually being Django (a `manage.py`, or
`django` imports). On anything else they report nothing and **say so on stderr** —
so silence is never mistaken for a clean bill of health.

## What the detectors cannot decide

Deterministic checks find the mechanical problems. Everything below is a judgment
call, and getting it wrong in either direction is expensive — load the guide.

| Load this when… | File |
|---|---|
| Deciding how to fix a query problem: select_related vs prefetch_related, when bulk operations change semantics, when raw SQL is the right answer, what `only()` costs | `references/django-orm.md` |
| Deciding where logic belongs: fat model vs service layer, signals vs explicit calls, CBV vs FBV, where an app boundary should fall | `references/django-architecture.md` |
| Judging whether an abstraction earns its keep — the manager, mixin, middleware, and service-layer questions, and what to do instead | `references/django-overengineering.md` |
| Reviewing the security of a Django app beyond what settings show — object-level authorization, escaping, file uploads, the admin | `references/django-security.md` |
| Modernizing dated Django — `url()`, string choices, `USE_L10N`, `index_together`, and what changed under you | `references/django-modernization.md` |

## Workflow

1. **Run `analyze_django.py`.** Triage the mechanical findings before reading
   anything by hand.
2. **Verify every 🔴 by hand.** `missing_ownership_filter` and `n_plus_one_query`
   both have real false-positive modes — authorization enforced elsewhere, a loop
   over a list that only looks like a queryset.
3. **Measure the query findings before and after.** Use `django-debug-toolbar`,
   `assertNumQueries`, or `CaptureQueriesContext`. A prefetch that doesn't reduce
   the query count is a guess that happened to compile.
4. **Review the hot code with the guides open.** Effort follows churn, not line
   count — `git log --since="1 year ago" --name-only --pretty=format: | grep '\.py$'
   | sort | uniq -c | sort -rn | head -30`.
5. **Pin behavior before refactoring.** Django code is heavily coupled to the
   database; a test that exercises the real query is the only safety net that
   counts. See python-simplifier's `references/safety-net-and-testing.md`.
6. **Ratchet.** `assertNumQueries` around the view that had the N+1 is what stops
   it coming back on the next feature.

## Rules

- **Behavior is sacred**, and in Django that includes the *number of queries* and
  the *number of round trips*. A refactor that changes either has changed behavior
  under load even when every test passes.
- **Migrations are not refactorable.** Changing a model field changes the schema.
  Never "simplify" a field definition without generating and reading the migration.
- **A missing `related_name` cannot be changed freely** once other code uses the
  default accessor. Grep before renaming.
- **Don't remove a signal without finding every sender.** Signals are invisible at
  the call site — that is precisely why they should usually be explicit methods,
  and also why removing one is riskier than it looks.
