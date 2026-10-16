"""False positives removed from the correctness and security detectors.

Each rule here was written against a real library's report, where the detector
charged a correctness or security defect to code that had none: an explicit
`return None`, a pydantic field default, an `example.com` fixture, a DDL
statement whose identifier cannot be bound. Every test that pins a removed false
positive sits next to one that pins the true positive the same rule must keep
catching — a fix that quietened both would pass the first test alone.

Fixtures are written to tmp_path at runtime, as in test_detectors.py.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


def run_detector(script: str, target: Path, *extra: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json", *extra],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[:500]}"
    return json.loads(result.stdout)


def records(script: str, tmp_path: Path, source: str, smell: str, name: str = "sample.py") -> list[dict]:
    """The records of one smell type for one fixture file."""
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return [r for r in run_detector(script, path) if r["smell_type"] == smell]


def findings(found: list[dict]) -> list[dict]:
    return [r for r in found if r.get("kind", "finding") == "finding"]


def candidates(found: list[dict]) -> list[dict]:
    return [r for r in found if r.get("kind") == "candidate"]


def assert_candidate_shape(record: dict) -> None:
    """A candidate names its benign readings and carries no fix."""
    assert record["kind"] == "candidate"
    assert record["also_caused_by"], "a candidate names how healthy code produces it"
    assert not record["suggestion"], "a candidate carries what to confirm, not a fix"


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS_DIR, "common")


# ---- common.reasoned_noqa / looks_like_test ------------------------------- #


@pytest.mark.parametrize("line", [
    "except Exception:  # noqa: BLE001 -- best-effort cache warm",
    "except Exception:  # noqa: BLE001 - bookkeeping never fails a call",
    "except Exception:  # noqa: BLE001 \u2014 a disconnected client can't be told",
    "except Exception:  # noqa: BLE001: see the docstring",
    "except Exception:  # noqa: S110, BLE001 -- both, for one reason",
])
def test_a_noqa_with_a_reason_is_reasoned(common, line):
    assert common.reasoned_noqa([line], 1, "BLE001")


@pytest.mark.parametrize("line", [
    "except Exception:  # noqa: BLE001",
    "except Exception:  # noqa: BLE001 --",
    "except Exception:  # noqa: E722 -- a different rule's reason",
    "except Exception:  # best-effort, BLE001 is not even named in a noqa",
])
def test_a_bare_or_foreign_noqa_is_not_reasoned(common, line):
    assert not common.reasoned_noqa([line], 1, "BLE001")


def test_a_reasoned_noqa_may_sit_on_the_comment_line_below(common):
    lines = ["except Exception:", "    # noqa: BLE001 -- the caller reads None as unreadable", "    pass"]
    assert common.reasoned_noqa(lines, 1, "BLE001")
    # ...but not on a code line below: that is a different statement's suppression.
    lines = ["except Exception:", "    pass  # noqa: BLE001 -- a later line", ""]
    assert not common.reasoned_noqa(lines, 1, "BLE001")


@pytest.mark.parametrize("path, expected", [
    ("pkg/tests/helpers.py", True),
    ("pkg/test_thing.py", True),
    ("pkg/thing_test.py", True),
    ("pkg/conftest.py", True),
    ("pkg/testing.py", False),
    ("pkg/contest/entry.py", False),
])
def test_looks_like_test(common, path, expected):
    assert common.looks_like_test(path) is expected


# ---- inconsistent_returns -------------------------------------------------- #


def test_explicit_return_none_beside_a_value_is_consistent(tmp_path):
    """PEP 8 asks for exactly this spelling: no finding, at any severity."""
    found = records("find_return_issues.py", tmp_path,
                    "def f(x) -> int | None:\n"
                    "    if x:\n"
                    "        return None\n"
                    "    return 1\n",
                    "inconsistent_returns")
    assert found == []


def test_a_nested_functions_returns_are_not_charged_to_its_parent(tmp_path):
    """A `def` directly in the body is its own scope; its `return 1` is not the outer's."""
    found = records("find_return_issues.py", tmp_path,
                    "def outer(c):\n"
                    "    def inner():\n"
                    "        return 1\n"
                    "    if c:\n"
                    "        return\n"
                    "    inner()\n",
                    "inconsistent_returns")
    assert found == []


