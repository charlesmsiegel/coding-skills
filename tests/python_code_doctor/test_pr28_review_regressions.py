"""Receiver scope and rebinding controls for the PR 28 detector changes."""

import json
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "skills/python-code-doctor/scripts"


@pytest.mark.parametrize(("nested", "expected"), [
    ("        class Payload:\n"
     "            def __init__(self):\n                self.cache = None\n", 1),
    ("        def prepare(self):\n            self.cache = None\n", 1),
    ("        def remember():\n            self.cache = key\n"
     "        remember()\n", 0),
])
def test_wrapper_state_belongs_to_the_enclosing_receiver(run_script, tmp_path, nested, expected):
    source = tmp_path / "sample.py"
    source.write_text("class Wrapper:\n"
                      "    def __init__(self, client):\n        self.client = client\n"
                      "    def get(self, key):\n" + nested
                      + "        return self.client.get(key)\n"
                      "    def put(self, key, value):\n        return self.client.put(key, value)\n",
                      encoding="utf-8")

    result = run_script(SCRIPTS / "find_overengineering.py", source, "--format", "json")
    wrappers = [item for item in json.loads(result.stdout) if item["issue_type"] == "thin_wrapper"]

    assert len(wrappers) == expected


@pytest.mark.parametrize(("declaration", "late_import", "expected"), [
    ("build = None\n", True, []),
    ("def build():\n    return 0\n", True, []),
    ("build = None\n", False, ["server.py"]),
    ("def build():\n    return 0\n", False, ["server.py"]),
])
def test_reexport_identity_follows_the_last_direct_binding(
        run_script, tmp_path, declaration, late_import, expected):
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "server.py").write_text("def build():\n    return 1\n", encoding="utf-8")
    imported = "from .server import build\n"
    (package / "facade.py").write_text(
        declaration + imported if late_import else imported + declaration, encoding="utf-8")
    (tmp_path / "test_entry.py").write_text("from package.facade import build\n", encoding="utf-8")

    result = run_script(SCRIPTS / "find_untested_modules.py", tmp_path, "--format", "json")
    untested = [Path(item["file"]).name for item in json.loads(result.stdout)
                if item["smell_type"] == "untested_module"]

    assert untested == expected
