# coding-skills

A repository of agentic skills to help with AI coding.

## Skills

- **[python-simplifier](python-simplifier/)** — critically review and simplify Python
  code: deterministic AST detectors (`scripts/`) plus judgment guides (`references/`).

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

```bash
python -m pip install -e ".[dev]"   # pytest + ruff
python -m pytest                    # all skills' tests
python -m ruff check .
```

CI also runs a ratchet: python-simplifier's bug-class detectors must stay silent on
every skill's own `scripts/` directory.

## Releasing a skill

Tag `<skill>-v<version>` (e.g. `python-simplifier-v0.1.0`). The release workflow
validates the skill's frontmatter, packages `SKILL.md`, `LICENSE`, `references/`, and
`scripts/` into `<skill>.skill`, and attaches it to a GitHub release. Install the
artifact in Claude Code (`~/.claude/skills/`) or upload it to claude.ai.
