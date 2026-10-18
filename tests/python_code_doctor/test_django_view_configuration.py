"""Configuration-only Django generic views inherit their behavior."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'skills/python-code-doctor/scripts/find_code_smells.py'
CONFIG = '\n    model = Model\n    form_class = Form\n    template_name = "form.html"\n    success_message = "Saved"\n    error_message = "Failed"\n'


def data_findings(tmp_path, source):
    sample = tmp_path / 'sample.py'
    sample.write_text(source)
    result = subprocess.run([sys.executable, str(SCRIPT), str(sample), '--format', 'json'],
                            capture_output=True, text=True, check=True)
    return [item for item in json.loads(result.stdout) if item['smell_type'] == 'data_class']


@pytest.mark.parametrize(('imports', 'base'), [
    ('from django.views.generic import UpdateView', 'UpdateView'),
    ('from django.views.generic.edit import UpdateView as Editor', 'Editor'),
    ('from django.views import generic as views', 'views.UpdateView'),
    ('import django.views.generic as views', 'views.ListView'),
    ('import django.views.generic', 'django.views.generic.TemplateView'),
    ('from django.views.generic.detail import DetailView', 'DetailView'),
])
def test_imported_generic_configuration_is_not_data_class(tmp_path, imports, base):
    assert data_findings(tmp_path, f'{imports}\nclass Config(MessageMixin, {base}):{CONFIG}') == []


@pytest.mark.parametrize('prefix, base', [
    ('', 'object'),
    ('class UpdateView: pass', 'UpdateView'),
    ('from custom.views import UpdateView', 'UpdateView'),
    ('from django.views.generic import InventedView', 'InventedView'),
    ('from django.views.generic import UpdateView\nUpdateView = Custom', 'UpdateView'),
    ('from django.views import generic\ngeneric.UpdateView = Custom', 'generic.UpdateView'),
    ('from django.views.generic import UpdateView\ndef scope(UpdateView):\n    pass', 'UpdateView'),
    ('from django.views.generic import UpdateView\nclass UpdateView: pass', 'UpdateView'),
    ('if condition:\n    from django.views.generic import UpdateView', 'UpdateView'),
    ('from django.views.generic import UpdateView\nfrom custom import *', 'UpdateView'),
    ('from django.views.generic import UpdateView\nmatch value:\n    case {"view": UpdateView}:\n        pass', 'UpdateView'),
])
def test_plain_or_unproven_bases_stay_scored(tmp_path, prefix, base):
    assert len(data_findings(tmp_path, f'{prefix}\nclass Config({base}):{CONFIG}')) == 1


def test_later_import_does_not_prove_base_identity(tmp_path):
    assert len(data_findings(tmp_path,
                            f'class Config(UpdateView):{CONFIG}\nfrom django.views.generic import UpdateView')) == 1
