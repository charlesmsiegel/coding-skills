"""Tests for the aggregator, the diff lens, and the external-tool runner."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import SCRIPTS_DIR, build_project

PY_SIMPLIFIER = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


def run(script, *args):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), *[str(a) for a in args]],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=420)
    assert result.returncode == 0, result.stderr[:600]
    return result


def run_json(script, *args):
    return json.loads(run(script, *args, "--format", "json").stdout)


# A project that gives many detectors something to say.
BUSY_PROJECT = {
    "pyproject.toml": '[project]\nname = "x"\ndependencies = ["Django==4.2"]\n',
    "shop/models.py": "from django.db import models\n\n\n"
                      "class Thing(models.Model):\n"
                      "    name = models.CharField(max_length=10, null=True)\n"
                      "    owner = models.ForeignKey('Thing', on_delete=models.CASCADE)\n\n"
                      "    class Meta:\n"
                      "        index_together = [['name']]\n",
    "shop/views.py": "from django.shortcuts import get_object_or_404\n"
                     "from .models import Thing\n\n"
                     "def detail(request, pk):\n"
                     "    for thing in Thing.objects.all():\n"
                     "        thing.save()\n"
                     "    return get_object_or_404(Thing, pk=pk)\n",
    "config/settings.py": "import django\nDEBUG = True\n",
    "shop/templates/a.html": "{{ x.items.count }}\n",
    "shop/migrations/0001_initial.py":
        "from django.db import migrations\n\n\n"
        "def forwards(apps, schema_editor):\n"
        "    pass\n\n\n"
        "class Migration(migrations.Migration):\n"
        "    dependencies = []\n"
        "    operations = [migrations.RunPython(forwards)]\n",
    # The remaining detectors each need something to say, so that the shared
    # contract tests below exercise all fifteen rather than skipping the quiet
    # ones. A contract that is only tested on ten detectors is not tested.
    "shop/forms.py": "from django import forms\n"
                     "from .models import Thing\n\n\n"
                     "class ThingForm(forms.ModelForm):\n"
                     "    class Meta:\n"
                     "        model = Thing\n"
                     "        fields = '__all__'\n",
    "shop/admin.py": "from django.contrib import admin\n"
                     "from .models import Thing\n\n\n"
                     "@admin.register(Thing)\n"
                     "class ThingAdmin(admin.ModelAdmin):\n"
                     "    list_display = ['name', 'owner']\n",
    "shop/api.py": "from rest_framework.viewsets import ModelViewSet\n"
                   "from .models import Thing\n\n\n"
                   "class ThingViewSet(ModelViewSet):\n"
                   "    queryset = Thing.objects.all()\n",
    "shop/jobs.py": "from .models import Thing\n\n"
                    "async def fetch(pk):\n"
                    "    return Thing.objects.get(pk=pk)\n",
    "shop/money.py": "import requests\n"
                     "from django.db import transaction\n\n"
                     "def pay(thing):\n"
                     "    with transaction.atomic():\n"
                     "        requests.post('https://example.test/charge')\n"
                     "        thing.save()\n",
    "shop/tests.py": "from django.test import TestCase\n\n\n"
                     "class ThingTests(TestCase):\n"
                     "    def test_ok(self):\n"
                     "        self.assertTrue(True)\n",
    "shop/mixins.py": "import django\n\n\n"
                      "class AuditMixin:\n"
                      "    def audit(self):\n"
                      "        return True\n",
}

ALL_CATEGORIES = ["query", "model", "view", "security", "overengineering", "templates",
                  "forms", "admin", "drf", "migrations", "settings", "async",
                  "transactions", "tests", "version"]


# ---- the aggregator ------------------------------------------------------------- #

def test_every_category_is_registered(tmp_path):
    project = build_project(tmp_path / "p", BUSY_PROJECT)
    output = run("analyze_django.py", project).stdout
    for category in ALL_CATEGORIES:
        assert category in output, category + " is not in the report header"


def test_the_report_names_the_detected_django_version(tmp_path):
    project = build_project(tmp_path / "p", BUSY_PROJECT)
    output = run("analyze_django.py", project).stdout
    assert "Django 4.2 LTS" in output
    assert "pyproject.toml" in output


def test_the_aggregator_merges_findings_from_new_and_old_categories(tmp_path):
    project = build_project(tmp_path / "p", BUSY_PROJECT)
    smells = {f["smell_type"] for f in run_json("analyze_django.py", project)}
    assert "debug_true" in smells                      # security (existing)
    assert "save_in_loop" in smells                    # query (existing)
    assert "run_python_without_reverse" in smells      # migrations (new)
    assert "default_user_model" in smells              # settings (new)
    assert "django_end_of_life" in smells              # version (new)


def test_skip_drops_a_new_category(tmp_path):
    project = build_project(tmp_path / "p", BUSY_PROJECT)
    smells = {f["smell_type"] for f in run_json("analyze_django.py", project,
                                                "--skip", "migrations,version")}
    assert "run_python_without_reverse" not in smells
    assert "django_end_of_life" not in smells
    assert "debug_true" in smells                      # other categories still ran


def test_the_target_version_changes_what_the_version_sweep_reports(tmp_path):
    project = build_project(tmp_path / "p", BUSY_PROJECT)

    on_42 = run_json("analyze_django.py", project, "--target-version", "4.2")
    index_together = [f for f in on_42 if "index_together" in f["description"]]
    assert index_together and index_together[0]["severity"] == "medium"

    on_52 = run_json("analyze_django.py", project, "--target-version", "5.2")
    index_together = [f for f in on_52 if "index_together" in f["description"]]
    assert index_together and index_together[0]["severity"] == "high"


def test_findings_are_ordered_worst_first(tmp_path):
    project = build_project(tmp_path / "p", BUSY_PROJECT)
    rank = {"high": 0, "medium": 1, "low": 2}
    severities = [rank[f["severity"]] for f in run_json("analyze_django.py", project)]
    assert severities == sorted(severities)


def test_output_still_feeds_python_code_doctors_formatter(tmp_path):
    # The reason every detector shares one findings shape.
    project = build_project(tmp_path / "p", BUSY_PROJECT)
    findings = run_json("analyze_django.py", project)
    formatted = subprocess.run(
        [sys.executable, str(PY_SIMPLIFIER / "format_findings.py"), "--format", "json"],
        input=json.dumps(findings), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300)
    assert formatted.returncode == 0, formatted.stderr[:400]
    assert json.loads(formatted.stdout)


def test_a_non_django_tree_yields_an_empty_report(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("DEBUG = True\n", encoding="utf-8")
    assert run_json("analyze_django.py", plain) == []


# ---- shared contract across all fifteen ------------------------------------------- #

DETECTORS = ["find_query_issues.py", "find_model_issues.py", "find_view_issues.py",
             "find_django_security.py", "find_django_overengineering.py",
             "find_template_issues.py", "find_form_issues.py", "find_admin_issues.py",
             "find_drf_issues.py", "find_migration_issues.py", "find_settings_issues.py",
             "find_async_issues.py", "find_transaction_issues.py", "find_test_issues.py",
             "find_version_issues.py"]


@pytest.mark.parametrize("script", DETECTORS)
def test_every_detector_speaks_the_shared_findings_shape(tmp_path, script):
    project = build_project(tmp_path / "p", BUSY_PROJECT)
    for f in run_json(script, project):
        assert {"file", "line", "smell_type", "description", "suggestion", "severity"} <= f.keys()
        assert f["severity"] in ("high", "medium", "low")
        assert isinstance(f["line"], int) and f["line"] >= 1


@pytest.mark.parametrize("script", DETECTORS)
def test_every_detector_accepts_ignore(tmp_path, script):
    project = build_project(tmp_path / "p", BUSY_PROJECT)
    found = {f["smell_type"] for f in run_json(script, project)}
    if not found:
        pytest.skip(script + " has nothing to ignore on this fixture")
    victim = sorted(found)[0]
    assert victim not in {f["smell_type"] for f in run_json(script, project, "--ignore", victim)}


@pytest.mark.parametrize("script", DETECTORS)
def test_every_detector_stays_silent_outside_django(tmp_path, script):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text(
        "class UserService:\n    def get(self):\n        return 1\n\nDEBUG = True\n",
        encoding="utf-8")
    assert run_json(script, plain) == [], script + " fired on non-Django code"


# ---- the diff lens ------------------------------------------------------------------ #

@pytest.fixture
def git_project(tmp_path):
    """A Django project in a git repo, with one commit as the base."""
    root = build_project(tmp_path / "repo", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Customer(models.Model):\n"
                          "    name = models.CharField(max_length=10)\n\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return self.name\n\n\n"
                          "class Order(models.Model):\n"
                          "    customer = models.ForeignKey(Customer, on_delete=models.CASCADE,\n"
                          "                                 related_name='orders')\n\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return 'o'\n",
        "shop/old.py": "from .models import Order\n\n"
                       "def legacy():\n"
                       "    for order in Order.objects.all():\n"
                       "        print(order.customer)\n",
    })
    for args in (["init", "-q"], ["config", "user.email", "t@t.co"],
                 ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"],
                 ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=120)
    return root


def test_the_diff_lens_reports_only_changed_lines(git_project):
    # An N+1 already existed in old.py; a new one is added in new.py.
    (git_project / "shop" / "new.py").write_text(
        "from .models import Order\n\n"
        "def added():\n"
        "    for order in Order.objects.all():\n"
        "        print(order.customer)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(git_project), "add", "-A"], capture_output=True, timeout=120)

    findings = run_json("analyze_diff.py", "--path", git_project)
    files = {Path(f["file"]).name for f in findings}
    assert "new.py" in files
    assert "old.py" not in files, "the diff lens reported untouched legacy code"


def test_the_diff_lens_names_what_it_did_not_run(git_project):
    output = run("analyze_diff.py", "--path", git_project).stdout
    # Silence about a whole-tree category must not read as a clean bill of health.
    assert "not run" in output
    assert "overengineering" in output
    assert "version" in output


# ---- external tools -------------------------------------------------------------- #

def test_missing_tools_are_reported_rather_than_installed(tmp_path):
    project = build_project(tmp_path / "p", BUSY_PROJECT)
    report = run_json("run_external_tools.py", project, "--tools", "djlint,mypy,bandit,pip-audit")
    assert set(report) == {"tools_run", "missing_tools", "findings"}
    # Whatever is or is not installed here, every named tool is accounted for.
    accounted = set(report["missing_tools"]) | {t.split()[0] for t in report["tools_run"]}
    assert {"djlint", "mypy", "bandit", "pip-audit"} <= accounted


def test_every_missing_tool_carries_an_install_hint(tmp_path):
    project = build_project(tmp_path / "p", BUSY_PROJECT)
    report = run_json("run_external_tools.py", project)
    for name, hint in report["missing_tools"].items():
        assert hint.strip(), name + " is listed as missing with no install hint"


def test_django_upgrade_does_not_rewrite_without_fix(tmp_path):
    project = build_project(tmp_path / "p", BUSY_PROJECT)
    before = (project / "shop" / "models.py").read_text(encoding="utf-8")
    run_json("run_external_tools.py", project, "--tools", "django-upgrade")
    assert (project / "shop" / "models.py").read_text(encoding="utf-8") == before


def test_the_migrations_check_is_opt_in(tmp_path):
    # It imports the project and its settings, so it must not run by default.
    project = build_project(tmp_path / "p", BUSY_PROJECT)
    report = run_json("run_external_tools.py", project)
    assert not any("makemigrations" in t for t in report["tools_run"])
