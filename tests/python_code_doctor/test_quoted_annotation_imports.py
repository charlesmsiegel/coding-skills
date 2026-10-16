"""Quoted type expressions count as import uses, ordinary strings do not."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


def unused_imports(tmp_path, source):
    target = tmp_path / "sample.py"
    target.write_text(source)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_dead_code.py"), str(target), "--format", "json"],
        capture_output=True, text=True, check=True,
    )
    return {item["name"] for item in json.loads(result.stdout) if item["issue_type"] == "unused_import"}


@pytest.mark.parametrize("declaration", [
    'def result() -> dict[str, "models.Model"]: pass',
    'async def result() -> "dict[str, models.Model]": pass',
    'def result(value: "models.Model"): pass',
    'def result(value: list["models.Model"]): pass',
    'value: "models.Model"',
    'class Container:\n    value: dict[str, "models.Model"]',
    'def result() -> "dict[str, \'models.Model\']": pass',
    'def result(*values: "models.Model", **named: "models.Model"): pass',
])
def test_quoted_annotation_uses_import(tmp_path, declaration):
    assert unused_imports(tmp_path, 'from django.db import models\n' + declaration + '\n') == set()


def test_quoted_annotation_uses_module_alias_and_preserves_unused_import(tmp_path):
    assert unused_imports(tmp_path, 'import django.db.models as orm\nimport unused\n'
                          'def result() -> list["orm.Model"]: pass\n') == {"unused"}


def test_quoted_annotation_with_future_annotations(tmp_path):
    assert unused_imports(tmp_path, 'from __future__ import annotations\n'
                          'from django.db import models\n'
                          'def result() -> dict[str, "models.Model"]: pass\n') <= {"annotations"}


@pytest.mark.parametrize("module", ["typing", "typing_extensions"])
@pytest.mark.parametrize("annotation", [
    'Literal["models.Model"]',
    'Annotated[int, "models.Model"]',
    'Values["models.Model"]',
    'Metadata[int, "models.Model"]',
])
def test_quoted_type_metadata_does_not_use_import(tmp_path, annotation, module):
    source = ('from django.db import models\n'
              f'from {module} import Literal, Annotated, Literal as Values, Annotated as Metadata\n'
              f'value: {annotation!r}\n')
    assert "models" in unused_imports(tmp_path, source)


@pytest.mark.parametrize("name", ["Literal", "Annotated"])
def test_custom_generic_names_are_type_positions(tmp_path, name):
    assert unused_imports(tmp_path, f'from mytypes import {name}\n'
                          'from django.db import models\n'
                          f'value: {name}["models.Model"]\n') == set()


@pytest.mark.parametrize("statement", [
    '"models.Model"',
    'value = "models.Model"',
    'def result():\n    "models.Model"\n    return "models.Model"',
    'value: Literal["models.Model"]',
    'value: Annotated[int, "models.Model"]',
    'value: "not a valid models.Model expression!"',
])
def test_non_type_strings_do_not_use_import(tmp_path, statement):
    assert "models" in unused_imports(tmp_path, 'from django.db import models\n'
                                      'from typing import Literal, Annotated\n' + statement + '\n')


@pytest.mark.parametrize('name, annotation', [
    ('Literal', 'Literal["unused.Type"]'),
    ('Annotated', 'Annotated[int, "unused.Type"]'),
])
def test_local_typing_metadata_does_not_use_module_import(tmp_path, name, annotation):
    source = f'import unused\ndef f():\n    from typing import {name}\n    value: {annotation}\n'
    assert 'unused' in unused_imports(tmp_path, source)


@pytest.mark.parametrize('name', ['Literal', 'Annotated'])
def test_local_shadowing_keeps_custom_generic_type_arguments(tmp_path, name):
    annotation = f'{name}["used.Type"]' if name == 'Literal' else 'Annotated[int, "used.Type"]'
    source = (f'import used\nfrom typing import {name}\n'
              f'def f({name}):\n    value: {annotation}\n')
    assert 'used' not in unused_imports(tmp_path, source)


def test_local_typing_import_does_not_leak_into_sibling_function(tmp_path):
    source = ('import used\n'
              'def f():\n    from typing import Literal\n    value: Literal["used.Type"]\n'
              'def g(Literal):\n    value: Literal["used.Type"]\n')
    assert 'used' not in unused_imports(tmp_path, source)
