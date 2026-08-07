# Porting skills from claude-tooling

**Date:** 2026-07-29
**Skills touched:** python-code-doctor (modified); django-code-doctor, brutal-review,
fix-issue, fix-pr, update-docs (new)

## Problem

`charlesmsiegel/claude-tooling` carries five skills and four slash commands that
`coding-skills` does not. Commands are deprecated across most agent frameworks, so
anything worth keeping there has to become a skill. Not everything is worth keeping:
one of the five skills is the direct ancestor of ours, and another duplicates it.

## What ships, and what does not

### Ported

| Source | Destination | Treatment |
|---|---|---|
| `skills/django-code-doctor` | `skills/django-code-doctor` | Detectors restructured and rewritten to house standard |
| `commands/brutal-review.md` | `skills/brutal-review` | Expanded: SKILL.md + two judgment guides |
| `commands/fix-issue.md` | `skills/fix-issue` | Expanded: SKILL.md + guides + `fetch_issue.py` |
| `commands/fix-pr.md` | `skills/fix-pr` | Expanded: SKILL.md + guides + `fetch_pr_feedback.py` |
| `commands/update-docs.md` | `skills/update-docs` | Expanded: SKILL.md + guides + `check_doc_staleness.py` |

### Not ported

**`skills/python-code-doctor`** — the ancestor of ours. Eight scripts against our forty.
Nothing in it that ours lacks.

**`skills/technical-debt-detector`** — ~85% redundant with our python-code-doctor:

| Their script | Our coverage | Verdict |
|---|---|---|
| `find_deferred_work.py` | `find_comment_smells.py` → `todo_comment` | redundant |
| `find_security_issues.py` | `find_security_issues.py`; bandit via `run_external_tools.py` | redundant |
| `find_maintainability_issues.py` | `find_missing_docstrings.py`, `find_type_gaps.py`, `find_naming_issues.py`, `analyze_complexity.py` | redundant, split more usefully by us |
| `analyze_test_coverage.py` | `find_untested_modules.py`, `find_test_smells.py` | redundant **except** `--run-coverage` |
| `check_dependencies.py` | `find_dependency_issues.py` | redundant **except** pip-audit CVEs and outdated pins |
| `analyze_all.py` | `analyze_all.py` | redundant |

Shipping it would put two skills on the same triggers ("audit this codebase") reporting
the same findings twice. Its two non-redundant capabilities are external-tool
integrations, and we already have a home for those — see §3.

**`skills/strands-agents`, `skills/langfuse-strands`** — vendor SDK references for an AWS
agent framework. No scripts, nothing for CI to test, and they go stale as the SDK moves.
Every skill in this repo is a language- or codebase-analysis skill; these are
documentation for a third-party library.

**`agents/`, `hooks/`, `ide_plugins/`, `install.py`** — out of scope. This repo ships
skills.

## 1. django-code-doctor

Their three detectors are already AST-based (`find_django_antipatterns.py`,
`find_django_issues.py`, `find_django_overengineering.py`; only the template checks scan
strings). The rewrite is therefore not about replacing regex. It is about three things:

1. **Granularity.** Three monoliths spanning unrelated concerns become six detectors a
   user can run and ignore individually, matching python-code-doctor's shape.
2. **Cross-file awareness.** Every interesting Django question — is this abstract model
   extended anywhere, does this manager earn its keep, is this FK missing
   `related_name` — needs a project-wide model. Their detectors are per-file.
3. **Interface.** Nothing there emits our findings shape, so nothing there can feed
   `format_findings.py`.

### Layout

