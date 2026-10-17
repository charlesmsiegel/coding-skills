"""Inherited model configuration must not hide shared mutable class state."""

from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "skills/python-code-doctor/scripts"


@pytest.mark.parametrize("base_location", ["local", "imported", "reexported"])
@pytest.mark.parametrize("configuration", [
    "    model_config = ConfigDict(ignored_types=(list,))\n",
    "    model_config = {'ignored_types': (list,)}\n",
    "    class Config:\n        keep_untouched = (list,)\n",
])
def test_inherited_ignored_types_preserve_shared_attribute_findings(
        load_module, tmp_path, base_location, configuration):
    detector = load_module(SCRIPTS, "find_mutation_hazards")
    base = ("from pydantic import BaseModel, ConfigDict\n"
            "class IgnoringModel(BaseModel):\n" + configuration)
    source = tmp_path / "models.py"
    if base_location == "local":
        prefix = base
    elif base_location == "imported":
        (tmp_path / "base.py").write_text(base, encoding="utf-8")
        prefix = "from base import IgnoringModel\n"
    else:
        package = tmp_path / "package"
        package.mkdir()
        (package / "base.py").write_text(base, encoding="utf-8")
        (package / "__init__.py").write_text("from .base import IgnoringModel\n", encoding="utf-8")
        prefix = "from package import IgnoringModel\n"
    source.write_text(prefix + "class Queue(IgnoringModel):\n"
                      "    pending = []\n    rows: list[int] = []\n", encoding="utf-8")

    findings = [item for item in detector.analyze_file(source, set())
                if item.smell_type == "mutable_class_attribute"]

    assert len(findings) == 1
    assert "'pending'" in findings[0].description


@pytest.mark.parametrize(("configuration", "expected_names"), [
    ("    model_config = ConfigDict(ignored_types=())\n", []),
    ("    model_config = {'ignored_types': (dict,)}\n", ["mapping"]),
    ("    model_config = ConfigDict(frozen=True)\n", ["pending"]),
    ("    class Config:\n        keep_untouched = ()\n", []),
])
def test_subclass_configuration_overrides_only_explicit_ignored_types(
        load_module, tmp_path, configuration, expected_names):
    detector = load_module(SCRIPTS, "find_mutation_hazards")
    source = tmp_path / "models.py"
    base_configuration = ("    class Config:\n        keep_untouched = (list,)\n"
                          if configuration.startswith("    class Config:") else
                          "    model_config = ConfigDict(ignored_types=(list,))\n")
    source.write_text("from pydantic import BaseModel, ConfigDict\n"
                      "class IgnoringModel(BaseModel):\n"
                      + base_configuration + "class Queue(IgnoringModel):\n" + configuration
                      + "    pending = []\n    mapping = {}\n"
                      "    rows: list[int] = []\n", encoding="utf-8")

    findings = [item for item in detector.analyze_file(source, set())
                if item.smell_type == "mutable_class_attribute"]

    assert len(findings) == len(expected_names)
    for name, finding in zip(expected_names, findings):
        assert f"'{name}'" in finding.description
