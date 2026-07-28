"""Smoke tests: every detector fires on a known-bad fixture and stays quiet on clean code.

These are characterization tests for the detectors themselves — the safety net the
skill tells everyone else to build first. Fixtures are written to tmp_path at runtime
(not committed) so the intentionally-bad code never trips the linters or the
detectors' own path-based skip rules (e.g. files under a "tests" segment).
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def run_detector(script: str, target: Path, *extra: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json", *extra],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[:500]}"
    return json.loads(result.stdout)


def smell_types(findings: list[dict]) -> set[str]:
    return {f["smell_type"] for f in findings}


# (detector script, {relative path: file content}, smell types that must fire)
CASES = [
    (
        "find_debug_leftovers.py",
        {"sample.py": "import pdb\n\ndef f():\n    pdb.set_trace()\n    breakpoint()\n"},
        {"pdb_trace", "breakpoint_call"},
    ),
    (
        "find_resource_leaks.py",
        {"sample.py": "def f(p):\n    handle = open(p)\n    return handle.read()\n"},
        {"unmanaged_open"},
    ),
    (
        "find_security_issues.py",
        {
            "sample.py": (
                "import subprocess\n"
                "password = 'hunter2secret'\n"
                "def f(x, d):\n"
                "    eval(x)\n"
                "    subprocess.run(f'ls {d}', shell=True)\n"
                "    cursor.execute(f'select * from t where id={x}')\n"
            )
        },
        {"eval_exec", "command_injection", "sql_injection", "hardcoded_secret"},
    ),
    (
        "find_exception_issues.py",
        {
            "sample.py": (
                "def f():\n"
                "    try:\n"
                "        g()\n"
                "    except Exception:\n"
                "        pass\n"
                "    try:\n"
                "        g()\n"
                "    except KeyError:\n"
                "        raise RuntimeError('boom')\n"
            )
        },
        {"swallowed_exception", "raise_without_from"},
    ),
    (
        "find_global_state.py",
        {"sample.py": "CACHE = {}\n\ndef put(k, v):\n    CACHE[k] = v\n"},
        {"mutated_global"},
    ),
    (
        "find_ai_scaffolding.py",
        {
            "sample.py": (
                "API_KEY = 'your-api-key'\n"
                "# TODO: implement the parser\n"
                "def f():\n"
                "    raise NotImplementedError\n"
                "def g(a, **kwargs):\n"
                "    return a + 1\n"
            )
        },
        {"placeholder_value", "todo_implement", "stub_not_implemented", "unused_kwargs"},
    ),
    (
        "find_duplicate_definitions.py",
        {"sample.py": "def f():\n    return 1\n\ndef f():\n    return 2\n"},
        {"duplicate_definition"},
    ),
    (
        "find_duplicate_definitions.py",
        {"conflicted.py": "<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> branch\n"},
        {"merge_conflict_marker"},
    ),
    (
        "find_unawaited_coroutines.py",
        {"sample.py": "async def work():\n    return 1\n\ndef main():\n    work()\n"},
        {"unawaited_coroutine"},
    ),
    (
        "find_local_imports.py",
        {"sample.py": "x = 1\nimport os\n\ndef f():\n    import re\n    return re, os\n"},
        {"local_import", "import_not_at_top"},
    ),
    (
        "find_redundant_comments.py",
        {"sample.py": "def f(count):\n    # increment count\n    count += 1\n    return count\n"},
        {"redundant_comment"},
    ),
    (
        "find_outdated_idioms.py",
        {
            "sample.py": (
                "import os\n"
                "def f(name, d):\n"
                "    a = '%s!' % name\n"
                "    b = '{}!'.format(name)\n"
                "    return a, b, os.path.join(d, 'x')\n"
            )
        },
        {"percent_format", "str_format_call", "os_path_join"},
    ),
    (
        "find_missing_docstrings.py",
        {"sample.py": "def visible(a):\n    b = a + 1\n    c = b * 2\n    return c\n"},
        {"public_function_no_docstring"},
    ),
    (
        "find_type_gaps.py",
        {
            "sample.py": (
                "from typing import Any\n"
                "def f(a):\n"
                "    return a\n"
                "def g(b: Any) -> Any:\n"
                "    return b\n"
                "x = 1  # type: ignore\n"
            )
        },
        {"missing_return_annotation", "missing_param_annotation", "any_overuse", "broad_type_ignore"},
    ),
    (
        "find_test_smells.py",
        {
            "test_sample.py": (
                "def test_nothing():\n"
                "    x = 1\n"
                "def test_trivial():\n"
                "    assert True\n"
            )
        },
        {"test_without_assertion", "trivial_assertion"},
    ),
    (
        "find_untested_modules.py",
        {"sample.py": "def thing():\n    return 1\n"},
        {"no_tests_in_repo"},
    ),
    (
        "find_import_cycles.py",
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from pkg import b\n",
            "pkg/b.py": "from pkg import a\n",
            "pkg/c.py": "from os import *\n",
        },
        {"circular_import", "wildcard_import"},
    ),
    (
        "find_dependency_issues.py",
        {
            "requirements.txt": "leftpadpy\n",
            "main.py": "import requests\n\ndef f():\n    return requests\n",
        },
        {"missing_dependency", "unpinned_dependency"},
    ),
    (
        "find_design_smells.py",
        {
            "sample.py": (
                "def dispatch(kind, other):\n"
                "    print(other._secret)\n"
                "    if kind == 'a':\n"
                "        out = 1\n"
                "        log()\n"
                "    elif kind == 'b':\n"
                "        out = 2\n"
                "        log()\n"
                "    elif kind == 'c':\n"
                "        out = 3\n"
                "        log()\n"
                "    elif kind == 'd':\n"
                "        out = 4\n"
                "        log()\n"
                "    else:\n"
                "        out = 5\n"
                "        log()\n"
                "    return out\n"
                "\n"
                "def wait(jobs):\n"
                "    done = False\n"
                "    while not done:\n"
                "        if not jobs:\n"
                "            done = True\n"
            )
        },
        {"type_switch", "duplicate_conditional_fragment", "control_flag", "inappropriate_intimacy"},
    ),
    (
        "find_design_smells.py",
        {
            "sample.py": (
                "class Base:\n"
                "    def render(self):\n"
                "        return 'base'\n"
                "\n"
                "class Child(Base):\n"
                "    def __init__(self):\n"
                "        self.scratch = None\n"
                "        self.kept = 1\n"
                "\n"
                "    def render(self):\n"
                "        raise NotImplementedError\n"
                "\n"
                "    def only_user(self):\n"
                "        self.scratch = object()\n"
                "        log(self.scratch)\n"
                "\n"
                "class Echo(Child):\n"
                "    pass\n"
            )
        },
        {"refused_bequest", "temporary_field", "lazy_class"},
    ),
    (
        "find_pattern_issues.py",
        {
            "sample.py": (
                "_CACHE = {}\n"
                "\n"
                "def slow(x):\n"
                "    if x in _CACHE:\n"
                "        return _CACHE[x]\n"
                "    _CACHE[x] = x * 2\n"
                "    return _CACHE[x]\n"
                "\n"
                "class Config:\n"
                "    _instance = None\n"
                "    def __new__(cls):\n"
                "        if cls._instance is None:\n"
                "            cls._instance = super().__new__(cls)\n"
                "        return cls._instance\n"
                "\n"
                "class Borg:\n"
                "    _shared = {}\n"
                "    def __init__(self):\n"
                "        self.__dict__ = self._shared\n"
                "\n"
                "class RegistryMeta(type):\n"
                "    REGISTRY = {}\n"
                "    def __new__(mcs, name, bases, ns):\n"
                "        new_cls = super().__new__(mcs, name, bases, ns)\n"
                "        mcs.REGISTRY[name] = new_cls\n"
                "        return new_cls\n"
                "\n"
                "class Person:\n"
                "    def get_name(self):\n"
                "        return self._name\n"
                "    def set_name(self, value):\n"
                "        self._name = value\n"
                "    @property\n"
                "    def conn(self):\n"
                "        if self._conn is None:\n"
                "            self._conn = object()\n"
                "        return self._conn\n"
            )
        },
        {"handrolled_memoize", "handrolled_singleton", "borg_shared_state",
         "registry_metaclass", "getter_setter_pair", "handrolled_lazy_property"},
    ),
    (
        "find_pattern_issues.py",
        {
            "sample.py": (
                "class CountUp:\n"
                "    def __iter__(self):\n"
                "        return self\n"
                "    def __next__(self):\n"
                "        self.i += 1\n"
                "        return self.i\n"
                "\n"
                "class QueryBuilder:\n"
                "    def set_table(self, t):\n"
                "        self.table = t\n"
                "        return self\n"
                "    def set_cols(self, c):\n"
                "        self.cols = c\n"
                "        return self\n"
                "    def set_limit(self, n):\n"
                "        self.limit = n\n"
                "        return self\n"
                "    def build(self):\n"
                "        return (self.table, self.cols, self.limit)\n"
                "\n"
                "class Conn:\n"
                "    def __del__(self):\n"
                "        self.sock.close()\n"
                "\n"
                "class Order:\n"
                "    def pay(self):\n"
                "        if self.status == 'new':\n"
                "            self.status = 'paid'\n"
                "    def ship(self):\n"
                "        if self.status == 'paid':\n"
                "            self.status = 'shipped'\n"
                "    def cancel(self):\n"
                "        if self.status == 'shipped':\n"
                "            raise ValueError\n"
                "        self.status = 'cancelled'\n"
                "\n"
                "class Discount:\n"
                "    def apply(self, order):\n"
                "        raise NotImplementedError\n"
                "\n"
                "class TenPercent(Discount):\n"
                "    def apply(self, order):\n"
                "        return order * 0.9\n"
                "\n"
                "class OnSale(Discount):\n"
                "    def apply(self, order):\n"
                "        return order * 0.5\n"
                "\n"
                "def read(f):\n"
                "    try:\n"
                "        return f.read()\n"
                "    finally:\n"
                "        f.close()\n"
            )
        },
        {"iterator_class", "fluent_builder", "finalizer_del",
         "string_state_machine", "stateless_strategy_classes", "try_finally_close"},
    ),
]


@pytest.mark.parametrize(
    "script,files,expected", CASES, ids=[f"{c[0]}:{'+'.join(sorted(c[2]))}" for c in CASES]
)
def test_detector_fires_on_known_bad_fixture(tmp_path, script, files, expected):
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    found = smell_types(run_detector(script, tmp_path))
    missing = expected - found
    assert not missing, f"{script} did not report {missing}; reported {found or '{}'}"


CLEAN_CODE = '''"""A clean module."""


def add(first: int, second: int) -> int:
    """Add two integers."""
    return first + second
'''

QUIET_ON_CLEAN = [
    "find_debug_leftovers.py",
    "find_resource_leaks.py",
    "find_security_issues.py",
    "find_exception_issues.py",
    "find_global_state.py",
    "find_ai_scaffolding.py",
    "find_duplicate_definitions.py",
    "find_unawaited_coroutines.py",
    "find_local_imports.py",
    "find_redundant_comments.py",
    "find_outdated_idioms.py",
    "find_missing_docstrings.py",
    "find_type_gaps.py",
    "find_test_smells.py",
    "find_design_smells.py",
    "find_pattern_issues.py",
]


@pytest.mark.parametrize("script", QUIET_ON_CLEAN)
def test_detector_quiet_on_clean_code(tmp_path, script):
    (tmp_path / "sample.py").write_text(CLEAN_CODE)
    findings = run_detector(script, tmp_path)
    assert findings == [], f"{script} false-positives on clean code: {smell_types(findings)}"


def test_ignore_flag_suppresses_smell_type(tmp_path):
    (tmp_path / "sample.py").write_text("def f(x):\n    return eval(x)\n")
    assert "eval_exec" in smell_types(run_detector("find_security_issues.py", tmp_path))
    suppressed = run_detector("find_security_issues.py", tmp_path, "--ignore", "eval_exec")
    assert "eval_exec" not in smell_types(suppressed)


def test_analyze_all_aggregates_and_is_valid_json(tmp_path):
    (tmp_path / "sample.py").write_text("def f(x):\n    return eval(x)\n")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "analyze_all.py"),
            str(tmp_path),
            "--format",
            "json",
            "--skip-duplicates",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[:500]
    report = json.loads(result.stdout)
    assert {"meta", "summary", "categories"} <= report.keys()
    assert report["summary"]["by_category"].get("security", 0) >= 1


def test_every_script_parses():
    scripts = sorted(SCRIPTS_DIR.glob("*.py"))
    assert len(scripts) >= 30
    for script in scripts:
        ast.parse(script.read_text(encoding="utf-8"), filename=str(script))


# --------------------------------------------------------------------------- #
# Regression tests for review findings (each pins a fixed false pos/neg)
# --------------------------------------------------------------------------- #


def test_cross_kind_duplicate_definition_is_flagged(tmp_path):
    (tmp_path / "sample.py").write_text("def thing():\n    return 1\n\nclass thing:\n    pass\n")
    assert "duplicate_definition" in smell_types(run_detector("find_duplicate_definitions.py", tmp_path))


def test_subprocess_without_shell_is_not_command_injection(tmp_path):
    (tmp_path / "sample.py").write_text(
        "import subprocess\n\ndef f(name):\n    subprocess.run(f'tool-{name}')\n"
    )
    assert "command_injection" not in smell_types(run_detector("find_security_issues.py", tmp_path))


def test_open_closed_in_finally_is_not_a_leak(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def f(p):\n"
        "    handle = open(p)\n"
        "    try:\n"
        "        return handle.read()\n"
        "    finally:\n"
        "        handle.close()\n"
    )
    assert "unmanaged_open" not in smell_types(run_detector("find_resource_leaks.py", tmp_path))


def test_assert_in_nested_helper_does_not_count_as_test_assertion(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "def test_a():\n"
        "    def helper():\n"
        "        assert False\n"
        "    helper()\n"
    )
    assert "test_without_assertion" in smell_types(run_detector("find_test_smells.py", tmp_path))


def test_sync_method_sharing_name_with_async_function_not_flagged(tmp_path):
    (tmp_path / "sample.py").write_text(
        "async def fetch():\n"
        "    return 1\n"
        "\n"
        "class Cache:\n"
        "    def fetch(self):\n"
        "        return 2\n"
        "\n"
        "def main():\n"
        "    Cache().fetch()\n"
    )
    assert "unawaited_coroutine" not in smell_types(run_detector("find_unawaited_coroutines.py", tmp_path))


def test_import_cycle_detected_in_src_layout(tmp_path):
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("from pkg import b\n")
    (pkg / "b.py").write_text("from pkg import a\n")
    assert "circular_import" in smell_types(run_detector("find_import_cycles.py", tmp_path))


def test_empty_manifest_yields_missing_dependency_not_no_manifest(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\ndependencies = []\n')
    (tmp_path / "main.py").write_text("import requests\n\ndef f():\n    return requests\n")
    found = smell_types(run_detector("find_dependency_issues.py", tmp_path))
    assert "missing_dependency" in found
    assert "no_dependency_manifest" not in found


def test_dotted_test_import_does_not_bless_same_stem_module(tmp_path):
    for pkg in ("pkg1", "pkg2"):
        d = tmp_path / pkg
        d.mkdir()
        (d / "__init__.py").write_text("")
        (d / "util.py").write_text("def helper():\n    return 1\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_u.py").write_text("from pkg1 import util\n\ndef test_x():\n    assert util.helper() == 1\n")
    findings = run_detector("find_untested_modules.py", tmp_path)
    untested_files = {f["file"] for f in findings if f["smell_type"] == "untested_module"}
    assert any(f.endswith("pkg2/util.py") for f in untested_files)
    assert not any(f.endswith("pkg1/util.py") for f in untested_files)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t.co", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )


def test_analyze_diff_rejects_invalid_base_ref(tmp_path):
    _git(tmp_path, "init", "-q")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "no-such-ref"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert "does not resolve" in result.stderr


def test_analyze_diff_retains_findings_anchored_at_changed_definition(tmp_path):
    _git(tmp_path, "init", "-q")
    target = tmp_path / "mod.py"
    target.write_text("def f(x):\n    return x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # Change only the body; the missing-annotation finding anchors at the def line.
    target.write_text("def f(x):\n    return x + 1\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[:500]
    found = {f["smell_type"] for f in json.loads(result.stdout)}
    assert "missing_return_annotation" in found


# --------------------------------------------------------------------------- #
# Regression tests for the second review round
# --------------------------------------------------------------------------- #


def test_conditional_close_is_still_a_leak(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def f(p, ok):\n"
        "    handle = open(p)\n"
        "    if ok:\n"
        "        handle.close()\n"
        "    return ok\n"
    )
    assert "unmanaged_open" in smell_types(run_detector("find_resource_leaks.py", tmp_path))


def test_unconditional_close_is_not_a_leak(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def f(p):\n"
        "    handle = open(p)\n"
        "    data = handle.read()\n"
        "    handle.close()\n"
        "    return data\n"
    )
    assert "unmanaged_open" not in smell_types(run_detector("find_resource_leaks.py", tmp_path))


def test_yaml_load_with_positional_safe_loader_not_flagged(tmp_path):
    (tmp_path / "sample.py").write_text(
        "import yaml\n\ndef f(x):\n    a = yaml.load(x)\n    b = yaml.load(x, yaml.SafeLoader)\n    return a, b\n"
    )
    findings = [f for f in run_detector("find_security_issues.py", tmp_path) if f["smell_type"] == "unsafe_yaml"]
    assert len(findings) == 1 and findings[0]["line"] == 4


def test_weak_hash_usedforsecurity_false_not_flagged(tmp_path):
    (tmp_path / "sample.py").write_text(
        "import hashlib\n\ndef f(b):\n    return hashlib.md5(b, usedforsecurity=False).hexdigest()\n"
    )
    assert "weak_hash" not in smell_types(run_detector("find_security_issues.py", tmp_path))


def test_verify_false_only_flagged_on_http_apis(tmp_path):
    (tmp_path / "sample.py").write_text(
        "import requests\n"
        "\n"
        "def f(url, v):\n"
        "    check_result(v, verify=False)\n"
        "    return requests.get(url, verify=False)\n"
    )
    findings = [f for f in run_detector("find_security_issues.py", tmp_path) if f["smell_type"] == "tls_verify_disabled"]
    assert len(findings) == 1 and findings[0]["line"] == 5


def test_pytest_approx_alone_is_not_an_assertion(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n\ndef test_x(actual):\n    pytest.approx(actual)\n"
    )
    assert "test_without_assertion" in smell_types(run_detector("find_test_smells.py", tmp_path))


def test_src_layout_local_package_is_not_missing_dependency(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0"\ndependencies = []\n')
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("from pkg import b\n")
    (pkg / "b.py").write_text("x = 1\n")
    missing = {f["description"] for f in run_detector("find_dependency_issues.py", tmp_path)
               if f["smell_type"] == "missing_dependency"}
    assert not any("pkg" in d.split("'")[1] for d in missing if "'" in d), missing


def test_plain_def_after_property_group_is_flagged(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class C:\n"
        "    @property\n"
        "    def x(self):\n"
        "        return self._x\n"
        "    @x.setter\n"
        "    def x(self, v):\n"
        "        self._x = v\n"
        "    def x(self):\n"
        "        return 1\n"
    )
    assert "duplicate_definition" in smell_types(run_detector("find_duplicate_definitions.py", tmp_path))


def test_overload_set_with_implementation_not_flagged(tmp_path):
    (tmp_path / "sample.py").write_text(
        "from typing import overload\n"
        "\n"
        "@overload\n"
        "def f(x: int) -> int: ...\n"
        "@overload\n"
        "def f(x: str) -> str: ...\n"
        "def f(x):\n"
        "    return x\n"
    )
    assert "duplicate_definition" not in smell_types(run_detector("find_duplicate_definitions.py", tmp_path))


def test_nested_stub_in_abstract_class_is_flagged(tmp_path):
    (tmp_path / "sample.py").write_text(
        "from abc import ABC, abstractmethod\n"
        "\n"
        "class C(ABC):\n"
        "    @abstractmethod\n"
        "    def m(self): ...\n"
        "    def concrete(self):\n"
        "        def helper():\n"
        "            pass\n"
        "        return helper\n"
    )
    found = smell_types(run_detector("find_ai_scaffolding.py", tmp_path))
    assert "empty_stub" in found


def test_docstring_with_example_url_is_not_a_placeholder(tmp_path):
    (tmp_path / "sample.py").write_text(
        '"""See https://example.com/docs for details."""\n'
        "\n"
        "def f() -> int:\n"
        '    """Compute. See https://example.com/docs."""\n'
        "    return 1\n"
    )
    assert "placeholder_value" not in smell_types(run_detector("find_ai_scaffolding.py", tmp_path))


