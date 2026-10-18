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


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("imports, callee", [
    ("import converter", "converter.cast"),
    ("from converter import cast", "cast"),
])
def test_unrelated_cast_strings_remain_metadata_data(tmp_path, quoted, imports, callee):
    annotation = f"Annotated[int, {callee}('unused.Type', 1)]"
    source = (f"import unused\nfrom typing import Annotated\n{imports}\n"
              f"value: {repr(annotation) if quoted else annotation}\n")
    assert "unused" in unused_imports(tmp_path, source)


@pytest.mark.parametrize("module", ["typing", "typing_extensions"])
@pytest.mark.parametrize("imports, callee", [
    ("from {module} import cast as convert", "convert"),
    ("import {module} as types", "types.cast"),
])
def test_typing_cast_aliases_retain_quoted_type_imports(tmp_path, module, imports, callee):
    annotation = f"Annotated[int, {callee}('used.Type', 1)]"
    source = (f"import used\nfrom typing import Annotated\n{imports.format(module=module)}\n"
              f"value: {annotation!r}\n")
    assert "used" not in unused_imports(tmp_path, source)


def test_a_shadowed_typing_cast_is_an_ordinary_metadata_call(tmp_path):
    source = ("import unused\nfrom typing import Annotated, cast\n"
              "def run(cast):\n    value: \"Annotated[int, cast('unused.Type', 1)]\"\n")
    assert "unused" in unused_imports(tmp_path, source)


def test_typing_cast_type_reader_preserves_literal_string_data(tmp_path):
    source = ("import unused\nfrom typing import Annotated, Literal, cast\n"
              "value: \"Annotated[int, cast(\\\"Literal['unused.Type']\\\", 1)]\"\n")
    assert "unused" in unused_imports(tmp_path, source)


@pytest.mark.parametrize("declaration", [
    "def result(cast=cast('used.Type', None)):\n    return cast",
    "async def result(*, cast=cast('used.Type', None)):\n    return cast",
    "@cast('used.Type', lambda function: function)\ndef result(cast):\n    return cast",
    "result = lambda cast=cast('used.Type', None): cast",
])
def test_definition_expressions_resolve_cast_in_the_enclosing_scope(tmp_path, declaration):
    source = f"import used\nfrom typing import cast\n{declaration}\n"
    assert "used" not in unused_imports(tmp_path, source)


def test_a_default_using_an_enclosing_parameter_does_not_become_a_typing_cast(tmp_path):
    source = ("import unused\nfrom typing import cast\n"
              "def outer(cast):\n"
              "    def inner(cast=cast('unused.Type', None)):\n        return cast\n"
              "    return inner\n")
    assert "unused" in unused_imports(tmp_path, source)


@pytest.mark.parametrize("metadata", [
    "(lambda cast: cast('unused.Type', 1))(object())",
    "[cast('unused.Type', 1) for cast in converters]",
    "{cast('unused.Type', 1) for cast in converters}",
    "{key: cast('unused.Type', 1) for key, cast in converters}",
    "tuple(cast('unused.Type', 1) for cast in converters)",
    "[value for cast in converters for value in cast('unused.Type', [])]",
])
@pytest.mark.parametrize("quoted", [False, True])
def test_expression_local_cast_bindings_keep_type_like_strings_as_data(tmp_path, metadata, quoted):
    annotation = f"Annotated[int, {metadata}]"
    expression = repr(annotation) if quoted else annotation
    source = ("import unused\nfrom typing import Annotated, cast\n"
              f"value: {expression}\n")
    assert "unused" in unused_imports(tmp_path, source)


@pytest.mark.parametrize("metadata", [
    "(lambda cast=cast('used.Type', 1): cast)()",
    "[cast for cast in cast('used.Type', [])]",
    "[cast('used.Type', value) for value in values]",
])
def test_nested_metadata_keeps_enclosing_typing_casts(tmp_path, metadata):
    annotation = f"Annotated[int, {metadata}]"
    source = f"import used\nfrom typing import Annotated, cast\nvalue: {annotation!r}\n"
    assert "used" not in unused_imports(tmp_path, source)


