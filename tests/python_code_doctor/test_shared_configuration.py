"""Separate declarative class configuration from demonstrated shared mutation."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'skills/python-code-doctor/scripts/find_mutation_hazards.py'


def scan(tmp_path, source):
    target = tmp_path / 'sample.py'
    target.write_text(source)
    result = subprocess.run([sys.executable, str(SCRIPT), str(target), '--format', 'json'],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('source', [
    'from project.forms import BaseForm\nclass Form(BaseForm):\n    class Meta:\n        widgets = {}\n        help_texts = {}\n',
    'class Registry:\n    handlers = {}\n',
    'from django import forms\nclass Form(forms.ModelForm):\n    class Meta:\n        widgets = {}\n        help_texts = {}\n',
    'from project.base import Base\nclass Example(Base):\n    items = []\n    def add(self, item):\n        self.items.append(item)\n',
    'class Example:\n    items = []\n    def add(self, item):\n        self = item\n        self.items.append(item)\n',
    'class Example:\n    items: list = []\n',
    'class Example:\n    items = []\n    def __init__(self):\n        self.items = []\n    def add(self, item):\n        self.items.append(item)\n',
    'class Example:\n    items = []\n    @staticmethod\n    def add(self, item):\n        self.items.append(item)\n',
])
def test_declaration_without_shared_instance_mutation_is_candidate(tmp_path, source):
    records = scan(tmp_path, source)
    assert records
    assert all(record['kind'] == 'candidate' for record in records)
    assert all(not record['suggestion'] and record['also_caused_by'] for record in records)


@pytest.mark.parametrize('mutation', ['self.items.append(value)', 'self.items[0] = value', 'del self.items[0]'])
def test_demonstrated_instance_mutation_retains_finding(tmp_path, mutation):
    record, = scan(tmp_path, 'class Example:\n    items = []\n    def add(self, value):\n        ' + mutation + '\n')
    assert record['kind'] == 'finding'
    assert record['suggestion']
    assert not record['also_caused_by']


def test_candidate_text_has_benign_explanations_without_fix_arrow(tmp_path):
    target = tmp_path / 'sample.py'
    target.write_text('class Registry:\n    handlers = {}\n')
    result = subprocess.run([sys.executable, str(SCRIPT), str(target)],
                            capture_output=True, text=True, check=True)
    assert '0 mutation hazard(s), 1 candidate(s)' in result.stdout
    assert '[CANDIDATE]' in result.stdout
    assert 'also caused by:' in result.stdout
    assert '→' not in result.stdout
