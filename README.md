# coding-skills

A repository of agentic skills to help with AI coding.

## Skills

- **[python-code-doctor](skills/python-code-doctor/)** — critically review and simplify
  Python code: deterministic AST detectors (`scripts/`) plus judgment guides
  (`references/`). `run_external_tools.py` also drives the real tools when they are
  installed — ruff, mypy, black, isort, bandit, flake8, plus pip-audit (dependency
  advisories) and coverage (code that never executes).
- **[typescript-code-doctor](skills/typescript-code-doctor/)** — the same idea for
  TypeScript, and fully standalone: it ships its own TS/TSX scanner (strings,
  template literals, regex-vs-division, JSX text, bracket matching) so the
  detectors run against a checkout with no `node_modules` and no build. Covers
  the type system (`any`/`as`/`!`/`@ts-ignore` and a tsconfig strictness audit),
  promise bugs, encapsulation leaks, import cycles and barrels, dependency
  reconciliation, and the usual smell/design/duplication set.
  `run_external_tools.py` drives `tsc`, ESLint, Biome, Prettier, madge, knip,
  `npm audit` and coverage when the project already has them — the compiler
  answers what a syntax scanner structurally cannot.
- **[django-code-doctor](skills/django-code-doctor/)** — the Django-specific companion,
  and the one that also writes and upgrades. Fifteen detectors: N+1 queries and
  per-row writes, model and migration problems, missing object-level authorization
  (in views, DRF viewsets, and the admin alike), insecure settings, work hidden in
  templates, form and admin traps, async and transaction-boundary bugs,
  Django-shaped test smells, and abstractions that never earned their keep. It is
  **version-aware across Django 4, 5, and 6** — a declarative change table plus
  version detection from the project's manifests lets it derive severity from the
  target and drive a 4→5→6 upgrade. `references/django-idioms.md` is prescriptive,
  for writing new Django rather than reviewing old. Detectors share one
  whole-project class graph, parse each file once, and stay silent outside Django
  projects.
