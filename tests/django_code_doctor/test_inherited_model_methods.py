"""Inherited model behavior must not be reported as missing local declarations."""

from helpers import build_project, run_detector


def model_records(tmp_path, source):
    project = build_project(tmp_path / 'p', {'app/models.py': source})
    return run_detector('find_model_issues.py', project)


def test_transitive_model_str_and_ordering_are_inherited(tmp_path):
    records = model_records(tmp_path, '''from django.db import models
class Named(models.Model):
    def __str__(self):
        return self.name
    class Meta:
        ordering = ['name']
class Human(Named):
    pass
class AutumnPerson(Human):
    pass
''')
    assert not [r for r in records if r['smell_type'] in {'missing_str_method', 'no_default_ordering'}]


def test_project_class_named_model_keeps_its_str_method(tmp_path):
    records = model_records(tmp_path, '''from django.db import models
class Model(models.Model):
    def __str__(self):
        return 'named'
class Character(Model):
    pass
''')
    assert not [r for r in records if r['smell_type'] == 'missing_str_method']


def test_unknown_mixin_ancestry_is_a_candidate(tmp_path):
    records = model_records(tmp_path, '''from django.db import models
from project.mixins import Named
class Child(Named, models.Model):
    pass
''')
    relevant = [r for r in records if r['smell_type'] in {'missing_str_method', 'no_default_ordering'}]
    assert len(relevant) == 2
    assert all(r['kind'] == 'candidate' and not r['suggestion'] and r['also_caused_by'] for r in relevant)


def test_direct_model_without_str_retains_finding(tmp_path):
    records = model_records(tmp_path, 'from django.db import models\nclass Plain(models.Model):\n    pass\n')
    record, = [r for r in records if r['smell_type'] == 'missing_str_method']
    assert record['kind'] == 'finding'
    assert record['suggestion']


def test_explicit_empty_ordering_does_not_inherit_parent_ordering(tmp_path):
    records = model_records(tmp_path, '''from django.db import models
class Parent(models.Model):
    class Meta:
        ordering = ['pk']
class Child(Parent):
    class Meta:
        ordering = []
''')
    records = [r for r in records if r['smell_type'] == 'no_default_ordering']
    assert len(records) == 1
    assert 'Child' in records[0]['description']


def test_abstract_meta_is_not_inherited_when_child_defines_own_meta(tmp_path):
    records = model_records(tmp_path, """from django.db import models
class Named(models.Model):
    class Meta:
        abstract = True
        ordering = ['pk']
class Child(Named):
    class Meta:
        verbose_name = 'Child'
""")
    record, = [r for r in records if r['smell_type'] == 'no_default_ordering']
    assert 'Child' in record['description']


def test_dynamic_ordering_and_assigned_str_are_candidates(tmp_path):
    records = model_records(tmp_path, """from django.db import models
from project.config import ORDERING, display
class Child(models.Model):
    __str__ = display
    class Meta:
        ordering = ORDERING
""")
    relevant = [r for r in records if r['smell_type'] in {'missing_str_method', 'no_default_ordering'}]
    assert len(relevant) == 2
    assert all(r['kind'] == 'candidate' for r in relevant)


def test_import_path_resolves_unrelated_duplicate_class_names(tmp_path):
    project = build_project(tmp_path / 'p', {
        'app/base.py': 'from django.db import models\nclass Named(models.Model):\n    def __str__(self):\n        return "name"\n',
        'app/models.py': 'from django.db import models\nfrom app.base import Named\nclass Child(Named, models.Model):\n    pass\n',
        'app/tests.py': 'def helper():\n    class Named:\n        pass\n',
    })
    records = run_detector('find_model_issues.py', project)
    assert not [r for r in records if r['smell_type'] == 'missing_str_method' and 'Child' in r['description']]


def test_relative_aliased_import_preserves_inherited_behavior(tmp_path):
    project = build_project(tmp_path / 'p', {
        'app/base.py': 'from django.db import models\nclass Named(models.Model):\n    def __str__(self):\n        return "name"\n    class Meta:\n        ordering = ["pk"]\n',
        'app/models.py': 'from django.db import models\nfrom .base import Named as Base\nclass Child(Base, models.Model):\n    pass\n',
    })
    records = run_detector('find_model_issues.py', project)
    assert not [r for r in records if r['smell_type'] in {'missing_str_method', 'no_default_ordering'}]


def test_external_import_does_not_resolve_to_same_named_project_class(tmp_path):
    project = build_project(tmp_path / 'p', {
        'app/base.py': 'from django.db import models\nclass Named(models.Model):\n    pass\n',
        'app/models.py': 'from django.db import models\nfrom external.library import Named\nclass Child(Named, models.Model):\n    pass\n',
    })
    records = run_detector('find_model_issues.py', project)
    record, = [r for r in records if r['smell_type'] == 'missing_str_method' and 'Child' in r['description']]
    assert record['kind'] == 'candidate'
