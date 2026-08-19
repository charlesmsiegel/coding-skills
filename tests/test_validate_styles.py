"""Tests for tools/validate_styles.py.

Same bargain as test_validate_skills.py: every rule gets a deliberately-invalid
style built in tmp_path and an assertion that the rule fires, plus one
assertion that the repo's own styles pass.

The rule worth having is `keep-coding-instructions`. Claude Code drops its
built-in software engineering instructions unless a style opts back in, so a
typo'd or non-boolean value silently turns a coding style into one that deletes
the coding instructions — a failure with no visible symptom.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = REPO_ROOT / "tools" / "validate_styles.py"

sys.path.insert(0, str(REPO_ROOT / "tools"))
import validate_styles  # noqa: E402

GOOD_STYLE = """---
name: Sample
description: A style that exists to be broken by the tests below
keep-coding-instructions: true
---

Style Active: Sample

Answer first. Say the thing.
"""


@pytest.fixture
def style(tmp_path):
    """Write one style file, then let each test break one thing about it."""

    def _build(text: str = GOOD_STYLE, filename: str = "sample.md") -> Path:
        path = tmp_path / filename
        path.write_text(text, encoding="utf-8")
        return path

    return _build


def test_the_repos_own_styles_pass():
    result = subprocess.run([sys.executable, str(VALIDATOR)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


def test_a_valid_style_produces_no_errors(style):
    assert validate_styles.check_style(style()) == []


def test_missing_frontmatter_is_rejected(style):
    errors = validate_styles.check_style(style(text="Style Active: Sample\n\nNo frontmatter.\n"))
    assert any("no YAML frontmatter" in e for e in errors)


def test_missing_description_is_rejected(style):
    text = "---\nname: Sample\n---\n\nStyle Active: Sample\n\nBody.\n"
    errors = validate_styles.check_style(style(text=text))
    assert any("no description" in e for e in errors)


def test_description_over_the_api_limit_is_rejected(style):
    text = GOOD_STYLE.replace("description: A style", "description: " + "x" * 1100 + " A style")
    assert any("limit is 1024" in e for e in validate_styles.check_style(style(text=text)))


def test_unknown_frontmatter_key_is_rejected(style):
    """Claude Code ignores keys it does not know, so a typo fails silently at runtime."""
    text = GOOD_STYLE.replace("keep-coding-instructions:", "keep-coding-instruction:")
    assert any("unsupported frontmatter key" in e for e in validate_styles.check_style(style(text=text)))


@pytest.mark.parametrize("value", ["yes", "True", "1", ""])
def test_non_boolean_keep_coding_instructions_is_rejected(style, value):
    text = GOOD_STYLE.replace("keep-coding-instructions: true", f"keep-coding-instructions: {value}")
    errors = validate_styles.check_style(style(text=text))
    assert any("keep-coding-instructions" in e for e in errors), f"{value!r} was accepted"


@pytest.mark.parametrize("value", ["true", "false"])
def test_both_booleans_are_accepted(style, value):
    text = GOOD_STYLE.replace("keep-coding-instructions: true", f"keep-coding-instructions: {value}")
    assert validate_styles.check_style(style(text=text)) == []


def test_omitting_keep_coding_instructions_is_accepted(style):
    """It defaults to false, which is right for the non-coding styles."""
    text = GOOD_STYLE.replace("keep-coding-instructions: true\n", "")
    assert validate_styles.check_style(style(text=text)) == []


def test_filename_must_be_hyphen_case(style):
    """The filename is the install name and Kiro's #style-<name> handle."""
    errors = validate_styles.check_style(style(filename="Sample_Style.md"))
    assert any("hyphen-case" in e for e in errors)


def test_frontmatter_only_is_rejected(style):
    text = "---\nname: Sample\ndescription: No body follows this\n---\n"
    assert any("no body" in e for e in validate_styles.check_style(style(text=text)))


def test_missing_style_active_marker_is_rejected(style):
    """The marker is how a style announces itself; styles/README.md promises it."""
    text = GOOD_STYLE.replace("Style Active: Sample", "This style is active")
    assert any("Style Active:" in e for e in validate_styles.check_style(style(text=text)))


def test_style_active_marker_must_name_the_style(style):
    text = GOOD_STYLE.replace("Style Active: Sample", "Style Active: Something Else")
    errors = validate_styles.check_style(style(text=text))
    assert any("Style Active:" in e for e in errors)


def test_readme_is_not_treated_as_a_style():
    """styles/README.md documents the set; it is not one of them."""
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--styles-dir", str(REPO_ROOT / "styles")],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "README" not in result.stdout


def test_missing_directory_is_an_error(tmp_path):
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--styles-dir", str(tmp_path / "nope")],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 2
