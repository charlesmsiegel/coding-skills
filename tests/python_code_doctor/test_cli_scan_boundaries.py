"""A bounded scan retains its own CLI directory, not checkout ancestors."""

import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "skills/python-code-doctor/scripts/find_debug_leftovers.py"


def scan(run_script, root):
    result = run_script(SCRIPT, root, "--format", "json")
    return json.loads(result.stdout)


@pytest.mark.parametrize("directory", ["scripts", "bin", "cli", "tools"])
@pytest.mark.parametrize("relative", ["output.py", "support/output.py"])
def test_a_scanned_cli_directory_keeps_its_output_helpers_quiet(
        tmp_path, run_script, directory, relative):
    checkout = tmp_path / "project"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    root = checkout / directory
    helper = root / relative
    helper.parent.mkdir(parents=True)
    helper.write_text("def emit(value):\n    print(value)\n", encoding="utf-8")

    assert scan(run_script, root) == []


@pytest.mark.parametrize("relative", ["project/src", "scripts/project/src", "scripts/project"])
def test_a_checkout_ancestor_does_not_make_source_a_cli(tmp_path, run_script, relative):
    root = tmp_path / relative
    root.mkdir(parents=True)
    (root / "service.py").write_text("def compute(value):\n    print(value)\n", encoding="utf-8")

    assert [r["smell_type"] for r in scan(run_script, root)] == ["debug_print"]


@pytest.mark.parametrize("relative", ["service.py", "src/service.py"])
@pytest.mark.parametrize("marker", ["pyproject.toml", ".git"])
def test_a_marked_checkout_named_scripts_is_not_wholly_cli(
        tmp_path, run_script, relative, marker):
    root = tmp_path / "scripts"
    source = root / relative
    source.parent.mkdir(parents=True)
    (root / marker).write_text("", encoding="utf-8")
    source.write_text("print('debug')\n", encoding="utf-8")

    assert [r["smell_type"] for r in scan(run_script, root)] == ["debug_print"]


def test_cli_output_does_not_exempt_real_debugger_calls(tmp_path, run_script):
    root = tmp_path / "scripts"
    root.mkdir()
    (root / "output.py").write_text("print('result')\nbreakpoint()\n", encoding="utf-8")

    assert [r["smell_type"] for r in scan(run_script, root)] == ["breakpoint_call"]
