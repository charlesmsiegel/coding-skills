"""Quoted type expressions count as import uses, ordinary strings do not."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


def dead_code_issues(tmp_path, source):
    target = tmp_path / "sample.py"
    target.write_text(source)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_dead_code.py"), str(target), "--format", "json"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def unused_imports(tmp_path, source):
    return {item["name"] for item in dead_code_issues(tmp_path, source) if item["issue_type"] == "unused_import"}


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


@pytest.mark.parametrize("declaration", [
    'def result(value: "Literal[Color.RED, \'unused.Type\']"): pass',
    'async def result() -> "Literal[Color.RED, \'unused.Type\']": pass',
    'def result(*values: "Literal[Color.RED, \'unused.Type\']"): pass',
    'def result(**values: "Literal[Color.RED, \'unused.Type\']"): pass',
    'value: "Literal[Color.RED, \'unused.Type\']"',
])
@pytest.mark.parametrize("module", ["typing", "typing_extensions"])
def test_quoted_literal_values_retain_expression_names_but_not_string_data(tmp_path, declaration, module):
    source = (f"from {module} import Literal\nfrom colors import Color\nimport unused\n"
              + declaration + "\n")
    assert unused_imports(tmp_path, source) == {"unused"}


@pytest.mark.parametrize("imports, annotation", [
    ("from typing import Literal as Choices", "Choices[Color.RED, 'unused.Type']"),
    ("import typing as types", "types.Literal[Color.RED, 'unused.Type']"),
    ("from typing import Annotated", "Annotated[int, Color.RED, 'unused.Type']"),
    ("from typing import Annotated", "Annotated[int, {'color': Color.RED, 'text': 'unused.Type'}]"),
])
def test_quoted_metadata_expressions_retain_imports(tmp_path, imports, annotation):
    source = f"{imports}\nfrom colors import Color\nimport unused\nvalue: {annotation!r}\n"
    assert unused_imports(tmp_path, source) == {"unused"}


@pytest.mark.parametrize("annotation", [
    "Annotated[int, metadata()]",
    "Annotated[int, [metadata(), 'metadata()']]",
    "Literal[metadata()]",
])
def test_calls_in_quoted_metadata_do_not_report_local_helpers_as_unused(tmp_path, annotation):
    source = ("from typing import Annotated, Literal\n"
              "def metadata():\n    return 1\n"
              f"value: {annotation!r}\n")
    issues = dead_code_issues(tmp_path, source)
    assert not any(item["issue_type"] == "unused_function" and item["name"] == "metadata" for item in issues)


@pytest.mark.parametrize("quoted", [False, True])
def test_metadata_call_is_not_a_callback_reference(tmp_path, quoted):
    annotation = "Annotated[int, metadata(1)]"
    expression = repr(annotation) if quoted else annotation
    source = ("from typing import Annotated\n"
              "def metadata(unused):\n    return 1\n"
              f"value: {expression}\n")
    issues = dead_code_issues(tmp_path, source)
    assert any(item["issue_type"] == "unused_parameter" and item["name"] == "unused"
               and item["kind"] == "finding" for item in issues)
