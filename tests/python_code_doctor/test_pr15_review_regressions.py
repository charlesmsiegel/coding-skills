"""Inputs that must remain visible after the PR 15 false-positive fixes."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills/python-code-doctor/scripts"


def findings(script, target, kind):
    result = subprocess.run([sys.executable, str(SCRIPTS / script), str(target),
                             "--format", "json"], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return [item for item in json.loads(result.stdout)
            if (item.get("smell_type") or item.get("issue_type")) == kind]


@pytest.mark.parametrize("body", [
    "        if recover:\n            raise\n        return None\n",
    "        if recover:\n            return None\n        raise\n",
])
def test_a_reraise_on_one_path_does_not_hide_a_swallowing_path(tmp_path, body):
    sample = tmp_path / "sample.py"
    sample.write_text("def run(work, recover):\n    try:\n        work()\n"
                      "    except BaseException:\n" + body, encoding="utf-8")
    assert findings("find_exception_issues.py", sample, "catches_baseexception")


def test_a_nested_helper_return_does_not_hide_a_terminal_reraise(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text("def run(work):\n    try:\n        work()\n"
                      "    except BaseException:\n"
                      "        def cleanup_result():\n            return 1\n"
                      "        cleanup_result()\n        raise\n", encoding="utf-8")
    assert not findings("find_exception_issues.py", sample, "catches_baseexception")


def test_a_relative_reexport_preserves_the_copying_model_family(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("from .base import FrozenModel\n", encoding="utf-8")
    (package / "base.py").write_text("from pydantic import BaseModel\n"
                                    "class FrozenModel(BaseModel):\n    pass\n", encoding="utf-8")
    (package / "consumer.py").write_text("from package import FrozenModel\n"
                                        "class Payload(FrozenModel):\n"
                                        "    tags: list[str] = []\n", encoding="utf-8")
    assert not findings("find_mutation_hazards.py", package, "mutable_class_attribute")


@pytest.mark.parametrize("config", [
    "    model_config = ConfigDict(ignored_types=(list,))\n",
    "    model_config = {'ignored_types': (list,)}\n",
    "    model_config = {'ignored_types': (list, dict)}\n",
    "    class Config:\n        keep_untouched = (list,)\n",
])
def test_an_ignored_model_attribute_is_still_shared(tmp_path, config):
    sample = tmp_path / "models.py"
    sample.write_text("from pydantic import BaseModel, ConfigDict\nclass Queue(BaseModel):\n"
                      + config + "    pending = []\n    rows: list[int] = []\n", encoding="utf-8")
    found = findings("find_mutation_hazards.py", sample, "mutable_class_attribute")
    assert len(found) == 1 and "pending" in found[0]["description"]


@pytest.mark.parametrize("decorator", ["model_validator(mode='after')", "validates_schema"])
def test_inherited_fields_do_not_make_validation_rules_incohesive(tmp_path, decorator):
    sample = tmp_path / "models.py"
    sample.write_text("from pydantic import BaseModel, model_validator\n"
                      "from marshmallow import validates_schema\n"
                      "class BaseEntry(BaseModel):\n    a: int\n    b: int\n"
                      "class Entry(BaseEntry):\n"
                      f"    @{decorator}\n    def check_a(self):\n        return self.a\n"
                      f"    @{decorator}\n    def check_b(self):\n        return self.b\n",
                      encoding="utf-8")
    assert not findings("find_coupling_issues.py", sample, "low_cohesion")
