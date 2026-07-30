"""Tests for tools/validate_skills.py.

A validator that cannot fail is worse than no validator, because it reads as
coverage. So every rule gets a deliberately-invalid skill built in tmp_path and
an assertion that the rule actually fires on it — plus one assertion that the
repo's own nine skills pass, which is the thing CI relies on.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = REPO_ROOT / "tools" / "validate_skills.py"

sys.path.insert(0, str(REPO_ROOT / "tools"))
import validate_skills  # noqa: E402

GOOD_FRONTMATTER = """---
name: sample-skill
description: A description long enough to look real. Use when the user asks for a sample.
---

# Sample Skill

Body text.
"""

GOOD_EVALS = {
    "skill_name": "sample-skill",
    "evals": [{"id": "triggers", "prompt": "do the thing", "expected_output": "the thing is done"}],
}


@pytest.fixture
def skillset(tmp_path):
    """Build a valid skill + evals pair, then let each test break one thing."""

    def _build(skill_md: str = GOOD_FRONTMATTER, evals: dict | None = GOOD_EVALS, name: str = "sample-skill"):
        skills = tmp_path / "skills" / name
        skills.mkdir(parents=True, exist_ok=True)
        (skills / "SKILL.md").write_text(skill_md, encoding="utf-8")
        eval_dir = tmp_path / "evals" / name
        eval_dir.mkdir(parents=True, exist_ok=True)
        if evals is not None:
            (eval_dir / "evals.json").write_text(json.dumps(evals), encoding="utf-8")
        return skills, tmp_path / "evals"

    return _build


def errors_for(skillset_result) -> list[str]:
    skill_dir, evals_root = skillset_result
    return validate_skills.check_skill(skill_dir, evals_root)


def test_the_repos_own_skills_pass():
    result = subprocess.run([sys.executable, str(VALIDATOR)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


def test_a_valid_skill_produces_no_errors(skillset):
    assert errors_for(skillset()) == []


def test_missing_frontmatter_is_rejected(skillset):
    errors = errors_for(skillset(skill_md="# Sample Skill\n\nNo frontmatter here.\n"))
    assert any("no YAML frontmatter" in e for e in errors)


def test_name_must_match_the_directory(skillset):
    md = GOOD_FRONTMATTER.replace("name: sample-skill", "name: something-else")
    errors = errors_for(skillset(skill_md=md))
    assert any("but the directory is" in e for e in errors)


def test_name_must_be_hyphen_case(skillset):
    md = GOOD_FRONTMATTER.replace("name: sample-skill", "name: Sample_Skill")
    errors = errors_for(skillset(skill_md=md, name="Sample_Skill"))
    assert any("hyphen-case" in e for e in errors)


def test_description_over_the_api_limit_is_rejected(skillset):
    md = GOOD_FRONTMATTER.replace("description: A description", "description: " + "x" * 1100 + " A description")
    errors = errors_for(skillset(skill_md=md))
    assert any("the API limit is 1024" in e for e in errors)


def test_missing_description_is_rejected(skillset):
    md = "---\nname: sample-skill\n---\n\n# Sample Skill\n\nBody.\n"
    errors = errors_for(skillset(skill_md=md))
    assert any("no description" in e for e in errors)


def test_unknown_frontmatter_key_is_rejected(skillset):
    """A typo'd key silently drops the real field, so it fails rather than warns."""
    md = GOOD_FRONTMATTER.replace("description:", "descriptions:")
    errors = errors_for(skillset(skill_md=md))
    assert any("unsupported frontmatter key" in e for e in errors)


def test_frontmatter_only_is_rejected(skillset):
    md = GOOD_FRONTMATTER.split("---\n")[1]
    errors = errors_for(skillset(skill_md=f"---\n{md}---\n"))
    assert any("no body" in e for e in errors)


def test_bare_script_path_is_rejected(skillset):
    """`python scripts/x.py` only works if cwd is the skill dir, which it never is."""
    md = GOOD_FRONTMATTER + "\n```bash\npython scripts/tool.py .\n```\n"
    errors = errors_for(skillset(skill_md=md))
    assert any("bare `python scripts/" in e for e in errors)


def test_the_skill_prefixed_form_is_accepted(skillset):
    md = GOOD_FRONTMATTER + '\n```bash\npython "$SKILL/scripts/tool.py" .\n```\n'
    assert errors_for(skillset(skill_md=md)) == []


def test_missing_evals_file_is_rejected(skillset):
    errors = errors_for(skillset(evals=None))
    assert any("no evals/" in e for e in errors)


def test_eval_declaring_a_missing_fixture_is_rejected(skillset):
    evals = {
        "skill_name": "sample-skill",
        "evals": [{"id": "a", "prompt": "p", "expected_output": "e", "files": ["fixtures/gone.py"]}],
    }
    errors = errors_for(skillset(evals=evals))
    assert any("missing fixture" in e for e in errors)


def test_duplicate_eval_ids_are_rejected(skillset):
    evals = {
        "skill_name": "sample-skill",
        "evals": [
            {"id": "same", "prompt": "p", "expected_output": "e"},
            {"id": "same", "prompt": "q", "expected_output": "f"},
        ],
    }
    errors = errors_for(skillset(evals=evals))
    assert any("duplicate case id" in e for e in errors)


def test_eval_missing_expected_output_is_rejected(skillset):
    evals = {"skill_name": "sample-skill", "evals": [{"id": "a", "prompt": "p"}]}
    errors = errors_for(skillset(evals=evals))
    assert any("has no expected_output" in e for e in errors)


def test_invalid_eval_json_is_reported_not_raised(skillset):
    skill_dir, evals_root = skillset()
    (evals_root / "sample-skill" / "evals.json").write_text("{not json", encoding="utf-8")
    errors = validate_skills.check_skill(skill_dir, evals_root)
    assert any("invalid JSON" in e for e in errors)


def test_indented_frontmatter_is_rejected(skillset):
    """Nested YAML is rejected rather than half-parsed by the stdlib-only reader."""
    md = "---\nname: sample-skill\ndescription: A real description here.\nmeta:\n  nested: yes\n---\n\n# S\n\nBody.\n"
    errors = errors_for(skillset(skill_md=md))
    assert any("must be flat" in e for e in errors)


def test_cli_exits_nonzero_and_names_the_problem(skillset, tmp_path):
    skillset(skill_md="# no frontmatter\n")
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--skills-dir", str(tmp_path / "skills"),
         "--evals-dir", str(tmp_path / "evals")],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 1
    assert "no YAML frontmatter" in result.stderr


def test_cli_rejects_an_unknown_skill_name(tmp_path):
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--skill", "does-not-exist"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 2
    assert "no such skill" in result.stderr