@pytest.mark.parametrize("metadata", [
    "(lambda cast: cast('unused.Type', 1))(object())",
    "[cast('unused.Type', 1) for cast in converters]",
])
def test_metadata_expression_bindings_do_not_leak_to_the_next_argument(tmp_path, metadata):
    annotation = f"Annotated[int, {metadata}, cast('used.Type', 1)]"
    source = f"import used\nimport unused\nfrom typing import Annotated, cast\nvalue: {annotation!r}\n"
    assert "used" not in unused_imports(tmp_path, source)
    assert "unused" in unused_imports(tmp_path, source)


@pytest.mark.parametrize("imports,callee", [
    ("from typing import cast", "cast"),
    ("from typing_extensions import cast as convert", "convert"),
    ("import typing as types", "types.cast"),
])
def test_a_function_body_resolves_module_imports_bound_after_its_definition(tmp_path, imports, callee):
    source = (f"import used\ndef result(value):\n    return {callee}('used.Type', value)\n"
              f"{imports}\n")
    assert "used" not in unused_imports(tmp_path, source)


@pytest.mark.parametrize("binding", [
    "from converter import cast",
    "from typing import cast\ncast = converter",
])
def test_a_deferred_body_does_not_assume_a_later_cast_is_typing(tmp_path, binding):
    source = ("import unused\ndef result(value):\n    return cast('unused.Type', value)\n"
              f"{binding}\n")
    assert "unused" in unused_imports(tmp_path, source)


def test_a_method_body_does_not_close_over_class_cast_bindings(tmp_path):
    source = ("import used\nclass Container:\n    cast = converter\n"
              "    def result(self, value):\n        return cast('used.Type', value)\n"
              "from typing import cast\n")
    assert "used" not in unused_imports(tmp_path, source)


def test_a_nested_body_sees_its_enclosing_functions_later_import(tmp_path):
    source = ("import used\ndef outer():\n"
              "    def inner(value):\n        return cast('used.Type', value)\n"
              "    from typing import cast\n    return inner\n")
    assert "used" not in unused_imports(tmp_path, source)


@pytest.mark.parametrize("rebind", ["cast = converter", "from converter import cast"])
def test_a_cast_used_before_later_rebinding_keeps_its_type_import(tmp_path, rebind):
    source = ("import used\nfrom typing import cast\ndef result(value):\n"
              "    return cast('used.Type', value)\n"
              f"value = result(1)\n{rebind}\n")
    assert "used" not in unused_imports(tmp_path, source)


def test_a_nested_class_header_uses_its_enclosing_class_namespace(tmp_path):
    source = ("import used\nclass Container:\n    from typing import cast as convert\n"
              "    @convert('used.Type', lambda cls: cls)\n    class Nested:\n        pass\n")
    assert "used" not in unused_imports(tmp_path, source)


@pytest.mark.parametrize("statement", [
    "cast = cast('used.Type', None)",
    "cast: object = cast('used.Type', None)",
    "(cast := cast('used.Type', None))",
    "for cast in cast('used.Type', []):\n    pass",
])
@pytest.mark.parametrize("scope", ["module", "class"])
def test_rebinding_visits_the_value_before_shadowing_the_callee(tmp_path, statement, scope):
    if scope == "class":
        statement = "class Container:\n" + "\n".join("    " + line for line in statement.splitlines())
    source = f"import used\nfrom typing import cast\n{statement}\n"
    assert "used" not in unused_imports(tmp_path, source)


def test_an_async_loop_reads_its_iterable_before_binding_its_target(tmp_path):
    source = ("import used\nasync def outer():\n    from typing import cast\n"
              "    async for cast in cast('used.Type', stream):\n        pass\n")
    assert "used" not in unused_imports(tmp_path, source)


@pytest.mark.parametrize("module", ["typing", "typing_extensions"])
@pytest.mark.parametrize("binding", ["", "cast = converter\n", "from converter import *\n"])
def test_typing_wildcard_records_cast_until_a_later_binding(tmp_path, module, binding):
    source = f"import used\nfrom {module} import *\n{binding}value = cast('used.Type', None)\n"
    assert ("used" in unused_imports(tmp_path, source)) == bool(binding)


@pytest.mark.parametrize("condition,branches", [
    ("False", "    cast = converter\n"),
    ("True", "    pass\nelse:\n    cast = converter\n"),
])
def test_a_nonexecuting_branch_does_not_replace_the_visible_cast(tmp_path, condition, branches):
    source = ("import used\nfrom typing import cast\n"
              f"if {condition}:\n{branches}"
              "def result(value):\n    return cast('used.Type', value)\n")
    assert "used" not in unused_imports(tmp_path, source)


