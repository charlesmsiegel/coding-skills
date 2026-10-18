"""Concrete origin-flow failures reported in PR 32's review threads."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


def unused_imports(tmp_path, source):
    target = tmp_path / "sample.py"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_dead_code.py"), str(target), "--format", "json"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return {item["name"] for item in json.loads(result.stdout) if item["issue_type"] == "unused_import"}


@pytest.mark.parametrize("pattern", ["_ as captured", "(_ as captured) as outer"])
def test_nested_wildcard_capture_is_exhaustive(tmp_path, pattern):
    source = ('import unused\nfrom typing import cast\n'
              f'match value:\n    case {pattern}:\n        cast = converter.cast\n'
              'value = cast("unused.Type", value)\n')
    assert "unused" in unused_imports(tmp_path, source)


@pytest.mark.parametrize("loop", ["for item in [1, 2]:", "for item in values:", "while flag:"])
def test_loop_origin_rotation_reaches_later_iterations(tmp_path, loop):
    source = ('import used\nimport typing\nimport converter\n'
              'a = typing.cast\nb = converter.cast\nc = converter.cast\n'
              + loop + '\n    a, b, c = b, c, a\n'
              'value = b("used.Type", value)\n')
    assert "used" not in unused_imports(tmp_path, source)


def test_loop_body_references_include_later_iteration_origins(tmp_path):
    source = ('import used\nfrom converter import cast\n'
              'for item in [1, 2]:\n    value = cast("used.Type", value)\n'
              '    from typing import cast\n')
    assert "used" not in unused_imports(tmp_path, source)


@pytest.mark.parametrize("keyword", ["with", "async with"])
def test_suppressing_context_keeps_partial_body_origins(tmp_path, keyword):
    source = ('import used\nimport typing\nimport converter\nfrom contextlib import suppress\n'
              'async def run(value):\n    cast = converter.cast\n'
              f'    {keyword} suppress(Exception):\n        cast = typing.cast\n'
              '        may_fail()\n        cast = converter.cast\n'
              '    return cast("used.Type", value)\n')
    assert "used" not in unused_imports(tmp_path, source)


@pytest.mark.parametrize("termination", ["return None", "raise SystemExit", "raise RuntimeError('stop')"])
def test_terminating_branch_does_not_contribute_to_later_origins(tmp_path, termination):
    source = ('import unused\nfrom typing import cast\n'
              'def run(value):\n    if flag:\n'
              f'        {termination}\n    else:\n        cast = converter.cast\n'
              '    return cast("unused.Type", value)\n')
    # The local import pins the original typing origin in this invocation.
    source = source.replace('def run(value):\n', 'def run(value):\n    from typing import cast\n')
    assert "unused" in unused_imports(tmp_path, source)


def test_known_enclosing_converter_rebinding_is_already_preserved(tmp_path):
    source = ('import used\nfrom converter import convert\n'
              'def run(value):\n    return convert("used.Type", value)\n'
              'from typing import cast as convert\nrun(value)\n')
    assert "used" not in unused_imports(tmp_path, source)


def test_exact_literal_trip_count_does_not_add_impossible_rotations(tmp_path):
    source = ('import unused\nimport typing\nimport converter\n'
              'a = typing.cast\nb = converter.cast\nc = converter.cast\n'
              'for item in [1, 2]:\n    a, b, c = b, c, a\n'
              'value = a("unused.Type", value)\n')
    assert "unused" in unused_imports(tmp_path, source)


def test_loop_fixed_point_has_a_finite_attribute_origin_domain(tmp_path):
    source = ('import unused\nimport typing\na = typing\n'
              'while flag:\n    a = a.member\n'
              'value = a.cast("unused.Type", value)\n')
    assert "unused" not in unused_imports(tmp_path, source)


def test_a_continue_runs_finally_before_the_next_loop_iteration(tmp_path):
    source = ('import unused\nimport typing\nimport converter\n'
              'cast = converter.cast\nfor item in [1, 2]:\n'
              '    try:\n        cast = typing.cast\n        continue\n'
              '    finally:\n        cast = converter.cast\n'
              'value = cast("unused.Type", value)\n')
    assert "unused" in unused_imports(tmp_path, source)


def test_returned_function_body_does_not_read_unreachable_type_strings(tmp_path):
    source = ('import unused\nfrom typing import cast\n'
              'def run(value):\n    return value\n    cast("unused.Type", value)\n')
    assert "unused" in unused_imports(tmp_path, source)


def test_exhaustive_match_excludes_returned_case_origins(tmp_path):
    source = ('import unused\ndef run(value):\n    from typing import cast\n'
              '    match flag:\n        case True:\n            return value\n'
              '        case _:\n            cast = converter.cast\n'
              '    return cast("unused.Type", value)\n')
    assert "unused" in unused_imports(tmp_path, source)
