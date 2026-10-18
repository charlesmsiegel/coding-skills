"""Lazy ModelChoiceField querysets are not cached class-level collections."""
import pytest

from test_detectors import build_project, run_detector


def scan(tmp_path, expression, field=True):
    value = "forms.ModelMultipleChoiceField(queryset=" + expression + ")" if field else expression
    project = build_project(tmp_path / 'project', {'app/forms.py': '''from django import forms
from .models import Customer
class CustomerForm(forms.Form):
    customers = ''' + value + '''
'''})
    return run_detector('find_form_issues.py', project)


@pytest.mark.parametrize('expression', [
    'Customer.objects.all()',
    'Customer.objects.none()',
    'Customer.objects.filter(active=True)',
    'Customer.objects.filter(active=True).order_by("name")',
])
def test_lazy_field_queryset_is_not_an_evaluated_collection(tmp_path, expression):
    assert not scan(tmp_path, expression)


@pytest.mark.parametrize('expression', [
    'list(Customer.objects.all())',
    'tuple(Customer.objects.filter(active=True))',
    '[customer for customer in Customer.objects.all()]',
])
def test_eager_class_collection_is_still_reported(tmp_path, expression):
    rows = scan(tmp_path, expression, field=False)
    assert len(rows) == 1
    assert rows[0]['smell_type'] == 'queryset_at_class_scope'
    assert rows[0]['kind'] == 'finding'


def test_custom_manager_method_is_only_a_candidate(tmp_path):
    rows = scan(tmp_path, 'Customer.objects.available_choices()')
    assert len(rows) == 1
    assert rows[0]['kind'] == 'candidate'
    assert rows[0]['also_caused_by']
