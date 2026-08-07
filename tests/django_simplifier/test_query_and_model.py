"""Tests for the hardened query and model detectors.

The negative cases carry more weight than the positive ones. A Django detector
that fires on correct code is worse than no detector: it trains people to skip
the output, and the real N+1 goes out with it. So every new check here is
paired with the correct form of the same construct, asserted silent.
"""

import pytest

from helpers import build_project, run_detector, smells

MODELS = """\
from django.db import models


class Customer(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["pk"]

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


# ---- queries: the new checks ------------------------------------------------- #

def test_count_inside_a_loop_is_a_query_per_row(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from .models import Customer\n\n"
                         "def go():\n"
                         "    for customer in Customer.objects.all():\n"
                         "        print(customer.orders.count())\n",
    })
    assert "count_in_loop" in smells(run_detector("find_query_issues.py", project))


def test_an_annotated_count_is_not_reported(tmp_path):
    # The fix for count_in_loop must not itself be a finding.
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from django.db.models import Count\n"
                         "from .models import Customer\n\n"
                         "def go():\n"
                         "    for customer in Customer.objects.annotate(n=Count('orders')):\n"
                         "        print(customer.n)\n",
    })
    found = smells(run_detector("find_query_issues.py", project))
    assert "count_in_loop" not in found
    assert "n_plus_one_query" not in found


def test_a_count_outside_a_loop_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from .models import Order\n\n"
                         "def go():\n"
                         "    return Order.objects.count()\n",
    })
    assert "count_in_loop" not in smells(run_detector("find_query_issues.py", project))


def test_exists_inside_a_loop_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from .models import Order, Customer\n\n"
                         "def go(ids):\n"
                         "    for pk in ids:\n"
                         "        if Order.objects.filter(pk=pk).exists():\n"
                         "            print(pk)\n",
    })
    assert "exists_in_loop" in smells(run_detector("find_query_issues.py", project))


def test_read_modify_write_on_a_model_attribute_is_a_lost_update(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from .models import Order\n\n"
                         "def sell(pk):\n"
                         "    order = Order.objects.get(pk=pk)\n"
                         "    order.stock = order.stock - 1\n"
                         "    order.save()\n",
    })
    findings = run_detector("find_query_issues.py", project)
    hits = [f for f in findings if f["smell_type"] == "read_modify_write_race"]
    assert hits and hits[0]["severity"] == "high"


def test_augmented_assignment_then_save_is_the_same_bug(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from .models import Order\n\n"
                         "def sell(pk):\n"
                         "    order = Order.objects.get(pk=pk)\n"
                         "    order.stock -= 1\n"
                         "    order.save()\n",
    })
    assert "read_modify_write_race" in smells(run_detector("find_query_issues.py", project))


def test_the_f_expression_fix_is_not_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from django.db.models import F\n"
                         "from .models import Order\n\n"
                         "def sell(pk):\n"
                         "    Order.objects.filter(pk=pk).update(stock=F('stock') - 1)\n",
    })
    found = smells(run_detector("find_query_issues.py", project))
    assert "read_modify_write_race" not in found
    assert "update_without_f" not in found


def test_assigning_an_unrelated_value_then_saving_is_not_a_race(tmp_path):
    # Setting a field to a constant is not a read-modify-write.
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from .models import Order\n\n"
                         "def close(pk):\n"
                         "    order = Order.objects.get(pk=pk)\n"
                         "    order.stock = 0\n"
                         "    order.save()\n",
    })
    assert "read_modify_write_race" not in smells(run_detector("find_query_issues.py", project))


def test_update_on_a_sliced_queryset_is_a_runtime_error(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from .models import Order\n\n"
                         "def go():\n"
                         "    Order.objects.all()[:10].update(stock=0)\n",
    })
    findings = run_detector("find_query_issues.py", project)
    hits = [f for f in findings if f["smell_type"] == "update_on_sliced_queryset"]
    assert hits and hits[0]["severity"] == "high"


def test_bulk_create_without_batch_size_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from .models import Order\n\n"
                         "def go(rows):\n"
                         "    Order.objects.bulk_create(rows)\n",
    })
    assert "bulk_create_without_batch_size" in smells(run_detector("find_query_issues.py", project))


def test_bulk_create_with_batch_size_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from .models import Order\n\n"
                         "def go(rows):\n"
                         "    Order.objects.bulk_create(rows, batch_size=1000)\n",
    })
    assert "bulk_create_without_batch_size" not in smells(run_detector("find_query_issues.py", project))


def test_indexing_a_queryset_at_zero_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from .models import Order\n\n"
                         "def go():\n"
                         "    return Order.objects.filter(stock=1)[0]\n",
    })
    assert "index_instead_of_first" in smells(run_detector("find_query_issues.py", project))


def test_slicing_a_queryset_for_a_page_is_not_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from .models import Order\n\n"
                         "def go():\n"
                         "    return Order.objects.all()[:20]\n",
    })
    assert "index_instead_of_first" not in smells(run_detector("find_query_issues.py", project))


# ---- models: the new checks --------------------------------------------------- #

def test_a_save_override_that_ignores_update_fields_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Thing(models.Model):\n"
                          "    name = models.CharField(max_length=10)\n"
                          "    slug_cache = models.CharField(max_length=10)\n\n"
                          "    class Meta:\n"
                          "        ordering = ['pk']\n\n"
                          "    def __str__(self):\n"
                          "        return self.name\n\n"
                          "    def save(self, *args, **kwargs):\n"
                          "        self.slug_cache = self.name.lower()\n"
                          "        super().save(*args, **kwargs)\n",
    })
    assert "save_ignores_update_fields" in smells(run_detector("find_model_issues.py", project))


def test_a_save_override_that_extends_update_fields_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Thing(models.Model):\n"
                          "    name = models.CharField(max_length=10)\n"
                          "    slug_cache = models.CharField(max_length=10)\n\n"
                          "    class Meta:\n"
                          "        ordering = ['pk']\n\n"
                          "    def __str__(self):\n"
                          "        return self.name\n\n"
                          "    def save(self, *args, **kwargs):\n"
                          "        self.slug_cache = self.name.lower()\n"
                          "        if kwargs.get('update_fields') is not None:\n"
                          "            kwargs['update_fields'] = {*kwargs['update_fields'], 'slug_cache'}\n"
                          "        super().save(*args, **kwargs)\n",
    })
    assert "save_ignores_update_fields" not in smells(run_detector("find_model_issues.py", project))


def test_a_decimal_field_without_precision_is_high_severity(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Invoice(models.Model):\n"
                          "    total = models.DecimalField()\n\n"
                          "    class Meta:\n"
                          "        ordering = ['pk']\n\n"
                          "    def __str__(self):\n"
                          "        return 'i'\n",
    })
    findings = run_detector("find_model_issues.py", project)
    hits = [f for f in findings if f["smell_type"] == "decimal_without_precision"]
    assert hits and hits[0]["severity"] == "high"


def test_a_decimal_field_with_precision_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Invoice(models.Model):\n"
                          "    total = models.DecimalField(max_digits=10, decimal_places=2)\n\n"
                          "    class Meta:\n"
                          "        ordering = ['pk']\n\n"
                          "    def __str__(self):\n"
                          "        return 'i'\n",
    })
    assert "decimal_without_precision" not in smells(run_detector("find_model_issues.py", project))


def test_on_delete_passed_positionally_is_not_reported_as_missing(tmp_path):
    # ForeignKey(Customer, models.CASCADE) is legal and common. The old check
    # only looked at keywords and would have called this a high-severity bug.
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Customer(models.Model):\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return 'c'\n\n\n"
                          "class Order(models.Model):\n"
                          "    customer = models.ForeignKey(Customer, models.CASCADE, related_name='o')\n\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return 'o'\n",
    })
    assert "missing_on_delete" not in smells(run_detector("find_model_issues.py", project))


def test_multi_table_inheritance_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Place(models.Model):\n"
                          "    name = models.CharField(max_length=10)\n\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return self.name\n\n\n"
                          "class Restaurant(Place):\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return 'r'\n",
    })
    assert "multi_table_inheritance" in smells(run_detector("find_model_issues.py", project))


def test_abstract_inheritance_is_not_multi_table(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Base(models.Model):\n"
                          "    name = models.CharField(max_length=10)\n\n"
                          "    class Meta:\n        abstract = True\n\n\n"
                          "class Real(Base):\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return 'r'\n",
    })
    assert "multi_table_inheritance" not in smells(run_detector("find_model_issues.py", project))


def test_db_index_on_a_foreign_key_is_redundant(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Customer(models.Model):\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return 'c'\n\n\n"
                          "class Order(models.Model):\n"
                          "    customer = models.ForeignKey(Customer, on_delete=models.CASCADE,\n"
                          "                                 related_name='o', db_index=True)\n\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return 'o'\n",
    })
    assert "redundant_db_index_on_fk" in smells(run_detector("find_model_issues.py", project))


def test_related_name_plus_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Customer(models.Model):\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return 'c'\n\n\n"
                          "class Order(models.Model):\n"
                          "    customer = models.ForeignKey(Customer, on_delete=models.CASCADE,\n"
                          "                                 related_name='+')\n\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return 'o'\n",
    })
    found = smells(run_detector("find_model_issues.py", project))
    assert "related_name_disabled" in found
    assert "missing_related_name" not in found     # it has one, it just disables the reverse


def test_unique_together_is_reported_once_per_model(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Thing(models.Model):\n"
                          "    a = models.CharField(max_length=1)\n"
                          "    b = models.CharField(max_length=1)\n\n"
                          "    class Meta:\n"
                          "        ordering = ['pk']\n"
                          "        unique_together = [['a', 'b']]\n\n"
                          "    def __str__(self):\n        return 'x'\n",
    })
    findings = [f for f in run_detector("find_model_issues.py", project)
                if f["smell_type"] == "unique_together_over_constraints"]
    assert len(findings) == 1


def test_a_constraint_based_model_is_quiet(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Thing(models.Model):\n"
                          "    a = models.CharField(max_length=1)\n\n"
                          "    class Meta:\n"
                          "        ordering = ['pk']\n"
                          "        constraints = [models.UniqueConstraint(fields=['a'], name='u')]\n\n"
                          "    def __str__(self):\n        return 'x'\n",
    })
    assert run_detector("find_model_issues.py", project) == []


def test_auto_now_add_with_a_default_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n"
                          "from django.utils import timezone\n\n\n"
                          "class Thing(models.Model):\n"
                          "    created = models.DateTimeField(auto_now_add=True, default=timezone.now)\n\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return 'x'\n",
    })
    assert "auto_now_add_with_default" in smells(run_detector("find_model_issues.py", project))


def test_a_file_field_without_upload_to_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Doc(models.Model):\n"
                          "    attachment = models.FileField()\n\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return 'd'\n",
    })
    assert "file_field_without_upload_to" in smells(run_detector("find_model_issues.py", project))


def test_a_slug_model_without_get_absolute_url_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Post(models.Model):\n"
                          "    slug = models.SlugField()\n\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return self.slug\n",
    })
    assert "missing_get_absolute_url" in smells(run_detector("find_model_issues.py", project))


def test_a_slug_model_with_get_absolute_url_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n"
                          "from django.urls import reverse\n\n\n"
                          "class Post(models.Model):\n"
                          "    slug = models.SlugField()\n\n"
                          "    class Meta:\n        ordering = ['pk']\n\n"
                          "    def __str__(self):\n        return self.slug\n\n"
                          "    def get_absolute_url(self):\n"
                          "        return reverse('post', kwargs={'slug': self.slug})\n",
    })
    assert "missing_get_absolute_url" not in smells(run_detector("find_model_issues.py", project))


def test_a_model_with_no_slug_is_not_asked_for_get_absolute_url(tmp_path):
    project = build_project(tmp_path / "p", {"shop/models.py": MODELS})
    assert "missing_get_absolute_url" not in smells(run_detector("find_model_issues.py", project))


def test_the_well_formed_fixture_still_produces_nothing(tmp_path):
    # The regression guard on every check added above.
    project = build_project(tmp_path / "p", {"shop/models.py": MODELS})
    assert run_detector("find_model_issues.py", project) == []
