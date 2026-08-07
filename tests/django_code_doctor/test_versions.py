"""Tests for the version table, version detection, and the upgrade detector.

The version work is the part of this skill that makes claims with dates on
them, so the tests concentrate on the two ways that goes wrong: a version
guessed from the wrong place, and a severity that does not follow from the
target. Both produce confident, wrong advice about whether a project is about
to break.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "django-code-doctor" / "scripts"


@pytest.fixture
def versions(load_module):
    return load_module(SCRIPTS_DIR, "django_versions")


@pytest.fixture
def detector(load_module):
    return load_module(SCRIPTS_DIR, "django_detect_version")


def build_project(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manage.py").write_text("import django\n", encoding="utf-8")
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def run_version_detector(target: Path, *extra: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_version_issues.py"), str(target), "--format", "json", *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert result.returncode == 0, result.stderr[:600]
    return json.loads(result.stdout)


def smells(findings):
    return {f["smell_type"] for f in findings}


def by_name(findings, name):
    return [f for f in findings if name in f["description"] or name in f["suggestion"]]


# ---- parse_version ----------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("5.2", (5, 2)),
    ("6.1.3", (6, 1)),
    (">=4.2,<5.0", (4, 2)),
    ("~=6.1.0", (6, 1)),
    ("== 5.2.3", (5, 2)),
    ("^5.0", (5, 0)),
    ("4", (4, 0)),
    ("", None),
    (None, None),
    ("not-a-version", None),
])
def test_parse_version_reads_what_manifests_actually_contain(versions, text, expected):
    assert versions.parse_version(text) == expected


def test_a_range_takes_the_lower_bound(versions):
    # <5.0 says nothing about whether the code is 4.0-shaped or 4.2-shaped;
    # the floor is the version the project promises to run on.
    assert versions.parse_version(">=4.2,<5.0") == (4, 2)


# ---- the table --------------------------------------------------------------- #

def test_every_change_has_a_known_match_kind(versions):
    for change in versions.CHANGES:
        assert change.match["kind"] in versions.MATCH_KINDS, change.name


def test_every_change_carries_at_least_one_version(versions):
    for change in versions.CHANGES:
        assert change.deprecated_in or change.removed_in, change.name


def test_every_change_names_a_replacement(versions):
    # A finding that says "this is going away" without saying what to write
    # instead is a chore, not a fix.
    for change in versions.CHANGES:
        assert change.replacement.strip(), change.name


def test_a_removal_never_precedes_its_deprecation(versions):
    for change in versions.CHANGES:
        if change.deprecated_in and change.removed_in:
            assert change.deprecated_in < change.removed_in, change.name


def test_change_names_are_unique(versions):
    names = [c.name for c in versions.CHANGES]
    assert len(names) == len(set(names))


def test_django_4_is_end_of_life_and_5_2_is_not(versions):
    assert versions.is_end_of_life((4, 2))
    assert versions.is_end_of_life((5, 1))
    assert not versions.is_end_of_life((5, 2))
    assert not versions.is_end_of_life((6, 1))


def test_an_unknown_version_is_not_called_end_of_life(versions):
    # An unreleased future version and an ancient one are both absent from the
    # table. Guessing in the alarming direction is how a finding stops being
    # believed.
    assert not versions.is_end_of_life(None)
    assert not versions.is_end_of_life((9, 0))


def test_describe_marks_the_lts_releases(versions):
    assert versions.describe((5, 2)) == "5.2 LTS"
    assert versions.describe((6, 1)) == "6.1"
    assert versions.describe(None) == "unknown"


# ---- version detection -------------------------------------------------------- #

def test_pep621_dependencies_are_read(detector, tmp_path):
    project = build_project(tmp_path / "p", {
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["Django>=5.2,<6.0", "celery"]\n',
    })
    version, source = detector.detect_django_version(project)
    assert version == (5, 2)
    assert "pyproject" in source


def test_poetry_dependencies_are_read(detector, tmp_path):
    project = build_project(tmp_path / "p", {
        "pyproject.toml": '[tool.poetry.dependencies]\npython = "^3.12"\ndjango = "^6.0"\n',
    })
    assert detector.detect_django_version(project)[0] == (6, 0)


def test_poetry_table_form_is_read(detector, tmp_path):
    project = build_project(tmp_path / "p", {
        "pyproject.toml": '[tool.poetry.dependencies]\ndjango = {version = "~5.2", extras = ["argon2"]}\n',
    })
    assert detector.detect_django_version(project)[0] == (5, 2)


def test_requirements_txt_is_read(detector, tmp_path):
    project = build_project(tmp_path / "p", {"requirements.txt": "celery==5.3\nDjango==4.2.11\npsycopg\n"})
    version, source = detector.detect_django_version(project)
    assert version == (4, 2)
    assert "requirements" in source


def test_a_commented_out_pin_is_ignored(detector, tmp_path):
    project = build_project(tmp_path / "p", {"requirements.txt": "# Django==4.2\nDjango==6.1\n"})
    assert detector.detect_django_version(project)[0] == (6, 1)


def test_a_package_merely_containing_django_is_not_the_pin(detector, tmp_path):
    # django-debug-toolbar is not Django, and reading its version as Django's
    # would date the project by several years in the wrong direction.
    project = build_project(tmp_path / "p", {
        "requirements.txt": "django-debug-toolbar==4.3\ndjango-extensions==3.2\nDjango==6.0\n",
    })
    assert detector.detect_django_version(project)[0] == (6, 0)


def test_an_unpinned_django_yields_no_version(detector):
    # An unpinned `Django` says nothing about the version. Detection falls
    # through to the installed environment; the manifest must not claim one.
    assert detector._from_requirements_text("Django\ncelery\n") is None


def test_the_extras_form_is_recognised(detector):
    assert detector._from_requirement_line("Django[argon2]>=5.2") == (5, 2)


def test_a_pipfile_is_read(detector, tmp_path):
    project = build_project(tmp_path / "p", {"Pipfile": '[packages]\ndjango = "==5.2.1"\n'})
    assert detector.detect_django_version(project)[0] == (5, 2)


def test_a_manifest_in_a_parent_directory_is_found(detector, tmp_path):
    # Detectors are routinely pointed at src/app/ while the pin sits at the root.
    root = tmp_path / "repo"
    build_project(root / "src" / "app", {})
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["Django==6.1"]\n', encoding="utf-8")
    assert detector.detect_django_version(root / "src" / "app")[0] == (6, 1)


def test_no_manifest_reports_why_rather_than_guessing(detector, tmp_path, monkeypatch):
    monkeypatch.setattr(detector, "_from_installed_environment", lambda: None)
    bare = tmp_path / "bare"
    bare.mkdir()
    version, source = detector.detect_django_version(bare)
    assert version is None
    assert "no Django pin found" in source


def test_the_installed_fallback_labels_itself_as_such(detector, tmp_path, monkeypatch):
    monkeypatch.setattr(detector, "_from_installed_environment", lambda: (6, 1))
    bare = tmp_path / "bare2"
    bare.mkdir()
    version, source = detector.detect_django_version(bare)
    assert version == (6, 1)
    assert "not a project pin" in source


# ---- find_version_issues ------------------------------------------------------ #

PINNED_42 = '[project]\nname = "x"\ndependencies = ["Django==4.2"]\n'

INDEX_TOGETHER = {
    "pyproject.toml": PINNED_42,
    "shop/models.py": "from django.db import models\n\n\n"
                      "class Thing(models.Model):\n"
                      "    name = models.CharField(max_length=10)\n\n"
                      "    class Meta:\n"
                      "        index_together = [['name']]\n",
}


def test_severity_follows_the_target_not_the_construct(tmp_path):
    project = build_project(tmp_path / "p", INDEX_TOGETHER)

    # Deprecated in 4.2, removed in 5.1. Staying on 4.2: a clock, not a wall.
    on_42 = run_version_detector(project, "--target", "4.2")
    deprecated = [f for f in on_42 if "index_together" in f["description"]]
    assert deprecated and deprecated[0]["severity"] == "medium"
    assert deprecated[0]["smell_type"] == "deprecated_api"

    # Targeting 5.2: the project does not start.
    on_52 = run_version_detector(project, "--target", "5.2")
    removed = [f for f in on_52 if "index_together" in f["description"]]
    assert removed and removed[0]["severity"] == "high"
    assert removed[0]["smell_type"] == "removed_in_target"


def test_the_replacement_is_named_in_the_suggestion(tmp_path):
    project = build_project(tmp_path / "p", INDEX_TOGETHER)
    findings = run_version_detector(project, "--target", "5.2")
    hit = [f for f in findings if "index_together" in f["description"]][0]
    assert "Meta.indexes" in hit["suggestion"]


def test_an_end_of_life_django_is_reported_once(tmp_path):
    project = build_project(tmp_path / "p", {"pyproject.toml": PINNED_42, "shop/models.py": "import django\n"})
    findings = run_version_detector(project)
    eol = [f for f in findings if f["smell_type"] == "django_end_of_life"]
    assert len(eol) == 1
    assert eol[0]["severity"] == "high"


def test_a_supported_django_is_not_reported_as_end_of_life(tmp_path):
    project = build_project(tmp_path / "p", {
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["Django==5.2"]\n',
        "shop/models.py": "import django\n",
    })
    assert "django_end_of_life" not in smells(run_version_detector(project))


def test_an_unknown_version_reports_the_gap_and_nothing_conditional(tmp_path):
    project = build_project(tmp_path / "p", {"shop/models.py": "import django\n"})
    findings = run_version_detector(project, "--from", "unknown")
    assert "django_version_unknown" in smells(findings)
    assert "django_end_of_life" not in smells(findings)


def test_a_removed_setting_is_found_in_a_settings_module(tmp_path):
    project = build_project(tmp_path / "p", {
        "pyproject.toml": PINNED_42,
        "config/settings.py": "import django\nUSE_L10N = True\n",
    })
    findings = run_version_detector(project, "--target", "5.2")
    assert [f for f in findings if "USE_L10N" in f["description"]]


def test_a_settings_name_used_as_a_local_variable_is_not_a_setting(tmp_path):
    # USE_L10N in application code is a local flag. Scoping the rule to settings
    # modules is what keeps this rule from firing on unrelated code.
    project = build_project(tmp_path / "p", {
        "pyproject.toml": PINNED_42,
        "shop/util.py": "import django\nUSE_L10N = True\n",
    })
    findings = run_version_detector(project, "--target", "5.2")
    assert not [f for f in findings if "USE_L10N" in f["description"]]


def test_a_removed_import_is_found(tmp_path):
    project = build_project(tmp_path / "p", {
        "pyproject.toml": PINNED_42,
        "shop/util.py": "from django.utils.timezone import utc\n",
    })
    findings = run_version_detector(project, "--target", "5.2")
    assert [f for f in findings if "timezone" in f["description"] or "utc" in f["description"]]


def test_a_removed_template_filter_is_found(tmp_path):
    project = build_project(tmp_path / "p", {
        "pyproject.toml": PINNED_42,
        "shop/templates/a.html": "{% if items|length_is:'4' %}yes{% endif %}\n",
    })
    findings = run_version_detector(project, "--target", "5.2")
    assert [f for f in findings if "length_is" in f["description"]]


def test_a_deprecated_kwarg_is_found(tmp_path):
    project = build_project(tmp_path / "p", {
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["Django==5.1"]\n',
        "shop/models.py": "from django.db import models\n\n"
                          "C = models.CheckConstraint(check=models.Q(x__gt=0), name='c')\n",
    })
    findings = run_version_detector(project, "--target", "6.0")
    hits = [f for f in findings if "CheckConstraint" in f["description"]]
    assert hits and hits[0]["severity"] == "high"


def test_modern_code_targeting_current_django_is_quiet(tmp_path):
    project = build_project(tmp_path / "p", {
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["Django==6.1"]\n',
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Thing(models.Model):\n"
                          "    name = models.CharField(max_length=10)\n\n"
                          "    class Meta:\n"
                          "        indexes = [models.Index(fields=['name'])]\n",
        "config/settings.py": "import django\n"
                              "STORAGES = {'default': {'BACKEND': 'x'}}\n",
    })
    assert run_version_detector(project, "--target", "6.1") == []


def test_list_known_reports_the_tables_high_water_mark(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_version_issues.py"), "--list-known"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert result.returncode == 0
    assert "6.1" in result.stdout


def test_targeting_past_what_the_table_knows_says_so(tmp_path):
    project = build_project(tmp_path / "p", {"pyproject.toml": PINNED_42, "shop/m.py": "import django\n"})
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_version_issues.py"), str(project),
         "--target", "9.0", "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert result.returncode == 0
    assert "does not know" in result.stderr or "9.0" in result.stderr


def test_it_stays_silent_outside_django(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("USE_L10N = True\n", encoding="utf-8")
    assert run_version_detector(plain) == []
