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


@pytest.mark.parametrize("factory", ["Lock", "RLock"])
@pytest.mark.parametrize("body", [
    "        await prepare()\n        registry[key] = 1\n",
    "        registry[key] = 1\n        await finish()\n",
    "        async for value in values:\n            registry[key] = value\n",
    "        registry[key] = [value async for value in values]\n",
    "        def read(value=await prepare()):\n            return value\n        registry[key] = 1\n",
])
def test_thread_locks_do_not_certify_suspended_task_mutations(detector, factory, body):
    found = mutations(detector, f"from threading import {factory}\n_guard = {factory}()\nregistry = {{}}\n"
                      "async def put(key):\n    with _guard:\n" + body)
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("contexts", ["_guard, _guard", "_guard:\n        with _guard"])
@pytest.mark.parametrize("factory, expected", [("Lock", "finding"), ("RLock", "candidate")])
def test_only_reentrant_locks_support_repeated_acquisition(detector, contexts, factory, expected):
    indent = "        " if "," in contexts else "            "
    found = mutations(detector, f"from threading import {factory}\n_guard = {factory}()\nregistry = {{}}\n"
                      f"def put(key):\n    with {contexts}:\n{indent}registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == expected


@pytest.mark.parametrize("replacement", [
    "class Replace:\n    threading.Lock = make_guard\n",
    "class Outer:\n    class Replace:\n        threading.Lock = make_guard\n",
])
def test_class_execution_can_replace_the_imported_factory(detector, replacement):
    found = mutations(detector, "import threading\n" + replacement +
                      "_guard = threading.Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("setup", [
    "from threading import Lock\nfrom app_guards import *\n_guard = Lock()\n",
    "import threading\nfrom app_guards import *\n_guard = threading.Lock()\n",
    "from threading import Lock\n_guard = Lock()\nfrom app_guards import *\n",
])
def test_wildcard_exports_leave_the_lock_identity_unproved(detector, setup):
    found = mutations(detector, setup + "registry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


def test_an_async_function_without_suspension_can_use_a_thread_lock(detector):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      "async def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"


def test_explicit_import_and_guard_after_a_wildcard_restore_known_bindings(detector):
    found = mutations(detector, "from app_guards import *\nfrom threading import Lock\n"
                      "_guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"


@pytest.mark.parametrize("factory, expected", [("Lock", "finding"), ("RLock", "candidate")])
@pytest.mark.parametrize("contexts", ["_a, _b", "_a:\n        with _b"])
def test_chained_constructor_targets_share_one_mutex_identity(detector, factory, expected, contexts):
    indent = "        " if "," in contexts else "            "
    found = mutations(detector, f"from threading import {factory}\n_a = _b = {factory}()\nregistry = {{}}\n"
                      f"def put(key):\n    with {contexts}:\n{indent}registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == expected


@pytest.mark.parametrize("setup", [
    "_guard = Lock()\n_alias = _guard\n",
    "_guard = _alias = Lock()\n",
    "_guard = Lock()\n_first = _guard\n_alias = _first\n",
])
def test_release_through_a_module_alias_invalidates_the_shared_guard(detector, setup):
    found = mutations(detector, "from threading import Lock\n" + setup + "registry = {}\n"
                      "def put(key):\n    with _guard:\n        _alias.release()\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("alias", ["threads = threading\n", "first = threading\nthreads = first\n"])
def test_factory_replacement_through_a_module_alias_invalidates_the_origin(detector, alias):
    found = mutations(detector, "import threading\n" + alias +
                      "threads.Lock = make_guard\n_guard = threading.Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


def test_different_names_for_the_same_guard_still_exclude_direct_writes(detector):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\n_alias = _guard\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n"
                      "def drop(key):\n    with _alias:\n        registry.pop(key)\n")
    assert len(found) == 1 and found[0].kind == "candidate"


@pytest.mark.parametrize("setup, context", [
    ("saved = guard\nfrom threading import Lock\nguard = Lock()\n", "saved"),
    ("threads = threading\nimport threading\nguard = threads.Lock()\n", "guard"),
    ("new_lock = Lock\nfrom threading import Lock\nguard = new_lock()\n", "guard"),
])
def test_aliases_keep_the_object_visible_when_the_assignment_runs(detector, setup, context):
    # The wildcard can supply each source name before its later explicit binding.
    # Capturing that earlier object does not capture the later proven lock.
    found = mutations(detector, "from app_guards import *\n" + setup + "registry = {}\n"
                      f"def put(key):\n    with {context}:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("setup, expected", [
    ("guard = Lock()\nfrom threading import Lock\n", "finding"),
    ("guard = threading.Lock()\nimport threading\n", "finding"),
    ("from threading import Lock; guard = Lock()\n", "candidate"),
])
def test_constructor_proof_requires_the_import_to_have_executed(detector, setup, expected):
    found = mutations(detector, "from app_guards import *\n" + setup + "registry = {}\n"
                      "def put(key):\n    with guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == expected


@pytest.mark.parametrize("setup", [
    "import threading\nfirst = threading\nthreads: object = first\nguard = threads.Lock()\n",
    "from threading import Lock\nfirst = Lock\nnew_lock: object = first\nguard = new_lock()\n",
])
def test_stable_module_and_factory_aliases_preserve_a_known_constructor(detector, setup):
    found = mutations(detector, setup + "registry = {}\n"
                      "def put(key):\n    with guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"


@pytest.mark.parametrize("assignment", ["threads = threading", "first = threading; threads = first"])
def test_local_module_aliases_cannot_hide_factory_replacement(detector, assignment):
    found = mutations(detector, "import threading\n"
                      f"def configure():\n    {assignment}\n    threads.Lock = make_guard\n"
                      "configure()\n_guard = threading.Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("assignment", ["alias = _guard", "first = _guard; alias = first"])
@pytest.mark.parametrize("release", ["release()", "__exit__(None, None, None)"])
def test_local_guard_aliases_cannot_hide_manual_release(detector, assignment, release):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      f"def put(key):\n    {assignment}\n    with _guard:\n        alias.{release}\n"
                      "        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("assignment", ["threading.Lock = make_guard", "threads = threading; threads.Lock = make_guard"])
def test_patched_constructor_imports_cannot_establish_a_mutex(detector, assignment):
    found = mutations(detector, "import threading\n" + assignment + "\nfrom threading import Lock\n"
                      "_guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("body", [
    "threading = fake_module\n    threading.Lock = make_guard",
    "_guard = fake_guard\n    _guard.release()",
])
def test_class_local_objects_do_not_interfere_with_module_mutexes(detector, body):
    found = mutations(detector, "import threading\n_guard = threading.Lock()\nregistry = {}\n"
                      f"class Other:\n    {body}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"


@pytest.mark.parametrize("setup", [
    "from app_guards import *; from threading import Lock; _guard = Lock()\n",
    "from app_guards import *; import threading; _guard = threading.Lock()\n",
])
def test_same_line_explicit_bindings_after_a_wildcard_restore_the_proof(detector, setup):
    found = mutations(detector, setup + "registry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"


@pytest.mark.parametrize("body, expected", [
    ("alias = fake_guard\n        alias.release()", "candidate"),
    ("alias = _guard\n        alias = fake_guard\n        alias.release()", "candidate"),
    ("if key:\n            alias = _guard\n        else:\n            alias = fake_guard\n        alias.release()", "finding"),
])
def test_local_aliases_follow_bindings_and_keep_uncertain_branches_conservative(detector, body, expected):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      f"def put(key):\n    with _guard:\n        {body}\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == expected


def test_nested_class_lookup_does_not_inherit_an_outer_class_local(detector):
    found = mutations(detector, "import threading\n_guard = threading.Lock()\nregistry = {}\n"
                      "class Other:\n    threading = fake_module\n    class Inner:\n        threading.Lock = make_guard\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("alias", ["", "new_lock = Lock\n"])
def test_imported_factory_keeps_its_captured_object_after_module_replacement(detector, alias):
    factory = "new_lock" if alias else "Lock"
    found = mutations(detector, "import threading\nfrom threading import Lock\n" + alias +
                      f"threading.Lock = make_guard\n_guard = {factory}()\nregistry = {{}}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"


def test_class_shadowing_cannot_hide_interference_before_the_assignment(detector):
    found = mutations(detector, "import threading\n_guard = threading.Lock()\nregistry = {}\n"
                      "class Other:\n    threading.Lock = make_guard\n    threading = fake_module\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("body", [
    "alias = _guard\n            fail()\n        except Exception:\n            alias.release()",
    "alias = _guard\n            fail()\n        finally:\n            alias.release()",
])
def test_exception_paths_keep_possible_guard_aliases(detector, body):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      f"def put(key):\n    with _guard:\n        try:\n            {body}\n"
                      "        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


def test_releasing_an_earlier_wildcard_object_does_not_release_a_later_guard(detector):
    found = mutations(detector, "from app_guards import *\nalias = guard\nalias.release()\nfrom threading import Lock\n"
                      "guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    with guard:\n        alias.release()\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"


@pytest.mark.parametrize("body, expected", [
    ("_guard = fake_guard\n        with _guard:\n            registry[key] = 1", "finding"),
    ("with _guard:\n            registry[key] = 1", "candidate"),
    ("with _guard:\n            registry[key] = 1\n        _guard = fake_guard", "candidate"),
    ("if key:\n            _guard = fake_guard\n        with _guard:\n            registry[key] = 1", "finding"),
])
def test_class_mutation_guards_use_their_ordered_namespace(detector, body, expected):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      f"def put(key):\n    class Other:\n        {body}\n")
    assert len(found) == 1 and found[0].kind == expected


def test_match_aliases_keep_every_possible_guard_origin(detector):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        match key:\n"
                      "            case 1:\n                alias = _guard\n"
                      "            case _:\n                alias = fake_guard\n"
                      "        alias.release()\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("loop", ["while ready", "for value in values"])
@pytest.mark.parametrize("after_break", ["", "\n            alias = fake_guard"])
def test_break_paths_preserve_guard_aliases_before_a_loop_else(detector, loop, after_break):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      f"def put(key):\n    with _guard:\n        {loop}:\n"
                      f"            alias = _guard\n            break{after_break}\n"
                      "        else:\n            alias = fake_guard\n"
                      "        alias.release()\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("expression, expected", [
    ("(lambda: _guard.release())()", "finding"),
    ("(lambda alias: alias.release())(_guard)", "finding"),
    ("(lambda alias=_guard: alias.release())()", "finding"),
    ("(lambda _guard: _guard.release())(fake_guard)", "candidate"),
    ("(lambda _guard=fake_guard: _guard.release())()", "candidate"),
    ("(lambda: _guard.release())", "candidate"),
])
def test_immediate_lambda_calls_use_their_own_parameter_scope(detector, expression, expected):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      f"def put(key):\n    with _guard:\n        {expression}\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == expected


@pytest.mark.parametrize("factory, expected", [("Lock", "finding"), ("RLock", "candidate")])
@pytest.mark.parametrize("contexts", ["_guard, alias", "_guard:\n        with alias"])
def test_local_alias_acquisition_uses_the_recorded_mutex_identity(detector, factory, expected, contexts):
    indent = "        " if "," in contexts else "            "
    found = mutations(detector, f"from threading import {factory}\n_guard = {factory}()\nregistry = {{}}\n"
                      f"def put(key):\n    alias = _guard\n    with {contexts}:\n{indent}registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == expected


@pytest.mark.parametrize("factory", ["Lock", "RLock"])
@pytest.mark.parametrize("replacement", [
    'setattr(threading, "{factory}", make_guard)',
    'threads = threading\nsetattr(threads, "{factory}", make_guard)',
    'def configure():\n    threads = threading\n    setattr(threads, "{factory}", make_guard)\nconfigure()',
])
def test_literal_setattr_replacements_cannot_establish_a_mutex(detector, factory, replacement):
    found = mutations(detector, "import threading\n" + replacement.format(factory=factory) +
                      f"\n_guard = threading.{factory}()\nregistry = {{}}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("setup", [
    'def other(setattr):\n    setattr(threading, "Lock", make_guard)\n',
    'setattr = lambda obj, name, value: None\nsetattr(threading, "Lock", make_guard)\n',
])
def test_shadowed_setattr_does_not_prove_a_builtin_replacement(detector, setup):
    found = mutations(detector, "import threading\n" + setup +
                      "_guard = threading.Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"


@pytest.mark.parametrize("loop", ["for index in range(2)", "while ready"])
def test_later_iterations_cannot_reuse_an_earlier_class_guard_binding(detector, loop):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      f"def put(key):\n    class Other:\n        {loop}:\n"
                      "            with _guard:\n                registry[key] = 1\n            _guard = fake_guard\n")
    assert len(found) == 1 and found[0].kind == "finding"


def test_each_for_iteration_rebinds_its_target_before_the_body(detector):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        for alias in fake_guards:\n"
                      "            alias.release()\n            alias = _guard\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"


@pytest.mark.parametrize("pattern", ["_ as alias", "object() as alias"])
def test_top_level_as_patterns_capture_the_whole_match_subject(detector, pattern):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      f"def put(key):\n    with _guard:\n        match _guard:\n            case {pattern}:\n"
                      "                alias.release()\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("loop, reset", [
    ("for index in range(2)", "            _guard = _other\n"),
    ("while (_guard := _other) and ready", ""),
])
def test_each_iteration_can_reestablish_a_known_class_guard(detector, loop, reset):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\n_other = Lock()\nregistry = {}\n"
                      f"def put(key):\n    class Other:\n        {loop}:\n" + reset +
                      "            with _guard:\n                registry[key] = 1\n            _guard = fake_guard\n")
    assert len(found) == 1 and found[0].kind == "candidate"


@pytest.mark.parametrize("definition", [
    "def inner(value=registry.pop(key)):\n    pass",
    "async def inner(value=registry.pop(key)):\n    pass",
    "@decorate(registry.pop(key))\ndef inner():\n    pass",
    "inner = lambda value=registry.pop(key): None",
])
@pytest.mark.parametrize("protected", [False, True])
def test_definition_inputs_execute_with_the_enclosing_guard_state(detector, definition, protected):
    indent = "        " if protected else "    "
    tail = "\n".join(indent + line for line in definition.splitlines())
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n" + tail + "\n")
    assert len(found) == 1 and found[0].kind == ("candidate" if protected else "finding")


@pytest.mark.parametrize("expression, expected", [
    ("_guard if key else _guard", "finding"),
    ("_guard if key else fake_guard", "finding"),
    ("fake_guard if key else other_fake", "candidate"),
])
def test_conditional_expression_aliases_keep_their_possible_origins(detector, expression, expected):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      f"def put(key):\n    alias = {expression}\n    with _guard:\n"
                      "        alias.release()\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == expected


@pytest.mark.parametrize("body, expected", [
    ("threading = fake_module\n    del threading\n    threading.Lock = make_guard", "finding"),
    ("_guard = fake_guard\n    del _guard\n    _guard.release()", "finding"),
])
def test_class_deletion_restores_outer_object_lookup(detector, body, expected):
    found = mutations(detector, "import threading\n_guard = threading.Lock()\nregistry = {}\n"
                      f"class Other:\n    {body}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == expected


@pytest.mark.parametrize("factory", ["Lock", "RLock"])
@pytest.mark.parametrize("arguments, blocking", [("", True), ("True", True), ("False", False), ("timeout=0", False)])
def test_only_blocking_plain_lock_reacquisition_defeats_the_guard(detector, factory, arguments, blocking):
    found = mutations(detector, f"from threading import {factory}\n_guard = {factory}()\nregistry = {{}}\n"
                      f"def put(key):\n    with _guard:\n        _guard.acquire({arguments})\n        registry[key] = 1\n")
    expected = "finding" if factory == "Lock" and blocking else "candidate"
    assert len(found) == 1 and found[0].kind == expected


@pytest.mark.parametrize("setup", [
    'from builtins import setattr as replace\nreplace(threading, "Lock", make_guard)',
    'import builtins\nbuiltins.setattr(threading, "Lock", make_guard)',
    'import builtins as core\nother = core\nother.setattr(threading, "Lock", make_guard)',
])
def test_explicit_builtin_origins_cannot_hide_factory_replacement(detector, setup):
    found = mutations(detector, "import threading\n" + setup + "\n_guard = threading.Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("constructor, expected", [
    ("Lock(1)", "finding"), ("Lock(flag=True)", "finding"),
    ("threading.Lock(1)", "finding"), ("threading.Lock(flag=True)", "finding"),
    ("Lock(*())", "candidate"), ("Lock(**{})", "candidate"),
    ("RLock(1)", "candidate"), ("RLock(flag=True)", "candidate"),
])
def test_lock_constructor_arguments_must_be_valid_for_the_factory(detector, constructor, expected):
    found = mutations(detector, "import threading\nfrom threading import Lock, RLock\n"
                      f"_guard = {constructor}\nregistry = {{}}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == expected


@pytest.mark.parametrize("expression, protected, expected", [
    ("(lambda: registry.pop(key))()", False, "finding"),
    ("(lambda: registry.pop(key))()", True, "candidate"),
    ("(lambda registry: registry.pop(key))(fake_registry)", False, "candidate"),
    ("(lambda: registry.pop(key))", False, "candidate"),
])
def test_immediate_lambda_mutations_use_invocation_scope_and_guard_state(detector, expression, protected, expected):
    indent = "        " if protected else "    "
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n" + indent + expression + "\n")
    assert len(found) == 1 and found[0].kind == expected


@pytest.mark.parametrize("expression, expected", [
    ("(lambda: (yield registry.pop(key)))()", "candidate"),
    ("(lambda: (registry.pop(key) for value in values))()", "candidate"),
    ("(lambda: (value for value in registry.pop(key)))()", "finding"),
])
def test_generator_creation_defers_its_body_but_evaluates_the_outer_iterable(detector, expression, expected):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n    " + expression + "\n")
    assert len(found) == 1 and found[0].kind == expected


@pytest.mark.parametrize("expression, expected", [
    ("(lambda alias: alias.acquire())(_guard)", "finding"),
    ("(lambda alias: alias.acquire())(fake_guard)", "candidate"),
    ("_guard.acquire(timeout=0.01)", "candidate"),
])
def test_acquisition_evidence_uses_receiver_origins_and_a_finite_timeout(detector, expression, expected):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      f"def put(key):\n    with _guard:\n        {expression}\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == expected


def test_deleting_a_class_local_guard_restores_the_module_guard_for_mutations(detector):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    class Other:\n        _guard = fake_guard\n        del _guard\n"
                      "        with _guard:\n            registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"


def test_eager_class_definition_inputs_do_not_charge_a_class_local_registry(detector):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n"
                      "    class Other:\n        registry = {}\n"
                      "        def inner(value=registry.pop(key, None)):\n            pass\n")
    assert len(found) == 1 and found[0].kind == "candidate"


def test_immediate_lambda_comprehension_targets_do_not_shadow_the_module_object(detector):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n"
                      "    (lambda: [registry.pop(key) for registry in fake_registries])()\n")
    assert len(found) == 1 and found[0].kind == "candidate"


def test_a_global_registry_rebound_inside_a_function_is_still_module_owned(detector):
    found = mutations(detector, "registry = {}\n"
                      "def put(key):\n    global registry\n    registry = {}\n    registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


@pytest.mark.parametrize("statement", [
    "(_guard if key else _guard).release()",
    'setattr(threading if key else threading, "Lock", make_guard)',
])
def test_calls_evaluate_conditional_receiver_and_argument_origins_first(detector, statement):
    found = mutations(detector, "import threading\n_guard = threading.Lock()\nregistry = {}\n"
                      f"def put(key):\n    with _guard:\n        {statement}\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "finding"


def test_a_conditional_context_can_prove_one_identical_mutex(detector):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    with (_guard if key else _guard):\n        registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"


def test_a_conditional_local_binding_never_becomes_a_module_registry(detector):
    found = mutations(detector, "from threading import Lock\n_guard = Lock()\nregistry = {}\n"
                      "def put(key):\n    with _guard:\n        registry[key] = 1\n"
                      "def local(key):\n    if key:\n        registry = {}\n    registry[key] = 1\n")
    assert len(found) == 1 and found[0].kind == "candidate"
