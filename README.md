# coding-skills

A repository of agentic skills to help with AI coding.

## Skills

- **[python-simplifier](skills/python-simplifier/)** — critically review and simplify
  Python code: deterministic AST detectors (`scripts/`) plus judgment guides
  (`references/`).
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
pyproject.toml           shared dev tooling config (pytest, ruff)
.github/workflows/       CI (lint + tests + ratchet) and per-skill releases
```

Skill directories stay free of tests and packaging metadata so a skill can be zipped
and uploaded as-is.

## Development

Requires **Python 3.12+** (the code-visualization scripts use PEP 701 f-strings).

```bash
python -m pip install -e ".[dev]"   # pytest + ruff
python -m pytest                    # all skills' tests
python -m ruff check .
```

code-visualization and pr-visualization each carry their own copy of `common.py`,
`extract_tabs.py`, `check_codemap_state.py`, `verify_citations.py`,
`lint_fragments.py`, `coverage_data.py`, `resources.py` (runtime resource
references — templates, prompts, embedded data), and `llmops.py` (LLM call sites,
models, prompt lineage). The copies are byte-identical and CI
enforces it — a skill directory has to be self-contained to be zipped and installed
on its own, so the duplication is deliberate, not drift. `assemble.py` is identical
except for one line (the `--label` default), also CI-checked. Two files differ on
purpose and are NOT synced: `assets/template.html` (each skill has its own theme;
the renderers and placeholders match, which a test pins) and
`references/llm-tabs.md` (different tab sets).

CI also runs a ratchet: python-simplifier's bug-class detectors must stay silent on
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

Tag `<skill>-v<version>` (e.g. `python-simplifier-v0.1.0`) — the tag names the
directory under `skills/`. The release workflow validates that skill's frontmatter,
packages `SKILL.md`, `LICENSE`, `references/`, `scripts/`, and `assets/` into
`<skill>.skill`, and attaches it to a GitHub release. The archive unpacks to
`<skill>/`, without the `skills/` prefix, so the artifact drops straight into Claude
Code (`~/.claude/skills/`) or uploads to claude.ai.
