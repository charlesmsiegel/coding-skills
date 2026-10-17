"""Declared framework fields and inherited behavior are not dataclass evidence."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'skills/python-code-doctor/scripts/find_code_smells.py'
CONFIG = '\n    first = field()\n    second = field()\n    third = field()\n    fourth = field()\n'


def records(tmp_path, source, output_format='json'):
    sample = tmp_path / 'sample.py'
    sample.write_text(source)
    result = subprocess.run([sys.executable, str(SCRIPT), str(sample), '--format', output_format],
                            capture_output=True, text=True, check=True)
    if output_format == 'text':
        return result.stdout
    return [f for f in json.loads(result.stdout) if f['smell_type'] == 'data_class']


@pytest.mark.parametrize(('imports', 'base'), [
    ('from django.db import models', 'models.Model'),
    ('from django.db.models import Model as Record', 'Record'),
    ('import django.db.models as orm', 'orm.Model'),
    ('import django.db.models', 'django.db.models.Model'),
    ('from django.db.models.base import Model', 'Model'),
    ('from django import forms', 'forms.Form'),
    ('from django.forms import Form as Input', 'Input'),
    ('from django.forms.forms import BaseForm', 'BaseForm'),
    ('from django.forms import ModelForm', 'ModelForm'),
    ('from django.forms.models import BaseModelForm', 'BaseModelForm'),
    ('from django.forms import BaseFormSet', 'BaseFormSet'),
    ('from django.forms.models import BaseInlineFormSet', 'BaseInlineFormSet'),
    ('from django.contrib import admin', 'admin.ModelAdmin'),
    ('from django.contrib.admin.options import TabularInline', 'TabularInline'),
    ('from django.db.models import TextChoices', 'TextChoices'),
    ('from django.db.models.enums import IntegerChoices', 'IntegerChoices'),
])
def test_known_declarative_bases_inherit_framework_behavior(tmp_path, imports, base):
    assert records(tmp_path, f'{imports}\nclass Config(Mixin, {base}):{CONFIG}') == []


@pytest.mark.parametrize(('prefix', 'base'), [
    ('', 'Model'),
    ('class Form: pass', 'Form'),
    ('from custom import Model', 'Model'),
    ('from django.db import models\nmodels = fake', 'models.Model'),
    ('from django import forms\nforms.Form = Custom', 'forms.Form'),
    ('from django.forms import Form\nForm = Custom', 'Form'),
    ('from django.forms import Form\ndef scope(Form): pass', 'Form'),
    ('if ready:\n    from django.forms import Form', 'Form'),
    ('from django.forms import UnknownForm', 'UnknownForm'),
    ('from django.db import models\nclass Local(models.Model): pass', 'Local'),
    ('from django.contrib.auth.views import PasswordResetView', 'PasswordResetView'),
])
def test_unresolved_inheritance_is_candidate_without_dataclass_fix(tmp_path, prefix, base):
    findings = records(tmp_path, f'{prefix}\nclass Config({base}):{CONFIG}')
    assert len(findings) == 1
    assert findings[0]['kind'] == 'candidate'
    assert not findings[0]['suggestion']
    assert findings[0]['also_caused_by']


@pytest.mark.parametrize('base', ['', '(object)'])
def test_plain_data_classes_retain_findings(tmp_path, base):
    findings = records(tmp_path, f'class Config{base}:{CONFIG}')
    assert len(findings) == 1
    assert findings[0]['kind'] == 'finding'
    assert '@dataclass' in findings[0]['suggestion']


def test_candidate_text_reports_uncertainty_and_no_fix(tmp_path):
    output = records(tmp_path, f'class Config(Custom):{CONFIG}', 'text')
    assert '[CANDIDATE]' in output
    assert '0 finding(s), 1 candidate(s)' in output
    assert 'also caused by:' in output
    assert '→' not in output


def test_framework_subclass_with_behavior_still_reports_other_smells(tmp_path):
    source = ('from django.forms import Form\nclass Config(Form):' + CONFIG
              + '\n    def save(self, values=[]):\n        return values\n')
    sample = tmp_path / 'sample.py'
    sample.write_text(source)
    result = subprocess.run([sys.executable, str(SCRIPT), str(sample), '--format', 'json'],
                            capture_output=True, text=True, check=True)
    assert 'mutable_default' in {f['smell_type'] for f in json.loads(result.stdout)}


@pytest.mark.parametrize('prefix', [
    'from custom import Model as object',
    'object = CustomBase',
    'def scope(object):\n    pass',
])
def test_rebound_object_name_is_not_proven_builtin(tmp_path, prefix):
    findings = records(tmp_path, f'{prefix}\nclass Config(object):{CONFIG}')
    assert len(findings) == 1
    assert findings[0]['kind'] == 'candidate'
    assert not findings[0]['suggestion']