```
skills/django-code-doctor/
  SKILL.md
  scripts/
    common.py                       byte-identical copy of python-code-doctor's; CI-synced
    django_context.py               project discovery: settings module, installed apps,
                                    model/field graph, template roots. Gates every
                                    detector so they stay silent on non-Django repos.
    analyze_django.py               aggregator (--format, --skip)
    find_query_issues.py            N+1 risk, save/create/delete in loop, unbounded
                                    queryset, update-without-F, raw SQL, .extra()
    find_model_issues.py            missing __str__, missing related_name, null=True on
                                    Char/Text, fat model, too many fields, str choices
                                    that should be TextChoices, missing Meta
                                    ordering/indexes
    find_view_issues.py             fat view, hardcoded URL, unnamed URL, .get() without
                                    an ownership filter, query inside form.clean
    find_django_security.py         DEBUG=True, hardcoded SECRET_KEY, mark_safe on a
                                    non-literal, ALLOWED_HOSTS='*'
    find_django_overengineering.py  single-impl abstract model, unused abstract model,
                                    thin custom manager, signal for simple save logic,
                                    single-use mixin, deep form/serializer inheritance,
                                    thin middleware, CRUD-only service layer, CBV with
                                    6+ overrides
    find_template_issues.py         nested loops, queries in templates, iteration over
                                    unprefetched relations
  references/
    django-orm.md                   select_related vs prefetch_related, when raw SQL is
                                    right, bulk-operation tradeoffs
    django-architecture.md          fat model vs service layer, signals vs explicit
                                    calls, CBV vs FBV, app boundaries
    django-overengineering.md       the abstraction table; when a manager, mixin, or
                                    middleware earns its keep
    django-security.md              ownership checks, autoescaping, secrets
    django-modernization.md         dated Django idioms to current ones
```

Their SKILL.md prose (pattern examples, the when-to-use-what tables, the security
checklist) moves into `references/` rather than bloating SKILL.md.

### Contracts

Every detector takes `--format text|json` and `--ignore type1,type2`, emits 🔴/🟡/🟢
severities and a flat JSON list of findings, and is conservative — false negatives over
false positives. `analyze_django.py` aggregates and supports `--skip`. Output feeds
`python-code-doctor/scripts/format_findings.py` unchanged.

`django_context.py` returns `None` when the tree is not a Django project (no `manage.py`,
no settings module, no `django` import). Detectors given `None` report nothing and say so
on stderr rather than emitting noise.

`common.py` is a byte-identical copy of python-code-doctor's, joining the set CI enforces
at `.github/workflows/ci.yml`. A skill directory has to be self-contained to be zipped
and installed alone, so the duplication is deliberate — the same reasoning that governs
the two visualization skills.

### Tests

`tests/django_code_doctor/`, one module per detector, each running the script as a
subprocess over a throwaway Django-shaped repo built in `tmp_path` — asserting both the
JSON the agent reads and the text a human reads. `evals/django-code-doctor/` for the
judgment half.

## 2. The four workflow skills

All four are language-agnostic. Each gets a substantial SKILL.md plus `references/` for
judgment; three get a script where something is genuinely mechanical.

### brutal-review

Adversarial review of a diff. No script — the mechanical half already exists as
`python-code-doctor/scripts/analyze_diff.py`, and SKILL.md delegates to it when the diff
is Python instead of re-deriving it.

- `references/attack-checklist.md` — the angles a hostile reviewer takes: edge cases and
  boundary values, error and failure paths, concurrency and ordering, resource lifecycle,
  API/contract breaks for existing callers, tests that cannot fail.
- `references/review-verdicts.md` — severity rubric, blocking versus nit, and how to be
  harsh without being wrong (every criticism names the input that breaks it).

**Trigger separation.** This is the one real collision risk in the port. brutal-review's
description claims "tear this apart", "what would a hostile reviewer say", "what am I
missing", "review my diff". python-code-doctor keeps "simplify", "refactor", "clean up",
"is this over-engineered". Neither description claims the other's phrases.

### fix-issue

GitHub issue to merged fix: read, reproduce, fail a test, fix the cause, verify, PR.

- `scripts/fetch_issue.py` — one call returns normalized JSON (body, labels, state,
  comments, linked PRs, referenced commits) instead of several `gh` invocations.
- `references/root-cause.md` — reproduce before fixing; symptom versus cause; when the
  reported bug is not the real bug.
