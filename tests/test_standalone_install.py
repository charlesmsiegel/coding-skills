"""Every skill has to work when it is the only skill installed.

A release archive holds exactly one skill directory plus LICENSE, and that is
also what install.sh copies. So a path like `../python-simplifier/scripts/x.py`
resolves in this monorepo and nowhere a user will ever run it.

These tests build that release layout in tmp_path — one skill, alone, with no
siblings on disk — and then exercise what its SKILL.md tells an agent to do.
The point is to catch the monorepo assumption at the moment it is written, not
at the moment a user reports that the documented command does not exist.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"

# What the release workflow packages, and what install.sh copies.
PACKAGED_DIRS = ("references", "scripts", "assets")

SKILL_NAMES = sorted(p.name for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())
OTHER_SKILL_NAMES = set(SKILL_NAMES)


def install_alone(skill: str, dest: Path) -> Path:
    """Reproduce the release artifact: one skill directory plus LICENSE, no siblings."""
    root = dest / skill
    root.mkdir(parents=True)
    shutil.copy(SKILLS_DIR / skill / "SKILL.md", root / "SKILL.md")
    shutil.copy(REPO_ROOT / "LICENSE", root / "LICENSE")
    for sub in PACKAGED_DIRS:
        src = SKILLS_DIR / skill / sub
        if src.is_dir():
            shutil.copytree(src, root / sub, ignore=shutil.ignore_patterns("__pycache__"))
    return root


@pytest.fixture(params=SKILL_NAMES)
def installed(request, tmp_path) -> Path:
    """One skill, installed by itself, exactly as a release archive unpacks."""
    return install_alone(request.param, tmp_path / "skills")


def shipped_text_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.suffix in {".md", ".py"} and p.is_file()]


def test_ships_a_skill_md_and_license(installed):
    assert (installed / "SKILL.md").is_file()
    assert (installed / "LICENSE").is_file()


def sibling_reaches(text: str) -> list[int]:
    """Line numbers where `text` uses a `../<sibling-skill>/` path."""
    return [
        lineno
        for lineno, line in enumerate(text.splitlines(), 1)
        for match in re.finditer(r"\.\./([a-z0-9-]+)/", line)
        if match.group(1) in OTHER_SKILL_NAMES
    ]


def test_sibling_reach_detector_catches_a_planted_path():
    """The guard below is only worth having if it fails on the thing it forbids."""
    planted = 'run this:\n  python ../python-simplifier/scripts/format_findings.py\n'
    assert sibling_reaches(planted) == [2]
    assert sibling_reaches('python "$SKILL/scripts/format_findings.py"\n') == []
    assert sibling_reaches("see ../docs/notes.md\n") == [], "only skill names count"


def test_no_file_reaches_into_a_sibling_skill(installed):
    """`../other-skill/...` is a monorepo-only path — it cannot resolve once installed."""
    offenders = []
    for path in shipped_text_files(installed):
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno in sibling_reaches("\n".join(lines)):
            offenders.append(f"{path.relative_to(installed)}:{lineno}: {lines[lineno - 1].strip()}")
    assert not offenders, (
        f"{installed.name} reaches into another skill's directory; a release archive "
        "contains only this skill:\n  " + "\n  ".join(offenders)
    )


def test_documented_resource_paths_resolve(installed):
    """Every bundled path SKILL.md points the agent at has to be in the archive.

    Scoped to SKILL.md deliberately. It is the always-loaded body — the file that
    routes an agent to a script or a guide, so a dangling path there is a dead end
    at the moment of use. References are free to show paths belonging to *other*
    trees (update-docs/references/doc-structure.md sketches the layout of the docs
    skill it generates), and those are not claims about this archive's contents.
    """
    text = (installed / "SKILL.md").read_text(encoding="utf-8")
    sub = "(?:scripts|references|assets)"
    cited = set()
    # `references/guide.md` — a backticked path, the usual "load this" form.
    cited.update(m.group(1) for m in re.finditer(rf"`({sub}/[A-Za-z0-9_.-]+)`", text))
    # python "$SKILL/scripts/tool.py" — the runnable form, inside a fenced block.
    cited.update(m.group(1) for m in re.finditer(rf"\$\{{?SKILL\}}?/({sub}/[A-Za-z0-9_.-]+)", text))

    dangling = sorted(t for t in cited if not (installed / t).exists())
    assert not dangling, f"{installed.name}/SKILL.md cites bundled files that are not shipped: {dangling}"


def test_every_script_runs_from_a_foreign_cwd(installed, tmp_path):
    """Scripts are invoked as `python "$SKILL/scripts/x.py"` from the user's project.

    That means sibling imports (`from common import ...`) have to resolve off the
    script's own directory, and no script may need the skill dir to be the cwd.
    """
    scripts = sorted((installed / "scripts").glob("*.py")) if (installed / "scripts").is_dir() else []
    elsewhere = tmp_path / "some-users-project"
    elsewhere.mkdir()
    for script in scripts:
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True, text=True, timeout=120, cwd=elsewhere,
        )
        assert result.returncode == 0, (
            f"{installed.name}/scripts/{script.name} --help exited {result.returncode} "
            f"when run from outside the skill directory: {result.stderr[-600:]}"
        )


def test_django_simplifier_reporting_pipe_runs_without_python_simplifier(tmp_path):
    """The pipe django-simplifier's SKILL.md documents, run with no sibling present."""
    installed = install_alone("django-simplifier", tmp_path / "skills")
    project = tmp_path / "project"
    project.mkdir()
    (project / "manage.py").write_text("import django\n", encoding="utf-8")
    (project / "models.py").write_text(
        "from django.db import models\n\n\nclass Order(models.Model):\n"
        "    name = models.CharField(max_length=10, null=True)\n",
        encoding="utf-8",
    )

    analyze = subprocess.run(
        [sys.executable, str(installed / "scripts" / "analyze_django.py"), str(project), "--format", "json"],
        capture_output=True, text=True, timeout=300, cwd=project,
    )
    assert analyze.returncode == 0, analyze.stderr[-600:]

    report = subprocess.run(
        [sys.executable, str(installed / "scripts" / "format_findings.py"), "--format", "cards"],
        input=analyze.stdout, capture_output=True, text=True, timeout=120, cwd=project,
    )
    assert report.returncode == 0, report.stderr[-600:]
    assert report.stdout.strip(), "the documented reporting pipe produced no output"


def test_brutal_review_documents_a_fallback_for_its_optional_companion():
    """It may use python-simplifier when present, but must not require it."""
    text = (SKILLS_DIR / "brutal-review" / "SKILL.md").read_text(encoding="utf-8")
    assert "not installed" in text, (
        "brutal-review names python-simplifier's analyze_diff.py as an accelerator; "
        "it has to say what to do when that skill is absent"
    )
