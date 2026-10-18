"""Django's documented metadata API is not proof of private coupling."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'skills/python-code-doctor/scripts/find_design_smells.py'


def records(tmp_path, source, *args):
    sample = tmp_path / 'sample.py'
    sample.write_text(source)
    result = subprocess.run([sys.executable, str(SCRIPT), str(sample), *args],
                            capture_output=True, text=True, check=True)
    return result.stdout


@pytest.mark.parametrize('operation', ['get_field("name")', 'get_fields()', 'db_table',
                                      'local_fields', 'related_objects', 'verbose_name'])
def test_documented_django_metadata_is_unscored_when_identity_unknown(tmp_path, operation):
    result = json.loads(records(tmp_path, f'''from django.db import models

def inspect(model):
    return model._meta.{operation}
''', '--format', 'json'))
    assert len(result) == 1
    assert result[0]['kind'] == 'candidate'
    assert result[0]['also_caused_by']
    assert not result[0]['suggestion']


@pytest.mark.parametrize('source', [
    'def inspect(obj):\n    return obj._meta.db_table\n',
    'from django.db import models\ndef inspect(obj):\n    return obj._meta\n',
    'from django.db import models\ndef inspect(obj):\n    return obj._private\n',
    'from django.db import models\ndef inspect(obj):\n    return obj._meta._private\n',
    'from django.db import models\ndef inspect(obj):\n    return obj._meta.custom_secret\n',
])
def test_unknown_private_access_stays_scored(tmp_path, source):
    result = json.loads(records(tmp_path, source, '--format', 'json'))
    assert len(result) == 1
    assert result[0].get('kind', 'finding') == 'finding'
    assert result[0]['suggestion']


def test_metadata_candidate_text_explains_uncertainty(tmp_path):
    result = records(tmp_path, '''from django.core.exceptions import FieldDoesNotExist

def inspect(model):
    return model._meta.get_field("name")
''')
    assert 'CANDIDATE' in result
    assert 'Django' in result
    assert 'Found 1 design smell' not in result


def test_ignore_still_suppresses_metadata_candidates(tmp_path):
    result = json.loads(records(tmp_path, '''from django.db import models

def inspect(model):
    return model._meta.db_table
''', '--format', 'json', '--ignore', 'inappropriate_intimacy'))
    assert result == []


def test_relative_django_package_is_not_framework_evidence(tmp_path):
    result = json.loads(records(tmp_path, '''from .django import models

def inspect(obj):
    return obj._meta.db_table
''', '--format', 'json'))
    assert len(result) == 1
    assert result[0]['kind'] == 'finding'