- `references/pr-writing.md` — what a PR description owes a reviewer.

### fix-pr

Address review feedback on an existing PR.

- `scripts/fetch_pr_feedback.py` — inline review threads grouped by `file:line`, with
  resolved state, review verdicts, and CI status.
- `references/triaging-feedback.md` — classify each item as must-fix, nit, question, or
  wrong-suggestion; when to push back rather than comply. A reviewer suggestion that is
  technically wrong gets a reasoned reply, not an implementation.
- `references/replying.md` — closing the loop on each thread.

### update-docs

Regenerate a project's own documentation skill.

- `scripts/check_doc_staleness.py` — validates the existing docs skill's file and symbol
  citations against the tree and reports churn since the docs were last written, so a
  refresh knows what actually moved.
- `references/doc-structure.md` — what belongs in SKILL.md versus `references/`.
- `references/exploration-plan.md` — the parallel exploration sweep, rewritten
  harness-agnostic (the original hardcodes Claude Code's Agent tool and `model: haiku`).

### Script testing

`fetch_issue.py` and `fetch_pr_feedback.py` split into a thin shell-out layer and a pure
normalization layer. Tests exercise the normalization layer against captured `gh` JSON
fixtures; the shell-out layer is a few lines and is not mocked.

## 3. python-code-doctor absorption

`scripts/run_external_tools.py` already detects and runs ruff, mypy, black, isort,
bandit, and flake8, merging their output into this skill's findings shape under a
detect-if-installed → `missing_tools` → ask-before-install contract. Two tools join it:

- **pip-audit** — dependency advisories. This is the half of `check_dependencies.py`
  our `find_dependency_issues.py` does not do; that detector reads the manifest and
  cannot know about advisories. It audits `requirements*.txt` when present and the
  installed environment otherwise, labelling which it did.
- **coverage** — what actually executed. `find_untested_modules.py` answers "does any
  test mention this module"; coverage answers "does this code ever run". Reports only
  the unambiguous cases — a module or function with zero covered statements — because
  "under-covered" is a reviewer's judgment, not a detector's finding.

**Deviation from the plan above: `pip list --outdated` is not included.** It was listed
as part of pip-audit's value, but it audits the *interpreter running this script*, not
the target project, so for an arbitrary path its answer is about the wrong environment.
The subset that matters — outdated *and* vulnerable — pip-audit already reports with a
`fix_versions` upgrade target, and unpinned dependencies are already
`find_dependency_issues.py`'s job. What would be left is "a newer release exists", which
is true of nearly every package and would dilute the findings.

Two facts about the real tools, both verified against their actual output rather than
assumed, are load-bearing enough to record:

- coverage's per-function `start_line` only exists in 7.10+. Older versions still list
  `missing_lines`, and every statement of a fully-uncovered function is missing, so its
  lowest missing line is the function's first statement.
- `coverage json` prints "No data to report." to **stdout** and exits 1, so an unmeasured
  project and a crashed tool are indistinguishable by exit code.

SKILL.md's tool list and its "Relationship to Ruff and type checkers" section are updated
to name both. Tests extend the existing `run_external_tools` suite.

## 4. Repository plumbing

- README skills list gains five entries; the shared-`common.py` note names
  django-code-doctor.
- CI's `common.py` sync check covers django-code-doctor.
- CI's ratchet — python-code-doctor's bug-class detectors stay silent on every skill's
  `scripts/` — must pass on all four new script directories.
- `install.sh` discovers skill directories by globbing for `SKILL.md`. No change.
- `release.yml` is tag-driven per skill directory. No change.

## Build order

1. python-code-doctor absorption — smallest, independent, no new directories.
2. The four workflow skills — independent of each other.
3. django-code-doctor — largest.
4. README and CI.

## Known follow-ups

Deliberately out of scope, recorded so they are not mistaken for oversights:

- django-code-doctor will be refined further after this lands; this port establishes the
  structure and interface, not the final detector set.
- Both simplifier skills may be renamed later.
