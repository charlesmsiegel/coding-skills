#!/usr/bin/env python3
"""
Detect test-quality smells in Python test files via AST analysis.

Only analyses files whose name starts with "test_", ends with "_test.py",
or whose path contains a "tests" or "test" directory segment.

Finds:
  - test_without_assertion : test function with no assertion of any kind — HIGH
                             when it runs nothing that could fail, LOW when it
                             calls code and so checks, weakly, that it does not raise
  - overmocking            : test function with more than 4 mock/patch constructs
  - skipped_without_reason : @skip / @pytest.mark.skip with no reason/message arg
  - logic_in_test          : test function body contains if/while control flow, or a
                             for loop that may not run every iteration
  - trivial_assertion      : every assertion in the test is one that cannot fail

An assertion is an `assert`, an assert-style call (`self.assertEqual`,
`mock.assert_called_once`, a helper named `assert_*`/`check_*`/`verify_*`/
`expect`, leading underscores ignored, awaited or not), `pytest.raises`/`warns`,
`pytest.fail`, or `raise AssertionError`, in any branch including `except`
clauses. A test also asserts when a nested function or class it uses, a lambda,
or a helper defined in the same module (or its own class) does — one level deep.

Not logic: a `for` over a non-empty literal table (or `range(3)`, a dict's
`.items()`, `zip`/`enumerate`/`product` of such, or a name bound once to one),
which runs every case every time; and `if <precondition>: pytest.skip(...)` or
`pytest.fail(...)`, which ends the test loudly rather than passing it.
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import (is_test_file, sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files,
                    warn_detector_error, warn_unparseable)


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""


def _is_test_file(filepath: Path) -> bool:
    return is_test_file(filepath)


def _get_line(lines: list[str], lineno: int) -> str:
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


# ---------------------------------------------------------------------------
# Assertion detection helpers
# ---------------------------------------------------------------------------

# A helper whose name says it checks something is an assertion at the call
# site: `_assert_same(a, b)`, `check_response(r)`, `verify(page)`,
# `expect(locator).to_be_visible()`. The leading underscore of a module-private
# helper does not change what it is for.
_ASSERTION_PREFIXES = ("assert", "expect", "check", "verify")


def _assertion_named(name: str) -> bool:
    """True for `assert_x`, `_check`, `verifyAll`, `expect` — not `checkout` or `expected`.

    The prefix must be the whole name or end at a word boundary (an underscore
    or a camelCase capital), so a word that merely starts with the letters is
    not taken for a check.
    """
    bare = name.lstrip("_")
    for prefix in _ASSERTION_PREFIXES:
        if bare[:len(prefix)].lower() != prefix:
            continue
        rest = bare[len(prefix):]
        if not rest or rest[0] == "_" or rest[0].isupper():
            return True
    return False


def _is_pytest_attr(func: ast.expr, names: tuple[str, ...]) -> bool:
    return (isinstance(func, ast.Attribute) and func.attr in names
            and isinstance(func.value, ast.Name) and func.value.id == "pytest")


def _is_assert_call(node: ast.expr) -> bool:
    """Return True if node is a call to an assert-style method/function.

    An awaited call is judged by what it awaits: `await assert_reply(...)` is
    the async spelling of the same check.
    """
    if isinstance(node, ast.Await):
        node = node.value
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        attr = func.attr
        # self.assertXxx(...), self._assert_xxx(...), mock.assert_called_once_with(...),
        # np.testing.assert_array_equal(...): any receiver, the name says assert.
        if attr.lstrip("_").startswith(("assert", "Assert")):
            return True
        # A test class's own helpers — self.check_reply(), self._verify() — and
        # unittest's self.fail(). Other receivers are the code under test: a
        # validator's .check() is what is being exercised, not an assertion.
        if isinstance(func.value, ast.Name) and func.value.id in ("self", "cls"):
            if attr == "fail" or _assertion_named(attr):
                return True
        # pytest.raises / pytest.warns used as plain call (not `with`), and
        # pytest.fail(), which is an assertion that has already decided.
        # Note: pytest.approx() on its own cannot fail a test — do NOT count it
        # as an assertion.  It only has meaning inside a real assert statement.
        if _is_pytest_attr(func, ("raises", "warns", "fail")):
            return True
        # expect(x).to_equal(y), assert_that(x).is_true(): a fluent check is
        # judged by the call that opens the chain.
        if isinstance(func.value, (ast.Call, ast.Await)):
            return _is_assert_call(func.value)
        return False
    if isinstance(func, ast.Name):
        return _assertion_named(func.id) or func.id in ("raises", "warns")
    return False


def _is_assertion_raise(node: ast.stmt) -> bool:
    """`raise AssertionError(...)` — an assert statement that chose its own message."""
    if not isinstance(node, ast.Raise) or node.exc is None:
        return False
    exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    return isinstance(exc, ast.Name) and exc.id == "AssertionError"


def _is_pytest_raises_with(node: ast.stmt) -> bool:
    """Return True for `with pytest.raises(...)` / `pytest.warns` / `self.assertRaises(...)`."""
    if not isinstance(node, (ast.With, ast.AsyncWith)):
        return False
    return any(isinstance(item.context_expr, ast.Call) and _is_assert_call(item.context_expr)
               for item in node.items)


def _child_stmts(stmt: ast.stmt):
    """The statements nested directly in a compound statement, every branch of it.

    `ast.iter_child_nodes` alone misses two: an `except` clause and a `case`
    clause are not statements themselves, so a walk that keeps only `ast.stmt`
    children never enters their bodies.
    """
    for child in ast.iter_child_nodes(stmt):
        if isinstance(child, ast.stmt):
            yield child
        elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
            yield from child.body


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _scope_nodes(body: list[ast.stmt]):
    """Every node in one scope's statements: nested defs, classes and lambdas yielded, not entered."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPES):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _local_bindings(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Parameters and every name the function binds in its own scope."""
    args = func_node.args
    bound = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg) if a}
    for node in _scope_nodes(func_node.body):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
    return bound