def test_a_bare_return_beside_a_value_is_still_flagged(tmp_path):
    """RET502: the true positive the explicit-None rule must not swallow."""
    found = records("find_return_issues.py", tmp_path,
                    "def f(x):\n"
                    "    if x:\n"
                    "        return\n"
                    "    return 1\n",
                    "inconsistent_returns")
    assert len(findings(found)) == 1
    assert "bare 'return'" in found[0]["description"]


def test_falling_off_the_end_after_a_value_is_flagged(tmp_path):
    """RET503: one path returns 1, the other runs off the end and returns None."""
    found = records("find_return_issues.py", tmp_path,
                    "def f(x):\n"
                    "    if x:\n"
                    "        return 1\n",
                    "inconsistent_returns")
    assert len(findings(found)) == 1
    assert "end of its body" in found[0]["description"]
    assert found[0]["severity"] == "medium"


@pytest.mark.parametrize("body", [
    # every branch returns
    "    if x:\n        return 1\n    else:\n        return 2\n",
    # a with block that returns
    "    with x:\n        return 1\n",
    # a handler that raises
    "    try:\n        return int(x)\n    except ValueError:\n        raise\n",
    # an infinite loop left only by returning
    "    while True:\n        if x:\n            return 1\n",
    # a never-returning call
    "    if x:\n        return 1\n    sys.exit(2)\n",
    # a finally that returns
    "    try:\n        pass\n    finally:\n        return 1\n",
    # a method annotated NoReturn, called through self
    "    if x:\n        return 1\n    self._abort()\n",
])
def test_bodies_that_cannot_fall_off_are_not_flagged(tmp_path, body):
    source = ("import sys\n"
              "from typing import NoReturn\n"
              "class C:\n"
              "    def _abort(self) -> NoReturn:\n"
              "        raise SystemExit\n"
              "def f(x, self=None):\n" + body)
    assert records("find_return_issues.py", tmp_path, source, "inconsistent_returns") == []


@pytest.mark.parametrize("body", [
    # a handler that logs and carries on
    "    try:\n        return int(x)\n    except ValueError:\n        print('bad')\n",
    # a while True with its own break
    "    while True:\n        if x:\n            return 1\n        break\n",
    # an elif chain with no else
    "    if x > 0:\n        return 1\n    elif x < 0:\n        return -1\n",
    # a for loop over a possibly empty iterable
    "    for y in x:\n        return y\n",
])
def test_bodies_that_can_fall_off_are_flagged(tmp_path, body):
    found = records("find_return_issues.py", tmp_path, "def f(x):\n" + body, "inconsistent_returns")
    assert len(findings(found)) == 1


# ---- raise_without_from ----------------------------------------------------- #


def test_an_http_exception_raised_without_from_is_a_candidate(tmp_path):
    """The framework renders it as a response; the chain is never printed."""
    found = records("find_exception_issues.py", tmp_path,
                    "def get(d, k):\n"
                    "    try:\n"
                    "        return d[k]\n"
                    "    except KeyError:\n"
                    "        raise HTTPException(status_code=404)\n",
                    "raise_without_from")
    assert len(found) == 1
    assert_candidate_shape(found[0])


def test_a_domain_translation_without_from_is_a_low_finding(tmp_path):
    """The true positive stays, at the severity of the clarity issue it is."""
    found = records("find_exception_issues.py", tmp_path,
                    "import json\n"
                    "def load(s):\n"
                    "    try:\n"
                    "        return json.loads(s)\n"
                    "    except ValueError as e:\n"
                    "        raise BundleError(f'bad: {e}')\n",
                    "raise_without_from")
    assert len(findings(found)) == 1
    assert found[0]["severity"] == "low"
    # The description no longer claims the cause is discarded: it is kept as __context__.
    assert "discards" not in found[0]["description"]
    assert "__context__" in found[0]["description"]


# ---- swallowed_exception / suppressible_exception --------------------------- #


def test_a_broad_catch_with_a_reasoned_noqa_is_a_candidate(tmp_path):
    found = records("find_exception_issues.py", tmp_path,
                    "def warm():\n"
                    "    try:\n"
                    "        f()\n"
                    "    except Exception:  # noqa: BLE001 -- best-effort cache warm\n"
                    "        pass\n",
                    "swallowed_exception")
    assert len(found) == 1
    assert_candidate_shape(found[0])


