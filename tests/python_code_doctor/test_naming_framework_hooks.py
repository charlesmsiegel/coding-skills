"""Framework callback names are required API contracts, not rename candidates."""
import json
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[2] / 'skills/python-code-doctor/scripts/find_naming_issues.py'


def names(tmp_path, source):
    path = tmp_path / 'sample.py'
    path.write_text(source)
    result = subprocess.run([sys.executable, str(SCRIPT), str(path), '--format', 'json'],
                            capture_output=True, text=True, check=True)
    return [f['description'] for f in json.loads(result.stdout)
            if f['smell_type'] == 'non_snake_case_function']


def test_imported_testcase_hooks_are_required_names(tmp_path):
    assert names(tmp_path, '''
import unittest as ut
from unittest import IsolatedAsyncioTestCase as AsyncCase
from django.test import TestCase as DatabaseCase
from django.test.testcases import TransactionTestCase
class Sync(ut.TestCase):
    def setUp(self): pass
    def tearDown(self): pass
    @classmethod
    def setUpClass(cls): pass
    @classmethod
    def tearDownClass(cls): pass
class Async(AsyncCase):
    async def asyncSetUp(self): pass
    async def asyncTearDown(self): pass
class Database(DatabaseCase):
    @classmethod
    def setUpTestData(cls): pass
class Transaction(TransactionTestCase):
    def setUp(self): pass
''') == []


def test_only_direct_framework_callbacks_are_exempt(tmp_path):
    found = names(tmp_path, '''
from unittest import TestCase
from django.test import SimpleTestCase
class Plain:
    def setUp(self): pass
class Real(TestCase):
    def setUp(self):
        def tearDown(): pass
    def assertCustom(self): pass
    def setUpTestData(self): pass
    async def asyncSetUp(self): pass
class Fake:
    class Nested:
        def setUp(self): pass
class Simple(SimpleTestCase):
    def setUpTestData(self): pass
def tearDown(): pass
''')
    assert found.count("Function 'setUp' is not snake_case") == 2
    assert found.count("Function 'tearDown' is not snake_case") == 2
    assert "Function 'assertCustom' is not snake_case" in found
    assert found.count("Function 'setUpTestData' is not snake_case") == 2
    assert "Function 'asyncSetUp' is not snake_case" in found


def test_untrusted_testcase_names_are_not_exempt(tmp_path):
    for prefix, base in [
        ('from custom import TestCase', 'TestCase'),
        ('from unittest import TestCase\nTestCase = custom', 'TestCase'),
        ('import unittest\nunittest.TestCase = custom', 'unittest.TestCase'),
        ('from unittest import TestCase\ndef helper(TestCase): pass', 'TestCase'),
        ('from unittest import TestCase\nfrom custom import *', 'TestCase'),
        ('if enabled:\n    from unittest import TestCase', 'TestCase'),
    ]:
        assert names(tmp_path, prefix + '\nclass Sample(' + base + '):\n    def setUp(self): pass\n') == [
            "Function 'setUp' is not snake_case"]
