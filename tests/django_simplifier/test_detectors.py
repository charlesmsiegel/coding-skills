"""Smoke tests: every Django detector fires on a known-bad fixture and stays quiet on good code.

Fixtures are written to tmp_path at runtime rather than committed, so the
deliberately-bad Django code never trips the repo's own linters or this skill's
own detectors.

The negative cases matter at least as much as the positive ones here. A Django
detector that fires on correct code is worse than no detector: it trains people
to skip the output, and the real N+1 goes out with it.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "django-simplifier" / "scripts"


def build_project(root: Path, files: dict[str, str]) -> Path:
    """Write a Django-shaped project (manage.py plus the given files)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "manage.py").write_text("import django\n", encoding="utf-8")
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def run_detector(script: str, target: Path, *extra: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json", *extra],
        # The detectors warn on stderr through the console encoding, which is cp1252
        # on Windows — decode leniently so a warning cannot fail an assertion about
        # stdout, which is always JSON.
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[:500]}"
    return json.loads(result.stdout)


def smells(findings: list[dict]) -> set[str]:
    return {f["smell_type"] for f in findings}


MODELS = """\
from django.db import models


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class Customer(TimeStamped):
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class Order(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="orders")
    stock = models.IntegerField(default=0)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return str(self.pk)
"""


# (detector, {path: content}, smells that must fire)
CASES = [
    (
        "find_query_issues.py",
        {"shop/models.py": MODELS,
         "shop/views.py": "from .models import Order\n\n"
                          "def go():\n"
                          "    for order in Order.objects.all():\n"
                          "        print(order.customer)\n"},
        {"n_plus_one_query"},
    ),
    (
        "find_query_issues.py",
        {"shop/models.py": MODELS,
         "shop/views.py": "from .models import Order\n\n"
                          "def go(rows):\n"
                          "    for row in Order.objects.all():\n"
                          "        row.save()\n"},
        {"save_in_loop"},
    ),
    (
        "find_query_issues.py",
        {"shop/models.py": MODELS,
         "shop/views.py": "from .models import Order\n\n"
                          "def go(n):\n"
                          "    Order.objects.filter(pk=1).update(stock=n - 1)\n"},
        {"update_without_f"},
    ),
    (
        "find_model_issues.py",
        {"shop/models.py": "from django.db import models\n\n\n"
                           "class Thing(models.Model):\n"
                           "    name = models.CharField(max_length=10, null=True)\n"
                           "    owner = models.ForeignKey('Thing')\n"},
        {"missing_str_method", "null_on_text_field", "missing_related_name",
         "missing_on_delete", "no_default_ordering"},
    ),
    (
        "find_view_issues.py",
        {"shop/views.py": "from django.shortcuts import get_object_or_404\n"
                          "from .models import Order\n\n"
                          "def detail(request, pk):\n"
                          "    return get_object_or_404(Order, pk=pk)\n"},
        {"missing_ownership_filter"},
    ),
    (
        "find_view_issues.py",
        {"shop/urls.py": "from django.urls import path\n"
                         "from django.db import models\n\n"
                         "urlpatterns = [path('a/', None)]\n"},
        {"url_without_name"},
    ),
    (
        "find_django_security.py",
        {"config/settings.py": "import django\n"
                               "DEBUG = True\n"
                               "SECRET_KEY = 'literal-secret'\n"
                               "ALLOWED_HOSTS = ['*']\n"},
        {"debug_true", "hardcoded_secret", "wildcard_allowed_hosts"},
    ),
    (
        "find_django_security.py",
        {"shop/render.py": "from django.utils.safestring import mark_safe\n\n"
                           "def show(value):\n"
                           "    return mark_safe('<b>' + value + '</b>')\n"},
        {"mark_safe_on_dynamic_value"},
    ),
    (
        "find_django_overengineering.py",
        {"shop/models.py": "from django.db import models\n\n\n"
                           "class Orphan(models.Model):\n"
                           "    x = models.IntegerField()\n\n"
                           "    class Meta:\n"
                           "        abstract = True\n"},
        {"unused_abstract_model"},
    ),
    (
        "find_django_overengineering.py",
        {"shop/mixins.py": "import django\n\n\n"
                           "class AuditMixin:\n"
                           "    def audit(self):\n"
                           "        return True\n\n\n"
                           "class Only(AuditMixin):\n"
                           "    pass\n"},
        {"single_use_mixin"},
    ),
    (
        "find_django_overengineering.py",
        {"shop/signals.py": "from django.db.models.signals import post_save\n"
                            "from django.dispatch import receiver\n\n\n"
                            "@receiver(post_save, sender=None)\n"
                            "def touch(sender, instance, **kwargs):\n"
                            "    instance.seen = True\n"},
        {"save_signal_for_simple_logic"},
    ),
    (
        "find_template_issues.py",
        {"shop/models.py": MODELS,
         "shop/templates/shop/a.html": "{% for order in orders %}\n"
                                       "  {{ order.customer.name }}\n"
                                       "{% endfor %}\n"},
        {"relation_walk_in_loop"},
    ),
    (
        "find_template_issues.py",
        {"shop/models.py": MODELS,
         "shop/templates/shop/b.html": "{{ order.items.count }}\n"},
        {"query_in_template"},
    ),
]