@pytest.mark.parametrize("comment", ["", "  # noqa: BLE001"])
def test_a_broad_catch_with_no_reason_stays_a_finding(tmp_path, comment):
    """No noqa, or a bare one, proves nothing: the swallow is still the defect."""
    found = records("find_exception_issues.py", tmp_path,
                    "def warm():\n"
                    "    try:\n"
                    "        f()\n"
                    f"    except Exception:{comment}\n"
                    "        pass\n",
                    "swallowed_exception")
    assert len(findings(found)) == 1
    assert found[0]["severity"] == "medium"


def test_a_narrow_pass_is_a_suppress_idiom_not_a_swallow(tmp_path):
    (tmp_path / "sample.py").write_text(
        "import os\n"
        "def drop(p):\n"
        "    try:\n"
        "        os.remove(p)\n"
        "    except FileNotFoundError:\n"
        "        pass\n")
    found = run_detector("find_exception_issues.py", tmp_path)
    assert [r["smell_type"] for r in found] == ["suppressible_exception"]
    assert found[0]["severity"] == "low"
    assert "contextlib.suppress(FileNotFoundError)" in found[0]["suggestion"]


@pytest.mark.parametrize("tail", [
    "    else:\n        g()\n",
    "    finally:\n        g()\n",
    "    except KeyError:\n        g()\n",
])
def test_a_narrow_pass_suppress_cannot_rewrite_is_not_reported(tmp_path, tail):
    """With an else, a finally or another handler, suppress is not the same code."""
    (tmp_path / "sample.py").write_text(
        "import os\n"
        "def drop(p):\n"
        "    try:\n"
        "        os.remove(p)\n"
        "    except FileNotFoundError:\n"
        "        pass\n" + tail)
    assert run_detector("find_exception_issues.py", tmp_path) == []


# ---- catches_baseexception -------------------------------------------------- #


@pytest.mark.parametrize("body", [
    "    except BaseException:\n        cleanup()\n        raise\n",
    "    except BaseException as e:\n        cleanup()\n        raise e\n",
    "    except BaseException as e:\n        fut.set_exception(e)\n",
    "    except BaseException as e:\n        errors.append(e)\n",
])
def test_a_baseexception_handler_that_hands_the_error_on_is_not_flagged(tmp_path, body):
    found = records("find_exception_issues.py", tmp_path,
                    "def work():\n    try:\n        w()\n" + body,
                    "catches_baseexception")
    assert found == []


@pytest.mark.parametrize("body", [
    "    except BaseException:\n        log.warning('x')\n",
    # logging or formatting the exception is not delivering it: the Ctrl-C stops here
    "    except BaseException as e:\n        log.warning('failed: %s', e)\n",
    "    except BaseException as e:\n        print(str(e))\n",
])
def test_a_baseexception_handler_that_traps_the_error_is_still_flagged(tmp_path, body):
    found = records("find_exception_issues.py", tmp_path,
                    "def work():\n    try:\n        w()\n" + body,
                    "catches_baseexception")
    assert len(findings(found)) == 1


def test_a_baseexception_trap_with_a_reasoned_noqa_is_a_candidate(tmp_path):
    found = records("find_exception_issues.py", tmp_path,
                    "def child():\n"
                    "    try:\n"
                    "        w()\n"
                    "    except BaseException as e:  # noqa: BLE001 -- a forked child must exit, never resume\n"
                    "        report(type(e).__name__)\n",
                    "catches_baseexception")
    assert len(found) == 1
    assert_candidate_shape(found[0])


# ---- assert_for_validation -------------------------------------------------- #


@pytest.mark.parametrize("source", [
    # a private helper's precondition
    "def _f(x):\n    assert x > 0\n",
    # narrowing, even on a parameter of a public function
    "def f(x):\n    assert x is not None\n    return x.y\n",
    "def f(x):\n    assert isinstance(x, int) and x is not None\n    return x\n",
    # an invariant on a local, not on an argument
    "def g():\n    y = h()\n    assert y\n    return y\n",
    # module scope
    "assert VERSION\n",
])
def test_an_assert_that_states_an_invariant_is_a_candidate(tmp_path, source):
    found = records("find_exception_issues.py", tmp_path, source, "assert_for_validation")
    assert len(found) == 1
    assert_candidate_shape(found[0])


@pytest.mark.parametrize("source", [
    "def withdraw(amount):\n    assert amount > 0\n",
    "def delete(user):\n    assert user.is_admin\n",
    # a dunder is public: __init__ validates a caller's input as much as any method
    "class Account:\n    def __init__(self, balance):\n        assert balance >= 0\n",
])
def test_an_assert_validating_a_public_argument_is_a_finding(tmp_path, source):
    found = records("find_exception_issues.py", tmp_path, source, "assert_for_validation")
    assert len(findings(found)) == 1