def test_design_smells_skip_idiomatic_patterns(tmp_path):
    """Lazy-init property, dunder privates, namedtuple API, imported modules,
    exception subclasses and short ladders are idioms, not smells."""
    (tmp_path / "sample.py").write_text(
        "import os\n"
        "from collections import namedtuple\n"
        "\n"
        "Point = namedtuple('Point', 'x y')\n"
        "\n"
        "class Config:\n"
        "    def __init__(self):\n"
        "        self._cache = None\n"
        "\n"
        "    @property\n"
        "    def cache(self):\n"
        "        if self._cache is None:\n"
        "            self._cache = os.environ.get('X')\n"
        "        return self._cache\n"
        "\n"
        "    def __eq__(self, other):\n"
        "        return self._cache == other._cache\n"
        "\n"
        "class MyError(ValueError):\n"
        "    pass\n"
        "\n"
        "def move(p):\n"
        "    os._exit\n"
        "    return p._replace(x=p.x + 1)\n"
        "\n"
        "def two_way(kind):\n"
        "    if kind == 'a':\n"
        "        return 1\n"
        "    elif kind == 'b':\n"
        "        return 2\n"
        "    return 3\n"
    )
    assert smell_types(run_detector("find_design_smells.py", tmp_path)) == set()


def test_ladder_on_mixed_subjects_is_not_a_type_switch(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def f(kind, size, mode, flag):\n"
        "    if kind == 'a':\n"
        "        return 1\n"
        "    elif size == 2:\n"
        "        return 2\n"
        "    elif mode == 'x':\n"
        "        return 3\n"
        "    elif flag == 'y':\n"
        "        return 4\n"
        "    else:\n"
        "        return 5\n"
    )
    assert "type_switch" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_flag_assigned_only_in_nested_scope_is_not_a_control_flag(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def wait(register):\n"
        "    done = False\n"
        "    while not done:\n"
        "        def callback():\n"
        "            done = True\n"
        "        register(callback)\n"
    )
    assert "control_flag" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_documented_marker_subclass_is_not_lazy(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Node:\n"
        "    def walk(self):\n"
        "        return []\n"
        "\n"
        "class Leaf(Node):\n"
        '    """Marker type: distinguishes terminal nodes in isinstance checks."""\n'
    )
    assert "lazy_class" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_annotated_none_field_is_a_temporary_field(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Job:\n"
        "    def __init__(self):\n"
        "        self.scratch: object | None = None\n"
        "\n"
        "    def run(self):\n"
        "        self.scratch = object()\n"
        "        log(self.scratch)\n"
    )
    assert "temporary_field" in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_nested_classes_sharing_a_name_do_not_cross_hierarchies(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class ContainerA:\n"
        "    class Base:\n"
        "        def render(self):\n"
        "            return 'a'\n"
        "\n"
        "class ContainerB:\n"
        "    class Base:\n"
        "        pass\n"
        "\n"
        "    class Child(Base):\n"
        "        def render(self):\n"
        "            raise NotImplementedError\n"
    )
    assert "refused_bequest" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_analyze_diff_keeps_finding_anchored_at_unchanged_conditional(tmp_path):
    _git(tmp_path, "init", "-q")
    target = tmp_path / "mod.py"
    target.write_text(
        "def f(kind):\n"
        "    if kind == 'a':\n"
        "        first()\n"
        "    else:\n"
        "        second()\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # Edit only the branch bodies so they now end identically; the resulting
    # duplicate_conditional_fragment finding anchors at the unchanged `if` line.
    target.write_text(
        "def f(kind):\n"
        "    if kind == 'a':\n"
        "        log()\n"
        "    else:\n"
        "        log()\n"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[:500]
    found = {f["smell_type"] for f in json.loads(result.stdout)}
    assert "duplicate_conditional_fragment" in found


def test_deferred_none_assignment_in_init_callback_is_not_a_temporary_field(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Job:\n"
        "    def __init__(self, register):\n"
        "        def reset():\n"
        "            self.scratch = None\n"
        "        register(reset)\n"
        "\n"
        "    def run(self):\n"
        "        self.scratch = object()\n"
        "        return self.scratch\n"
    )
    assert "temporary_field" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_field_only_read_back_as_none_is_not_a_temporary_field(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Job:\n"
        "    def __init__(self):\n"
        "        self.result = None\n"
        "\n"
        "    def run(self):\n"
        "        return self.result\n"
    )
    assert "temporary_field" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_nested_concrete_hierarchy_still_reports_refused_bequest(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Container:\n"
        "    class Base:\n"
        "        def render(self):\n"
        "            return 'base'\n"
        "\n"
        "    class Child(Base):\n"
        "        def render(self):\n"
        "            raise NotImplementedError\n"
    )
    assert "refused_bequest" in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_enum_member_ladder_is_a_type_switch(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def f(kind):\n"
        "    if kind == Kind.CREATE:\n"
        "        return 1\n"
        "    elif kind == Kind.UPDATE:\n"
        "        return 2\n"
        "    elif kind == Kind.DELETE:\n"
        "        return 3\n"
        "    elif kind == Kind.LIST:\n"
        "        return 4\n"
        "    return 0\n"
    )
    assert "type_switch" in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_chained_receivers_are_inappropriate_intimacy(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Billing:\n"
        "    def charge(self, request):\n"
        "        token = self.account._token\n"
        "        state = request.user._state\n"
        "        return token, state\n"
    )
    findings = [f for f in run_detector("find_design_smells.py", tmp_path)
                if f["smell_type"] == "inappropriate_intimacy"]
    assert len(findings) == 2


def test_callback_inside_dunder_is_not_exempt_from_intimacy(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Watcher:\n"
        "    def __init__(self, other, register):\n"
        "        def callback():\n"
        "            return other._secret\n"
        "        register(callback)\n"
    )
    assert "inappropriate_intimacy" in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_parameter_shadowing_class_name_is_not_exempt_from_intimacy(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Account:\n"
        "    def balance(self):\n"
        "        return 0\n"
        "\n"
        "def expose(Account):\n"
        "    return Account._token\n"
    )
    assert "inappropriate_intimacy" in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_analyze_diff_skips_preexisting_finding_at_unchanged_conditional(tmp_path):
    _git(tmp_path, "init", "-q")
    target = tmp_path / "mod.py"
    ladder = (
        "def f(kind):\n"
        "    if kind == 'a':\n"
        "        return {}\n"
        "    elif kind == 'b':\n"
        "        return 2\n"
        "    elif kind == 'c':\n"
        "        return 3\n"
        "    elif kind == 'd':\n"
        "        return 4\n"
        "    return 0\n"
    )
    target.write_text(ladder)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # Edit one branch body; the dispatch structure is unchanged, so the
    # pre-existing type_switch at the `if` header must not resurface.
    target.write_text(ladder.replace("return {}", "return 10"))
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    found = {f["smell_type"] for f in json.loads(result.stdout)}
    assert "type_switch" not in found


def test_local_reassignment_of_class_name_is_not_exempt_from_intimacy(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Account:\n"
        "    def balance(self):\n"
        "        return 0\n"
        "\n"
        "def expose(factory):\n"
        "    Account = factory()\n"
        "    return Account._token\n"
    )
    assert "inappropriate_intimacy" in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_noop_method_on_earlier_base_shadows_concrete_later_base(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class NoopBase:\n"
        "    def render(self):\n"
        "        pass\n"
        "\n"
        "class Concrete:\n"
        "    def render(self):\n"
        "        return 'x'\n"
        "\n"
        "class FollowsNoop(NoopBase, Concrete):\n"
        "    def render(self):\n"
        "        raise NotImplementedError\n"
        "\n"
        "class FollowsConcrete(Concrete, NoopBase):\n"
        "    def render(self):\n"
        "        raise NotImplementedError\n"
    )
    findings = [f for f in run_detector("find_design_smells.py", tmp_path)
                if f["smell_type"] == "refused_bequest"]
    # MRO reaches NoopBase.render first for FollowsNoop: nothing concrete is refused.
    assert len(findings) == 1 and "FollowsConcrete" in findings[0]["description"]


def test_annotated_flag_assignment_is_a_control_flag(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def wait(jobs):\n"
        "    done = False\n"
        "    while not done:\n"
        "        if not jobs:\n"
        "            done: bool = True\n"
    )
    assert "control_flag" in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_suffix_named_test_module_is_exempt_from_intimacy(tmp_path):
    (tmp_path / "account_test.py").write_text(
        "def check(account):\n"
        "    assert account._token is None\n"
    )
    assert "inappropriate_intimacy" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_analyze_diff_uses_prerename_path_for_baseline(tmp_path):
    _git(tmp_path, "init", "-q")
    ladder = (
        "def f(kind):\n"
        "    if kind == 'a':\n"
        "        return {}\n"
        "    elif kind == 'b':\n"
        "        return 2\n"
        "    elif kind == 'c':\n"
        "        return 3\n"
        "    elif kind == 'd':\n"
        "        return 4\n"
        "    return 0\n"
    )
    (tmp_path / "mod.py").write_text(ladder)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "mv", "mod.py", "renamed.py")
    (tmp_path / "renamed.py").write_text(ladder.replace("return {}", "return 10"))
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    found = {f["smell_type"] for f in json.loads(result.stdout)}
    assert "type_switch" not in found


def test_analyze_diff_baseline_does_not_mask_identical_finding_elsewhere(tmp_path):
    _git(tmp_path, "init", "-q")
    f1 = (
        "def f1(kind):\n"
        "    if kind == 'a':\n"
        "        return 1\n"
        "    elif kind == 'b':\n"
        "        return 2\n"
        "    elif kind == 'c':\n"
        "        return 3\n"
        "    elif kind == 'd':\n"
        "        return 4\n"
        "    return 0\n"
    )
    f2_base = (
        "def f2(kind):\n"
        "    if kind == 'a':\n"
        "        return 1\n"
        "    elif kind == 'b':\n"
        "        return 2\n"
        "    elif kind == 'c':\n"
        "        return 3\n"
        "    return 0\n"
    )
    target = tmp_path / "mod.py"
    target.write_text(f1 + "\n" + f2_base)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # f2 grows a fourth branch: a NEW type_switch with a description identical
    # to f1's pre-existing one. The def-scoped baseline must not consume it.
    f2_head = f2_base.replace(
        "    return 0\n",
        "    elif kind == 'd':\n        return 4\n    return 0\n",
    )
    target.write_text(f1 + "\n" + f2_head)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    switches = [f for f in json.loads(result.stdout) if f["smell_type"] == "type_switch"]
    assert len(switches) == 1


def test_callback_only_usage_is_not_a_temporary_field(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Job:\n"
        "    def __init__(self):\n"
        "        self.scratch = None\n"
        "\n"
        "    def run(self, register):\n"
        "        def callback():\n"
        "            self.scratch = object()\n"
        "            return self.scratch\n"
        "        register(callback)\n"
    )
    assert "temporary_field" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_diamond_mro_resolves_noop_before_concrete(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class A:\n"
        "    def render(self):\n"
        "        return 'a'\n"
        "\n"
        "class B(A):\n"
        "    def other(self):\n"
        "        return 1\n"
        "\n"
        "class C(A):\n"
        "    def render(self):\n"
        "        pass\n"
        "\n"
        "class D(B, C):\n"
        "    def render(self):\n"
        "        raise NotImplementedError\n"
    )
    # Python's MRO for D is [D, B, C, A]: lookup reaches C's no-op render, so
    # D refuses nothing concrete (C itself legitimately overrides A's).
    findings = [f for f in run_detector("find_design_smells.py", tmp_path)
                if f["smell_type"] == "refused_bequest" and "'D." in f["description"]]
    assert findings == []


def test_analyze_diff_baseline_distinguishes_same_named_methods(tmp_path):
    _git(tmp_path, "init", "-q")

    def handle(cls_name, branches):
        body = "".join(
            f"        {'if' if i == 0 else 'elif'} kind == '{chr(97 + i)}':\n            return {i}\n"
            for i in range(branches)
        )
        return f"class {cls_name}:\n    def handle(self, kind):\n{body}        return -1\n"

    target = tmp_path / "mod.py"
    target.write_text(handle("First", 4) + "\n" + handle("Second", 3))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # Second.handle grows to four branches: identical description to the
    # pre-existing switch in First.handle, but a different qualified def.
    target.write_text(handle("First", 4) + "\n" + handle("Second", 4))
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    switches = [f for f in json.loads(result.stdout) if f["smell_type"] == "type_switch"]
    assert len(switches) == 1


def test_unresolved_base_in_hierarchy_silences_refused_bequest(tmp_path):
    (tmp_path / "sample.py").write_text(
        "from somewhere import External\n"
        "\n"
        "class Local:\n"
        "    def render(self):\n"
        "        return 'local'\n"
        "\n"
        "class Child(External, Local):\n"
        "    def render(self):\n"
        "        raise NotImplementedError\n"
    )
    assert "refused_bequest" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_flag_exiting_enclosing_loop_from_inner_loop_is_not_a_control_flag(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def drain(queues):\n"
        "    done = False\n"
        "    while not done:\n"
        "        for q in queues:\n"
        "            if q.empty():\n"
        "                done = True\n"
    )
    assert "control_flag" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_analyze_diff_reports_finding_introduced_by_pure_deletion(tmp_path):
    _git(tmp_path, "init", "-q")
    target = tmp_path / "mod.py"
    target.write_text(
        "def f(ok):\n"
        "    if ok:\n"
        "        log()\n"
        "    else:\n"
        "        log()\n"
        "        other()\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # Deleting the else-branch's distinct final statement makes every branch
    # end identically — a finding introduced purely by deletion.
    target.write_text(
        "def f(ok):\n"
        "    if ok:\n"
        "        log()\n"
        "    else:\n"
        "        log()\n"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    found = {f["smell_type"] for f in json.loads(result.stdout)}
    assert "duplicate_conditional_fragment" in found


def test_analyze_diff_baseline_distinguishes_twin_ladders_in_one_def(tmp_path):
    _git(tmp_path, "init", "-q")

    def ladder(branches):
        return "".join(
            f"    {'if' if i == 0 else 'elif'} kind == '{chr(97 + i)}':\n        pass\n"
            for i in range(branches)
        )

    target = tmp_path / "mod.py"
    target.write_text("def f(kind):\n" + ladder(4) + ladder(3) + "    return kind\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # The second ladder in the SAME function grows to four branches: identical
    # description and enclosing def as the first — only the ordinal differs.
    target.write_text("def f(kind):\n" + ladder(4) + ladder(4) + "    return kind\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    switches = [f for f in json.loads(result.stdout) if f.get("smell_type") == "type_switch"]
    assert len(switches) == 1


def test_private_decorator_on_dunder_is_still_intimacy(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class C:\n"
        "    @registry._private_hook\n"
        "    def __init__(self):\n"
        "        self.x = 1\n"
    )
    assert "inappropriate_intimacy" in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_flag_assignment_away_from_termination_is_not_a_control_flag(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def spin(q):\n"
        "    done = False\n"
        "    while not done:\n"
        "        if q.reset():\n"
        "            done = False\n"
    )
    assert "control_flag" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_mixed_eq_and_isinstance_ladder_is_not_a_type_switch(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def f(value):\n"
        "    if value == 0:\n"
        "        return 1\n"
        "    elif isinstance(value, str):\n"
        "        return 2\n"
        "    elif value == 3:\n"
        "        return 3\n"
        "    elif isinstance(value, bytes):\n"
        "        return 4\n"
        "    return 0\n"
    )
    assert "type_switch" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_analyze_diff_reports_temporary_field_introduced_by_method_edit(tmp_path):
    _git(tmp_path, "init", "-q")
    target = tmp_path / "mod.py"
    target.write_text(
        "class Job:\n"
        "    def __init__(self):\n"
        "        self.scratch = None\n"
        "\n"
        "    def run(self):\n"
        "        return 1\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # Editing only run() introduces the temporary field; the finding anchors at
    # the unchanged initializer line, outside the changed lines and def headers.
    target.write_text(
        "class Job:\n"
        "    def __init__(self):\n"
        "        self.scratch = None\n"
        "\n"
        "    def run(self):\n"
        "        self.scratch = object()\n"
        "        log(self.scratch)\n"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    found = {f.get("smell_type") for f in json.loads(result.stdout)}
    assert "temporary_field" in found


def test_analyze_diff_line_mapping_beats_ordinal_shifts(tmp_path):
    _git(tmp_path, "init", "-q")

    def ladder(branches):
        return "".join(
            f"    {'if' if i == 0 else 'elif'} kind == '{chr(97 + i)}':\n        pass\n"
            for i in range(branches)
        )

    target = tmp_path / "mod.py"
    target.write_text("def f(kind):\n" + ladder(3) + ladder(4) + "    return kind\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # The EARLIER ladder grows to four branches: by rank it becomes ordinal 0,
    # stealing the later pre-existing finding's baseline slot — line mapping
    # must attribute the baseline finding to the later (unchanged) construct.
    target.write_text("def f(kind):\n" + ladder(4) + ladder(4) + "    return kind\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    switches = [f for f in json.loads(result.stdout) if f.get("smell_type") == "type_switch"]
    assert len(switches) == 1 and switches[0]["line"] == 2


def test_definition_order_resolves_base_bound_at_class_execution(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Base:\n"
        "    def render(self):\n"
        "        return 'module level'\n"
        "\n"
        "class Container:\n"
        "    class Child(Base):\n"
        "        def render(self):\n"
        "            raise NotImplementedError\n"
        "\n"
        "    class Base:\n"
        "        pass\n"
    )
    # At execution time Child(Base) binds the concrete module-level Base — the
    # later nested Base must not shadow it retroactively.
    assert "refused_bequest" in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_function_local_import_exempts_only_its_own_scope(tmp_path):
    (tmp_path / "sample.py").write_text(
        "helper = make_object()\n"
        "\n"
        "def uses_import():\n"
        "    import registry as helper\n"
        "    return helper._registry_internal\n"
        "\n"
        "def uses_module_object():\n"
        "    return helper._secret\n"
    )
    findings = [f for f in run_detector("find_design_smells.py", tmp_path)
                if f["smell_type"] == "inappropriate_intimacy"]
    assert len(findings) == 1 and "_secret" in findings[0]["description"]


def test_inherited_usage_disqualifies_temporary_field(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Base:\n"
        "    def consume(self):\n"
        "        return self.scratch\n"
        "\n"
        "class Child(Base):\n"
        "    def __init__(self):\n"
        "        self.scratch = None\n"
        "\n"
        "    def run(self):\n"
        "        self.scratch = object()\n"
        "        return self.scratch\n"
    )
    assert "temporary_field" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_while_else_loop_is_not_a_control_flag(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def wait(jobs):\n"
        "    done = False\n"
        "    while not done:\n"
        "        if not jobs:\n"
        "            done = True\n"
        "    else:\n"
        "        finish()\n"
    )
    assert "control_flag" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_overload_declarations_are_not_refused_bequest(tmp_path):
    (tmp_path / "sample.py").write_text(
        "from typing import overload\n"
        "\n"
        "class Base:\n"
        "    def convert(self, x):\n"
        "        return x\n"
        "\n"
        "class Child(Base):\n"
        "    @overload\n"
        "    def convert(self, x: int) -> int: ...\n"
        "    @overload\n"
        "    def convert(self, x: str) -> str: ...\n"
        "    def convert(self, x):\n"
        "        return x * 2\n"
    )
    assert "refused_bequest" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_analyze_diff_baseline_keeps_test_directory_context(tmp_path):
    _git(tmp_path, "init", "-q")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "helpers.py").write_text(
        "def test_no_assert():\n"
        "    x = 1\n"
        "\n"
        "def other():\n"
        "    return 2\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # Edit only other(): the pre-existing assertion-less test elsewhere in the
    # file must not resurface — its baseline requires the tests/ context.
    (tests_dir / "helpers.py").write_text(
        "def test_no_assert():\n"
        "    x = 1\n"
        "\n"
        "def other():\n"
        "    return 3\n"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    found = {f.get("smell_type") for f in json.loads(result.stdout)}
    assert "test_without_assertion" not in found


def test_analyze_diff_reports_refusal_introduced_by_base_edit(tmp_path):
    _git(tmp_path, "init", "-q")
    target = tmp_path / "mod.py"
    target.write_text(
        "class Base:\n"
        "    def render(self):\n"
        "        pass\n"
        "\n"
        "class Child(Base):\n"
        "    def render(self):\n"
        "        pass\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # Making Base.render concrete turns Child's unchanged no-op override into a
    # refused bequest — a finding introduced outside the edited class's span.
    target.write_text(
        "class Base:\n"
        "    def render(self):\n"
        "        return 'real'\n"
        "\n"
        "class Child(Base):\n"
        "    def render(self):\n"
        "        pass\n"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    found = {f.get("smell_type") for f in json.loads(result.stdout)}
    assert "refused_bequest" in found


def test_rebound_class_name_invalidates_base_resolution(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Base:\n"
        "    def render(self):\n"
        "        return 'class'\n"
        "\n"
        "Base = make_base()\n"
        "\n"
        "class Child(Base):\n"
        "    def render(self):\n"
        "        raise NotImplementedError\n"
    )
    assert "refused_bequest" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_lambda_and_comprehension_targets_shadow_module_names(tmp_path):
    (tmp_path / "sample.py").write_text(
        "import account\n"
        "\n"
        "grab = lambda account: account._secret\n"
        "\n"
        "def collect(accounts):\n"
        "    return [account._secret for account in accounts]\n"
    )
    findings = [f for f in run_detector("find_design_smells.py", tmp_path)
                if f["smell_type"] == "inappropriate_intimacy"]
    assert len(findings) == 2


def test_genexp_inside_dunder_keeps_dunder_exemption(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Vec:\n"
        "    def __eq__(self, other):\n"
        "        return all(a._v == b._v for a, b in zip(self._parts, other._parts))\n"
    )
    assert "inappropriate_intimacy" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_call_result_receiver_is_intimacy_but_super_is_not(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class C(Base):\n"
        "    def setup(self):\n"
        "        super()._configure()\n"
        "        return get_user()._token\n"
    )
    findings = [f for f in run_detector("find_design_smells.py", tmp_path)
                if f["smell_type"] == "inappropriate_intimacy"]
    assert len(findings) == 1 and "_token" in findings[0]["description"]


def test_name_mangled_attribute_outside_class_is_intimacy(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def leak(other):\n"
        "    return other.__secret\n"
        "\n"
        "class Account:\n"
        "    def merge(self, other):\n"
        "        return other.__balance\n"
    )
    findings = [f for f in run_detector("find_design_smells.py", tmp_path)
                if f["smell_type"] == "inappropriate_intimacy"]
    # Module level: foreign private state. Inside the class: mangling makes it
    # same-class-only access by construction.
    assert len(findings) == 1 and "__secret" in findings[0]["description"]


def test_repeated_identical_conditions_are_not_a_type_switch(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def f(kind):\n"
        "    if kind == 1:\n"
        "        return 1\n"
        "    elif kind == 1:\n"
        "        return 2\n"
        "    elif kind == 1:\n"
        "        return 3\n"
        "    elif kind == 1:\n"
        "        return 4\n"
        "    return 0\n"
    )
    assert "type_switch" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_flag_reset_after_terminating_assignment_is_not_a_control_flag(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def spin():\n"
        "    done = False\n"
        "    while not done:\n"
        "        done = True\n"
        "        done = False\n"
    )
    assert "control_flag" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_analyze_diff_gates_findings_on_lines_shifted_by_deletion(tmp_path):
    _git(tmp_path, "init", "-q")
    target = tmp_path / "mod.py"
    target.write_text(
        "def f():\n"
        "    cleanup()\n"
        "    try:\n"
        "        g()\n"
        "    except:\n"
        "        pass\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    # Deleting cleanup() shifts the unchanged bare except onto a seeded line;
    # the pre-existing bare_except must stay suppressed by the baseline.
    target.write_text(
        "def f():\n"
        "    try:\n"
        "        g()\n"
        "    except:\n"
        "        pass\n"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    found = {f.get("smell_type") for f in json.loads(result.stdout)}
    assert "bare_except" not in found


def test_lambda_default_evaluates_in_enclosing_scope(tmp_path):
    (tmp_path / "sample.py").write_text(
        "import account\n"
        "\n"
        "grab = lambda account=account._secret: account\n"
    )
    assert "inappropriate_intimacy" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_module_rebinding_revokes_import_exemption(tmp_path):
    (tmp_path / "sample.py").write_text(
        "import helper\n"
        "\n"
        "helper = make_object()\n"
        "\n"
        "def f():\n"
        "    return helper._secret\n"
    )
    assert "inappropriate_intimacy" in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_class_defined_in_both_branches_is_ambiguous(tmp_path):
    (tmp_path / "sample.py").write_text(
        "if fast_mode():\n"
        "    class Base:\n"
        "        def render(self):\n"
        "            pass\n"
        "else:\n"
        "    class Base:\n"
        "        def render(self):\n"
        "            return 'slow'\n"
        "\n"
        "class Child(Base):\n"
        "    def render(self):\n"
        "        raise NotImplementedError\n"
    )
    assert "refused_bequest" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_first_comprehension_iterable_evaluates_in_enclosing_scope(tmp_path):
    (tmp_path / "sample.py").write_text(
        "import account\n"
        "\n"
        "def f():\n"
        "    return [x for account in account._items for x in account]\n"
    )
    assert "inappropriate_intimacy" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_class_decorator_evaluates_outside_class_context(tmp_path):
    (tmp_path / "sample.py").write_text(
        "@other.__private_hook\n"
        "class C:\n"
        "    def m(self):\n"
        "        return 1\n"
    )
    assert "inappropriate_intimacy" in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_destructuring_write_populates_temporary_field(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Job:\n"
        "    def __init__(self):\n"
        "        self.scratch = None\n"
        "\n"
        "    def run(self):\n"
        "        self.scratch, status = make_pair()\n"
        "        log(self.scratch)\n"
        "        return status\n"
    )
    assert "temporary_field" in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_same_named_methods_in_distinct_classes_do_not_collapse(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Base:\n"
        "    def __init__(self):\n"
        "        self.scratch = None\n"
        "\n"
        "class Outer1:\n"
        "    class Child(Base):\n"
        "        def run(self):\n"
        "            self.scratch = object()\n"
        "            return self.scratch\n"
        "\n"
        "class Outer2:\n"
        "    class Child(Base):\n"
        "        def run(self):\n"
        "            self.scratch = object()\n"
        "            return self.scratch\n"
    )
    # Two distinct Child.run methods use the field — not confined to one.
    assert "temporary_field" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_analyze_diff_pure_rename_out_of_tests_is_analyzed(tmp_path):
    _git(tmp_path, "init", "-q")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "account.py").write_text(
        "def check(account):\n"
        "    return account._token\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    _git(tmp_path, "mv", "tests/account.py", "src/account.py")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=tmp_path, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    found = {f.get("smell_type") for f in json.loads(result.stdout)}
    # In tests/ the private access was exempt; as production code it's a
    # finding the (pure) rename introduced.
    assert "inappropriate_intimacy" in found


def test_abstract_stub_with_imported_base_is_not_refused_bequest(tmp_path):
    (tmp_path / "sample.py").write_text(
        "from abc import ABC, abstractmethod\n"
        "\n"
        "class Renderer(ABC):\n"
        "    @abstractmethod\n"
        "    def render(self):\n"
        "        raise NotImplementedError\n"
    )
    assert "refused_bequest" not in smell_types(run_detector("find_design_smells.py", tmp_path))


def test_analyze_diff_surfaces_detector_failures(tmp_path):
    import shutil

    broken_scripts = tmp_path / "scripts"
    shutil.copytree(SCRIPTS_DIR, broken_scripts)
    (broken_scripts / "find_type_gaps.py").write_text("import sys\nsys.exit(3)\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "m.py").write_text("def f(x):\n    return x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "m.py").write_text("def f(x):\n    return x + 1\n")
    result = subprocess.run(
        [sys.executable, str(broken_scripts / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=repo, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    errors = [f for f in json.loads(result.stdout) if f["smell_type"] == "detector_error"]
    assert errors and "find_type_gaps.py" in errors[0]["description"]


def test_working_metaclass_is_not_a_registry(tmp_path):
    # EnumType-style metaclasses subscript-assign while building the class;
    # only storing the *new class itself* into a mapping is registration.
    (tmp_path / "sample.py").write_text(
        "class WorkingMeta(type):\n"
        "    def __new__(mcs, name, bases, ns):\n"
        "        ns['_value_map'] = {}\n"
        "        new_cls = super().__new__(mcs, name, bases, ns)\n"
        "        new_cls._value_map['x'] = 1\n"
        "        return new_cls\n"
    )
    assert "registry_metaclass" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_self_release_in_finally_is_not_flagged(tmp_path):
    # logging.Handler-style self.acquire()/self.release() is internal lifecycle
    # management, not a drop-in `with` candidate.
    (tmp_path / "sample.py").write_text(
        "class Handler:\n"
        "    def emit(self):\n"
        "        self.acquire()\n"
        "        try:\n"
        "            self.write()\n"
        "        finally:\n"
        "            self.release()\n"
    )
    assert "try_finally_close" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_stateful_strategy_classes_are_not_flagged(tmp_path):
    # Strategies that carry configuration earn their classes.
    (tmp_path / "sample.py").write_text(
        "class Discount:\n"
        "    def apply(self, order):\n"
        "        raise NotImplementedError\n"
        "\n"
        "class Percent(Discount):\n"
        "    def __init__(self, rate):\n"
        "        self.rate = rate\n"
        "\n"
        "    def apply(self, order):\n"
        "        return order * self.rate\n"
        "\n"
        "class OnSale(Discount):\n"
        "    def apply(self, order):\n"
        "        return order * 0.5\n"
    )
    assert "stateless_strategy_classes" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_validating_metaclass_is_not_a_singleton(tmp_path):
    # An if-gate plus a *local* assignment from super().__call__ is validation,
    # not instance caching — only persistent storage counts.
    (tmp_path / "sample.py").write_text(
        "class ValidatingMeta(type):\n"
        "    def __call__(cls, *args, **kwargs):\n"
        "        obj = super().__call__(*args, **kwargs)\n"
        "        if not obj.is_valid():\n"
        "            raise ValueError(obj)\n"
        "        return obj\n"
    )
    assert "handrolled_singleton" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_instance_factory_recording_latest_is_not_a_singleton(tmp_path):
    # create_instance() builds a fresh object every call and records the latest;
    # without a guard/read of the stored attribute it is a factory, not a Singleton.
    (tmp_path / "sample.py").write_text(
        "class Widget:\n"
        "    @classmethod\n"
        "    def create_instance(cls, name):\n"
        "        obj = cls(name)\n"
        "        cls._last_instance = obj\n"
        "        return obj\n"
    )
    assert "handrolled_singleton" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_dict_restore_in_init_is_not_borg(tmp_path):
    # Restoring per-instance state from a parameter shares nothing.
    (tmp_path / "sample.py").write_text(
        "class Snapshot:\n"
        "    def __init__(self, state):\n"
        "        self.__dict__ = state\n"
    )
    assert "borg_shared_state" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_outer_function_not_blamed_for_nested_memoize(tmp_path):
    # Only the nested helper hand-rolls memoization; the outer function must not
    # receive a duplicate, mislocated finding.
    (tmp_path / "sample.py").write_text(
        "_CACHE = {}\n"
        "\n"
        "def outer(x):\n"
        "    def helper(k):\n"
        "        if k in _CACHE:\n"
        "            return _CACHE[k]\n"
        "        _CACHE[k] = k * 2\n"
        "        return _CACHE[k]\n"
        "    return helper(x)\n"
    )
    findings = [f for f in run_detector("find_pattern_issues.py", tmp_path)
                if f["smell_type"] == "handrolled_memoize"]
    assert [f["description"] for f in findings] == [
        "'helper' hand-rolls memoization through module-level dict '_CACHE'"
    ]


def test_warn_only_del_is_not_flagged(tmp_path):
    # A diagnostic finalizer reports the leak; it does not clean up.
    (tmp_path / "sample.py").write_text(
        "import warnings\n"
        "\n"
        "class Conn:\n"
        "    def __del__(self):\n"
        "        if not self.closed:\n"
        "            warnings.warn('unclosed Conn', ResourceWarning)\n"
    )
    assert "finalizer_del" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_async_stateless_strategies_are_flagged(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Transport:\n"
        "    async def send(self, payload):\n"
        "        raise NotImplementedError\n"
        "\n"
        "class Http(Transport):\n"
        "    async def send(self, payload):\n"
        "        return 'http'\n"
        "\n"
        "class Grpc(Transport):\n"
        "    async def send(self, payload):\n"
        "        return 'grpc'\n"
    )
    assert "stateless_strategy_classes" in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_nested_class_base_not_resolved_against_module_scope(tmp_path):
    # The nested Child's Base is the container's, not the unrelated top-level
    # Base — refused_bequest must not fire on the wrong hierarchy.
    (tmp_path / "sample.py").write_text(
        "class Base:\n"
        "    def render(self):\n"
        "        return 'top-level'\n"
        "\n"
        "class Container:\n"
        "    class Base:\n"
        "        pass\n"
        "\n"
        "    class Child(Base):\n"
        "        def render(self):\n"
        "            raise NotImplementedError\n"
    )
    assert "refused_bequest" not in smell_types(
        run_detector("find_design_smells.py", tmp_path)
    )


def test_factory_returning_recorded_attr_is_not_a_singleton(tmp_path):
    # Assign-then-return still constructs a fresh object per call; only a guard
    # that reads the stored attribute before constructing is singleton reuse.
    (tmp_path / "sample.py").write_text(
        "class Widget:\n"
        "    @classmethod\n"
        "    def create_instance(cls, name):\n"
        "        cls._last_instance = cls(name)\n"
        "        return cls._last_instance\n"
    )
    assert "handrolled_singleton" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_accessors_over_different_attrs_are_not_a_pair(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Reading:\n"
        "    def get_value(self):\n"
        "        return self.normalized_value\n"
        "    def set_value(self, value):\n"
        "        self.raw_value = value\n"
    )
    assert "getter_setter_pair" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_cursor_with_other_methods_is_not_an_iterator_class(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Cursor:\n"
        "    def __iter__(self):\n"
        "        return self\n"
        "    def __next__(self):\n"
        "        return self.fetchone()\n"
        "    def execute(self, sql):\n"
        "        self.sql = sql\n"
    )
    assert "iterator_class" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_accumulator_dict_is_not_memoization(tmp_path):
    # Gate + read + write of a session store is stateful accumulation;
    # lru_cache would suppress the updates.
    (tmp_path / "sample.py").write_text(
        "SESSIONS = {}\n"
        "\n"
        "def update_session(key, value):\n"
        "    if key not in SESSIONS:\n"
        "        SESSIONS[key] = []\n"
        "    SESSIONS[key].append(value)\n"
        "    return SESSIONS[key]\n"
    )
    assert "handrolled_memoize" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_exitstack_advice_names_the_actual_receiver(tmp_path):
    (tmp_path / "sample.py").write_text(
        "def send(conn, payload):\n"
        "    try:\n"
        "        conn.send(payload)\n"
        "    finally:\n"
        "        conn.disconnect()\n"
    )
    findings = [f for f in run_detector("find_pattern_issues.py", tmp_path)
                if f["smell_type"] == "try_finally_close"]
    assert findings and "conn.disconnect" in findings[0]["suggestion"]


def test_analyze_diff_keeps_cross_definition_finding_on_unchanged_anchor(tmp_path):
    # Adding set_name beside an existing get_name completes the accessor pair;
    # the finding anchors at the *unchanged* getter and must survive the filter.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "m.py").write_text(
        "class Person:\n"
        "    def get_name(self):\n"
        "        return self._name\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "m.py").write_text(
        "class Person:\n"
        "    def get_name(self):\n"
        "        return self._name\n"
        "\n"
        "    def set_name(self, value):\n"
        "        self._name = value\n"
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=repo, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    assert "getter_setter_pair" in {f["smell_type"] for f in json.loads(result.stdout)}


def test_recording_new_is_not_a_singleton(tmp_path):
    # cls.last_created = super().__new__(cls); return — constructs every call.
    (tmp_path / "sample.py").write_text(
        "class Tracker:\n"
        "    def __new__(cls):\n"
        "        cls.last_created = super().__new__(cls)\n"
        "        return cls.last_created\n"
    )
    assert "handrolled_singleton" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_local_dict_in_metaclass_is_not_a_registry(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class CheckingMeta(type):\n"
        "    def __new__(mcs, name, bases, ns):\n"
        "        new_cls = super().__new__(mcs, name, bases, ns)\n"
        "        local = {}\n"
        "        local[name] = new_cls\n"
        "        validate(local)\n"
        "        return new_cls\n"
    )
    assert "registry_metaclass" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_working_setters_are_not_a_fluent_builder(tmp_path):
    # Setters that validate / call collaborators are not builder boilerplate.
    (tmp_path / "sample.py").write_text(
        "class Pipeline:\n"
        "    def set_source(self, src):\n"
        "        self.source = validate_source(src)\n"
        "        self.refresh()\n"
        "        return self\n"
        "    def set_sink(self, sink):\n"
        "        if sink is None:\n"
        "            raise ValueError\n"
        "        self.sink = sink\n"
        "        return self\n"
        "    def set_mode(self, mode):\n"
        "        self.mode = mode\n"
        "        self.log.info(mode)\n"
        "        return self\n"
        "    def build(self):\n"
        "        return (self.source, self.sink, self.mode)\n"
    )
    assert "fluent_builder" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_result_field_only_written_is_not_temporary(tmp_path):
    # An output field populated for callers is part of the state model.
    (tmp_path / "sample.py").write_text(
        "class Job:\n"
        "    def __init__(self):\n"
        "        self.result = None\n"
        "\n"
        "    def run(self):\n"
        "        self.result = compute()\n"
    )
    assert "temporary_field" not in smell_types(
        run_detector("find_design_smells.py", tmp_path)
    )


def test_non_terminating_flag_assignment_is_not_a_control_flag(tmp_path):
    # `running = True` inside `while running` keeps the loop going.
    (tmp_path / "sample.py").write_text(
        "def serve(jobs):\n"
        "    running = False\n"
        "    while running:\n"
        "        if jobs:\n"
        "            running = True\n"
    )
    assert "control_flag" not in smell_types(
        run_detector("find_design_smells.py", tmp_path)
    )


def test_analyze_diff_drops_cross_definition_finding_on_unrelated_edit(tmp_path):
    # A pre-existing accessor pair must not surface when the change touches
    # only an unrelated function in the same file.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    pair = (
        "class Person:\n"
        "    def get_name(self):\n"
        "        return self._name\n"
        "\n"
        "    def set_name(self, value):\n"
        "        self._name = value\n"
        "\n"
    )
    (repo / "m.py").write_text(pair + "\ndef unrelated():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "m.py").write_text(pair + "\ndef unrelated():\n    return 2\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD", "--format", "json"],
        cwd=repo, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr[:500]
    assert "getter_setter_pair" not in {f["smell_type"] for f in json.loads(result.stdout)}


def test_validating_recorder_metaclass_is_not_a_singleton(tmp_path):
    # A validation branch beside a recorder assignment is not instance caching:
    # the guard must read the storage the instance lands in.
    (tmp_path / "sample.py").write_text(
        "class RecordingMeta(type):\n"
        "    def __call__(cls, *args, **kwargs):\n"
        "        if not args:\n"
        "            raise ValueError('args required')\n"
        "        cls.last_created = super().__call__(*args, **kwargs)\n"
        "        return cls.last_created\n"
    )
    assert "handrolled_singleton" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_metaclass_doing_more_than_registering_is_not_flagged(tmp_path):
    # __init_subclass__ would not preserve the namespace rewriting.
    (tmp_path / "sample.py").write_text(
        "class BusyMeta(type):\n"
        "    REGISTRY = {}\n"
        "    def __new__(mcs, name, bases, ns):\n"
        "        ns['extra'] = build_extra(ns)\n"
        "        new_cls = super().__new__(mcs, name, bases, ns)\n"
        "        mcs.REGISTRY[name] = new_cls\n"
        "        return new_cls\n"
    )
    assert "registry_metaclass" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_unconditional_recompute_is_not_memoization(tmp_path):
    # Membership gate + write + return without hit-branch reuse: every call
    # recomputes, so lru_cache would change behavior.
    (tmp_path / "sample.py").write_text(
        "CACHE = {}\n"
        "\n"
        "def refresh(key):\n"
        "    if key in CACHE:\n"
        "        audit(key)\n"
        "    CACHE[key] = calculate(key)\n"
        "    return CACHE[key]\n"
    )
    assert "handrolled_memoize" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_async_accessors_are_not_a_getter_setter_pair(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Remote:\n"
        "    async def get_name(self):\n"
        "        return self._name\n"
        "    async def set_name(self, value):\n"
        "        self._name = value\n"
    )
    assert "getter_setter_pair" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_async_lazy_property_not_pushed_to_cached_property(tmp_path):
    # cached_property would cache the coroutine, which cannot be re-awaited.
    (tmp_path / "sample.py").write_text(
        "class Client:\n"
        "    @property\n"
        "    async def conn(self):\n"
        "        if self._conn is None:\n"
        "            self._conn = await connect()\n"
        "        return self._conn\n"
    )
    assert "handrolled_lazy_property" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_decorated_strategy_classes_are_not_flagged(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Discount:\n"
        "    def apply(self, order):\n"
        "        raise NotImplementedError\n"
        "\n"
        "@register\n"
        "class TenPercent(Discount):\n"
        "    def apply(self, order):\n"
        "        return order * 0.9\n"
        "\n"
        "@register\n"
        "class OnSale(Discount):\n"
        "    def apply(self, order):\n"
        "        return order * 0.5\n"
    )
    assert "stateless_strategy_classes" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_nested_function_state_does_not_implicate_outer_method(tmp_path):
    # Compares/assigns inside a nested helper belong to that scope; with only
    # one real outer method touching self.status there is no state machine.
    (tmp_path / "sample.py").write_text(
        "class Worker:\n"
        "    def run(self):\n"
        "        if self.status == 'new':\n"
        "            self.status = 'busy'\n"
        "\n"
        "    def schedule(self):\n"
        "        def helper():\n"
        "            if self.status == 'busy':\n"
        "                self.status = 'done'\n"
        "        return helper\n"
    )
    assert "string_state_machine" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_returned_result_field_is_not_temporary(tmp_path):
    # run() populates and returns self.result — an output, not scratch.
    (tmp_path / "sample.py").write_text(
        "class Job:\n"
        "    def __init__(self):\n"
        "        self.result = None\n"
        "\n"
        "    def run(self):\n"
        "        self.result = compute()\n"
        "        return self.result\n"
    )
    assert "temporary_field" not in smell_types(
        run_detector("find_design_smells.py", tmp_path)
    )


def test_field_reset_to_none_is_temporary(tmp_path):
    # An explicit reset proves the None-except-during-one-operation lifecycle.
    (tmp_path / "sample.py").write_text(
        "class Parser:\n"
        "    def __init__(self):\n"
        "        self.buffer = None\n"
        "\n"
        "    def parse(self, text):\n"
        "        self.buffer = text.split()\n"
        "        out = transform(self.buffer)\n"
        "        self.buffer = None\n"
        "        return out\n"
    )
    assert "temporary_field" in smell_types(
        run_detector("find_design_smells.py", tmp_path)
    )


def test_empty_subclass_of_registering_base_is_not_lazy(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Plugin:\n"
        "    registry = []\n"
        "    def __init_subclass__(cls, **kwargs):\n"
        "        super().__init_subclass__(**kwargs)\n"
        "        Plugin.registry.append(cls)\n"
        "\n"
        "class CsvPlugin(Plugin):\n"
        "    pass\n"
    )
    assert "lazy_class" not in smell_types(
        run_detector("find_design_smells.py", tmp_path)
    )


def test_flag_set_in_nested_loop_is_not_a_control_flag(tmp_path):
    # A break at the assignment would exit only the inner for loop.
    (tmp_path / "sample.py").write_text(
        "def scan(batches):\n"
        "    done = False\n"
        "    while not done:\n"
        "        for item in next_batch():\n"
        "            if item.is_last:\n"
        "                done = True\n"
        "        commit()\n"
    )
    assert "control_flag" not in smell_types(
        run_detector("find_design_smells.py", tmp_path)
    )


def test_nested_helper_singleton_machinery_not_attributed_to_outer(tmp_path):
    # The guard and cache assignment live in a nested helper; the outer
    # accessor constructs fresh on every call.
    (tmp_path / "sample.py").write_text(
        "class Service:\n"
        "    @classmethod\n"
        "    def make_instance(cls):\n"
        "        def helper():\n"
        "            if cls._instance is None:\n"
        "                cls._instance = cls()\n"
        "            return cls._instance\n"
        "        return cls()\n"
    )
    assert "handrolled_singleton" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_async_fluent_setters_are_not_a_builder(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Session:\n"
        "    async def set_host(self, h):\n"
        "        self.host = h\n"
        "        return self\n"
        "    async def set_port(self, p):\n"
        "        self.port = p\n"
        "        return self\n"
        "    async def set_user(self, u):\n"
        "        self.user = u\n"
        "        return self\n"
        "    def build(self):\n"
        "        return (self.host, self.port, self.user)\n"
    )
    assert "fluent_builder" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_classmethod_strategies_are_not_flagged(tmp_path):
    (tmp_path / "sample.py").write_text(
        "class Codec:\n"
        "    def decode(self, raw):\n"
        "        raise NotImplementedError\n"
        "\n"
        "class JsonCodec(Codec):\n"
        "    @classmethod\n"
        "    def decode(cls, raw):\n"
        "        return cls.loads(raw)\n"
        "\n"
        "class XmlCodec(Codec):\n"
        "    @classmethod\n"
        "    def decode(cls, raw):\n"
        "        return cls.parse(raw)\n"
    )
    assert "stateless_strategy_classes" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_lazy_property_with_setter_not_pushed_to_cached_property(tmp_path):
    # cached_property has no setter API; client.conn = x would stop working.
    (tmp_path / "sample.py").write_text(
        "class Client:\n"
        "    @property\n"
        "    def conn(self):\n"
        "        if self._conn is None:\n"
        "            self._conn = connect()\n"
        "        return self._conn\n"
        "\n"
        "    @conn.setter\n"
        "    def conn(self, value):\n"
        "        self._conn = value\n"
    )
    assert "handrolled_lazy_property" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_uncalled_nested_helper_does_not_make_del_cleanup(tmp_path):
    (tmp_path / "sample.py").write_text(
        "import warnings\n"
        "\n"
        "class Conn:\n"
        "    def __del__(self):\n"
        "        def cleanup():\n"
        "            self.handle.close()\n"
        "        warnings.warn('unclosed Conn', ResourceWarning)\n"
    )
    assert "finalizer_del" not in smell_types(
        run_detector("find_pattern_issues.py", tmp_path)
    )


def test_callback_field_use_is_not_a_temporary_field(tmp_path):
    # The nested callback runs later; self.current persists between calls and
    # is not scratch local to make_callback.
    (tmp_path / "sample.py").write_text(
        "class Tracker:\n"
        "    def __init__(self):\n"
        "        self.current = None\n"
        "\n"
        "    def make_callback(self):\n"
        "        def on_event(value):\n"
        "            self.current = value\n"
        "            log(self.current)\n"
        "        return on_event\n"
    )
    assert "temporary_field" not in smell_types(
        run_detector("find_design_smells.py", tmp_path)
    )
