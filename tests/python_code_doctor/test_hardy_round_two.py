"""Keep Hardy detector evidence without charging known declarative patterns."""

import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / 'skills/python-code-doctor/scripts'


def detect(run_script, tmp_path, detector, source):
    (tmp_path / 'sample.py').write_text(source, encoding='utf-8')
    return json.loads(run_script(SCRIPTS / detector, tmp_path, '--format', 'json').stdout)


@pytest.mark.parametrize('arguments', [
    '"package.module"',
    '"package.module", fromlist=["Record"]',
    'name="package.module", fromlist=("Record",), level=0',
])
def test_literal_import_keeps_design_evidence(run_script, tmp_path, arguments):
    records = detect(run_script, tmp_path, 'find_security_issues.py',
                     f'def read():\n    return __import__({arguments})\n')
    assert len(records) == 1
    assert records[0]['smell_type'] == 'literal_import'
    assert 'dynamic' not in records[0]['description']
    assert records[0]['suggestion']


@pytest.mark.parametrize('arguments', [
    'module_name',
    '"package.module", fromlist=names',
    '*arguments',
    '"package.module", **options',
    '"package.module", globals=globals()',
])
def test_unresolved_import_arguments_keep_security_finding(run_script, tmp_path, arguments):
    records = detect(run_script, tmp_path, 'find_security_issues.py',
                     f'def read():\n    return __import__({arguments})\n')
    assert any(record['smell_type'] == 'unsafe_deserialization' for record in records)


@pytest.mark.parametrize('model_base', ['BaseModel', 'BaseSettings'])
def test_local_model_ancestry_connects_declared_fields(run_script, tmp_path, model_base):
    module = 'pydantic' if model_base == 'BaseModel' else 'pydantic_settings'
    (tmp_path / 'models.py').write_text(
        f'from {module} import {model_base}\nclass FrozenModel({model_base}): pass\n',
        encoding='utf-8',
    )
    records = detect(run_script, tmp_path, 'find_coupling_issues.py',
                     'from models import FrozenModel\nclass Entry(FrozenModel):\n'
                     '    first: int\n    second: int\n'
                     '    def one(self): return self.first\n'
                     '    def two(self): return self.second\n')
    assert not any(record['issue_type'] == 'low_cohesion' for record in records)


def test_plain_local_base_retains_disconnected_state_evidence(run_script, tmp_path):
    (tmp_path / 'models.py').write_text('class FrozenModel: pass\n', encoding='utf-8')
    records = detect(run_script, tmp_path, 'find_coupling_issues.py',
                     'from models import FrozenModel\nclass Entry(FrozenModel):\n'
                     '    first: int\n    second: int\n'
                     '    def one(self): return self.first\n'
                     '    def two(self): return self.second\n')
    assert any(record['issue_type'] == 'low_cohesion' for record in records)


@pytest.mark.parametrize('method', ['__enter__', '__exit__', '__aenter__', '__iter__',
                                    '__next__', '__call__', '_claim'])
def test_protocol_and_private_methods_are_behavior(run_script, tmp_path, method):
    records = detect(run_script, tmp_path, 'find_code_smells.py',
                     'class Lock:\n    first = 1\n    second = 2\n'
                     '    third = 3\n    fourth = 4\n'
                     f'    def {method}(self): return self.first\n')
    assert not any(record['smell_type'] == 'data_class' for record in records)


def test_initializer_alone_is_still_a_data_container(run_script, tmp_path):
    records = detect(run_script, tmp_path, 'find_code_smells.py',
                     'class Value:\n    def __init__(self):\n'
                     '        self.first = 1\n        self.second = 2\n'
                     '        self.third = 3\n        self.fourth = 4\n')
    assert any(record['smell_type'] == 'data_class' for record in records)


def test_clump_is_attributed_to_majority_file_without_losing_occurrences(run_script, tmp_path):
    (tmp_path / 'a_identity.py').write_text('def digest(name, binders, conclusion): pass\n', encoding='utf-8')
    records = detect(run_script, tmp_path, 'find_parameter_objects.py',
                     'def render(name, binders, conclusion): pass\n'
                     'def check(name, binders, conclusion): pass\n'
                     'def prove(name, binders, conclusion): pass\n')
    [clump] = [record for record in records if record['smell_type'] == 'data_clump']
    assert Path(clump['file']).name == 'sample.py'
    assert clump['line'] == 1
    assert len(clump['occurrences']) == 4
    assert any('a_identity.py:1' in member for member in clump['occurrences'])


def test_literal_import_is_scored_as_design(load_module):
    rubric = load_module(SCRIPTS.parents[1] / 'code-overview/scripts', 'rubric')
    assert rubric.categorize({'smell_type': 'literal_import', 'category': 'security'}) == ('design', True)
    assert rubric.categorize({'smell_type': 'unsafe_deserialization', 'category': 'security'}) == ('security', True)