def test_an_assert_in_a_test_file_is_not_reported(tmp_path):
    found = records("find_exception_issues.py", tmp_path,
                    "def withdraw(amount):\n    assert amount > 0\n",
                    "assert_for_validation", name="tests/test_bank.py")
    assert found == []


def test_a_raise_inside_a_def_in_a_handler_belongs_to_the_def(tmp_path):
    """The nested function raises later, when called — not inside this handler."""
    found = records("find_exception_issues.py", tmp_path,
                    "def f():\n"
                    "    try:\n"
                    "        g()\n"
                    "    except KeyError:\n"
                    "        def later():\n"
                    "            raise RuntimeError('x')\n"
                    "        register(later)\n",
                    "raise_without_from")
    assert found == []


# ---- mutated_global / global_rebind ---------------------------------------- #


def test_a_global_mutated_only_under_a_module_lock_is_medium(tmp_path):
    found = records("find_global_state.py", tmp_path,
                    "import threading\n"
                    "_L = threading.Lock()\n"
                    "_C = {}\n"
                    "def get(k):\n"
                    "    with _L:\n"
                    "        return _C.setdefault(k, object())\n",
                    "mutated_global")
    assert [(r["kind"], r["severity"]) for r in found] == [("finding", "medium")]


@pytest.mark.parametrize("source", [
    # no lock at all
    "_C = {}\ndef bump(k):\n    _C[k] = 1\n",
    # one site under the lock, one outside it
    "from threading import RLock\n_L = RLock()\n_C = {}\n"
    "def get(k):\n    with _L:\n        _C[k] = 1\n"
    "def drop(k):\n    _C.pop(k)\n",
    # a lock that is not module-level does not count
    "import threading\n_C = {}\n"
    "def get(k):\n    lock = threading.Lock()\n    with lock:\n        _C[k] = 1\n",
])
def test_an_unguarded_global_mutation_stays_high(tmp_path, source):
    found = records("find_global_state.py", tmp_path, source, "mutated_global")
    assert [(r["kind"], r["severity"]) for r in found] == [("finding", "high")]


@pytest.mark.parametrize("source", [
    # a local of the same spelling
    "cache = {}\ndef f(k, v):\n    cache = {}\n    cache[k] = v\n    return cache\n",
    # a parameter of the same spelling
    "cache = {}\ndef f(cache, k):\n    cache[k] = 1\n",
    # an enclosing function's local, reached by closure
    "cache = {}\ndef outer():\n    cache = {}\n    def inner(k):\n        cache[k] = 1\n    return inner\n",
    # a pure `global` rebind is global_rebind's, not also mutated_global's
    "cache = {}\ndef reset():\n    global cache\n    cache = {}\n",
])
def test_a_mutation_of_another_object_with_the_same_name_is_not_charged(tmp_path, source):
    assert records("find_global_state.py", tmp_path, source, "mutated_global") == []


@pytest.mark.parametrize("source", [
    "cache = {}\ndef put(k, v):\n    cache[k] = v\n",
    # declared global and mutated in place
    "cache = {}\ndef put(k, v):\n    global cache\n    cache.update({k: v})\n",
    # a closure that does NOT shadow the name mutates the module's
    "cache = {}\ndef outer():\n    def inner(k):\n        cache[k] = 1\n    return inner\n",
    # a comprehension variable of the same spelling is the comprehension's own
    "cache = {}\ndef put(xs):\n    _ = [cache for cache in xs]\n    cache['k'] = 1\n",
])
def test_a_mutation_of_the_module_object_is_still_charged(tmp_path, source):
    found = records("find_global_state.py", tmp_path, source, "mutated_global")
    assert len(findings(found)) == 1


def test_global_rebind_is_one_record_per_function_naming_every_global(tmp_path):
    found = records("find_global_state.py", tmp_path,
                    "a = b = None\n"
                    "def f():\n"
                    "    global a, b\n"
                    "    a = 1\n"
                    "    b = 2\n",
                    "global_rebind")
    assert len(findings(found)) == 1
    assert "'a'" in found[0]["description"] and "'b'" in found[0]["description"]


def test_global_rebind_with_a_reasoned_noqa_is_a_candidate(tmp_path):
    found = records("find_global_state.py", tmp_path,
                    "a = None\n"
                    "def g():\n"
                    "    global a  # noqa: PLW0603 -- fork hook\n"
                    "    a = 1\n",
                    "global_rebind")
    assert len(found) == 1
    assert_candidate_shape(found[0])


