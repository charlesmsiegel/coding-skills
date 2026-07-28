# coding-skills

A repository of agentic skills to help with AI coding.

## Skills

- **[python-simplifier](python-simplifier/)** — critically review and simplify Python
  code: deterministic AST detectors (`scripts/`) plus judgment guides (`references/`).
- **[theory-building](theory-building/)** — governs code being written *now*: state the
  theory, reuse before inventing, abstract over repetition, treat tests as a floor.
  Guides only, no scripts.
- **[code-visualization](code-visualization/)** — builds a single-file tabbed HTML
  "codebase atlas" (dependency graph with cycle detection, churn×complexity hotspots,
  inventory, plus judgment tabs).
- **[pr-visualization](pr-visualization/)** — builds a single-file tabbed HTML review
  report for a PR, branch, or diff (change footprint, contracts, test delta, blast
  radius). Step 7 refreshes the codemap atlas and expects **code-visualization** to be
  installed as a sibling — which it is here.

## Layout

```
<skill-name>/            one directory per skill — exactly what ships
  SKILL.md               name + description frontmatter (description <= 1024 chars)
  scripts/               executable detectors/tools, if the skill has any
  references/            judgment guides loaded on demand
tests/<skill_name>/      that skill's tests (underscored, so it's importable)
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
`extract_tabs.py`, `check_codemap_state.py`, and `verify_citations.py`. The copies are
byte-identical and must stay that way — a skill directory has to be self-contained to
be zipped and installed on its own, so the duplication is deliberate, not drift.

CI also runs a ratchet: python-simplifier's bug-class detectors must stay silent on
every skill's own `scripts/` directory.

## Releasing a skill

Tag `<skill>-v<version>` (e.g. `python-simplifier-v0.1.0`). The release workflow
validates the skill's frontmatter, packages `SKILL.md`, `LICENSE`, `references/`, and
`scripts/` into `<skill>.skill`, and attaches it to a GitHub release. Install the
artifact in Claude Code (`~/.claude/skills/`) or upload it to claude.ai.
