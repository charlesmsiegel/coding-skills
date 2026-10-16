"""False positives removed from the tests and hygiene detectors, each beside the case it still catches.

Every rule here narrows what a detector reports, so every test that shows a
benign shape going quiet sits next to one showing the defect it was written
for still firing. A narrowing that cannot show both is a suppression.
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "python-code-doctor" / "scripts"


def run_detector(script: str, target: Path, *extra: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json", *extra],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[:500]}"
    return json.loads(result.stdout)


def run_detector_text(script: str, target: Path) -> str:
    result = subprocess.run([sys.executable, str(SCRIPTS_DIR / script), str(target)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[:500]}"
    return result.stdout


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


def of_type(findings: list[dict], smell: str) -> list[dict]:
    return [f for f in findings if f["smell_type"] == smell]


def lines_of(findings: list[dict], smell: str) -> list[int]:
    return sorted(f["line"] for f in of_type(findings, smell))


# --------------------------------------------------------------------------- #
# reasoned_noqa(): a suppression somebody explained
# --------------------------------------------------------------------------- #


def test_a_reasoned_noqa_is_read_on_the_line_or_the_comment_line_below(load_module):
    noqa = load_module(SCRIPTS_DIR, "noqa")
    lines = [
        "x = 1  # noqa: A001 -- the protocol names it",   # 1: reasoned
        "y = 2  # noqa: A001",                            # 2: bare
        "z = 3  # noqa: A001 --",                         # 3: separator, no reason
        "w = 4  # noqa: E501, A002: wire name",           # 4: list, colon separator
        "def f(",                                         # 5: wrapped
        "    # " + "noqa: A002 \u2014 the field is called id",  # 6: comment-only line below 5
        "v = 5",                                          # 7
        "u = 6  # noqa: A001 - fixed by the HTTP API",    # 8: code line below 7
    ]
    assert noqa.reasoned_noqa(lines, 1, ["A001"])
    assert not noqa.reasoned_noqa(lines, 1, ["A002"]), "the code must be the one asked about"
    assert not noqa.reasoned_noqa(lines, 2, ["A001"]), "a bare noqa records no decision"
    assert not noqa.reasoned_noqa(lines, 3, ["A001"]), "a separator alone is not a reason"
    assert noqa.reasoned_noqa(lines, 4, ["A002"])
    assert noqa.reasoned_noqa(lines, 5, ["A002"])
    assert not noqa.reasoned_noqa(lines, 7, ["A001"]), "a code line below is another statement"


# --------------------------------------------------------------------------- #
# shadows_builtin
# --------------------------------------------------------------------------- #


def test_file_is_not_a_python3_builtin_but_id_is(tmp_path):
    write(tmp_path, {"m.py": "def upload(file: bytes) -> None:\n    print(file)\n\n"
                             "def fetch(id: str) -> str:\n    return id\n"})
    shadows = of_type(run_detector("find_naming_issues.py", tmp_path), "shadows_builtin")
    assert [f["line"] for f in shadows] == [4]


def test_a_method_named_after_a_builtin_hides_it_only_from_its_own_class_body(tmp_path):
    write(tmp_path, {
        "quiet.py": (
            "class Store:\n"
            "    def set(self, value):\n"
            "        self.v = value\n"
            "    def format(self):\n"
            "        return str(self.v)\n"
            "    type = 'store'\n"
        ),
        # The rest of the class body now calls the method, not the builtin.
        "loud.py": (
            "class Registry:\n"
            "    def set(self, value):\n"
            "        self.v = value\n"
            "    seen = set()\n"
        ),
    })
    shadows = of_type(run_detector("find_naming_issues.py", tmp_path), "shadows_builtin")
    assert [(Path(f["file"]).name, f["line"]) for f in shadows] == [("loud.py", 2)]


def test_a_module_level_rebinding_is_still_a_shadow(tmp_path):
    write(tmp_path, {"m.py": "list = []\n\nclass Box:\n    def put(self, input):\n        return input\n"})
    shadows = of_type(run_detector("find_naming_issues.py", tmp_path), "shadows_builtin")
    assert lines_of(shadows, "shadows_builtin") == [1, 4]


def test_a_reasoned_noqa_for_the_shadow_makes_a_candidate(tmp_path):
    write(tmp_path, {"m.py": (
        "def get(id: str) -> str:  # noqa: A002 -- the HTTP path parameter is named id\n"
        "    return id\n"
        "def put(id: str) -> str:  # noqa: A002\n"
        "    return id\n"
        "def post(id: str) -> str:  # noqa: A001 -- wrong code for an argument\n"
        "    return id\n"
        "def patch(\n"
        "    id: str,  # noqa: A002 -- wrapped signature, reason on the argument's line\n"
        ") -> str:\n"
        "    return id\n"
    )})
    shadows = of_type(run_detector("find_naming_issues.py", tmp_path), "shadows_builtin")
    by_line = {f["line"]: f for f in shadows}
    assert sorted(by_line) == [1, 3, 5, 8]
    for line in (1, 8):
        assert by_line[line]["kind"] == "candidate"
        assert by_line[line]["suggestion"] == "" and by_line[line]["also_caused_by"]
    for line in (3, 5):
        assert by_line[line]["kind"] == "finding" and by_line[line]["suggestion"]


def test_a_naming_candidate_prints_no_fix_arrow(tmp_path):
    write(tmp_path, {"m.py": "def get(id: str) -> str:  # noqa: A002 -- the path parameter\n"
                             "    return id\n"})
    text = run_detector_text("find_naming_issues.py", tmp_path)
    assert "[CANDIDATE]" in text and "also caused by" in text
    assert not [line for line in text.splitlines() if line.strip() == "→"]


# --------------------------------------------------------------------------- #
# os_path_join
# --------------------------------------------------------------------------- #


def test_nested_os_path_calls_are_one_finding_per_line(tmp_path):
    write(tmp_path, {"m.py": (
        "import os\n"
        "HERE = os.path.join(os.path.dirname(__file__), 'data')\n"
        "def f(p):\n"
        "    return os.path.exists(p)\n"
    )})
    assert lines_of(run_detector("find_outdated_idioms.py", tmp_path), "os_path_join") == [2, 4]


# --------------------------------------------------------------------------- #
# public_*_no_docstring
# --------------------------------------------------------------------------- #

BODY = "    a = 1\n    b = a + 1\n    return b\n"


def test_a_bare_exception_class_needs_no_docstring_but_other_classes_do(tmp_path):
    write(tmp_path, {"m.py": (
        '"""Module."""\n'
        "class StoreError(Exception):\n    pass\n"
        "class NotFound(StoreError):\n    ...\n"
        "class Shape:\n    pass\n"
        "class ParseError(ValueError):\n    def __init__(self, at):\n        self.at = at\n"
    )})
    classes = of_type(run_detector("find_missing_docstrings.py", tmp_path), "public_class_no_docstring")
    assert sorted(f["description"].split("'")[1] for f in classes) == ["ParseError", "Shape"]


def test_a_private_module_has_no_public_surface_but_init_does(tmp_path):
    for name in ("_impl.py", "impl.py", "__init__.py"):
        write(tmp_path, {name: '"""Module."""\n' f"def visible():\n{BODY}"
                               "class Thing:\n    pass\n"})
    found = run_detector("find_missing_docstrings.py", tmp_path)
    reported = {(Path(f["file"]).name, f["smell_type"]) for f in found}
    assert reported == {
        ("impl.py", "public_function_no_docstring"), ("impl.py", "public_class_no_docstring"),
        ("__init__.py", "public_function_no_docstring"), ("__init__.py", "public_class_no_docstring"),
    }


# --------------------------------------------------------------------------- #
# missing_return_annotation / missing_param_annotation
# --------------------------------------------------------------------------- #

GAPS = ("missing_return_annotation", "missing_param_annotation")


def gaps(findings: list[dict]) -> list[tuple[int, str]]:
    return sorted((f["line"], f["smell_type"]) for f in findings if f["smell_type"] in GAPS)


def test_closures_and_methods_of_private_or_local_classes_are_not_api_surface(tmp_path):
    write(tmp_path, {"m.py": (
        "def outer(x: int) -> int:\n"            # 1
        "    def helper(y):\n"                  # 2  closure
        "        return y + 1\n"
        "    class Local:\n"
        "        def run(self, z):\n"           # 5  method of a class inside a function
        "            return z\n"
        "    return helper(x)\n"
        "class _Private:\n"
        "    def run(self, z):\n"               # 9  method of a private class
        "        return z\n"
        "    class Inner:\n"
        "        def go(self, q):\n"            # 12 nested in a private class
        "            return q\n"
        "class Public:\n"
        "    def run(self, z):\n"               # 15 public method: still reported
        "        return z\n"
        "def plain(a):\n"                       # 17 module function: still reported
        "    return a\n"
    )})
    assert [line for line, _ in gaps(run_detector("find_type_gaps.py", tmp_path))] == [15, 17]


def test_route_handlers_are_exempt_and_the_same_function_undecorated_is_not(tmp_path):
    write(tmp_path, {"m.py": (
        "from fastapi import APIRouter\n"
        "from flask import Flask\n"
        "router = APIRouter()\n"
        "app = Flask(__name__)\n"
        "@router.get('/things/{tid}')\n"
        "def get_thing(tid: str):\n"           # 6
        "    return {'id': tid}\n"
        "@router.post('/things', response_model=dict)\n"
        "async def make_thing(body):\n"        # 9
        "    return body\n"
        "@app.route('/index')\n"
        "def index():\n"                       # 12
        "    return 'hi'\n"
        "def get_other(tid: str):\n"           # 14 not registered: still reported
        "    return {'id': tid}\n"
    )})
    assert gaps(run_detector("find_type_gaps.py", tmp_path)) == [(14, "missing_return_annotation")]


def test_a_bare_signature_is_one_finding_that_names_both_gaps(tmp_path):
    write(tmp_path, {"m.py": (
        "def both(a, b):\n    return a\n"
        "def params_only(a) -> int:\n    return a\n"
        "def return_only(a: int):\n    return a\n"
    )})
    found = run_detector("find_type_gaps.py", tmp_path)
    assert gaps(found) == [(1, "missing_return_annotation"), (3, "missing_param_annotation"),
                           (5, "missing_return_annotation")]
    both = next(f for f in found if f["line"] == 1)
    assert "a, b" in both["description"] and "a, b" in both["suggestion"]


def test_ignoring_the_return_gap_still_reports_the_parameters(tmp_path):
    write(tmp_path, {"m.py": "def both(a, b):\n    return a\n"})
    found = run_detector("find_type_gaps.py", tmp_path, "--ignore", "missing_return_annotation")
    assert gaps(found) == [(1, "missing_param_annotation")]


# --------------------------------------------------------------------------- #
# find_test_smells: logic_in_test
# --------------------------------------------------------------------------- #


def logic_tests(tmp_path: Path, source: str) -> set[str]:
    """The names of the tests logic_in_test reports in `source`."""
    write(tmp_path, {"test_sample.py": source})
    return {f["description"].split("'")[1]
            for f in of_type(run_detector("find_test_smells.py", tmp_path), "logic_in_test")}


def test_a_loop_over_a_literal_table_is_not_logic_but_a_computed_one_is(tmp_path):
    flagged = logic_tests(tmp_path, (
        "import itertools, pytest\n"
        "CASES = (('a', 1), ('b', 2))\n"
        "def test_tuple():\n"
        "    for x in (1, 2, 3):\n"
        "        assert x > 0\n"
        "def test_range_arrange():\n"
        "    xs = []\n"
        "    for n in range(10):\n"
        "        xs.append(n)\n"
        "    assert len(xs) == 10\n"
        "def test_module_table():\n"
        "    for name, n in CASES:\n"
        "        assert name and n\n"
        "def test_local_dict_items():\n"
        "    cases = {'a': 1, 'b': 2}\n"
        "    for k, v in cases.items():\n"
        "        assert k and v\n"
        "def test_zip_enumerate_product():\n"
        "    for i, (a, b) in enumerate(zip('ab', [1, 2])):\n"
        "        for p in itertools.product((1, 2), repeat=2):\n"
        "            assert a and b and p\n"
        # Still logic: each of these can run an iteration short, or none at all.
        "def test_computed(client):\n"
        "    for item in client.get('/x').json():\n"
        "        assert item['id']\n"
        "def test_empty_range():\n"
        "    for n in range(0):\n"
        "        assert n\n"
        "def test_break_skips():\n"
        "    for x in (1, 2, 3):\n"
        "        if x == 2:\n"
        "            break\n"
        "def test_continue_skips():\n"
        "    for x in (1, 2, 3):\n"
        "        continue\n"
        "def test_rebound_name():\n"
        "    cases = [1, 2]\n"
        "    cases = load()\n"
        "    for c in cases:\n"
        "        assert c\n"
        "def test_shrunk_name():\n"
        "    cases = [1, 2]\n"
        "    cases.clear()\n"
        "    for c in cases:\n"
        "        assert c\n"
        "def test_starred_only():\n"
        "    for c in [*load()]:\n"
        "        assert c\n"
        "def test_iterator_through_a_name():\n"
        "    pairs = zip((1, 2), (3, 4))\n"
        "    for a, b in pairs:\n"
        "        assert a < b\n"
        "def test_nested_if_in_table(k):\n"
        "    for x in (1, 2):\n"
        "        if k:\n"
        "            assert x\n"
    ))
    assert flagged == {"test_computed", "test_empty_range", "test_break_skips", "test_continue_skips",
                       "test_rebound_name", "test_shrunk_name", "test_starred_only",
                       "test_iterator_through_a_name", "test_nested_if_in_table"}


def test_a_skip_or_fail_guard_is_not_logic_but_a_branch_is(tmp_path):
    flagged = logic_tests(tmp_path, (
        "import shutil, pytest\n"
        "def test_env_guard():\n"
        "    if not shutil.which('git'):\n"
        "        pytest.skip('no git')\n"
        "    assert run()\n"
        "def test_fail_guard(actual, expected):\n"
        "    if actual != expected:\n"
        "        msg = report(actual, expected)\n"
        "        pytest.fail(msg)\n"
        "def test_raise_guard(x):\n"
        "    if x is None:\n"
        "        raise AssertionError('missing')\n"
        "def test_xfail_guard(platform):\n"
        "    if platform == 'win':\n"
        "        pytest.xfail('not on windows')\n"
        "    assert run()\n"
        # Still logic: a branch whose arms check different things.
        "@pytest.mark.parametrize('k', ['a', 'b'])\n"
        "def test_branch_on_param(k):\n"
        "    if k == 'a':\n"
        "        assert g() == 1\n"
        "def test_guard_with_else(x):\n"
        "    if x:\n"
        "        pytest.skip('x')\n"
        "    else:\n"
        "        assert y()\n"
        "def test_guard_that_loops(xs):\n"
        "    if xs:\n"
        "        for x in xs:\n"
        "            check(x)\n"
        "        pytest.fail('xs')\n"
        "def test_early_return(x):\n"
        "    if x:\n"
        "        return\n"
        "    assert y()\n"
        "def test_polling():\n"
        "    while not done():\n"
        "        tick()\n"
        "    assert done()\n"
    ))
    assert flagged == {"test_branch_on_param", "test_guard_with_else", "test_guard_that_loops",
                       "test_early_return", "test_polling"}


def test_logic_in_an_except_clause_or_an_async_for_is_logic(tmp_path):
    flagged = logic_tests(tmp_path, (
        "async def test_async_for(stream):\n"
        "    async for chunk in stream:\n"
        "        assert chunk\n"
        "def test_branch_in_except():\n"
        "    try:\n"
        "        run()\n"
        "    except ValueError as exc:\n"
        "        if 'x' in str(exc):\n"
        "            assert False\n"
    ))
    assert flagged == {"test_async_for", "test_branch_in_except"}


# --------------------------------------------------------------------------- #
# find_test_smells: test_without_assertion
# --------------------------------------------------------------------------- #


def assertion_free(tmp_path: Path, source: str) -> dict[str, str]:
    """test name -> severity, for every test reported as asserting nothing."""
    write(tmp_path, {"test_sample.py": source})
    return {f["description"].split("'")[1]: f["severity"]
            for f in of_type(run_detector("find_test_smells.py", tmp_path), "test_without_assertion")}


def test_assertions_the_test_reaches_in_one_step_count(tmp_path):
    reported = assertion_free(tmp_path, (
        "import asyncio, pytest, unittest\n"
        "def _assert_same(a, b):\n"
        "    assert a == b\n"
        "def bad_tree_case(tree, word):\n"
        "    with pytest.raises(ValueError, match=word):\n"
        "        parse(tree)\n"
        "def _first_matching(lines, pattern):\n"
        "    for line in lines:\n"
        "        if pattern in line:\n"
        "            return line\n"
        "    raise AssertionError(pattern)\n"
        "async def assert_async(x):\n"
        "    assert x\n"
        "def test_underscore_helper():\n"
        "    _assert_same(1, 1)\n"
        "def test_module_helper():\n"
        "    bad_tree_case({}, 'object')\n"
        "def test_helper_raising_assertion_error():\n"
        "    _first_matching(['a'], 'a')\n"
        "async def test_awaited_helper():\n"
        "    await assert_async(1)\n"
        "def test_assert_in_except():\n"
        "    try:\n"
        "        int('x')\n"
        "    except ValueError as e:\n"
        "        assert 'invalid' in str(e)\n"
        "def test_pytest_fail_in_if():\n"
        "    if run() != 2:\n"
        "        pytest.fail('broke')\n"
        "def test_raise_assertion_error_in_else():\n"
        "    try:\n"
        "        run()\n"
        "    except ValueError:\n"
        "        pass\n"
        "    else:\n"
        "        raise AssertionError('expected ValueError')\n"
        "def test_nested_called():\n"
        "    def check():\n"
        "        assert run()\n"
        "    check()\n"
        "def test_nested_run_by_asyncio():\n"
        "    async def main():\n"
        "        assert await go()\n"
        "    asyncio.run(main())\n"
        "def test_lambda_tripwire(monkeypatch):\n"
        "    monkeypatch.setattr(mod, 'write', lambda *a: pytest.fail('wrote'))\n"
        "    mod.repoint()\n"
        "def test_nested_def_tripwire(monkeypatch):\n"
        "    def read(*a):\n"
        "        raise AssertionError('read')\n"
        "    monkeypatch.setattr(mod, 'read', read)\n"
        "    mod.compose()\n"
        "def test_nested_class_tripwire():\n"
        "    class Manager:\n"
        "        def register(self, *a):\n"
        "            raise AssertionError('registered')\n"
        "    plugin.register(Manager())\n"
        "def test_fluent_expect(page):\n"
        "    expect(page).to_have_title('x')\n"
        "class TestReply(unittest.TestCase):\n"
        "    def check_reply(self, r):\n"
        "        self.assertEqual(r, 1)\n"
        "    def test_self_helper(self):\n"
        "        self.check_reply(run())\n"
    ))
    assert reported == {}


def test_a_test_that_reaches_no_assertion_is_still_reported(tmp_path):
    reported = assertion_free(tmp_path, (
        "import pytest\n"
        "def make_cart():\n"
        "    return []\n"
        "def checkout(cart):\n"
        "    return cart\n"
        "@pytest.fixture\n"
        "def client():\n"
        "    c = build()\n"
        "    assert c\n"
        "    return c\n"
        "def test_helper_that_asserts_nothing():\n"
        "    make_cart()\n"
        "def test_checkout_is_not_a_check():\n"
        "    checkout(make_cart())\n"
        "def test_a_fixture_is_not_a_helper(client):\n"
        "    client.get('/')\n"
        "def test_a_validator_check_is_the_code_under_test():\n"
        "    schemas.check({})\n"
        "def test_nested_helper_never_used():\n"
        "    def check():\n"
        "        assert False\n"
        "    run()\n"
    ))
    assert set(reported) == {"test_helper_that_asserts_nothing", "test_checkout_is_not_a_check",
                             "test_a_fixture_is_not_a_helper",
                             "test_a_validator_check_is_the_code_under_test",
                             "test_nested_helper_never_used"}


def test_a_test_that_runs_code_is_low_and_one_that_runs_nothing_is_high(tmp_path):
    reported = assertion_free(tmp_path, (
        "def test_placeholder():\n"
        "    pass\n"
        "def test_only_assigns():\n"
        "    '''Docstring.'''\n"
        "    x = 1\n"
        "def test_must_not_raise():\n"
        "    migrate()  # an empty store must not raise\n"
        "def test_context_manager(lock):\n"
        "    with lock:\n"
        "        pass\n"
    ))
    assert reported == {"test_placeholder": "high", "test_only_assigns": "high",
                        "test_must_not_raise": "low", "test_context_manager": "low"}


def test_the_low_finding_says_what_the_test_does_prove(tmp_path):
    write(tmp_path, {"test_sample.py": "def test_x():\n    run()\n"})
    [finding] = of_type(run_detector("find_test_smells.py", tmp_path), "test_without_assertion")
    assert "does not raise" in finding["description"] and finding["suggestion"]


# --------------------------------------------------------------------------- #
# find_test_smells: trivial_assertion
# --------------------------------------------------------------------------- #


def trivial(tmp_path: Path, source: str) -> set[str]:
    write(tmp_path, {"test_sample.py": source})
    return {f["description"].split("'")[1]
            for f in of_type(run_detector("find_test_smells.py", tmp_path), "trivial_assertion")}


def test_determinism_and_not_none_checks_can_fail_so_are_not_trivial(tmp_path):
    flagged = trivial(tmp_path, (
        "import unittest\n"
        "def _check(x):\n"
        "    assert x == 3\n"
        "def test_determinism():\n"
        "    assert build(cid) == build(cid)\n"
        "def test_awaited_determinism():\n"
        "    assert nominate(h) == nominate(h)\n"
        "def test_optional_present():\n"
        "    assert lookup('a') is not None\n"
        "def test_optional_absent():\n"
        "    assert lookup('z') is None\n"
        "def test_true_beside_a_real_helper():\n"
        "    assert True\n"
        "    _check(run())\n"
        "class TestT(unittest.TestCase):\n"
        "    def test_is_not_none(self):\n"
        "        self.assertIsNotNone(read())\n"
        "    def test_equal_calls(self):\n"
        "        self.assertEqual(make(), make())\n"
        # Still trivial: these cannot fail.
        "def test_true():\n"
        "    assert True\n"
        "def test_same_name(x):\n"
        "    assert x == x\n"
        "def test_literal(x):\n"
        "    assert 1 == 1\n"
        "class TestU(unittest.TestCase):\n"
        "    def test_equal_names(self):\n"
        "        y = 2\n"
        "        self.assertEqual(y, y)\n"
        "    def test_true_constant(self):\n"
        "        self.assertTrue(True)\n"
    ))
    assert flagged == {"test_true", "test_same_name", "test_literal", "test_equal_names",
                       "test_true_constant"}