- **[code-doctor](skills/code-doctor/)** — the language-agnostic member of the family,
  for repos the other three do not cover. No parsers, no comment-syntax tables, no
  framework knowledge: it finds committed credentials, merge markers, oversized files
  and TODO debt from text and git alone, on a fresh clone with nothing installed.
  (Duplication, dead-code leads, churn hotspots and the project-toolchain runner are
  landing in follow-up plans; the skill's description tracks what it actually ships.)
  Its distinguishing feature is the schema — a **finding**
  asserts a defect and carries a fix, a **candidate** reports a lead and carries the
  benign explanations instead, and the dataclass raises if a detector confuses them.
  Every detector whose evidence can be incomplete reports that incompleteness, so a
  degraded run never reads as a clean repository. It measures quality and bugs;
  **code-visualization** explains architecture. Routes to the language specialists
  on manifest evidence and merges every doctor's report into one attributed envelope.
- **[theory-building](skills/theory-building/)** — governs code being written *now*:
  state the theory, reuse before inventing, abstract over repetition, treat tests as a
  floor. Guides only, no scripts.
- **[code-visualization](skills/code-visualization/)** — builds a single-file tabbed
  HTML "codebase atlas" (dependency graph with cycle detection, covering both imports
  and runtime loads of templates/prompts, churn×complexity hotspots, inventory, LLM
  call sites and prompt lineage, plus judgment tabs).
- **[pr-visualization](skills/pr-visualization/)** — builds a single-file tabbed HTML
  review report for a PR, branch, or diff (change footprint, contracts, test delta,
  blast radius). It never touches `docs/codemap.html` — refreshing the atlas belongs to
  **code-visualization**.
- **[code-overview](skills/code-overview/)** — the orchestrator. It analyzes nothing
  itself; it decides *what the units are*, runs the others per unit, and binds the
  results into one navigable five-document set. `discover_packages.py` proposes
  packages from manifests, Django `INSTALLED_APPS` and layout, each with its
  evidence, and returns what it could not settle as questions for the user — the
  right unit is a design judgment, not a heuristic. Per package it drives
  **code-visualization** into `<pkg>/docs/codemap.html`, one **code-doctor** call
  into a graded `<pkg>/docs/health.html` — findings score it, candidates never do —
  **science-investigation** into `<pkg>/docs/measurement.html`, scored `null`
  rather than zero when nothing is measurable, and a three-judge panel into
  `<pkg>/docs/theory.html` grading whether the code expresses a coherent
  Naur-sense theory of its problem — median per dimension, disagreement of two
  rungs or more reported rather than hidden — plus a `<pkg>/docs/summary.html`
  linking all four. `health.html` carries seven weighted categories, a 0–100
  score, a letter grade, and a `code-health-meta` JSON block so the numbers
  extract without an HTML parser. A category nothing measured comes back
  *ungraded* and is dropped from the mean rather than scored 0 or 100 — including
  when a doctor crashed, where a zero count means unknown, not clean.
  `inject_nav.py` then links every page up to the overall document of its own
  type, across to its four siblings, and back down; it is idempotent,
  existence-checked, and styles itself with literal fallbacks so it can be
  injected into an atlas **code-visualization** wrote without that skill knowing.

### Workflow skills

Language-agnostic, and each carries a script only where something is genuinely
mechanical.

- **[brutal-review](skills/brutal-review/)** — adversarial review of a diff. Every
  finding must name the input that breaks the code; a complaint with no failing case
  is cut. Delegates to python-code-doctor's `analyze_diff.py` for Python.
- **[fix-issue](skills/fix-issue/)** — GitHub issue to reviewable PR: reproduce, fail a
  test, fix the cause. `fetch_issue.py` pulls related PRs and scrapes repro leads out
  of the thread.
- **[fix-pr](skills/fix-pr/)** — work through review feedback.
  `fetch_pr_feedback.py` pulls inline threads with their resolved state, which
  `gh pr view` cannot show. Complying with a wrong suggestion counts as a failure.
- **[literature-survey](skills/literature-survey/)** — the only skill here whose subject
  is not this checkout. Researches an external body of knowledge and leaves two things: a
  corpus on disk and a report whose every claim points into it. Discovery across arXiv,
  Semantic Scholar, OpenAlex and Crossref (one dead source is a caveat, all four dead is
  an error — an unreachable network must never render as an empty literature), hash-pinned
  download of every artifact, one reader per artifact with no reader shown another's notes,
  and citation snowballing that reports *cap reached* and *saturated* as the different
  facts they are. The failure it exists to prevent is fluent synthesis over unread
  abstracts, and downloading the PDFs does not prevent it — so a claim carries a locator
  (page, section or verbatim quote) and `verify_locators.py` re-resolves every one against
  the manifest before the report may stand. It is a **blocking** gate: a locator whose
  file changed, whose page is past the end of the document or whose quote is not in the
  text fails the run, and a check that could not be performed comes back *unverifiable*
  rather than clean. Every masthead number is computed by `corpus_stats.py` from the
  manifest, the notes and the snowball state — a hand-typed count would be an unverified
  claim in the one place whose job is to establish the report's numbers can be trusted.
  Rate-limited, `robots.txt`-respecting, and paywalls are recorded as gaps rather than
  circumvented.
- **[science-investigation](skills/science-investigation/)** — audits whether a system's
  numbers can be believed: is the right thing measured, on enough real data, with sound
  statistics, and does the reported score mean what the dashboard claims. A system can be
  fully tested and still have measurement that means nothing, and that gap is invisible to
  every other skill here. Sharpest on LLM systems (judge confound, prompt and
  model-version drift, silent fail-soft, contamination, RAG's two failure surfaces).
  Four stdlib scripts enumerate metrics and thresholds, count labeled examples, find
  default-off flags and swallowed errors, and trace one threshold across the tree — each
  emitting a headline, a caveat, and rows explicitly marked as candidates to confirm by
  reading. It reports; it does not fix. Ships the audit as a graded measurement.html —
  importance-weighted measured things over measurable things, with the inventory table
  the score was computed from.
- **[update-docs](skills/update-docs/)** — build or refresh a project's own
  documentation skill. `check_doc_staleness.py` checks citations against the tree and
  infers missing coverage from churn.

## Layout

```
skills/<skill-name>/     one directory per skill — exactly what ships
  SKILL.md               name + description frontmatter (description <= 1024 chars)
  scripts/               executable detectors/tools, if the skill has any
  references/            judgment guides loaded on demand
  assets/                templates and other bundled files, if any
tests/<skill_name>/      that skill's tests (underscored, so it's importable)
tests/conftest.py        shared fixtures: throwaway git repos, script runners
evals/<skill-name>/      judgment-half eval prompts (evals.json + fixtures)
tools/validate_skills.py structural validation, run by CI and by the release job
pyproject.toml           shared dev tooling config (pytest, ruff)
.github/workflows/       CI (lint + tests + ratchet) and per-skill releases
```

Skill directories stay free of tests and packaging metadata so a skill can be zipped
and uploaded as-is.

## Development

Requires **Python 3.11+**, and CI runs the suite on 3.11 and 3.12 so it stays that
way. A skill gets installed next to whatever interpreter the user already has, so
the scripts stay off PEP 701 f-strings (nested same-quote expressions, backslashes
in the expression part) — those turn into a `SyntaxError` raised from inside a
subprocess, which is a miserable thing to debug.

```bash
python -m pip install -e ".[dev]"   # pytest + ruff
python -m pytest                    # all skills' tests
python -m ruff check .
python tools/validate_skills.py     # frontmatter, naming, description limit, evals pairing
```

`tools/validate_skills.py` is the single definition of a structurally valid skill:
CI runs it over all fourteen on every pull request, and the release job runs it on the
one skill it is about to package. It is stdlib-only, like the detectors, so it runs
wherever the skills do. Its own rules are tested against deliberately-invalid skills
in `tests/test_validate_skills.py` — a validator that cannot fail reads as coverage
without being any.

code-visualization and pr-visualization each carry their own copy of `common.py`,
`extract_tabs.py`, `check_codemap_state.py`, `verify_citations.py`,
`lint_fragments.py`, `coverage_data.py`, `resources.py` (runtime resource
references — templates, prompts, embedded data), and `llmops.py` (LLM call sites,
models, prompt lineage). The copies are byte-identical and CI
enforces it — a skill directory has to be self-contained to be zipped and installed
on its own, so the duplication is deliberate, not drift. `assemble.py` is identical
except for one line (the `--label` default) across *three* skills — those two and
literature-survey, which assembles its report with the same fragment protocol — and
CI checks all three copies against each other. Two files differ on
purpose and are NOT synced: `assets/template.html` (each skill has its own theme;
the renderers and placeholders match, which a test pins) and
`references/llm-tabs.md` (different tab sets).

literature-survey ships a `common.py` of its own that is **not** a copy of anybody's:
same filename, entirely different subject (HTTP transport, the corpus records, the
reporter). It is the reason its masthead goes in through `inject_masthead.py` at a
marker rather than through a raw-HTML flag on `assemble.py` — a flag one skill needs
is how three copies of a file start to drift.

django-code-doctor ships its own copies of python-code-doctor's `common.py` and
`format_findings.py`, byte-identical and CI-enforced, for the same reason: a skill has
to be self-contained to be installed on its own, and the reporting pipe its SKILL.md
documents has to work with no sibling skill present.

No skill may reach into another skill's directory. A release archive holds exactly one
skill, so `../other-skill/scripts/...` is a path that exists only in this monorepo.
Where a companion genuinely adds something (brutal-review is better with
python-code-doctor's `analyze_diff.py`), the SKILL.md tests for it and documents the
fallback. `tests/test_standalone_install.py` enforces this by installing each skill
alone and running what its SKILL.md documents.

CI also runs a ratchet: python-code-doctor's bug-class detectors must stay silent on
every skill's own `scripts/` directory.

The visualization skills are tested through their CLIs — each analyzer runs as a
subprocess over a throwaway git repo built in `tmp_path`, and both what it prints
(the JSON the agent reads) and what it writes (the HTML fragment the reader sees)
are asserted. A test marked `xfail` records a known defect rather than a passing
assertion that the wrong behavior is correct; check its reason before "fixing" it.

## Installing locally

```bash
./install.sh --claude              # ~/.claude/skills/
./install.sh --claude --codex      # both ~/.claude/skills/ and ~/.codex/skills/
./install.sh --kiro --dest ~/proj  # ~/proj/.kiro/skills/ (per-project install)
```

Each skill is replaced wholesale on re-install (stale files removed) and the
copy mirrors the release artifact: skill directory plus LICENSE, minus caches.

## Releasing a skill

Tag `<skill>-v<version>` (e.g. `python-code-doctor-v0.1.0`) — the tag names the
directory under `skills/`. The release workflow validates that skill's frontmatter,
packages `SKILL.md`, `LICENSE`, `references/`, `scripts/`, and `assets/` into
`<skill>.skill`, and attaches it to a GitHub release. The archive unpacks to
`<skill>/`, without the `skills/` prefix, so the artifact drops straight into Claude
Code (`~/.claude/skills/`) or uploads to claude.ai.
