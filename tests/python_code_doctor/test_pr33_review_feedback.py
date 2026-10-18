"""Regression cases from PR #33's unresolved review threads 59 through 65."""

import ast
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "skills/python-code-doctor" / "scripts"


@pytest.fixture
def detector(load_module):
    return load_module(SCRIPTS, "find_global_state")


def classification(detector, source):
    found = [record for record in detector.detect(ast.parse(source), "sample.py", source.splitlines(), set())
             if record.smell_type == "mutated_global"]
    assert len(found) == 1
    return found[0].kind


PREFIX = "from threading import Lock\n_guard = Lock()\nregistry = {}\n"


@pytest.mark.parametrize("invoke", [False, True])
def test_returned_lambda_requires_a_proof_only_when_invoked(detector, invoke):
    source = (PREFIX + "def get_unlock():\n    return lambda: _guard.release()\n"
              "def put(key):\n    with _guard:\n        callback = get_unlock()\n" +
              ("        callback()\n" if invoke else "") + "        registry[key] = 1\n")
    assert classification(detector, source) == ("finding" if invoke else "candidate")


@pytest.mark.parametrize("setup, getter", [
    ("", "getattr"),
    ("from builtins import getattr as attribute\n", "attribute"),
    ("import builtins\n", "builtins.getattr"),
])
def test_builtin_getattr_cannot_hide_a_literal_mutex_release(detector, setup, getter):
    source = (PREFIX + setup + "def put(key):\n    with _guard:\n" +
              f"        {getter}(_guard, 'release')()\n        registry[key] = 1\n")
    assert classification(detector, source) == "finding"


def test_a_shadowed_getattr_is_not_proved_to_return_a_mutex_operation(detector):
    source = (PREFIX + "getattr = fake_getattr\ndef put(key):\n    with _guard:\n"
              "        getattr(_guard, 'release')()\n        registry[key] = 1\n")
    assert classification(detector, source) == "candidate"


@pytest.mark.parametrize("yielded, expected", [("_guard", "finding"), ("fake_guard", "candidate")])
def test_generator_yielded_mutex_aliases_cannot_keep_the_synchronization_claim(detector, yielded, expected):
    source = (PREFIX + "def put(key):\n    with _guard:\n" +
              f"        for alias in ({yielded} for _ in (0,)):\n            alias.release()\n" +
              "        registry[key] = 1\n")
    assert classification(detector, source) == expected


@pytest.mark.parametrize("after, expected", [
    ("    with _guard:\n        store()\n", "candidate"),
    ("    store()\n", "finding"),
    ("    with _guard:\n        store()\n    return store\n", "finding"),
])
def test_only_nonescaping_nested_helpers_avoid_duplicate_entry_scans(detector, after, expected):
    source = PREFIX + "def put(key):\n    def store():\n        registry[key] = 1\n" + after
    assert classification(detector, source) == expected


@pytest.mark.parametrize("protected", [False, True])
def test_defaultdict_default_factory_is_a_direct_global_write(detector, protected):
    indent = "        " if protected else "    "
    source = ("from collections import defaultdict\nfrom threading import Lock\n"
              "_guard = Lock()\nregistry = defaultdict(list)\n"
              "def put(key):\n    with _guard:\n        registry[key] = []\n" +
              indent + "registry.default_factory = set\n")
    assert classification(detector, source) == ("candidate" if protected else "finding")


@pytest.mark.parametrize("iteration", ["for registry[key] in (1,):\n    pass",
                                        "[None for registry[key] in (1,)]",
                                        "list(None for registry[key] in (1,))"])
@pytest.mark.parametrize("protected", [False, True])
def test_iteration_target_subscripts_are_direct_global_writes(detector, iteration, protected):
    indent = "        " if protected else "    "
    source = PREFIX + "def put(key):\n    with _guard:\n        registry[key] = 1\n"
    source += "".join(indent + line + "\n" for line in iteration.splitlines())
    assert classification(detector, source) == ("candidate" if protected else "finding")


def test_async_iteration_target_subscripts_are_direct_global_writes(detector):
    source = (PREFIX + "async def put(key, values):\n    with _guard:\n        registry[key] = 1\n"
              "    async for registry[key] in values:\n        pass\n")
    assert classification(detector, source) == "finding"


@pytest.mark.parametrize("callback, expected", [("lambda _: _guard.release()", "finding"),
                                               ("lambda _: 0", "candidate")])
def test_list_sort_key_callbacks_execute_at_the_mutation_site(detector, callback, expected):
    source = ("from threading import Lock\n_guard = Lock()\nregistry = [1]\n"
              "def put(value):\n    with _guard:\n" +
              f"        registry.sort(key={callback})\n        registry.append(value)\n")
    assert classification(detector, source) == expected
