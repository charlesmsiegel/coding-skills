"""Assertion context managers must count without leaking across nested scopes."""

import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


def findings_for(tmp_path, source):
    target = tmp_path / "test_context.py"
    target.write_text(dedent(source))
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_test_smells.py"), str(target), "--format", "json"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("context", [
    "self.assertRaises(KeyError)",
    "self.assertRaisesRegex(KeyError, 'missing')",
    "self.assertWarns(UserWarning)",
    "self.assertWarnsRegex(UserWarning, 'deprecated')",
    "self.assertNumQueries(1)",
    "self.assertLogs('app')",
    "self.assertNoLogs('app')",
    "pytest.raises(KeyError)",
    "pytest.warns(UserWarning)",
])
def test_assertion_context_is_meaningful(tmp_path, context):
    # Removing assertion context recognition must report a trivial-only test here.
    findings = findings_for(tmp_path, f"""
        def test_operation(self):
            with {context}:
                operation()
            assert True
    """)
    assert not {f["smell_type"] for f in findings} & {"test_without_assertion", "trivial_assertion"}


def test_unittest_assertion_context_alone_satisfies_test(tmp_path):
    findings = findings_for(tmp_path, """
        def test_missing(self):
            with self.assertRaises(KeyError):
                mapping['missing']
    """)
    assert findings == []


@pytest.mark.parametrize("context", ["open('input.txt')", "self.subTest(case=1)", "transaction.atomic()"])
def test_arbitrary_context_does_not_satisfy_test(tmp_path, context):
    findings = findings_for(tmp_path, f"""
        def test_operation(self):
            with {context}:
                operation()
    """)
    assert [f["smell_type"] for f in findings] == ["test_without_assertion"]


@pytest.mark.parametrize("definition", ["def helper():", "async def helper():", "class Helper:"])
def test_nested_assertion_context_does_not_satisfy_outer_test(tmp_path, definition):
    findings = findings_for(tmp_path, f"""
        def test_operation(self):
            {definition}
                with self.assertRaises(KeyError):
                    operation()
    """)
    assert [f["smell_type"] for f in findings] == ["test_without_assertion"]