def test_an_executing_branch_can_replace_the_visible_cast(tmp_path):
    source = ("import unused\nfrom typing import cast\nif True:\n    cast = converter\n"
              "def result(value):\n    return cast('unused.Type', value)\n")
    assert "unused" in unused_imports(tmp_path, source)


def test_quoted_metadata_stores_do_not_rebind_the_surrounding_source(tmp_path):
    source = ('import used\nfrom typing import Annotated, cast\n'
              'value: "Annotated[int, (cast := converter)]"\n'
              'value = cast("used.Type", None)\n')
    assert "used" not in unused_imports(tmp_path, source)


def test_a_known_converter_call_does_not_become_typing_after_a_later_import(tmp_path):
    source = ('import unused\ndef run(value):\n    from converter import convert\n'
              '    value = convert("unused.Type", value)\n'
              '    from typing import cast as convert\n    return value\n')
    assert "unused" in unused_imports(tmp_path, source)


@pytest.mark.parametrize("expression", [
    '[convert := converter for _ in [0]]',
    '{convert := converter for _ in [0]}',
    '{key: (convert := converter) for key in [0]}',
])
def test_comprehension_walrus_binds_in_its_containing_scope(tmp_path, expression):
    source = (f'import unused\nfrom typing import cast as convert\n{expression}\n'
              'value = convert("unused.Type", None)\n')
    assert "unused" in unused_imports(tmp_path, source)


@pytest.mark.parametrize("binding", [
    'convert = converter.convert',
    'from converter import convert',
])
def test_an_inner_function_uses_its_enclosing_functions_local_binding(tmp_path, binding):
    source = ('import unused\nfrom typing import cast as convert\ndef outer():\n'
              '    def inner(value):\n        return convert("unused.Type", value)\n'
              f'    {binding}\n    return inner\n')
    assert "unused" in unused_imports(tmp_path, source)


@pytest.mark.parametrize("scope", ["module", "class"])
def test_a_bare_annotation_does_not_replace_an_existing_import(tmp_path, scope):
    statement = 'Kind: Kind["unused"]'
    if scope == "class":
        statement = 'class Container:\n    ' + statement
    source = f'from typing import Literal as Kind\nimport unused\n{statement}\n'
    assert "unused" in unused_imports(tmp_path, source)


def test_a_generator_body_does_not_rebind_its_containing_scope_at_creation(tmp_path):
    source = ('import used\nfrom typing import cast as convert\n'
              'rows = (convert := converter for _ in [0])\n'
              'value = convert("used.Type", None)\n')
    assert "used" not in unused_imports(tmp_path, source)


def test_a_generators_first_iterable_still_executes_in_the_containing_scope(tmp_path):
    source = ('import unused\nfrom typing import cast as convert\n'
              'rows = (value for value in (convert := converter))\n'
              'value = convert("unused.Type", None)\n')
    assert "unused" in unused_imports(tmp_path, source)


def test_a_lambda_walrus_declares_a_local_before_its_first_reference(tmp_path):
    source = ('import unused\nfrom typing import cast as convert\n'
              'run = lambda value: (convert("unused.Type", value), (convert := converter))\n')
    assert "unused" in unused_imports(tmp_path, source)


@pytest.mark.parametrize("condition,body", [
    ('False', '        cast = converter\n'),
    ('True', '        pass\n    else:\n        cast = converter\n'),
])
def test_a_nonexecuting_local_branch_preserves_a_previous_cast_import(tmp_path, condition, body):
    source = ('import used\ndef run(value):\n    from typing import cast\n'
              f'    if {condition}:\n{body}'
              '    return cast("used.Type", value)\n')
    assert "used" not in unused_imports(tmp_path, source)


