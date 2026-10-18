"""Recall regressions found while independently reviewing PR 27."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills/python-code-doctor/scripts"


def records(script, target, kind):
    result = subprocess.run([sys.executable, str(SCRIPTS / script), str(target),
                             "--format", "json"], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return [item for item in json.loads(result.stdout)
            if (item.get("smell_type") or item.get("issue_type")) == kind]


@pytest.mark.parametrize("body", [
    "        if recover:\n            raise\n        return None\n",
    "        if recover:\n            return None\n        raise\n",
    "        discard(exc)\n",
])
def test_a_handler_must_propagate_on_every_visible_path(tmp_path, body):
    sample = tmp_path / "app.py"
    sample.write_text("def discard(error):\n    return None\n"
                      "def run(work, recover):\n    try:\n        work()\n"
                      "    except BaseException as exc:\n" + body, encoding="utf-8")
    found = records("find_exception_issues.py", sample, "catches_baseexception")
    assert len(found) == 1 and found[0]["kind"] == "finding"


@pytest.mark.parametrize("body", ["pytest.fail('never called')", "unused() or pytest.fail('never called')"])
def test_an_unused_asserting_lambda_does_not_check_a_test(tmp_path, body):
    sample = tmp_path / "test_empty.py"
    sample.write_text("import pytest\ndef test_empty():\n"
                      f"    unused = lambda: {body}\n", encoding="utf-8")
    found = records("find_test_smells.py", sample, "test_without_assertion")
    assert len(found) == 1 and found[0]["severity"] == "high"


def test_a_local_private_name_does_not_hide_an_imported_receivers_private(tmp_path):
    sample = tmp_path / "app.py"
    sample.write_text("from vendor import Remote\nclass Local:\n"
                      "    def __init__(self):\n        self._payload = {}\n"
                      "def read(remote: Remote):\n    return remote._payload\n", encoding="utf-8")
    found = records("find_design_smells.py", sample, "inappropriate_intimacy")
    assert len(found) == 1 and found[0]["kind"] == "finding"


def test_an_untyped_private_name_collision_remains_a_visible_lead(tmp_path):
    sample = tmp_path / "app.py"
    sample.write_text("class Local:\n    def __init__(self):\n        self._payload = {}\n"
                      "def read(remote):\n    return remote._payload\n", encoding="utf-8")
    found = records("find_design_smells.py", sample, "inappropriate_intimacy")
    assert len(found) == 1 and found[0]["kind"] == "candidate"
    assert found[0]["suggestion"] == "" and found[0]["also_caused_by"]


@pytest.mark.parametrize("sql", [
    "CREATE TABLE copied AS SELECT * FROM users WHERE name = '{name}'",
    "CREATE TABLE copied AS WITH source AS (SELECT * FROM users) SELECT * FROM source WHERE name = '{name}'",
    "CREATE TABLE copied AS /* a data query */ SELECT * FROM users WHERE name = '{name}'",
    "CREATE TABLE copied AS VALUES ('{name}')",
])
def test_create_data_queries_can_bind_interpolated_data(tmp_path, sql):
    sample = tmp_path / "app.py"
    sample.write_text("def copy_users(db, name):\n" + f"    db.execute(f{sql!r})\n",
                      encoding="utf-8")
    found = records("find_security_issues.py", sample, "sql_injection")
    assert len(found) == 1 and found[0]["kind"] == "finding"


@pytest.mark.parametrize("marked", [False, True])
def test_a_checkout_beneath_tests_keeps_production_sql_scored(tmp_path, marked):
    project = tmp_path / "tests" / "checkout"
    project.mkdir(parents=True)
    if marked:
        (project / "pyproject.toml").write_text('[project]\nname="app"\n', encoding="utf-8")
    (project / "app.py").write_text("def remove(db, name):\n"
                                   "    db.execute(f\"DELETE FROM users WHERE name = '{name}'\")\n",
                                   encoding="utf-8")
    found = records("find_security_issues.py", project, "sql_injection")
    assert len(found) == 1 and found[0]["kind"] == "finding"


def test_a_shadowing_parameter_is_not_the_modules_lock(tmp_path):
    sample = tmp_path / "app.py"
    sample.write_text("import threading\n_lock = threading.Lock()\nitems = []\n"
                      "def append(value, _lock):\n    with _lock:\n        items.append(value)\n",
                      encoding="utf-8")
    found = records("find_global_state.py", sample, "mutated_global")
    assert len(found) == 1 and found[0]["severity"] == "high"
    assert "thread-safe" not in found[0]["suggestion"]


@pytest.mark.parametrize("unused", ['"""Route /secret"""\n', 'URL = "/secret"\n'])
def test_an_unused_route_string_does_not_establish_test_coverage(tmp_path, unused):
    (tmp_path / "routes.py").write_text("@app.get('/secret')\ndef secret():\n    return 1\n",
                                       encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_math.py").write_text(unused + "def test_math():\n    assert 1 + 1 == 2\n",
                                        encoding="utf-8")
    found = records("find_untested_modules.py", tmp_path, "untested_module")
    assert len(found) == 1 and found[0]["kind"] == "candidate"


def test_local_foreign_base_does_not_break_inherited_model_configuration(tmp_path):
    sample = tmp_path / "app.py"
    sample.write_text(
        "from pydantic import BaseModel\ndef load():\n    import ctypes\n"
        "    class Record(BaseModel, ctypes.Structure):\n        tags = []\n"
        "    return Record\n"
        "class Cache:\n    items = []\n    def put(self, value):\n"
        "        self.items.append(value)\n", encoding="utf-8")
    found = records("find_mutation_hazards.py", sample, "mutable_class_attribute")
    assert [(r["code_snippet"], r["kind"]) for r in found] == [("items = []", "finding")]


def test_rebinding_a_loop_receiver_preserves_foreign_data_candidate(tmp_path):
    sample = tmp_path / "app.py"
    sample.write_text(
        "class Worker:\n    def __init__(self):\n        self.name = 'worker'\n"
        "    def work(self, rows, target):\n"
        "        for item in rows:\n            pass\n"
        "        item = target\n"
        "        return item.first + item.second + item.third + item.fourth\n",
        encoding="utf-8")
    found = records("find_coupling_issues.py", sample, "feature_envy")
    assert len(found) == 1 and found[0]["kind"] == "candidate"
    assert found[0]["metric_value"] == 4


def test_unrelated_case_families_do_not_promote_short_switches(tmp_path):
    sample = tmp_path / "app.py"
    sample.write_text(
        "def color_name(kind):\n"
        "    if kind == 1: return 'red'\n"
        "    elif kind == 2: return 'green'\n"
        "    elif kind == 3: return 'blue'\n"
        "    elif kind == 4: return 'white'\n\n"
        "def shape_name(kind):\n"
        "    if kind == 'c': return 'circle'\n"
        "    elif kind == 's': return 'square'\n"
        "    elif kind == 't': return 'triangle'\n"
        "    elif kind == 'h': return 'hexagon'\n", encoding="utf-8")
    found = records("find_design_smells.py", sample, "type_switch")
    assert [(r["kind"], r["severity"]) for r in found] == [("candidate", "low")] * 2
    assert all(r["also_caused_by"] and not r["suggestion"] for r in found)