@pytest.mark.parametrize("comment", ["", "  # noqa: PLW0603"])
def test_global_rebind_without_a_reason_is_still_a_finding(tmp_path, comment):
    found = records("find_global_state.py", tmp_path,
                    "counter = 0\n"
                    "def bump():\n"
                    f"    global counter{comment}\n"
                    "    counter += 1\n",
                    "global_rebind")
    assert len(findings(found)) == 1


def test_a_nested_functions_global_is_not_charged_to_its_parent(tmp_path):
    found = records("find_global_state.py", tmp_path,
                    "a = None\n"
                    "def outer():\n"
                    "    def inner():\n"
                    "        global a\n"
                    "        a = 1\n"
                    "    return inner\n",
                    "global_rebind")
    assert [r["description"].split("'")[1] for r in found] == ["inner"]


# ---- mutable_class_attribute ------------------------------------------------- #


@pytest.mark.parametrize("header", [
    "from pydantic import BaseModel\nclass M(BaseModel):\n",
    "import pydantic\nclass M(pydantic.BaseModel):\n",
    "from pydantic.v1 import BaseModel as V1\nclass M(V1):\n",
    "from pydantic_settings import BaseSettings\nclass M(BaseSettings):\n",
    "from sqlmodel import SQLModel\nclass M(SQLModel):\n",
    # inheritance through a local model is followed
    "from pydantic import BaseModel\nclass Citation(BaseModel):\n    scene: str = ''\nclass M(Citation):\n",
])
def test_pydantic_field_defaults_are_copied_not_shared(tmp_path, header):
    source = header + "    tags: list[str] = []\n    def add(self, t):\n        self.tags.append(t)\n"
    assert records("find_mutation_hazards.py", tmp_path, source, "mutable_class_attribute") == []


def test_a_basemodel_lookalike_is_still_flagged(tmp_path):
    found = records("find_mutation_hazards.py", tmp_path,
                    "class BaseModel:\n"
                    "    pass\n"
                    "class M(BaseModel):\n"
                    "    tags = []\n"
                    "    def add(self, t):\n"
                    "        self.tags.append(t)\n",
                    "mutable_class_attribute")
    assert len(findings(found)) == 1


@pytest.mark.parametrize("source", [
    "import ctypes\nclass S(ctypes.Structure):\n    _fields_ = [('a', ctypes.c_int)]\n",
    # imported inside the function that declares it, as platform-only code does
    "def load():\n    import ctypes\n    class S(ctypes.Structure):\n"
    "        _fields_ = [('a', ctypes.c_int)]\n        _anonymous_ = ['a']\n    return S\n",
    "from ctypes import Union, c_int\nclass U(Union):\n    _fields_ = [('a', c_int)]\n",
])
def test_ctypes_layout_declarations_are_not_shared_state(tmp_path, source):
    assert records("find_mutation_hazards.py", tmp_path, source, "mutable_class_attribute") == []


def test_a_ctypes_class_still_reports_its_other_mutable_state(tmp_path):
    found = records("find_mutation_hazards.py", tmp_path,
                    "import ctypes\n"
                    "class S(ctypes.Structure):\n"
                    "    _fields_ = [('a', ctypes.c_int)]\n"
                    "    seen = []\n"
                    "    def note(self):\n"
                    "        S.seen.append(self)\n",
                    "mutable_class_attribute")
    assert [r["code_snippet"] for r in findings(found)] == ["seen = []"]


def test_a_class_mutable_nothing_mutates_is_a_candidate(tmp_path):
    found = records("find_mutation_hazards.py", tmp_path,
                    "class Calendar:\n"
                    "    _MAX_DAYS = {'Tishrei': 30}\n"
                    "    def days(self, month):\n"
                    "        return self._MAX_DAYS[month]\n",
                    "mutable_class_attribute")
    assert len(found) == 1
    assert_candidate_shape(found[0])


@pytest.mark.parametrize("mutation", [
    "R.items.append(x)",
    "self.items.append(x)",
    "self.items[x] = 1",
    "del self.items[x]",
    "self.items += [x]",
    "heapq.heappush(self.items, x)",
])
def test_a_class_mutable_something_mutates_is_a_finding(tmp_path, mutation):
    found = records("find_mutation_hazards.py", tmp_path,
                    "import heapq\n"
                    "class R:\n"
                    "    items = []\n"
                    "    def add(self, x):\n"
                    f"        {mutation}\n",
                    "mutable_class_attribute")
    assert len(findings(found)) == 1