@pytest.mark.parametrize("target,imports", [
    ('holder.value', 'import holder'),
    ('mapping[key]', 'import mapping\nimport key'),
    ('holder.values[key]', 'import holder\nimport key'),
])
def test_a_value_less_complex_annotation_reads_its_target(tmp_path, target, imports):
    assert unused_imports(tmp_path, f'{imports}\n{target}: int\n') == set()


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 type parameters require Python 3.12")
@pytest.mark.parametrize("declaration", [
    'def build[T: "models.Model"](): pass',
    'class Build[T: "models.Model"]: pass',
    'type Build[T: "models.Model"] = T',
    'def build[T: (int, "models.Model")](): pass',
    'class Build[T: (int, "models.Model")]: pass',
    'type Build[T: (int, "models.Model")] = T',
])
def test_quoted_type_parameter_bounds_and_constraints_use_imports(tmp_path, declaration):
    assert "models" not in unused_imports(tmp_path, f'import models\n{declaration}\n')


@pytest.mark.skipif(sys.version_info < (3, 13), reason="PEP 696 defaults require Python 3.13")
@pytest.mark.parametrize("declaration", [
    'def build[T = "models.Model"](): pass',
    'class Build[T = "models.Model"]: pass',
    'type Build[T = "models.Model"] = T',
    'def build[**P = ["models.Model"]](): pass',
    'def build[*Ts = *tuple["models.Model"]](): pass',
])
def test_quoted_type_parameter_defaults_use_imports(tmp_path, declaration):
    assert "models" not in unused_imports(tmp_path, f'import models\n{declaration}\n')


@pytest.mark.parametrize("branch", [
    'if flag:\n    cast = converter\n',
    'if flag:\n    cast = converter\nelse:\n    pass\n',
    'if flag:\n    pass\nelse:\n    cast = converter\n',
])
def test_a_conditional_shadow_retains_the_possible_typing_origin(tmp_path, branch):
    source = ('import used\nfrom typing import cast\n' + branch
              + 'def run(value):\n    return cast("used.Type", value)\n')
    assert "used" not in unused_imports(tmp_path, source)


def test_both_conditional_branches_can_replace_a_typing_origin(tmp_path):
    source = ('import unused\nfrom typing import cast\n'
              'if flag:\n    cast = converter\nelse:\n    cast = other_converter\n'
              'def run(value):\n    return cast("unused.Type", value)\n')
    assert "unused" in unused_imports(tmp_path, source)


@pytest.mark.parametrize("name,annotation", [
    ('Literal', "Literal['unused.Type']"),
    ('Annotated', "Annotated[int, 'unused.Type']"),
    ('Literal as Values', "Values['unused.Type']"),
])
def test_later_typing_import_resolves_a_fully_quoted_special_form(tmp_path, name, annotation):
    source = (f'import unused\ndef run(value: {annotation!r}): pass\n'
              f'from typing import {name}\n')
    assert "unused" in unused_imports(tmp_path, source)


def test_a_later_custom_generic_still_reads_its_forward_reference(tmp_path):
    source = ('import used\ndef run(value: "Values[\'used.Type\']"): pass\n'
              'from custom_types import Values\n')
    assert "used" not in unused_imports(tmp_path, source)


def test_deleting_a_class_shadow_exposes_the_enclosing_typing_import(tmp_path):
    source = ('import used\nfrom typing import cast\nclass Container:\n'
              '    cast = converter\n    del cast\n'
              '    value = cast("used.Type", None)\n')
    assert "used" not in unused_imports(tmp_path, source)


def test_deleting_a_function_local_does_not_expose_the_global_cast(tmp_path):
    source = ('import unused\nfrom typing import cast\ndef run(value):\n'
              '    cast = converter\n    del cast\n'
              '    return cast("unused.Type", value)\n')
    assert "unused" in unused_imports(tmp_path, source)


def test_a_call_before_its_own_function_local_import_is_not_a_typing_cast(tmp_path):
    source = ('import unused\ndef run(value):\n'
              '    value = convert("unused.Type", value)\n'
              '    from typing import cast as convert\n    return value\n')
    assert "unused" in unused_imports(tmp_path, source)


def test_a_nested_function_can_use_a_later_enclosing_cast_import(tmp_path):
    source = ('import used\ndef outer():\n    def inner(value):\n'
              '        return convert("used.Type", value)\n'
              '    from typing import cast as convert\n    return inner\n')
    assert "used" not in unused_imports(tmp_path, source)


def test_a_possible_custom_generic_retains_its_type_argument(tmp_path):
    source = ('import used\nfrom typing import Literal\n'
              'if flag:\n    Literal = custom_generic\n'
              'value: Literal["used.Type"]\n')
    assert "used" not in unused_imports(tmp_path, source)
