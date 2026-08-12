"""Tests for the aggregator and the shared project context.

The context is the piece the six detectors all rest on, so its inheritance
resolution gets pinned directly rather than only through the detectors.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "django-code-doctor" / "scripts"
PY_SIMPLIFIER = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


@pytest.fixture
def context_module(load_module):
    return load_module(SCRIPTS_DIR, "django_context")


def build_project(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manage.py").write_text("import django\n", encoding="utf-8")
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def run_aggregator(target: Path, *extra: str):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_django.py"), str(target), "--format", "json", *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert result.returncode == 0, result.stderr[:500]
    return json.loads(result.stdout)


def run_aggregator_text(target: Path):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_django.py"), str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert result.returncode == 0, result.stderr[:500]
    return result.stdout


PROJECT = {
    "shop/models.py": "from django.db import models\n\n\n"
                      "class Thing(models.Model):\n"
                      "    name = models.CharField(max_length=10, null=True)\n",
    "shop/views.py": "from django.shortcuts import get_object_or_404\n"
                     "from .models import Thing\n\n"
                     "def d(request, pk):\n"
                     "    return get_object_or_404(Thing, pk=pk)\n",
    "config/settings.py": "import django\nDEBUG = True\n",
    "shop/templates/a.html": "{{ x.items.count }}\n",
}


# ---- the project context ----------------------------------------------------- #

def test_a_model_is_recognised_through_a_chain_of_intermediate_classes(context_module, tmp_path):
    # Django models routinely inherit through two or three project classes. A
    # detector that only checks direct bases misses every one of them.
    project = build_project(tmp_path / "proj", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class A(models.Model):\n"
                          "    class Meta:\n        abstract = True\n\n\n"
                          "class B(A):\n"
                          "    class Meta:\n        abstract = True\n\n\n"
                          "class C(B):\n    pass\n",
    })
    ctx = context_module.build_context(project)
    assert set(ctx.models) == {"A", "B", "C"}
    assert ctx.is_abstract("A") and ctx.is_abstract("B")
    assert not ctx.is_abstract("C")
    assert ctx.concrete_subclasses("B") == ["C"]


def test_a_class_defined_twice_is_marked_ambiguous(context_module, tmp_path):
    # Inheritance resolves by name, so two `Order` classes make every base named
    # `Order` unresolvable. Detectors skip ambiguous names rather than guess.
    project = build_project(tmp_path / "proj", {
        "a/models.py": "from django.db import models\n\n\nclass Order(models.Model):\n    pass\n",
        "b/models.py": "from django.db import models\n\n\nclass Order(models.Model):\n    pass\n",
    })
    ctx = context_module.build_context(project)
    assert ctx.is_ambiguous("Order")


def test_inheritance_cycles_do_not_hang_the_walk(context_module, tmp_path):
    project = build_project(tmp_path / "proj", {
        "shop/models.py": "import django\n\n\nclass A(B):\n    pass\n\n\nclass B(A):\n    pass\n",
    })
    ctx = context_module.build_context(project)
    # Terminating at all is the point. ancestors() is strict, so A is not its own.
    assert ctx.ancestors("A") == {"B"}
    assert ctx.ancestors("B") == {"A"}


def test_a_tree_without_django_has_no_context(context_module, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("class Thing:\n    pass\n", encoding="utf-8")
    assert context_module.build_context(plain, quiet=True) is None


def test_an_installed_app_without_manage_py_is_still_django(context_module, tmp_path):
    # A reusable app or a library has no manage.py; its django imports are the tell.
    app = tmp_path / "app"
    app.mkdir()
    (app / "models.py").write_text("from django.db import models\n\n\n"
                                   "class T(models.Model):\n    pass\n", encoding="utf-8")
    ctx = context_module.build_context(app, quiet=True)
    assert ctx is not None and set(ctx.models) == {"T"}


def test_unparseable_files_are_skipped_without_losing_the_rest(context_module, tmp_path):
    project = build_project(tmp_path / "proj", {
        "shop/broken.py": "def (:\n",
        "shop/models.py": "from django.db import models\n\n\nclass Good(models.Model):\n    pass\n",
    })
    ctx = context_module.build_context(project)
    assert "Good" in ctx.models


# ---- the aggregator ---------------------------------------------------------- #

def test_the_aggregator_merges_every_category(tmp_path):
    findings = run_aggregator(build_project(tmp_path / "proj", PROJECT))
    smells = {f["smell_type"] for f in findings}
    assert {"debug_true", "missing_ownership_filter", "null_on_text_field",
            "query_in_template"} <= smells


def test_skip_drops_a_whole_category(tmp_path):
    project = build_project(tmp_path / "proj", PROJECT)
    smells = {f["smell_type"] for f in run_aggregator(project, "--skip", "security,templates")}
    assert "debug_true" not in smells
    assert "query_in_template" not in smells
    assert "missing_ownership_filter" in smells   # other categories still ran


def test_ignore_drops_one_smell_across_every_category(tmp_path):
    project = build_project(tmp_path / "proj", PROJECT)
    smells = {f["smell_type"] for f in run_aggregator(project, "--ignore", "debug_true")}
    assert "debug_true" not in smells
    assert smells


def test_findings_are_ordered_worst_first(tmp_path):
    findings = run_aggregator(build_project(tmp_path / "proj", PROJECT))
    rank = {"high": 0, "medium": 1, "low": 2}
    severities = [rank[f["severity"]] for f in findings]
    assert severities == sorted(severities)


def test_text_output_does_not_call_relation_walk_candidates_findings(tmp_path):
    project = build_project(tmp_path / "proj", {
        "shop/templates/a.html":
            "{% for obj in objects %}{{ obj.owner.profile.name }}{% endfor %}\n",
    })
    output = run_aggregator_text(project)
    assert "1 candidate(s)" in output
    assert "[CANDIDATE]" in output


def test_output_feeds_python_code_doctors_formatter(tmp_path):
    # The reason every detector shares one findings shape: the other skill's
    # reporting tools have to be able to read this one's output.
    project = build_project(tmp_path / "proj", PROJECT)
    findings = run_aggregator(project)
    formatted = subprocess.run(
        [sys.executable, str(PY_SIMPLIFIER / "format_findings.py"), "--format", "json"],
        input=json.dumps(findings), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300)
    assert formatted.returncode == 0, formatted.stderr[:400]
    assert json.loads(formatted.stdout)


def test_a_non_django_tree_yields_an_empty_report_not_a_crash(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("DEBUG = True\n", encoding="utf-8")
    assert run_aggregator(plain) == []