def test_a_nested_defs_mutation_is_not_charged_to_the_outer_default(tmp_path):
    """`helper`'s own `acc` is its parameter, not the outer function's default."""
    found = records("find_mutation_hazards.py", tmp_path,
                    "def collect(item, acc=[]):\n"
                    "    def helper(acc):\n"
                    "        acc.append(item)\n"
                    "    return helper\n",
                    "mutated_default_arg")
    assert found == []


# ---- placeholder_value ------------------------------------------------------- #


@pytest.mark.parametrize("source", [
    'def apply(rule):\n    return rule["replacement"]\n',
    'KEY = "replace_messages"\n',
    'CODE = "replacement_incomplete"\n',
    # a real domain that merely ends in "example.com"
    'URL = "https://myexample.com/api"\n',
    # PEP 257 attribute docstring, and a string used as a block comment
    'Subject = tuple\n"""A replacement for the old tuple; see example.com."""\n',
    'def f():\n    "lorem ipsum: notes about the filler below"\n    return 1\n',
])
def test_words_that_contain_a_placeholder_token_are_not_placeholders(tmp_path, source):
    assert records("find_ai_scaffolding.py", tmp_path, source, "placeholder_value") == []


@pytest.mark.parametrize("source", [
    'URL = "https://proxy.example.com/v1"\n',
    'EMAIL = "someone@example.org"\n',
    'BODY = "lorem ipsum " * 500\n',
])
def test_reserved_hosts_and_filler_are_fixture_values_in_a_test(tmp_path, source):
    found = records("find_ai_scaffolding.py", tmp_path, source, "placeholder_value",
                    name="tests/test_client.py")
    assert found == []


@pytest.mark.parametrize("source", [
    'API_KEY = "CHANGEME"\n',
    'TOKEN = "your-api-key"\n',
    'X = "REPLACE_ME"\n',
    'SECRET = "REPLACE_ME_WITH_A_REAL_KEY"\n',
    'HINT = "insert-your-token-here"\n',
])
@pytest.mark.parametrize("name", ["settings.py", "tests/test_settings.py"])
def test_a_placeholder_token_is_still_flagged_anywhere(tmp_path, source, name):
    found = records("find_ai_scaffolding.py", tmp_path, source, "placeholder_value", name=name)
    assert len(findings(found)) == 1


def test_a_reserved_host_in_shipping_code_is_still_flagged(tmp_path):
    found = records("find_ai_scaffolding.py", tmp_path,
                    'DEFAULT_ENDPOINT = "https://api.example.com/v1"\n',
                    "placeholder_value", name="app/settings.py")
    assert len(findings(found)) == 1


# ---- unused_kwargs ----------------------------------------------------------- #


@pytest.mark.parametrize("source", [
    # passed by reference: the callee that will invoke it dictates the signature
    "def boom(*a, **k):\n    raise RuntimeError\n"
    "def test(monkeypatch):\n    monkeypatch.setattr(m, 'f', boom)\n",
    "def on_done(result, **kw):\n    print(result)\nregister(callback=on_done)\n",
    # the declared-unused convention
    "def f(x, **_ignored):\n    return x\n",
    # an override of a base class's method
    "class Fake(Client):\n    def stream(self, messages, **kwargs):\n        return iter(messages)\n",
    # an adapter that absorbs any call shape
    "def done(result, *args, **kwargs):\n    return result\n",
])
def test_kwargs_whose_signature_someone_else_chose_are_not_flagged(tmp_path, source):
    assert records("find_ai_scaffolding.py", tmp_path, source, "unused_kwargs") == []


@pytest.mark.parametrize("source", [
    "def scale(x, **kwargs):\n    return x * 2\nscale(3, factr=2)\n",
    # *args used, **kwargs not: not an absorb-everything adapter
    "def total(*args, **kwargs):\n    return sum(args)\n",
    # a method on a class with no base chooses its own signature
    "class Thing:\n    def configure(self, x, **options):\n        self.x = x\n",
])
def test_kwargs_that_only_swallow_typos_are_still_flagged(tmp_path, source):
    found = records("find_ai_scaffolding.py", tmp_path, source, "unused_kwargs")
    assert len(findings(found)) == 1
