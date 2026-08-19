"""The install CLI is a contract: what lands where, and what refuses to run.

install.sh is the only part of this repo a user runs before reading anything,
so its argument parsing is tested the way a user meets it — as a subprocess
with real flags, writing into a throwaway --dest. The parsing is the risky
half: `--skills` takes an optional value, which means the script has to decide
whether the next token is a list or the next flag, and getting that wrong
silently installs everything when the user asked for two things.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL = REPO_ROOT / "install.sh"
SKILL_NAMES = sorted(p.name for p in (REPO_ROOT / "skills").iterdir() if (p / "SKILL.md").is_file())
STYLE_NAMES = sorted(p.stem for p in (REPO_ROOT / "styles").glob("*.md") if p.name != "README.md")


def run(*args, dest: Path | None = None) -> subprocess.CompletedProcess:
    argv = ["bash", str(INSTALL), *args]
    if dest is not None:
        argv += ["--dest", str(dest)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=300)


def installed(dest: Path, rel: str) -> list[str]:
    d = dest / rel
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


# --- usage and refusals -------------------------------------------------------

def test_help_exits_zero_and_documents_every_flag():
    result = run("--help")
    assert result.returncode == 0
    for flag in ("--claude", "--codex", "--kiro", "--skills", "--styles", "--dest"):
        assert flag in result.stdout, f"--help does not mention {flag}"


def test_help_says_codex_has_no_styles():
    assert "Codex" in run("--help").stdout


def test_no_target_is_an_error(tmp_path):
    result = run("--skills", dest=tmp_path)
    assert result.returncode == 2
    assert "--claude" in result.stderr


def test_no_content_flag_is_an_error(tmp_path):
    """`--claude` alone used to install skills; now it has to say what to install."""
    result = run("--claude", dest=tmp_path)
    assert result.returncode == 2
    assert "--skills" in result.stderr
    assert not (tmp_path / ".claude").exists()


def test_unknown_flag_is_an_error(tmp_path):
    assert run("--claude", "--skils", dest=tmp_path).returncode == 2


# --- skills -------------------------------------------------------------------

def test_bare_skills_installs_all_of_them(tmp_path):
    result = run("--claude", "--skills", dest=tmp_path)
    assert result.returncode == 0, result.stderr
    assert installed(tmp_path, ".claude/skills") == SKILL_NAMES


def test_skills_list_installs_only_those(tmp_path):
    result = run("--claude", "--skills", "fix-pr,brutal-review", dest=tmp_path)
    assert result.returncode == 0, result.stderr
    assert installed(tmp_path, ".claude/skills") == ["brutal-review", "fix-pr"]


def test_skills_list_accepts_the_equals_form(tmp_path):
    assert run("--claude", "--skills=fix-pr", dest=tmp_path).returncode == 0
    assert installed(tmp_path, ".claude/skills") == ["fix-pr"]


def test_skills_list_tolerates_spaces_after_commas(tmp_path):
    assert run("--claude", "--skills", "fix-pr, brutal-review", dest=tmp_path).returncode == 0
    assert installed(tmp_path, ".claude/skills") == ["brutal-review", "fix-pr"]


def test_bare_skills_before_another_flag_is_still_bare(tmp_path):
    """The token after `--skills` starts with `-`, so it is a flag, not a list."""
    result = run("--skills", "--claude", dest=tmp_path)
    assert result.returncode == 0, result.stderr
    assert installed(tmp_path, ".claude/skills") == SKILL_NAMES


def test_installed_skill_carries_the_license(tmp_path):
    run("--claude", "--skills", "fix-pr", dest=tmp_path)
    assert (tmp_path / ".claude/skills/fix-pr/LICENSE").is_file()
    assert (tmp_path / ".claude/skills/fix-pr/SKILL.md").is_file()


def test_a_second_partial_install_leaves_the_first_alone(tmp_path):
    run("--claude", "--skills", "fix-pr", dest=tmp_path)
    run("--claude", "--skills", "brutal-review", dest=tmp_path)
    assert installed(tmp_path, ".claude/skills") == ["brutal-review", "fix-pr"]


def test_unknown_skill_name_installs_nothing(tmp_path):
    """A typo must not half-install: the name is checked before anything is copied."""
    result = run("--claude", "--skills", "fix-pr,fix-prr", dest=tmp_path)
    assert result.returncode == 2
    assert "fix-prr" in result.stderr
    assert "fix-pr" in result.stderr, "the error should list the valid names"
    assert not (tmp_path / ".claude/skills").exists()


# --- styles -------------------------------------------------------------------

def test_bare_styles_installs_all_of_them(tmp_path):
    result = run("--claude", "--styles", dest=tmp_path)
    assert result.returncode == 0, result.stderr
    assert installed(tmp_path, ".claude/output-styles") == [f"{n}.md" for n in STYLE_NAMES]


def test_styles_go_to_output_styles_not_skills(tmp_path):
    run("--claude", "--styles", dest=tmp_path)
    assert not (tmp_path / ".claude/skills").exists()


def test_claude_styles_are_copied_verbatim(tmp_path):
    run("--claude", "--styles", "blunt", dest=tmp_path)
    installed_file = tmp_path / ".claude/output-styles/blunt.md"
    assert installed_file.read_bytes() == (REPO_ROOT / "styles/blunt.md").read_bytes()


def test_styles_readme_is_not_installed_as_a_style(tmp_path):
    run("--claude", "--styles", dest=tmp_path)
    assert not (tmp_path / ".claude/output-styles/README.md").exists()


def test_unknown_style_name_installs_nothing(tmp_path):
    result = run("--claude", "--styles", "blnut", dest=tmp_path)
    assert result.returncode == 2
    assert "blnut" in result.stderr
    assert not (tmp_path / ".claude/output-styles").exists()


def test_skills_and_styles_together(tmp_path):
    result = run("--claude", "--skills", "fix-pr", "--styles", "blunt", dest=tmp_path)
    assert result.returncode == 0, result.stderr
    assert installed(tmp_path, ".claude/skills") == ["fix-pr"]
    assert installed(tmp_path, ".claude/output-styles") == ["blunt.md"]


# --- kiro translation ---------------------------------------------------------

def test_kiro_styles_become_prefixed_steering_files(tmp_path):
    result = run("--kiro", "--styles", dest=tmp_path)
    assert result.returncode == 0, result.stderr
    assert installed(tmp_path, ".kiro/steering") == [f"style-{n}.md" for n in STYLE_NAMES]


def test_kiro_steering_frontmatter_is_manual_inclusion(tmp_path):
    run("--kiro", "--styles", "blunt", dest=tmp_path)
    text = (tmp_path / ".kiro/steering/style-blunt.md").read_text(encoding="utf-8")
    assert text.startswith("---\ninclusion: manual\n---\n")
    # Claude-only keys would be noise at best in a steering file.
    head = text.split("---")[1]
    assert "keep-coding-instructions" not in head
    assert "name:" not in head


def test_kiro_steering_keeps_the_body_intact(tmp_path):
    run("--kiro", "--styles", "blunt", dest=tmp_path)
    installed_body = (tmp_path / ".kiro/steering/style-blunt.md").read_text(encoding="utf-8")
    source = (REPO_ROOT / "styles/blunt.md").read_text(encoding="utf-8")
    source_body = source.split("---\n", 2)[2]
    assert installed_body.endswith(source_body)
    assert "Style Active: Blunt" in installed_body


def test_kiro_skills_still_go_to_kiro_skills(tmp_path):
    run("--kiro", "--skills", "fix-pr", dest=tmp_path)
    assert installed(tmp_path, ".kiro/skills") == ["fix-pr"]


# --- codex --------------------------------------------------------------------

def test_codex_styles_warns_and_skips(tmp_path):
    result = run("--codex", "--styles", dest=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Codex does not support styles" in result.stderr
    assert not (tmp_path / ".codex/output-styles").exists()
    assert not (tmp_path / ".codex/styles").exists()


def test_codex_skills_still_install_alongside_the_warning(tmp_path):
    result = run("--codex", "--skills", "fix-pr", "--styles", dest=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Codex does not support styles" in result.stderr
    assert installed(tmp_path, ".codex/skills") == ["fix-pr"]


def test_codex_warning_does_not_stop_other_targets(tmp_path):
    """The warning is about Codex only — Claude still gets its styles."""
    result = run("--claude", "--codex", "--styles", "blunt", dest=tmp_path)
    assert result.returncode == 0, result.stderr
    assert installed(tmp_path, ".claude/output-styles") == ["blunt.md"]


@pytest.mark.parametrize("style", STYLE_NAMES)
def test_every_style_installs_for_every_target(style, tmp_path):
    assert run("--claude", "--kiro", "--styles", style, dest=tmp_path).returncode == 0
    assert (tmp_path / f".claude/output-styles/{style}.md").is_file()
    assert (tmp_path / f".kiro/steering/style-{style}.md").is_file()


def test_a_directory_without_a_skill_md_does_not_truncate_the_listing(tmp_path):
    """A WIP directory under skills/ is skipped, not fatal.

    `[ -f "$dir/SKILL.md" ] && basename "$dir"` returns 1 for such a directory,
    and under `set -e` that killed the subshell mid-listing — so the skills
    after it silently vanished from `--skills` and from the "available skills"
    error. Found by reading rather than by failing, hence this test.
    """
    staged = tmp_path / "repo"
    staged.mkdir()
    for item in ("skills", "styles", "install.sh", "LICENSE"):
        source = REPO_ROOT / item
        if source.is_dir():
            shutil.copytree(source, staged / item)
        else:
            shutil.copy(source, staged / item)
    # Sorts before every real skill name, so a truncation loses all of them.
    (staged / "skills" / "aaa-work-in-progress").mkdir()

    dest = tmp_path / "dest"
    result = subprocess.run(
        ["bash", str(staged / "install.sh"), "--claude", "--skills", "--dest", str(dest)],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr
    assert installed(dest, ".claude/skills") == SKILL_NAMES