def _is_fixture(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """`@pytest.fixture` / `@fixture(...)`: pytest calls it, the test never does."""
    for dec in func_node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if (isinstance(target, ast.Attribute) and target.attr == "fixture") or \
                (isinstance(target, ast.Name) and target.id == "fixture"):
            return True
    return False


def _reaches_assertion(func_node: ast.FunctionDef | ast.AsyncFunctionDef,
                       module_helpers: dict, class_helpers: dict) -> bool:
    """True when the test asserts through code it defines or calls, one level deep.

    A test is not assertion-free because its `assert` sits one call away:
      - a nested def or class the test uses (calls it, hands it to
        `asyncio.run`, installs it with `monkeypatch.setattr`) whose own body
        asserts — the last two are how a tripwire is written;
      - a lambda whose body is a check, e.g. `lambda *a: pytest.fail("called")`;
      - a function defined in the same module, or a method of the test's own
        class reached as `self.name`, whose body asserts.
    One level only: the helper's own assertions count, not its helpers'.
    """
    own = list(_scope_nodes(func_node.body))
    parents = {id(child): parent for parent in ast.walk(func_node)
               for child in ast.iter_child_nodes(parent)}

    # Nested definitions, checked only when the rest of the test refers to them.
    loads: dict[str, int] = defaultdict(int)
    for node in ast.walk(func_node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loads[node.id] += 1
    for node in own:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            inside = sum(1 for n in ast.walk(node) if isinstance(n, ast.Name)
                         and isinstance(n.ctx, ast.Load) and n.id == node.name)
            if loads[node.name] - inside <= 0:
                continue
            bodies = [node] if not isinstance(node, ast.ClassDef) else [
                item for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if any(_collect_assertions(body) for body in bodies):
                return True
        elif isinstance(node, ast.Lambda):
            holder = parents.get(id(node))
            while holder is not None and not isinstance(holder, (ast.Assign, ast.AnnAssign, ast.Call)):
                holder = parents.get(id(holder))
            if isinstance(holder, (ast.Assign, ast.AnnAssign)):
                targets = holder.targets if isinstance(holder, ast.Assign) else [holder.target]
                names = {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)}
                inside = defaultdict(int)
                for inner in ast.walk(node.body):
                    if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
                        inside[inner.id] += 1
                if not any(loads[name] > inside[name] for name in names):
                    continue
            elif not isinstance(holder, ast.Call):
                continue
            if any(isinstance(n, ast.Call) and _is_assert_call(n) for n in ast.walk(node.body)):
                return True

    # Helpers defined beside the test. A name the test binds itself — a
    # parameter, which is how a fixture arrives, or a local — is not the
    # module-level function of the same name.
    local = _local_bindings(func_node)
    for node in ast.walk(func_node):
        target = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in local:
            target = module_helpers.get(node.id)
        elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
              and node.value.id == "self"):
            target = class_helpers.get(node.attr)
        if target is not None and target is not func_node and _collect_assertions(target):
            return True
    return False


# ---------------------------------------------------------------------------
# Mock/patch counting
# ---------------------------------------------------------------------------

_MOCK_CALL_NAMES = {"patch", "MagicMock", "Mock", "AsyncMock", "MagicMock", "patch_object"}
_MOCK_ATTRS = {"patch", "patch_object"}  # mock.patch / patch.object


def _count_mock_constructs(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """
    Count total mock/patch constructs in function body + its decorators.

    Counts:
    - @patch decorators on the function itself
    - Calls to mock.patch / patch / patch.object / MagicMock / Mock / AsyncMock
      anywhere in the body
    """
    count = 0

    # Decorators
    for dec in func_node.decorator_list:
        # @patch(...)  /  @mock.patch(...)  /  @patch.object(...)
        call = dec
        if isinstance(dec, ast.Call):
            call = dec.func
        if isinstance(call, ast.Attribute):
            if call.attr == "patch" or (hasattr(call, 'attr') and call.attr in ("patch", "object")):
                count += 1
                continue
            # mock.patch.object etc.
            if isinstance(call.value, ast.Attribute) and call.value.attr == "patch":
                count += 1
                continue
        if isinstance(call, ast.Name) and call.id == "patch":
            count += 1
            continue

    # Body: walk body statements explicitly to avoid double-counting
    # decorator nodes that ast.walk(func_node) would also visit.
    body_nodes = []
    for stmt in func_node.body:
        for node in ast.walk(stmt):
            body_nodes.append(node)

    for node in body_nodes:
        if isinstance(node, ast.Call):
            func = node.func
            # patch(...)  ->  Name("patch")
            if isinstance(func, ast.Name) and func.id in {"patch", "MagicMock", "Mock", "AsyncMock"}:
                count += 1
            # mock.patch(...)  ->  Attribute(Name("mock"), "patch")
            elif isinstance(func, ast.Attribute):
                if func.attr in {"patch", "MagicMock", "Mock", "AsyncMock"}:
                    count += 1
                # patch.object(...)
                elif func.attr == "object" and isinstance(func.value, ast.Attribute) and func.value.attr == "patch":
                    count += 1
                elif func.attr == "object" and isinstance(func.value, ast.Name) and func.value.id == "patch":
                    count += 1

    return count


# ---------------------------------------------------------------------------
# Skip-decorator detection
# ---------------------------------------------------------------------------

def _decorator_has_reason(dec: ast.expr) -> bool:
    """Return True if the decorator call has at least one argument (reason/message)."""
    if isinstance(dec, ast.Call):
        return bool(dec.args or dec.keywords)
    return False


def _is_skip_decorator(dec: ast.expr) -> tuple[bool, bool]:
    """
    Return (is_skip, has_reason).
    Handles:
      @skip  @skip("reason")
      @unittest.skip  @unittest.skip("reason")
      @pytest.mark.skip  @pytest.mark.skip(reason="...")
      @pytest.mark.xfail  @pytest.mark.xfail(reason="...")
    """
    # Unwrap call to get the base name/attribute
    base = dec.func if isinstance(dec, ast.Call) else dec

    # @skip  or  @skip("msg")
    if isinstance(base, ast.Name) and base.id == "skip":
        return True, _decorator_has_reason(dec)

    if isinstance(base, ast.Attribute):
        attr = base.attr
        obj = base.value

        # @unittest.skip
        if isinstance(obj, ast.Name) and obj.id == "unittest" and attr == "skip":
            return True, _decorator_has_reason(dec)

        # @pytest.mark.skip  /  @pytest.mark.xfail
        if attr in ("skip", "xfail") and isinstance(obj, ast.Attribute):
            if obj.attr == "mark" and isinstance(obj.value, ast.Name) and obj.value.id == "pytest":
                return True, _decorator_has_reason(dec)

        # @mark.skip  /  @mark.xfail
        if attr in ("skip", "xfail") and isinstance(obj, ast.Name) and obj.id == "mark":
            return True, _decorator_has_reason(dec)

    return False, False


# ---------------------------------------------------------------------------
# Trivial-assertion detection helpers
# ---------------------------------------------------------------------------

def _is_truthy_constant(node: ast.expr) -> bool:
    """Return True if node is a Constant that is truthy (True, non-zero number, non-empty string)."""
    if not isinstance(node, ast.Constant):
        return False
    return bool(node.value)


def _is_falsy_constant(node: ast.expr) -> bool:
    """Return True if node is a Constant that is falsy (False, 0, empty string, None)."""
    if not isinstance(node, ast.Constant):
        return False
    return not bool(node.value)


def _same_structure(a: ast.expr, b: ast.expr) -> bool:
    """Return True if two AST expression nodes are structurally identical."""
    return ast.dump(a) == ast.dump(b)


def _same_value_expression(a: ast.expr, b: ast.expr) -> bool:
    """Identical sides that must evaluate the same — so the comparison cannot fail.

    Not when either side calls or awaits something: `f(x) == f(x)` asks whether
    `f` is deterministic, and fails exactly when it is not.
    """
    if any(isinstance(n, (ast.Call, ast.Await)) for side in (a, b) for n in ast.walk(side)):
        return False
    return _same_structure(a, b)


def _is_trivial_assertion(node: ast.stmt) -> bool:
    """
    Return True if *node* is a trivially vacuous assertion.

    Trivial cases:
      1. assert <truthy-constant>          e.g. assert True, assert 1, assert "ok"
      2. self.assertTrue(True)             — assertTrue called with a truthy constant
      3. self.assertFalse(False)           — assertFalse called with a falsy constant
      4. assertEqual(x, x) / assert a==a  — both sides structurally identical
         and neither calls anything (`f(a) == f(a)` is a determinism check)

    Not trivial: `assert x is not None` / `assertIsNotNone(x)`. It fails
    whenever the value is None, and for a lookup, a parse or anything else
    typed `Optional` that is exactly the outcome under test.
    """
    # ---- ast.Assert nodes ----
    if isinstance(node, ast.Assert):
        test = node.test
        # Case 1: assert <truthy constant>
        if _is_truthy_constant(test):
            return True
        # Case 4: assert a == a  (Compare with a single Eq op, same operands)
        if (
            isinstance(test, ast.Compare)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and _same_value_expression(test.left, test.comparators[0])
        ):
            return True
        return False

    # ---- Expr(Call(...)) — assert* method calls ----
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
        return False
    call = node.value
    func = call.func

    # Resolve method name; only handle self.assertXxx style
    if not isinstance(func, ast.Attribute):
        return False
    method = func.attr
    args = call.args

    # Case 2: self.assertTrue(True)
    if method == "assertTrue" and len(args) >= 1 and _is_truthy_constant(args[0]):
        return True

    # Case 3: self.assertFalse(False)
    if method == "assertFalse" and len(args) >= 1 and _is_falsy_constant(args[0]):
        return True

    # Case 4: self.assertEqual(x, x)
    if method == "assertEqual" and len(args) >= 2 and _same_value_expression(args[0], args[1]):
        return True

    return False


def _collect_assertions(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    """
    Return a flat list of every statement that is an assertion within the
    function body, without descending into nested function/class definitions:
    an `assert`, an assert-style call (awaited or not), `pytest.fail(...)`,
    `raise AssertionError(...)`, and a `with pytest.raises(...)` block — in
    every branch, `except` and `case` clauses included.
    """
    results: list[ast.stmt] = []

    def _walk_stmts(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # don't cross scope boundaries
            if isinstance(stmt, ast.Assert) or _is_assertion_raise(stmt):
                results.append(stmt)
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, (ast.Call, ast.Await)):
                if _is_assert_call(stmt.value):
                    results.append(stmt)
            elif _is_pytest_raises_with(stmt):
                results.append(stmt)
            # Recurse into compound-statement children
            _walk_stmts(list(_child_stmts(stmt)))

    _walk_stmts(func_node.body)
    return results


def _exercises_code(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the test runs anything that could raise: a call, an await, a `with`."""
    return any(isinstance(node, (ast.Call, ast.Await, ast.With, ast.AsyncWith))
               for node in _scope_nodes(func_node.body))


def _all_assertions_trivial(assertions: list[ast.stmt]) -> bool:
    """
    Return True iff there is at least one assertion AND every one is trivial
    per _is_trivial_assertion.

    pytest.raises/warns `with` blocks are treated as meaningful (non-trivial).
    """
    if not assertions:
        return False
    for stmt in assertions:
        if _is_pytest_raises_with(stmt):
            return False  # meaningful — tests that an exception is raised
        if not _is_trivial_assertion(stmt):
            return False
    return True


# ---------------------------------------------------------------------------
# Logic-in-test detection
# ---------------------------------------------------------------------------

# A loop over a fixed, non-empty table runs every iteration every time: it is a
# table-driven test (or a fixed arrange step), not a branch that may not run.
# These are the iterables whose length is visible in the source.
_STATIC_WRAPPERS = frozenset({"enumerate", "reversed", "sorted", "list", "tuple", "set", "frozenset"})
_STATIC_DICT_VIEWS = frozenset({"items", "keys", "values"})
# Of those, the calls that build a collection rather than a one-shot iterator.
_REITERABLE_CALLS = frozenset({"range", "sorted", "list", "tuple", "set", "frozenset"})
# Calls that end a test as skipped or failed — never as a silent pass.
_PYTEST_EXITS = ("skip", "fail", "xfail")
# Methods that can empty a collection in place.
_SHRINKING_METHODS = frozenset({"clear", "pop", "popitem", "remove", "discard"})


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _int_literal(node: ast.expr) -> int | None:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant) and type(node.operand.value) is int):
        return -node.operand.value
    return None


def _static_iterable(node: ast.expr, env: dict) -> bool:
    """True when `node` is a collection whose non-empty contents the source spells out.

    A non-empty list/tuple/set/dict display or string; `range()` over int
    literals that is not empty; `.items()`/`.keys()`/`.values()` of a static
    dict; `enumerate`/`reversed`/`sorted`/`list`/`tuple`/`set`/`frozenset`, `zip`
    or `itertools.product` of static iterables; or a name bound once to a
    collection of that kind. `env` maps such names to their value.
    """
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(not isinstance(elt, ast.Starred) for elt in node.elts)
    if isinstance(node, ast.Dict):
        return any(key is not None for key in node.keys)
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, bytes)) and bool(node.value)
    if isinstance(node, ast.Name):
        # Through a name, only a collection: `pairs = zip(a, b)` is an iterator,
        # and a second loop over it runs zero times.
        bound = env.get(node.id)
        if isinstance(bound, ast.Call) and _call_name(bound.func) not in _REITERABLE_CALLS:
            return False
        return bound is not None and _static_iterable(bound, {})
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = _call_name(func)
    if name == "range" and isinstance(func, ast.Name) and not node.keywords:
        bounds = [_int_literal(arg) for arg in node.args]
        if not bounds or None in bounds or len(bounds) > 3:
            return False
        try:
            return len(range(*bounds)) > 0
        except ValueError:  # a zero step
            return False
    if (isinstance(func, ast.Attribute) and name in _STATIC_DICT_VIEWS and not node.args
            and isinstance(func.value, (ast.Dict, ast.Name))):
        value = func.value if isinstance(func.value, ast.Dict) else env.get(func.value.id)
        return isinstance(value, ast.Dict) and _static_iterable(value, {})
    if name in _STATIC_WRAPPERS and isinstance(func, ast.Name):
        return len(node.args) == 1 and _static_iterable(node.args[0], env)
    if name in ("zip", "product"):
        # The builtin zip, and product as imported or as itertools.product.
        if not (isinstance(func, ast.Name) or (
                name == "product" and isinstance(func.value, ast.Name) and func.value.id == "itertools")):
            return False
        for kw in node.keywords:
            if kw.arg == "repeat" and (_int_literal(kw.value) or 0) > 0:
                continue
            if kw.arg == "strict":
                continue
            return False
        return bool(node.args) and all(_static_iterable(arg, env) for arg in node.args)
    return False


def _static_bindings(body: list[ast.stmt], everywhere) -> dict:
    """Names the scope binds exactly once, by a plain assignment, and nothing shrinks.

    Bindings are counted in the scope's own statements (`body`); in-place
    shrinking — `.clear()`, `.pop()`, `del x[...]`, `x[:] = []` — and `global`
    rebinding are looked for in `everywhere`, since any function could do either.
    """
    stores: dict[str, int] = defaultdict(int)
    for node in _scope_nodes(body):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            stores[node.id] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stores[node.name] += 1
    shrunk: set[str] = set()
    for node in everywhere:
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _SHRINKING_METHODS and isinstance(node.func.value, ast.Name)):
            shrunk.add(node.func.value.id)
        elif (isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del))
              and isinstance(node.value, ast.Name)):
            shrunk.add(node.value.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            shrunk.update(node.names)
    assigns = [n for n in _scope_nodes(body) if isinstance(n, (ast.Assign, ast.AnnAssign))]
    env = {}
    for stmt in assigns:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None and stmt.simple:
            target, value = stmt.target, stmt.value
        else:
            continue
        if (isinstance(target, ast.Name) and stores[target.id] == 1
                and target.id not in shrunk):
            env[target.id] = value
    return env


class _Lazy:
    """A mapping built on first lookup.

    Most tests have no loop over a name, so the bindings a name would resolve
    against are never needed; building them for every test of a large suite
    doubled the detector's run time.
    """

    def __init__(self, build):
        self._build, self._value = build, None

    def get(self, key, default=None):
        if self._value is None:
            self._value = self._build()
        return self._value.get(key, default)

    def items(self):
        if self._value is None:
            self._value = self._build()
        return self._value.items()


def _module_static_env(tree: ast.Module) -> dict:
    """Module-level names bound once to a value, for `for case in CASES:`."""
    return _static_bindings(tree.body, list(ast.walk(tree)))


def _loop_can_skip(loop: ast.For) -> bool:
    """A `break`, `continue` or `return` in the body can cut an iteration short."""
    stack = list(loop.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Break, ast.Continue, ast.Return)):
            return True
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))
    return False


def _is_exit_call(stmt: ast.stmt) -> bool:
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    func = stmt.value.func
    if _is_pytest_attr(func, _PYTEST_EXITS):
        return True
    return (isinstance(func, ast.Attribute) and func.attr in ("skipTest", "fail")
            and isinstance(func.value, ast.Name) and func.value.id == "self")


_COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
             ast.Match, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef) + (
    (ast.TryStar,) if hasattr(ast, "TryStar") else ())


def _is_guard(node: ast.If) -> bool:
    """`if <unmet precondition>: pytest.skip(...)` — or `pytest.fail` / `xfail` / `raise`.

    Neither outcome is a silent pass: a skip is reported as skipped, and a fail
    or a raise is an assertion spelled with an `if`. Held to the guard shape — no
    `else`, straight-line statements ending in the exit — because anything
    more is a branch whose other arm runs different checks.
    """
    if node.orelse or not node.body:
        return False
    *lead, last = node.body
    if isinstance(last, ast.Return) and last.value is None and lead and _is_exit_call(lead[-1]):
        last = lead.pop()
    if any(isinstance(stmt, _COMPOUND) for stmt in lead):
        return False
    return isinstance(last, ast.Raise) or _is_exit_call(last)


def _has_logic(func_node: ast.FunctionDef | ast.AsyncFunctionDef, module_env) -> tuple[bool, int]:
    """
    Return (has_logic, first_line) where logic = For / AsyncFor / While / If
    within the test function body (recursively, every branch included, but
    skipping nested function/class definitions and comprehension scopes).

    Not logic, because no assertion can be skipped by it: a `for` over a static,
    non-empty iterable whose body has no `break`/`continue`/`return` (nested
    control flow inside it is judged on its own), and an `if` that only skips or
    fails the test.
    """
    def build_env() -> dict:
        local = _local_bindings(func_node)
        return {**{k: v for k, v in module_env.items() if k not in local},
                **_static_bindings(func_node.body, list(ast.walk(func_node)))}

    env = _Lazy(build_env)

    def walk_body(stmts):
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # don't descend into nested scopes
            if isinstance(stmt, ast.For):
                if not (_static_iterable(stmt.iter, env) and not _loop_can_skip(stmt)):
                    yield stmt
            elif isinstance(stmt, (ast.AsyncFor, ast.While)):
                yield stmt
            elif isinstance(stmt, ast.If):
                if not _is_guard(stmt):
                    yield stmt
            # Recurse into statement bodies (try/except, with, etc.)
            yield from walk_body(list(_child_stmts(stmt)))

    for node in walk_body(func_node.body):
        return True, node.lineno
    return False, 0


# ---------------------------------------------------------------------------
# Per-file analysis
# ---------------------------------------------------------------------------

def analyze_file(filepath: Path, ignore: set) -> list[CodeSmell]:
    if not _is_test_file(filepath):
        return []
    try:
        source, tree = cached_parse(filepath)
        lines = source.splitlines()
    except (SyntaxError, ValueError, OSError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []

    issues: list[CodeSmell] = []

    def add(line, st, desc, sug, sev, snippet=""):
        if st in ignore:
            return
        issues.append(CodeSmell(
            file=str(filepath),
            line=line,
            smell_type=st,
            description=desc,
            suggestion=sug,
            severity=sev,
            code_snippet=snippet,
        ))

    # What a test can reach in one step: module-level helpers (never a
    # fixture, which pytest calls, not the test) and its own class's methods.
    module_helpers = {n.name: n for n in tree.body
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _is_fixture(n)}
    class_helpers: dict[int, dict] = {}
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            methods = {m.name: m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for method in methods.values():
                class_helpers[id(method)] = methods
    module_env = _Lazy(lambda: _module_static_env(tree))

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue

        fn = node
        fn_snippet = _get_line(lines, fn.lineno)
        direct = _collect_assertions(fn)
        # Only looked for when it can change the verdict: no direct assertion,
        # or only trivial ones.
        reached = None

        def reaches(fn=fn):
            return _reaches_assertion(fn, module_helpers, class_helpers.get(id(fn), {}))

        # ---- test_without_assertion ----------------------------------------
        if not direct:
            reached = reaches()
        if not direct and not reached:
            if _exercises_code(fn):
                # It calls code, so it fails if that code raises: weak, not empty.
                add(
                    fn.lineno,
                    "test_without_assertion",
                    f"Test function '{fn.name}' contains no assertion: it passes whenever "
                    "the code it runs does not raise",
                    "Assert the result or the side effect the call is meant to have. If not raising "
                    "really is the whole contract, a postcondition (nothing written, state unchanged) "
                    "still makes the test able to catch more than a crash.",
                    "low",
                    fn_snippet,
                )
            else:
                add(
                    fn.lineno,
                    "test_without_assertion",
                    f"Test function '{fn.name}' contains no assertion and runs nothing that could fail",
                    "Add assertions to verify behaviour, or delete the test if it is a placeholder.",
                    "high",
                    fn_snippet,
                )

        # ---- overmocking ---------------------------------------------------
        mock_count = _count_mock_constructs(fn)
        if mock_count > 4:
            add(
                fn.lineno,
                "overmocking",
                f"Test function '{fn.name}' uses {mock_count} mock/patch constructs",
                (
                    "Heavy mocking tests the mocks, not real behaviour. "
                    "Prefer real objects or restructure to use fewer seams."
                ),
                "medium",
                fn_snippet,
            )

        # ---- skipped_without_reason ----------------------------------------
        for dec in fn.decorator_list:
            is_skip, has_reason = _is_skip_decorator(dec)
            if is_skip and not has_reason:
                dec_line = dec.lineno
                add(
                    dec_line,
                    "skipped_without_reason",
                    f"Test '{fn.name}' is skipped without a reason/message",
                    "Add a reason= argument explaining why the test is skipped, or remove the skip.",
                    "low",
                    _get_line(lines, dec_line),
                )

        # ---- logic_in_test -------------------------------------------------
        has_logic, logic_line = _has_logic(fn, module_env)
        if has_logic:
            add(
                logic_line,
                "logic_in_test",
                f"Test function '{fn.name}' contains control-flow (for/while/if) in its body",
                (
                    "Tests with logic can hide bugs. Make tests linear; "
                    "use parametrize for multiple cases."
                ),
                "low",
                _get_line(lines, logic_line),
            )

        # ---- trivial_assertion ---------------------------------------------
        if _all_assertions_trivial(direct) and not (reached if reached is not None else reaches()):
            add(
                fn.lineno,
                "trivial_assertion",
                f"Test function '{fn.name}' contains only trivial (vacuous) assertions",
                (
                    "Assert the actual expected value or behaviour — "
                    "an assertion that can never fail protects nothing."
                ),
                "medium",
                fn_snippet,
            )

    return issues


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect test-quality smells in Python test files")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    all_issues: list[CodeSmell] = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))
    sort_findings(all_issues)

    if args.format == "json":
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No test smells found!")
            return
        by_type: dict[str, int] = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} test smell(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = SEVERITY_ICONS
        for i in all_issues:
            print(f"{icons[i.severity]} [{i.severity.upper()}] {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            print(f"   → {i.suggestion}\n")


if __name__ == "__main__":
    main()
