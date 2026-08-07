"""Tests for the form and admin detectors."""

from helpers import build_project, run_detector, severities, smells

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
    reference = models.CharField(max_length=20)
    status = models.BooleanField(default=False)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return self.reference
"""


# ---- forms -------------------------------------------------------------------- #

def test_fields_all_is_high_severity(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/forms.py": "from django import forms\n"
                         "from .models import Order\n\n\n"
                         "class OrderForm(forms.ModelForm):\n"
                         "    class Meta:\n"
                         "        model = Order\n"
                         "        fields = '__all__'\n",
    })
    assert severities(run_detector("find_form_issues.py", project), "form_fields_all") == ["high"]


def test_an_explicit_field_list_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/forms.py": "from django import forms\n"
                         "from .models import Order\n\n\n"
                         "class OrderForm(forms.ModelForm):\n"
                         "    class Meta:\n"
                         "        model = Order\n"
                         "        fields = ['reference']\n",
    })
    assert run_detector("find_form_issues.py", project) == []


def test_exclude_is_reported_as_the_same_failure_one_step_removed(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/forms.py": "from django import forms\n"
                         "from .models import Order\n\n\n"
                         "class OrderForm(forms.ModelForm):\n"
                         "    class Meta:\n"
                         "        model = Order\n"
                         "        exclude = ['status']\n",
    })
    assert "form_fields_all" in smells(run_detector("find_form_issues.py", project))


def test_a_queryset_at_class_scope_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/forms.py": "from django import forms\n"
                         "from .models import Customer\n\n\n"
                         "class OrderForm(forms.Form):\n"
                         "    customer = forms.ModelChoiceField(queryset=Customer.objects.all())\n",
    })
    assert "queryset_at_class_scope" in smells(run_detector("find_form_issues.py", project))


def test_a_queryset_assigned_in_init_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/forms.py": "from django import forms\n"
                         "from .models import Customer\n\n\n"
                         "class OrderForm(forms.Form):\n"
                         "    def __init__(self, *args, **kwargs):\n"
                         "        super().__init__(*args, **kwargs)\n"
                         "        self.fields['customer'].queryset = Customer.objects.all()\n",
    })
    assert "queryset_at_class_scope" not in smells(run_detector("find_form_issues.py", project))


def test_a_clean_method_that_returns_nothing_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/forms.py": "from django import forms\n\n\n"
                         "class OrderForm(forms.Form):\n"
                         "    def clean_reference(self):\n"
                         "        value = self.cleaned_data['reference']\n"
                         "        if not value:\n"
                         "            raise forms.ValidationError('required')\n",
    })
    assert severities(run_detector("find_form_issues.py", project),
                      "clean_method_returns_nothing") == ["high"]


def test_a_clean_method_that_returns_the_value_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/forms.py": "from django import forms\n\n\n"
                         "class OrderForm(forms.Form):\n"
                         "    def clean_reference(self):\n"
                         "        value = self.cleaned_data['reference']\n"
                         "        return value.strip()\n",
    })
    assert "clean_method_returns_nothing" not in smells(run_detector("find_form_issues.py", project))


def test_cleaned_data_read_without_is_valid_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from .forms import OrderForm\n\n"
                         "def create(request):\n"
                         "    form = OrderForm(request.POST)\n"
                         "    reference = form.cleaned_data['reference']\n"
                         "    return reference\n",
    })
    assert "cleaned_data_before_validation" in smells(run_detector("find_form_issues.py", project))


def test_cleaned_data_after_is_valid_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from .forms import OrderForm\n\n"
                         "def create(request):\n"
                         "    form = OrderForm(request.POST)\n"
                         "    if form.is_valid():\n"
                         "        return form.cleaned_data['reference']\n"
                         "    return None\n",
    })
    assert "cleaned_data_before_validation" not in smells(run_detector("find_form_issues.py", project))


def test_self_cleaned_data_inside_a_form_method_is_not_reported(tmp_path):
    # clean() and clean_<field>() run after validation by definition.
    project = build_project(tmp_path / "p", {
        "shop/forms.py": "from django import forms\n\n\n"
                         "class OrderForm(forms.Form):\n"
                         "    def clean(self):\n"
                         "        data = self.cleaned_data\n"
                         "        return data\n",
    })
    assert "cleaned_data_before_validation" not in smells(run_detector("find_form_issues.py", project))


def test_commit_false_without_save_m2m_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from .forms import OrderForm\n\n"
                         "def create(request):\n"
                         "    form = OrderForm(request.POST)\n"
                         "    if form.is_valid():\n"
                         "        order = form.save(commit=False)\n"
                         "        order.owner = request.user\n"
                         "        order.save()\n"
                         "    return None\n",
    })
    assert "commit_false_without_save_m2m" in smells(run_detector("find_form_issues.py", project))


def test_commit_false_with_save_m2m_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from .forms import OrderForm\n\n"
                         "def create(request):\n"
                         "    form = OrderForm(request.POST)\n"
                         "    if form.is_valid():\n"
                         "        order = form.save(commit=False)\n"
                         "        order.owner = request.user\n"
                         "        order.save()\n"
                         "        form.save_m2m()\n"
                         "    return None\n",
    })
    assert "commit_false_without_save_m2m" not in smells(run_detector("find_form_issues.py", project))


def test_a_form_saved_without_validation_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/forms.py": "from django import forms\n\n\n"
                         "class OrderForm(forms.Form):\n"
                         "    pass\n",
        "shop/views.py": "from .forms import OrderForm\n\n"
                         "def create(request):\n"
                         "    form = OrderForm(request.POST)\n"
                         "    form.save()\n"
                         "    return None\n",
    })
    assert severities(run_detector("find_form_issues.py", project),
                      "unvalidated_form_use") == ["high"]


# ---- admin --------------------------------------------------------------------- #

def test_list_display_walking_a_relation_is_an_n_plus_one(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/admin.py": "from django.contrib import admin\n"
                         "from .models import Order\n\n\n"
                         "@admin.register(Order)\n"
                         "class OrderAdmin(admin.ModelAdmin):\n"
                         "    list_display = ['reference', 'customer']\n"
                         "    search_fields = ['reference']\n",
    })
    assert "admin_list_display_n_plus_one" in smells(run_detector("find_admin_issues.py", project))


def test_list_select_related_answers_the_n_plus_one(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/admin.py": "from django.contrib import admin\n"
                         "from .models import Order\n\n\n"
                         "@admin.register(Order)\n"
                         "class OrderAdmin(admin.ModelAdmin):\n"
                         "    list_display = ['reference', 'customer']\n"
                         "    list_select_related = ['customer']\n"
                         "    search_fields = ['reference']\n"
                         "    autocomplete_fields = ['customer']\n",
    })
    assert "admin_list_display_n_plus_one" not in smells(run_detector("find_admin_issues.py", project))


def test_a_list_display_of_local_fields_only_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/admin.py": "from django.contrib import admin\n"
                         "from .models import Customer\n\n\n"
                         "@admin.register(Customer)\n"
                         "class CustomerAdmin(admin.ModelAdmin):\n"
                         "    list_display = ['name']\n"
                         "    search_fields = ['name']\n",
    })
    assert run_detector("find_admin_issues.py", project) == []


def test_list_filter_on_a_char_field_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/admin.py": "from django.contrib import admin\n"
                         "from .models import Customer\n\n\n"
                         "@admin.register(Customer)\n"
                         "class CustomerAdmin(admin.ModelAdmin):\n"
                         "    list_display = ['name']\n"
                         "    list_filter = ['name']\n"
                         "    search_fields = ['name']\n",
    })
    assert "list_filter_high_cardinality" in smells(run_detector("find_admin_issues.py", project))


def test_list_filter_on_a_boolean_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/admin.py": "from django.contrib import admin\n"
                         "from .models import Order\n\n\n"
                         "@admin.register(Order)\n"
                         "class OrderAdmin(admin.ModelAdmin):\n"
                         "    list_display = ['reference']\n"
                         "    list_filter = ['status']\n"
                         "    search_fields = ['reference']\n",
    })
    assert "list_filter_high_cardinality" not in smells(run_detector("find_admin_issues.py", project))


def test_an_admin_action_that_writes_without_a_permission_check_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/admin.py": "from django.contrib import admin\n"
                         "from .models import Order\n\n\n"
                         "@admin.register(Order)\n"
                         "class OrderAdmin(admin.ModelAdmin):\n"
                         "    list_display = ['reference']\n"
                         "    search_fields = ['reference']\n"
                         "    actions = ['approve']\n\n"
                         "    def approve(self, request, queryset):\n"
                         "        queryset.update(status=True)\n",
    })
    assert severities(run_detector("find_admin_issues.py", project),
                      "admin_action_without_permission_check") == ["high"]


def test_an_admin_action_that_checks_permission_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/admin.py": "from django.contrib import admin\n"
                         "from django.core.exceptions import PermissionDenied\n"
                         "from .models import Order\n\n\n"
                         "@admin.register(Order)\n"
                         "class OrderAdmin(admin.ModelAdmin):\n"
                         "    list_display = ['reference']\n"
                         "    search_fields = ['reference']\n"
                         "    actions = ['approve']\n\n"
                         "    def approve(self, request, queryset):\n"
                         "        if not request.user.has_perm('shop.change_order'):\n"
                         "            raise PermissionDenied\n"
                         "        queryset.update(status=True)\n",
    })
    assert "admin_action_without_permission_check" not in smells(
        run_detector("find_admin_issues.py", project))


def test_mark_safe_in_a_display_callable_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/admin.py": "from django.contrib import admin\n"
                         "from django.utils.safestring import mark_safe\n"
                         "from .models import Order\n\n\n"
                         "@admin.register(Order)\n"
                         "class OrderAdmin(admin.ModelAdmin):\n"
                         "    list_display = ['badge']\n"
                         "    search_fields = ['reference']\n\n"
                         "    def badge(self, obj):\n"
                         "        return mark_safe('<b>' + obj.reference + '</b>')\n",
    })
    assert severities(run_detector("find_admin_issues.py", project),
                      "mark_safe_in_admin_display") == ["high"]


def test_a_get_queryset_that_drops_super_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/admin.py": "from django.contrib import admin\n"
                         "from .models import Order\n\n\n"
                         "@admin.register(Order)\n"
                         "class OrderAdmin(admin.ModelAdmin):\n"
                         "    list_display = ['reference']\n"
                         "    search_fields = ['reference']\n\n"
                         "    def get_queryset(self, request):\n"
                         "        return Order.objects.filter(status=True)\n",
    })
    assert "admin_get_queryset_without_super" in smells(run_detector("find_admin_issues.py", project))


def test_a_get_queryset_built_on_super_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/admin.py": "from django.contrib import admin\n"
                         "from .models import Order\n\n\n"
                         "@admin.register(Order)\n"
                         "class OrderAdmin(admin.ModelAdmin):\n"
                         "    list_display = ['reference']\n"
                         "    search_fields = ['reference']\n\n"
                         "    def get_queryset(self, request):\n"
                         "        return super().get_queryset(request).select_related('customer')\n",
    })
    assert "admin_get_queryset_without_super" not in smells(
        run_detector("find_admin_issues.py", project))


def test_an_admin_without_search_fields_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/admin.py": "from django.contrib import admin\n"
                         "from .models import Customer\n\n\n"
                         "@admin.register(Customer)\n"
                         "class CustomerAdmin(admin.ModelAdmin):\n"
                         "    list_display = ['name']\n",
    })
    assert "admin_missing_search_fields" in smells(run_detector("find_admin_issues.py", project))
