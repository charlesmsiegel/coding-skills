"""Tests for the migration and settings detectors."""

from helpers import build_project, run_detector, severities, smells

# A settings module with nothing to complain about, so per-check tests can add
# exactly one problem and assert on it.
GOOD_SETTINGS = """\
import django
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
AUTH_USER_MODEL = "accounts.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
"""


# ---- migrations ----------------------------------------------------------------- #

def test_run_python_without_reverse_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/migrations/0001_initial.py":
            "from django.db import migrations\n\n\n"
            "def forwards(apps, schema_editor):\n"
            "    Order = apps.get_model('shop', 'Order')\n"
            "    Order.objects.update(status='new')\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = []\n"
            "    operations = [migrations.RunPython(forwards)]\n",
    })
    assert "run_python_without_reverse" in smells(run_detector("find_migration_issues.py", project))


def test_run_python_with_a_reverse_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/migrations/0001_initial.py":
            "from django.db import migrations\n\n\n"
            "def forwards(apps, schema_editor):\n"
            "    Order = apps.get_model('shop', 'Order')\n"
            "    Order.objects.update(status='new')\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = []\n"
            "    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]\n",
    })
    assert run_detector("find_migration_issues.py", project) == []


def test_a_run_python_using_an_imported_model_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/migrations/0002_backfill.py":
            "from django.db import migrations\n"
            "from shop.models import Order\n\n\n"
            "def forwards(apps, schema_editor):\n"
            "    Order.objects.update(status='new')\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n"
            "    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]\n",
    })
    assert severities(run_detector("find_migration_issues.py", project),
                      "run_python_imports_model") == ["high"]


def test_apps_get_model_is_the_correct_form(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/migrations/0002_backfill.py":
            "from django.db import migrations\n\n\n"
            "def forwards(apps, schema_editor):\n"
            "    Order = apps.get_model('shop', 'Order')\n"
            "    Order.objects.update(status='new')\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n"
            "    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]\n",
    })
    assert "run_python_imports_model" not in smells(run_detector("find_migration_issues.py", project))


def test_run_sql_without_a_reverse_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/migrations/0001_initial.py":
            "from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = []\n"
            "    operations = [migrations.RunSQL('CREATE INDEX x ON shop_order (ref);')]\n",
    })
    assert "run_sql_without_reverse" in smells(run_detector("find_migration_issues.py", project))


def test_a_non_nullable_field_without_a_default_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/migrations/0002_add.py":
            "from django.db import migrations, models\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n"
            "    operations = [\n"
            "        migrations.AddField(model_name='order', name='code',\n"
            "                            field=models.CharField(max_length=10)),\n"
            "    ]\n",
    })
    assert severities(run_detector("find_migration_issues.py", project),
                      "non_nullable_without_default") == ["high"]


def test_a_nullable_added_field_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/migrations/0002_add.py":
            "from django.db import migrations, models\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n"
            "    operations = [\n"
            "        migrations.AddField(model_name='order', name='code',\n"
            "                            field=models.CharField(max_length=10, null=True)),\n"
            "    ]\n",
    })
    assert "non_nullable_without_default" not in smells(
        run_detector("find_migration_issues.py", project))


def test_a_field_with_a_default_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/migrations/0002_add.py":
            "from django.db import migrations, models\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n"
            "    operations = [\n"
            "        migrations.AddField(model_name='order', name='code',\n"
            "                            field=models.CharField(max_length=10, default='')),\n"
            "    ]\n",
    })
    assert "non_nullable_without_default" not in smells(
        run_detector("find_migration_issues.py", project))


def test_schema_and_data_in_one_migration_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/migrations/0002_mixed.py":
            "from django.db import migrations, models\n\n\n"
            "def forwards(apps, schema_editor):\n"
            "    Order = apps.get_model('shop', 'Order')\n"
            "    Order.objects.update(code='x')\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n"
            "    operations = [\n"
            "        migrations.AddField(model_name='order', name='code',\n"
            "                            field=models.CharField(max_length=10, default='')),\n"
            "        migrations.RunPython(forwards, migrations.RunPython.noop),\n"
            "    ]\n",
    })
    assert "schema_and_data_in_one_migration" in smells(
        run_detector("find_migration_issues.py", project))


def test_two_leaf_migrations_are_a_merge_conflict(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/migrations/0001_initial.py":
            "from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = []\n    operations = []\n",
        "shop/migrations/0002_a.py":
            "from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n    operations = []\n",
        "shop/migrations/0002_b.py":
            "from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n    operations = []\n",
    })
    assert severities(run_detector("find_migration_issues.py", project),
                      "conflicting_leaf_migrations") == ["high"]


