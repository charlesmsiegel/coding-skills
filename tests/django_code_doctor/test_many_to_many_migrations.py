"""AddField checks apply to columns, not many-to-many intermediary tables."""

import pytest

from helpers import build_project, run_detector, severities


def added_field_findings(tmp_path, field):
    project = build_project(tmp_path / "project", {
        "shop/migrations/0002_add_relation.py": (
            "from django.db import migrations, models\n"
            "from django.db.models import ManyToManyField\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n"
            "    operations = [\n"
            "        migrations.AddField(model_name='order', name='related',\n"
            f"                            field={field}),\n"
            "    ]\n"
        ),
    })
    return run_detector("find_migration_issues.py", project)


@pytest.mark.parametrize("field", [
    "models.ManyToManyField(to='shop.Product')",
    "ManyToManyField(to='shop.Product')",
    "models.ManyToManyField(to='shop.Product', through='shop.OrderProduct')",
])
def test_many_to_many_add_field_does_not_add_a_non_nullable_column(tmp_path, field):
    assert added_field_findings(tmp_path, field) == []


@pytest.mark.parametrize("field", [
    "models.ForeignKey(to='shop.Product', on_delete=models.CASCADE)",
    "models.IntegerField()",
    "models.CharField(max_length=20)",
])
def test_column_add_field_without_default_still_reports_high(tmp_path, field):
    assert severities(
        added_field_findings(tmp_path, field), "non_nullable_without_default"
    ) == ["high"]