@pytest.mark.parametrize("script,files,expected", CASES,
                         ids=[f"{c[0]}-{sorted(c[2])[0]}" for c in CASES])
def test_detector_fires_on_its_fixture(tmp_path, script, files, expected):
    project = build_project(tmp_path / "proj", files)
    assert expected <= smells(run_detector(script, project))


# ---- the negative cases ----------------------------------------------------- #

def test_prefetched_loops_are_not_reported_as_n_plus_one(tmp_path):
    project = build_project(tmp_path / "proj", {
        "shop/models.py": MODELS,
        "shop/views.py": "from .models import Order\n\n"
                         "def go():\n"
                         "    for order in Order.objects.select_related('customer'):\n"
                         "        print(order.customer)\n",
    })
    assert "n_plus_one_query" not in smells(run_detector("find_query_issues.py", project))


def test_a_well_formed_model_produces_nothing(tmp_path):
    project = build_project(tmp_path / "proj", {"shop/models.py": MODELS})
    assert run_detector("find_model_issues.py", project) == []


def test_abstract_models_are_not_asked_for_str_or_ordering(tmp_path):
    # An abstract model is never instantiated or queried, so neither finding applies.
    project = build_project(tmp_path / "proj", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Base(models.Model):\n"
                          "    x = models.IntegerField()\n\n"
                          "    class Meta:\n"
                          "        abstract = True\n\n\n"
                          "class Real(Base):\n"
                          "    class Meta:\n"
                          "        ordering = ['pk']\n\n"
                          "    def __str__(self):\n"
                          "        return 'r'\n",
    })
    assert smells(run_detector("find_model_issues.py", project)) == set()


def test_a_view_that_scopes_to_the_user_is_not_flagged(tmp_path):
    project = build_project(tmp_path / "proj", {
        "shop/views.py": "from django.shortcuts import get_object_or_404\n"
                         "from .models import Order\n\n"
                         "def detail(request, pk):\n"
                         "    return get_object_or_404(Order, pk=pk, owner=request.user)\n",
    })
    assert "missing_ownership_filter" not in smells(run_detector("find_view_issues.py", project))


def test_mark_safe_on_a_literal_is_fine(tmp_path):
    project = build_project(tmp_path / "proj", {
        "shop/render.py": "from django.utils.safestring import mark_safe\n\n"
                          "BADGE = mark_safe('<b>ok</b>')\n",
    })
    assert run_detector("find_django_security.py", project) == []