def test_a_linear_migration_history_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/migrations/0001_initial.py":
            "from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = []\n    operations = []\n",
        "shop/migrations/0002_next.py":
            "from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n    operations = []\n",
    })
    assert run_detector("find_migration_issues.py", project) == []


# ---- settings -------------------------------------------------------------------- #

def test_a_default_user_model_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "config/settings.py": GOOD_SETTINGS.replace('AUTH_USER_MODEL = "accounts.User"\n', ""),
    })
    assert "default_user_model" in smells(run_detector("find_settings_issues.py", project))


def test_a_custom_user_model_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {"config/settings.py": GOOD_SETTINGS})
    assert run_detector("find_settings_issues.py", project) == []


def test_missing_csrf_middleware_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "config/settings.py": GOOD_SETTINGS.replace(
            '    "django.middleware.csrf.CsrfViewMiddleware",\n', ""),
    })
    assert severities(run_detector("find_settings_issues.py", project),
                      "missing_security_middleware") == ["high"]


def test_authentication_before_session_is_an_ordering_bug(tmp_path):
    broken = GOOD_SETTINGS.replace(
        '    "django.contrib.sessions.middleware.SessionMiddleware",\n'
        '    "django.middleware.common.CommonMiddleware",\n'
        '    "django.middleware.csrf.CsrfViewMiddleware",\n'
        '    "django.contrib.auth.middleware.AuthenticationMiddleware",\n',
        '    "django.contrib.auth.middleware.AuthenticationMiddleware",\n'
        '    "django.contrib.sessions.middleware.SessionMiddleware",\n'
        '    "django.middleware.common.CommonMiddleware",\n'
        '    "django.middleware.csrf.CsrfViewMiddleware",\n')
    project = build_project(tmp_path / "p", {"config/settings.py": broken})
    assert "middleware_order" in smells(run_detector("find_settings_issues.py", project))


def test_sqlite_in_a_production_module_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "config/settings/base.py": GOOD_SETTINGS,
        "config/settings/production.py":
            "from .base import *\n"
            "DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': 'db'}}\n",
    })
    assert severities(run_detector("find_settings_issues.py", project),
                      "sqlite_in_production") == ["high"]


def test_sqlite_in_a_dev_module_is_correct(tmp_path):
    project = build_project(tmp_path / "p", {
        "config/settings/base.py": GOOD_SETTINGS,
        "config/settings/dev.py":
            "from .base import *\n"
            "DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': 'db'}}\n",
        "config/settings/production.py": "from .base import *\n",
    })
    assert "sqlite_in_production" not in smells(run_detector("find_settings_issues.py", project))


def test_a_locmem_cache_in_production_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "config/settings/base.py": GOOD_SETTINGS,
        "config/settings/production.py":
            "from .base import *\n"
            "CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}\n",
    })
    assert "locmem_cache_in_production" in smells(run_detector("find_settings_issues.py", project))


def test_settings_importing_models_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "config/settings.py": GOOD_SETTINGS + "from shop.models import Order\n",
    })
    assert severities(run_detector("find_settings_issues.py", project),
                      "settings_imports_models") == ["high"]


def test_ospath_join_on_basedir_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "config/settings.py": GOOD_SETTINGS + "import os\n"
                              "STATIC_ROOT = os.path.join(BASE_DIR, 'static')\n",
    })
    assert "ospath_join_basedir" in smells(run_detector("find_settings_issues.py", project))


def test_the_pathlib_form_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "config/settings.py": GOOD_SETTINGS + "STATIC_ROOT = BASE_DIR / 'static'\n",
    })
    assert "ospath_join_basedir" not in smells(run_detector("find_settings_issues.py", project))


def test_a_removed_storage_setting_is_high_on_django_5_1_plus(tmp_path):
    project = build_project(tmp_path / "p", {
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["Django==5.2"]\n',
        "config/settings.py": GOOD_SETTINGS + "STATICFILES_STORAGE = 'whitenoise.Storage'\n",
    })
    assert severities(run_detector("find_settings_issues.py", project),
                      "deprecated_storage_setting") == ["high"]


def test_default_auto_field_is_only_advisory_on_django_6(tmp_path):
    # 6.0 made BigAutoField the default, so its absence stops being a problem.
    without = GOOD_SETTINGS.replace(
        'DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"\n', "")
    project = build_project(tmp_path / "six", {
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["Django==6.1"]\n',
        "config/settings.py": without,
    })
    assert severities(run_detector("find_settings_issues.py", project),
                      "missing_default_auto_field") == ["low"]

    project = build_project(tmp_path / "five", {
        "pyproject.toml": '[project]\nname = "x"\ndependencies = ["Django==5.2"]\n',
        "config/settings.py": without,
    })
    assert severities(run_detector("find_settings_issues.py", project),
                      "missing_default_auto_field") == ["medium"]
