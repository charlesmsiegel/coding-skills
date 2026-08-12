"""Tests for the hardened template and over-engineering detectors."""

from helpers import build_project, run_detector, smells


def relation_walks(findings):
    return [f for f in findings if f["smell_type"] == "relation_walk_in_loop"]


# ---- templates: the new checks ------------------------------------------------ #

def test_a_post_form_without_csrf_token_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": '<form method="post">\n'
                                 '  <input name="x">\n'
                                 '</form>\n',
    })
    assert "missing_csrf_token" in smells(run_detector("find_template_issues.py", project))


def test_a_post_form_with_csrf_token_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": '<form method="post">\n'
                                 '  {% csrf_token %}\n'
                                 '  <input name="x">\n'
                                 '</form>\n',
    })
    assert "missing_csrf_token" not in smells(run_detector("find_template_issues.py", project))


def test_a_get_form_needs_no_csrf_token(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": '<form method="get">\n  <input name="q">\n</form>\n',
    })
    assert "missing_csrf_token" not in smells(run_detector("find_template_issues.py", project))


def test_a_variable_inside_a_script_block_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": "<script>\n  var name = '{{ user.name }}';\n</script>\n",
    })
    assert "template_var_in_script" in smells(run_detector("find_template_issues.py", project))


def test_json_script_is_the_safe_form_and_is_not_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": "{{ payload|json_script:'data' }}\n"
                                 "<script>\n  var d = JSON.parse(document.getElementById('data').textContent);\n"
                                 "</script>\n",
    })
    assert "template_var_in_script" not in smells(run_detector("find_template_issues.py", project))


def test_a_variable_outside_a_script_block_is_not_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": "<script>var a = 1;</script>\n<p>{{ user.name }}</p>\n",
    })
    assert "template_var_in_script" not in smells(run_detector("find_template_issues.py", project))


def test_the_static_url_variable_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": '<link href="{{ STATIC_URL }}css/site.css">\n',
    })
    assert "static_url_variable" in smells(run_detector("find_template_issues.py", project))


def test_the_static_tag_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": "{% load static %}\n"
                                 "<link href=\"{% static 'css/site.css' %}\">\n",
    })
    assert "static_url_variable" not in smells(run_detector("find_template_issues.py", project))


def test_a_hardcoded_href_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": '<a href="/orders/list/">Orders</a>\n',
    })
    assert "hardcoded_url_in_template" in smells(run_detector("find_template_issues.py", project))


def test_the_url_tag_is_not_a_hardcoded_href(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": "<a href=\"{% url 'order-list' %}\">Orders</a>\n",
    })
    assert "hardcoded_url_in_template" not in smells(run_detector("find_template_issues.py", project))


def test_a_root_href_is_not_reported(tmp_path):
    project = build_project(tmp_path / "p", {"shop/templates/a.html": '<a href="/">Home</a>\n'})
    assert "hardcoded_url_in_template" not in smells(run_detector("find_template_issues.py", project))


def test_include_inside_a_loop_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": "{% for row in rows %}\n"
                                 "  {% include 'shop/row.html' %}\n"
                                 "{% endfor %}\n",
    })
    assert "include_in_loop" in smells(run_detector("find_template_issues.py", project))


def test_include_outside_a_loop_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": "{% include 'shop/header.html' %}\n",
    })
    assert "include_in_loop" not in smells(run_detector("find_template_issues.py", project))


def test_form_wrappers_only_report_nested_domain_relations(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": "{% for freebie_form in freebie_forms %}\n"
                                 "{{ freebie_form.character.name }}\n"
                                 "{{ freebie_form.character.gameline }}\n"
                                 "{{ freebie_form.character.get_absolute_url }}\n"
                                 "{{ freebie_form.character.owner.profile.get_absolute_url }}\n"
                                 "{% endfor %}\n",
    })
    hits = relation_walks(run_detector("find_template_issues.py", project))
    assert [(f["line"], f["suggestion"]) for f in hits] == [
        (5, "select_related/prefetch_related 'owner__profile' on the queryset the view passes in."),
    ]


def test_scalar_and_file_value_accesses_are_not_relations(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": "{% for obj in objects %}\n"
                                 "{{ obj.type.title }}\n"
                                 "{{ obj.image.url }}\n"
                                 "{% endfor %}\n",
    })
    assert relation_walks(run_detector("find_template_issues.py", project)) == []


def test_genuine_nested_model_relations_still_report(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html":
            "{% for obj in objects %}{{ obj.owner.profile.get_absolute_url }}{% endfor %}\n"
            "{% for journal in journals %}{{ journal.character.name }}{% endfor %}\n",
    })
    assert len(relation_walks(run_detector("find_template_issues.py", project))) == 2


def test_a_clean_template_produces_nothing(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templates/a.html": "{% load static %}\n"
                                 "{% for order in orders %}\n"
                                 "  <p>{{ order.reference }}</p>\n"
                                 "{% endfor %}\n"
                                 "<form method=\"post\" action=\"{% url 'checkout' %}\">\n"
                                 "  {% csrf_token %}\n"
                                 "</form>\n",
    })
    assert run_detector("find_template_issues.py", project) == []


# ---- over-engineering: the new checks ------------------------------------------ #

def test_a_query_in_app_ready_is_high_severity(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/apps.py": "from django.apps import AppConfig\n"
                        "from .models import Setting\n\n\n"
                        "class ShopConfig(AppConfig):\n"
                        "    name = 'shop'\n\n"
                        "    def ready(self):\n"
                        "        self.cache = Setting.objects.all()\n",
    })
    findings = run_detector("find_django_overengineering.py", project)
    hits = [f for f in findings if f["smell_type"] == "work_in_app_ready"]
    assert hits and hits[0]["severity"] == "high"


def test_a_ready_that_only_imports_signals_is_correct(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/apps.py": "from django.apps import AppConfig\n\n\n"
                        "class ShopConfig(AppConfig):\n"
                        "    name = 'shop'\n\n"
                        "    def ready(self):\n"
                        "        from . import signals  # noqa: F401\n",
    })
    assert "work_in_app_ready" not in smells(run_detector("find_django_overengineering.py", project))


def test_a_querying_context_processor_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/context_processors.py": "from .models import Category\n\n\n"
                                      "def categories(request):\n"
                                      "    return {'categories': Category.objects.all()}\n",
    })
    assert "query_in_context_processor" in smells(
        run_detector("find_django_overengineering.py", project))


def test_a_context_processor_that_reads_settings_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/context_processors.py": "from django.conf import settings\n\n\n"
                                      "def site(request):\n"
                                      "    return {'site_name': settings.SITE_NAME}\n",
    })
    assert "query_in_context_processor" not in smells(
        run_detector("find_django_overengineering.py", project))


def test_a_custom_filter_shadowing_a_builtin_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templatetags/shop_extras.py": "from django import template\n\n"
                                            "register = template.Library()\n\n\n"
                                            "@register.filter\n"
                                            "def length(value):\n"
                                            "    return len(value)\n",
    })
    assert "redundant_template_tag" in smells(run_detector("find_django_overengineering.py", project))


def test_a_custom_filter_with_its_own_name_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/templatetags/shop_extras.py": "from django import template\n\n"
                                            "register = template.Library()\n\n\n"
                                            "@register.filter\n"
                                            "def markdown_to_html(value):\n"
                                            "    return value\n",
    })
    assert "redundant_template_tag" not in smells(
        run_detector("find_django_overengineering.py", project))