def test_debug_true_outside_settings_is_not_a_django_setting(tmp_path):
    # A module-level DEBUG in application code is a local flag, not the setting.
    project = build_project(tmp_path / "proj", {
        "shop/util.py": "import django\n\nDEBUG = True\n",
    })
    assert "debug_true" not in smells(run_detector("find_django_security.py", project))


def test_an_abstract_model_with_two_children_is_earning_its_keep(tmp_path):
    project = build_project(tmp_path / "proj", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Base(models.Model):\n"
                          "    class Meta:\n"
                          "        abstract = True\n\n\n"
                          "class A(Base):\n    pass\n\n\n"
                          "class B(Base):\n    pass\n",
    })
    found = smells(run_detector("find_django_overengineering.py", project))
    assert "single_impl_abstract_model" not in found
    assert "unused_abstract_model" not in found


# ---- the gate ---------------------------------------------------------------- #

def test_detectors_stay_silent_on_a_tree_that_is_not_django(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text(
        "class UserService:\n"
        "    def get(self):\n"
        "        return 1\n"
        "\n"
        "DEBUG = True\n",
        encoding="utf-8")

    for script in ("find_model_issues.py", "find_django_security.py",
                   "find_django_overengineering.py", "find_query_issues.py"):
        assert run_detector(script, plain) == [], f"{script} fired on non-Django code"


def test_the_gate_says_why_it_reported_nothing(tmp_path):
    # Silence has to be distinguishable from a clean bill of health.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "app.py").write_text("x = 1\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_model_issues.py"), str(plain), "--format", "json"],
        # errors="replace" because the console encoding, not the script, decides how
        # the message's punctuation lands — on Windows stderr is cp1252, not UTF-8.
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert "no Django project found" in result.stderr


# ---- shared contracts -------------------------------------------------------- #

DETECTORS = ["find_query_issues.py", "find_model_issues.py", "find_view_issues.py",
             "find_django_security.py", "find_django_overengineering.py",
             "find_template_issues.py"]


# One project that gives all six detectors something to say, so the shared-contract
# tests below exercise every detector rather than skipping the quiet ones.
EVERY_DETECTOR_FIRES = {
    "shop/models.py": "from django.db import models\n\n\n"
                      "class Orphan(models.Model):\n"
                      "    class Meta:\n"
                      "        abstract = True\n\n\n"
                      "class Thing(models.Model):\n"
                      "    name = models.CharField(max_length=10, null=True)\n"
                      "    owner = models.ForeignKey('Thing', on_delete=models.CASCADE)\n",
    "shop/views.py": "from django.shortcuts import get_object_or_404\n"
                     "from .models import Thing\n\n"
                     "def d(request, pk):\n"
                     "    for thing in Thing.objects.all():\n"
                     "        thing.save()\n"
                     "    return get_object_or_404(Thing, pk=pk)\n",
    "config/settings.py": "import django\nDEBUG = True\n",
    "shop/templates/a.html": "{{ x.items.count }}\n",
}


@pytest.mark.parametrize("script", DETECTORS)
def test_every_detector_speaks_the_shared_findings_shape(tmp_path, script):
    project = build_project(tmp_path / "proj", EVERY_DETECTOR_FIRES)
    assert run_detector(script, project), f"{script} found nothing; the contract is untested"
    for f in run_detector(script, project):
        assert {"file", "line", "smell_type", "description", "suggestion", "severity"} <= f.keys()
        assert f["severity"] in ("high", "medium", "low")
        assert isinstance(f["line"], int) and f["line"] >= 1


@pytest.mark.parametrize("script", DETECTORS)
def test_ignore_drops_the_named_smell(tmp_path, script):
    project = build_project(tmp_path / "proj", EVERY_DETECTOR_FIRES)
    found = smells(run_detector(script, project))
    if not found:
        pytest.skip(f"{script} has nothing to ignore on this fixture")
    victim = sorted(found)[0]
    assert victim not in smells(run_detector(script, project, "--ignore", victim))
