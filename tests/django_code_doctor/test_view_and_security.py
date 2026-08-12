"""Tests for the hardened view and security detectors.

Two things get pinned here that are easy to get wrong in the alarming
direction: a settings rule must not fire on a name that merely looks like a
setting, and a version-gated finding (missing_csp) must stay silent on a
version where the setting does not exist.
"""

from helpers import build_project, run_detector, severities, smells

SETTINGS_HEAD = "import django\nfrom pathlib import Path\nBASE_DIR = Path(__file__)\n"


# ---- views: the new checks ---------------------------------------------------- #

def test_csrf_exempt_is_high_severity(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.views.decorators.csrf import csrf_exempt\n\n"
                         "@csrf_exempt\n"
                         "def hook(request):\n"
                         "    return None\n",
    })
    assert severities(run_detector("find_view_issues.py", project), "csrf_exempt") == ["high"]


def test_an_open_redirect_from_next_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.shortcuts import redirect\n\n"
                         "def after_login(request):\n"
                         "    return redirect(request.GET.get('next'))\n",
    })
    assert "open_redirect" in smells(run_detector("find_view_issues.py", project))


def test_a_validated_redirect_is_not_reported(tmp_path):
    # The validation sits several lines above the redirect, which is why the
    # check has to look at the whole function rather than the redirect line.
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.shortcuts import redirect\n"
                         "from django.utils.http import url_has_allowed_host_and_scheme\n\n"
                         "def after_login(request):\n"
                         "    target = request.GET.get('next')\n"
                         "    if not url_has_allowed_host_and_scheme(target, {request.get_host()}):\n"
                         "        target = '/'\n"
                         "    return redirect(target)\n",
    })
    assert "open_redirect" not in smells(run_detector("find_view_issues.py", project))


def test_expanding_request_data_into_filter_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Order(models.Model):\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return 'o'\n",
        "shop/views.py": "from .models import Order\n\n"
                         "def search(request):\n"
                         "    return Order.objects.filter(**request.GET.dict())\n",
    })
    assert "unfiltered_user_input_lookup" in smells(run_detector("find_view_issues.py", project))


def test_an_allow_listed_filter_is_not_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Order(models.Model):\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return 'o'\n",
        "shop/views.py": "from .models import Order\n\n"
                         "ALLOWED = {'status'}\n\n"
                         "def search(request):\n"
                         "    terms = {k: v for k, v in request.GET.items() if k in ALLOWED}\n"
                         "    return Order.objects.filter(**terms)\n",
    })
    assert "unfiltered_user_input_lookup" not in smells(run_detector("find_view_issues.py", project))


def test_a_class_queryset_with_no_scoping_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.views.generic import ListView\n"
                         "from .models import Order\n\n\n"
                         "class OrderList(ListView):\n"
                         "    queryset = Order.objects.all()\n",
    })
    findings = run_detector("find_view_issues.py", project)
    hits = [f for f in findings if f["smell_type"] == "unscoped_get_queryset"]
    assert hits and hits[0]["severity"] == "high"


def test_a_get_queryset_that_scopes_to_the_user_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.views.generic import ListView\n"
                         "from .models import Order\n\n\n"
                         "class OrderList(ListView):\n"
                         "    def get_queryset(self):\n"
                         "        return Order.objects.filter(owner=self.request.user)\n",
    })
    assert "unscoped_get_queryset" not in smells(run_detector("find_view_issues.py", project))


def test_a_view_overriding_many_hooks_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.views.generic import UpdateView\n\n\n"
                         "class Thing(UpdateView):\n"
                         "    def get_queryset(self):\n        return None\n"
                         "    def get_context_data(self, **kw):\n        return kw\n"
                         "    def get_form_kwargs(self):\n        return {}\n"
                         "    def form_valid(self, form):\n        return None\n"
                         "    def get_success_url(self):\n        return '/'\n"
                         "    def dispatch(self, request, *a, **kw):\n        return None\n",
    })
    assert "cbv_hook_overload" in smells(run_detector("find_view_issues.py", project))


def test_a_view_overriding_two_hooks_is_idiomatic(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.views.generic import ListView\n\n\n"
                         "class Thing(ListView):\n"
                         "    def get_queryset(self):\n"
                         "        return self.request.user.orders.all()\n"
                         "    def get_context_data(self, **kw):\n        return kw\n",
    })
    assert "cbv_hook_overload" not in smells(run_detector("find_view_issues.py", project))


def test_an_unauthenticated_write_view_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from .models import Order\n\n"
                         "def make(request):\n"
                         "    Order.objects.create(total=1)\n"
                         "    return None\n",
    })
    assert "unauthenticated_mutation" in smells(run_detector("find_view_issues.py", project))


def test_a_login_required_write_view_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.contrib.auth.decorators import login_required\n"
                         "from .models import Order\n\n"
                         "@login_required\n"
                         "def make(request):\n"
                         "    Order.objects.create(total=1)\n"
                         "    return None\n",
    })
    assert "unauthenticated_mutation" not in smells(run_detector("find_view_issues.py", project))


# ---- security: the new checks -------------------------------------------------- #

def test_an_empty_password_validator_list_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "config/settings.py": SETTINGS_HEAD + "AUTH_PASSWORD_VALIDATORS = []\n",
    })
    assert severities(run_detector("find_django_security.py", project),
                      "no_password_validators") == ["high"]


