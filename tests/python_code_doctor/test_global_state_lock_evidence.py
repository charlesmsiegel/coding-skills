"""A synchronized registry is a lead; inconsistent locking still earns a finding.

All direct writes must hold the same stable module lock. Merely seeing a lock
at each write, or recognizing the constructor before its name is rebound, is
not evidence that concurrent calls exclude one another.
"""

import ast
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


@pytest.fixture
def detector(load_module):
    return load_module(SCRIPTS, "find_global_state")


def mutations(detector, source):
    return [r for r in detector.detect(ast.parse(source), "sample.py", source.splitlines(), set())
            if r.smell_type == "mutated_global"]


@pytest.mark.parametrize("setup", [
    "import threading\n_guard = threading.Lock()\n",
    "import threading as threads\n_guard = threads.RLock()\n",
    "from threading import RLock as new_lock\n_guard = new_lock()\n",
    "import threading\n_guard: object = threading.Lock()\n",
])
def test_a_registry_with_one_shared_stable_lock_is_an_unscored_lead(detector, setup):
    found = mutations(detector, setup +
                      "registry = {}\n"
                      "def get(key):\n"
                      "    with _guard:\n"
                      "        return registry.setdefault(key, object())\n"
                      "def drop(key):\n"
                      "    with _guard:\n"
                      "        registry.pop(key, None)\n")
    assert len(found) == 1
    assert found[0].kind == "candidate"
    assert not found[0].suggestion
    assert found[0].also_caused_by


def test_nested_locks_need_only_one_common_guard_for_every_write(detector):
    found = mutations(detector,
                      "from threading import Lock\n"
                      "_guard = Lock()\n_other = Lock()\nregistry = {}\n"
                      "def put(key):\n"
                      "    with _guard, _other:\n"
                      "        registry[key] = 1\n"
                      "def drop(key):\n"
                      "    with _guard:\n"
                      "        del registry[key]\n")
    assert len(found) == 1 and found[0].kind == "candidate"


def test_later_context_expressions_run_with_the_preceding_lock_held(detector):
    found = mutations(detector,
                      "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard, registry.pop(key):\n        pass\n")
    assert len(found) == 1 and found[0].kind == "candidate"


@pytest.mark.parametrize("source", [
    # One write escapes the registry's guard.
    "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
    "def put(key):\n    with _guard:\n        registry[key] = 1\n"
    "def drop(key):\n    registry.pop(key)\n",
    # Two unrelated locks do not exclude one another.
    "from threading import Lock\n_a = Lock()\n_b = Lock()\nregistry = {}\n"
    "def put(key):\n    with _a:\n        registry[key] = 1\n"
    "def drop(key):\n    with _b:\n        registry.pop(key)\n",
    # The lock itself changes, at module scope or from a function.
    "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
    "_guard = Lock()\n"
    "def put(key):\n    with _guard:\n        registry[key] = 1\n",
    "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
    "def reset():\n    global _guard\n    _guard = Lock()\n"
    "def put(key):\n    with _guard:\n        registry[key] = 1\n",
    # An overwritten import no longer proves a threading lock constructor.
    "from threading import Lock\nLock = make_guard\n_guard = Lock()\nregistry = {}\n"
    "def put(key):\n    with _guard:\n        registry[key] = 1\n",
    # A visible replacement of the constructor invalidates its imported module too.
    "import threading\nthreading.Lock = make_guard\n_guard = threading.Lock()\nregistry = {}\n"
    "def put(key):\n    with _guard:\n        registry[key] = 1\n",
    "import threading\n_guard = threading.Lock()\nregistry = {}\n"
    "def replace():\n    threading.Lock = make_guard\n"
    "def put(key):\n    with _guard:\n        registry[key] = 1\n",
    # A manual release inside the lexical with block defeats its protection.
    "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
    "def put(key):\n    with _guard:\n        _guard.release()\n        registry[key] = 1\n",
    # A definition-time expression can replace an otherwise stable lock name.
    "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
    "def configure(value=(_guard := make_guard())):\n    return value\n"
    "def put(key):\n    with _guard:\n        registry[key] = 1\n",
    # A nested function is not run while the lock surrounding its definition is held.
    "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
    "def build():\n    with _guard:\n        def put(key):\n            registry[key] = 1\n"
    "    return put\n",
    # Context expressions run before their own __enter__: this write precedes the lock.
    "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
    "def put(key):\n    with registry.pop(key), _guard:\n        pass\n",
])
def test_incomplete_or_inconsistent_lock_evidence_keeps_the_finding(detector, source):
    found = mutations(detector, source)
    assert len(found) == 1
    assert (found[0].kind, found[0].severity) == ("finding", "high")


def test_unrelated_local_and_class_bindings_do_not_rebind_the_module_lock(detector):
    found = mutations(detector,
                      "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      "class Other:\n    _guard = None\n"
                      "def other():\n    _guard = None\n    return _guard\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"


def test_local_shadowing_does_not_replace_the_imported_factory_or_release_the_guard(detector):
    found = mutations(detector,
                      "import threading\n_guard = threading.Lock()\nregistry = {}\n"
                      "def other(threading, _guard):\n"
                      "    threading.Lock = make_guard\n    _guard.release()\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"