def test_a_weak_password_hasher_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "config/settings.py": SETTINGS_HEAD +
                              "PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']\n",
    })
    assert "weak_password_hasher" in smells(run_detector("find_django_security.py", project))


def test_a_loosened_frame_options_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "config/settings.py": SETTINGS_HEAD + "X_FRAME_OPTIONS = 'ALLOWALL'\n",
    })
    assert "weak_frame_options" in smells(run_detector("find_django_security.py", project))


def test_deny_frame_options_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "config/settings.py": SETTINGS_HEAD + "X_FRAME_OPTIONS = 'DENY'\n",
    })
    assert "weak_frame_options" not in smells(run_detector("find_django_security.py", project))


def test_missing_hsts_is_reported_once_not_per_settings_file(tmp_path):
    project = build_project(tmp_path / "p", {
        "config/settings/base.py": SETTINGS_HEAD,
        "config/settings/production.py": SETTINGS_HEAD + "DEBUG = False\n",
    })
    hits = [f for f in run_detector("find_django_security.py", project)
            if f["smell_type"] == "missing_hsts"]
    assert len(hits) == 1


def test_a_setting_defined_in_the_base_module_counts_as_set(tmp_path):
    # Split settings are the norm. Asking "is this set in THIS file" rather than
    # "anywhere" would report every split-settings project.
    project = build_project(tmp_path / "p", {
        "config/settings/base.py": SETTINGS_HEAD + "SECURE_HSTS_SECONDS = 3600\n"
                                                   "SECURE_SSL_REDIRECT = True\n",
        "config/settings/production.py": "from .base import *\nDEBUG = False\n",
    })
    found = smells(run_detector("find_django_security.py", project))
    assert "missing_hsts" not in found
    assert "missing_ssl_redirect" not in found


def test_debug_true_in_a_dev_module_is_ranked_below_production(tmp_path):
    # DEBUG=True in dev.py is correct. Reporting it at high severity is how a
    # security report stops being read.
    project = build_project(tmp_path / "p", {
        "config/settings/dev.py": SETTINGS_HEAD + "DEBUG = True\n",
        "config/settings/production.py": SETTINGS_HEAD + "DEBUG = False\n",
    })
    assert severities(run_detector("find_django_security.py", project), "debug_true") == ["low"]


def test_debug_true_in_the_only_settings_module_is_high(tmp_path):
    project = build_project(tmp_path / "p", {"config/settings.py": SETTINGS_HEAD + "DEBUG = True\n"})
    assert severities(run_detector("find_django_security.py", project), "debug_true") == ["high"]


def test_the_safe_filter_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": "<div>{{ comment.body|safe }}</div>\n",
    })
    assert "safe_filter_in_template" in smells(run_detector("find_django_security.py", project))


def test_autoescape_off_is_reported_only_for_html_templates(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": "{% autoescape off %}{{ body }}{% endautoescape %}\n",
        "shop/templates/b.htm": "{% autoescape off %}{{ body }}{% endautoescape %}\n",
        "shop/templates/c.xhtml": "{% autoescape off %}{{ body }}{% endautoescape %}\n",
        "shop/templates/email.txt": "{% autoescape off %}{{ body }}{% endautoescape %}\n",
    })
    findings = [
        finding
        for finding in run_detector("find_django_security.py", project)
        if finding["smell_type"] == "autoescape_off"
    ]
    assert {finding["file"].replace("\\", "/").rsplit("/", 1)[-1] for finding in findings} == {
        "a.html", "b.htm", "c.xhtml",
    }


def test_an_escaped_template_is_quiet(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": "<div>{{ comment.body }}</div>\n"
                                 "{{ data|json_script:'payload' }}\n",
    })
    assert run_detector("find_django_security.py", project) == []


# ---- the version gate ----------------------------------------------------------- #

def test_missing_csp_fires_only_on_django_6(tmp_path):
    files = {
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["Django==6.0"]\n',
        "config/settings.py": SETTINGS_HEAD + "DEBUG = False\n",
    }
    assert "missing_csp" in smells(run_detector("find_django_security.py",
                                                build_project(tmp_path / "six", files)))

    files["pyproject.toml"] = '[project]\nname = "x"\ndependencies = ["Django==5.2"]\n'
    assert "missing_csp" not in smells(run_detector("find_django_security.py",
                                                    build_project(tmp_path / "five", files)))


def test_missing_csp_is_silent_when_the_version_is_unknown(tmp_path):
    # Advising a 5.2 project to set a setting that does not exist there is worse
    # than saying nothing, so an unknown version means no version-gated finding.
    project = build_project(tmp_path / "p", {"config/settings.py": SETTINGS_HEAD + "DEBUG = False\n"})
    assert "missing_csp" not in smells(run_detector("find_django_security.py", project))


def test_a_configured_csp_on_django_6_is_quiet(tmp_path):
    project = build_project(tmp_path / "p", {
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["Django==6.1"]\n',
        "config/settings.py": SETTINGS_HEAD + "DEBUG = False\n"
                              "SECURE_HSTS_SECONDS = 3600\n"
                              "SECURE_SSL_REDIRECT = True\n"
                              "SECURE_CSP = {'default-src': [\"'self'\"]}\n",
    })
    assert run_detector("find_django_security.py", project) == []
