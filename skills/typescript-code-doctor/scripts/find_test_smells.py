#!/usr/bin/env python3
"""
Find tests that cannot fail, tests that were switched off, and the async
mistakes that make a suite green for the wrong reason.

A test with no assertion passes as long as nothing throws. A missing `await` on
`expect(...).rejects` passes *always*. Both are worse than no test: they occupy
the slot where a real one would go and report the code as covered.
"""

from common import SUITE_DEFINERS, Reporter, is_test_file, run_file_detector
from tsparse import (
    TsFile, argument_spans, body_indices, iter_calls, statement_end, statement_start,
)

TEST_DEFINERS = frozenset({"it", "test", "specify", "bench"})
ASSERTION_NAMES = frozenset({"expect", "assert", "should", "chai", "toThrow", "verify", "assertThat"})
# Testing Library's `getBy*` / `findBy*` queries throw when nothing matches, so
# they assert even though no `expect` appears. Treating them as non-assertions
# would report most of a well-written component suite.
QUERY_PREFIXES = ("getBy", "getAllBy", "findBy", "findAllBy")
MOCK_CALLS = frozenset({"mock", "spyOn", "doMock", "mockRestore", "unstable_mockModule"})

# Assertions that pass for almost any value, spelled as the chain after `expect(x)`
# with `resolves`/`rejects` dropped. `toBeNull()` and `toBeUndefined()` are not
# here: each passes for exactly one value, so it is as exact as `toBe(null)`.
# Their negations are the weak ones — `not.toBeNull()` passes for 0, "", {}, [].
WEAK_MATCHERS = frozenset({
    "toBeDefined", "toBeTruthy", "toBeFalsy",
    "not.toBeNull", "not.toBeUndefined", "not.toBeTruthy", "not.toBeFalsy",
    "toBeInstanceOf(Object)",
})
# Matchers that return a promise and are useless without `await`.
PROMISE_MATCHERS = frozenset({"resolves", "rejects"})
MOCK_LIMIT = 12


def _test_calls(file: TsFile):
    """Yield (paren, name, callback_func) for each `it(...)` / `test(...)`."""
    for paren, callee in iter_calls(file):
        head = callee.split(".")[0]
        if head not in TEST_DEFINERS:
            continue
        spans = argument_spans(file, paren)
        if len(spans) < 2:
            continue
        callback = None
        for func in file.functions:
            if func.has_body and spans[1][0] <= func.start <= spans[1][1]:
                if callback is None or func.start < callback.start:
                    callback = func
        title = file.slice(*spans[0]).strip().strip("'\"`")[:60]
        yield paren, callee, title, callback


def _matcher_chain(file: TsFile, index: int) -> str:
    """The matcher an assertion applies, as written after `expect(...)`.

    `expect(x).not.toBeNull()` yields "not.toBeNull" — the negation is part of
    the claim, and reading the words separately loses it. A matcher whose
    argument decides its strength keeps that argument:
    `toBeInstanceOf(Object)` is weak where `toBeInstanceOf(Date)` is not.
    """
    cursor = index + 1
    if cursor < len(file) and file.tokens[cursor].is_op("("):
        cursor = file.skip_group(cursor)  # step over expect(...)
    words: list[str] = []
    while cursor + 1 < len(file) and file.tokens[cursor].is_op(".", "?."):
        word = file.tokens[cursor + 1].value
        cursor += 2
        if cursor < len(file) and file.tokens[cursor].is_op("("):
            if word == "toBeInstanceOf":
                word += "(" + file.slice(cursor + 1, file.closer(cursor)).strip() + ")"
            cursor = file.skip_group(cursor)
        if word not in PROMISE_MATCHERS:  # they change what is awaited, not what is asserted
            words.append(word)
    return ".".join(words)


class _Evidence:
    """What a function's body asserts."""

    __slots__ = ("asserts", "matchers", "queries")

    def __init__(self) -> None:
        self.asserts = False
        self.matchers: set[str] = set()
        # A `getBy*`/`findBy*` query throws when nothing matches, so a test
        # that makes one has asserted presence whatever matcher follows it.
        self.queries = False

    def absorb(self, other: "_Evidence") -> bool:
        """Add ``other``'s assertions to these. True when anything was new."""
        before = (self.asserts, self.queries, len(self.matchers))
        self.asserts = self.asserts or other.asserts
        self.queries = self.queries or other.queries
        self.matchers |= other.matchers
        return before != (self.asserts, self.queries, len(self.matchers))


def _own_evidence(file: TsFile, func) -> _Evidence:
    """The assertions written in ``func``'s own body (nested callbacks included)."""
    evidence = _Evidence()
    asserts = [i for i in body_indices(func) if file.tokens[i].kind == "name"
               and (file.tokens[i].value in ASSERTION_NAMES
                    or file.tokens[i].value.startswith(QUERY_PREFIXES))]
    evidence.asserts = bool(asserts)
    evidence.queries = any(file.tokens[i].value.startswith(QUERY_PREFIXES) for i in asserts)
    for index in asserts:
        chain = _matcher_chain(file, index)
        if chain:
            evidence.matchers.add(chain)
    return evidence


def _helpers_called(file: TsFile, func, helpers: dict[str, list]) -> list:
    body = body_indices(func)
    if not body:
        return []
    return [helper for _, callee in iter_calls(file, body.start, body.stop)
            for helper in helpers.get(callee, ()) if helper is not func]


class _Assertions:
    """Per-function assertion evidence, counting the same-file helpers each one calls.

    `streamAndCompare(input)`, a helper looping over `expect(...).toBe(...)`,
    makes every test that calls it an asserting test; reading only the test's
    own body reported each of them as making no assertion. A helper's evidence
    is closed over the helpers it calls in turn, to a fixpoint, so a chain of
    them (and a recursive one) counts like a single body.
    """

    def __init__(self, file: TsFile):
        self.file = file
        self.helpers: dict[str, list] = {}
        for func in file.functions:
            if func.has_body and func.owner is None and func.name != "<anonymous>":
                self.helpers.setdefault(func.name, []).append(func)
        named = [func for group in self.helpers.values() for func in group]
        self.closed = {id(func): _own_evidence(file, func) for func in named}
        callees = {id(func): _helpers_called(file, func, self.helpers) for func in named}
        changed = True
        while changed:
            changed = False
            for func in named:
                for helper in callees[id(func)]:
                    changed |= self.closed[id(func)].absorb(self.closed[id(helper)])

    def of(self, func) -> _Evidence:
        evidence = _own_evidence(self.file, func)
        for helper in _helpers_called(self.file, func, self.helpers):
            evidence.absorb(self.closed[id(helper)])
        return evidence


def _check_assertionless(file: TsFile, report: Reporter) -> None:
    assertions = _Assertions(file)
    for paren, callee, title, callback in _test_calls(file):
        if callback is None:
            continue
        body = body_indices(callback)
        if not body:
            report.add(file.tokens[paren].line, "empty_test",
                       f"`{callee}('{title}')` has an empty body",
                       "Finish it or delete it. An empty test is a green tick for nothing.", "high")
            continue
        evidence = assertions.of(callback)
        if not evidence.asserts:
            report.add(file.tokens[paren].line, "test_without_assertion",
                       f"`{callee}('{title}')` makes no assertion",
                       "It passes as long as nothing throws, which is a much weaker claim than the "
                       "name promises. Assert the value the code is supposed to produce.", "high")
            continue
        matchers = evidence.matchers
        weak = matchers & WEAK_MATCHERS
        if matchers and matchers <= WEAK_MATCHERS and not evidence.queries:
            report.add(file.tokens[paren].line, "weak_assertion_only",
                       f"`{callee}('{title}')` only asserts {', '.join(sorted(weak))}",
                       "These pass for almost any value. Assert the expected value with `toEqual` "
                       "so the test fails when the behaviour changes.", "medium")


def _check_focused_and_skipped(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if token.kind != "name" or token.value not in TEST_DEFINERS | SUITE_DEFINERS:
            continue
        if index + 2 >= len(file) or not file.tokens[index + 1].is_op("."):
            continue
        modifier = file.tokens[index + 2]
        if modifier.is_name("only"):
            report.add(token.line, "focused_test",
                       f"`{token.value}.only` — every other test in the file is skipped",
                       "Remove it before committing. A `.only` that reaches main silently turns off "
                       "the suite while CI still reports green.", "high")
        elif modifier.is_name("skip", "todo", "failing"):
            report.add(token.line, "skipped_test",
                       f"`{token.value}.{modifier.value}` — this test does not run",
                       "Fix it or delete it, with the reason in the commit message. A permanently "
                       "skipped test is a claim of coverage that is not being checked.", "medium")
    for token in file.tokens:
        if token.is_name("xit", "xdescribe", "xtest"):
            report.add(token.line, "skipped_test",
                       f"`{token.value}` — this test does not run",
                       "Fix it or delete it.", "medium")


def _check_async_mistakes(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if token.kind != "name" or token.value not in PROMISE_MATCHERS:
            continue
        if index == 0 or not file.tokens[index - 1].is_op("."):
            continue
        # Walk left to the head of the expression: it must be `await` or `return`.
        cursor = index - 2
        if cursor >= 0 and file.tokens[cursor].is_op(")"):
            opener = file.closer(cursor)
            cursor = opener - 1 if opener > 0 else cursor
        head = file.tokens[cursor - 1] if cursor >= 1 else None
        if head is not None and head.is_name("await", "return"):
            continue
        report.add(token.line, "unawaited_async_assertion",
                   f"`expect(...).{token.value}` without `await` or `return`",
                   f"`.{token.value}` produces a promise; unawaited, the assertion is never checked "
                   "and the test passes unconditionally. Write "
                   f"`await expect(...).{token.value}.toThrow(...)`.", "high")

    async_helpers = {func.name for func in file.functions
                     if func.is_async and func.owner is None and func.name != "<anonymous>"}
    for paren, callee, title, callback in _test_calls(file):
        if callback is None or not callback.is_async:
            continue
        body = body_indices(callback)
        if any(file.tokens[i].is_name("await") for i in body):
            continue
        pending = _unawaited_promise(file, body, async_helpers)
        if pending:
            report.add(file.tokens[paren].line, "async_test_never_awaits",
                       f"`{callee}('{title}')` is async but never awaits `{pending}`",
                       f"Await it. `{pending}` produces a promise, and the test's own promise "
                       "resolves without waiting for it — whatever it checks or rejects with lands "
                       "after the test has already passed.", "medium")
            continue
        # Nothing visible here produces a promise, so the body runs to the end
        # before the test's promise settles and every assertion in it counts:
        # the `async` is redundant rather than a race. An imported call that
        # returns a promise is the case syntax cannot see, so the advice names it.
        report.add(file.tokens[paren].line, "async_test_never_awaits",
                   f"`{callee}('{title}')` is async but never awaits anything",
                   "Drop `async` if nothing in the body returns a promise. If a call does (an "
                   "imported async function, a user-event action), await it — an unawaited "
                   "promise settles after the test has already passed.", "low")


# Calls whose promise carries an assertion or the work under test: Testing
# Library's polling helpers and async queries, and the Promise methods that
# chain one. (`act` is not here: its synchronous form is the common one.)
_PROMISE_CALLS = frozenset({"waitFor", "waitForElementToBeRemoved"})
_PROMISE_METHODS = frozenset({"then", "catch", "finally"})
_ASYNC_QUERY_PREFIXES = ("findBy", "findAllBy")


def _unawaited_promise(file: TsFile, body: range, async_helpers: set[str]) -> str:
    """The first visibly promise-producing call in an `await`-free body, or "".

    A call in a `return` statement is not unawaited: an async function adopts
    the promise it returns, so the test waits for it.
    """
    if not body:
        return ""
    for paren, callee in iter_calls(file, body.start, body.stop):
        last = callee.rsplit(".", 1)[-1]
        if not (callee in async_helpers or last in _PROMISE_CALLS or last in _PROMISE_METHODS
                or last.startswith(_ASYNC_QUERY_PREFIXES)):
            continue
        if file.tokens[statement_start(file, paren)].is_name("return"):
            continue
        return f"….{last}(…)" if last in _PROMISE_METHODS else f"{callee}(…)"
    return ""


def _is_throw_guard(file: TsFile, keyword: int) -> bool:
    """`if (…) throw …` with no `else`: a narrowing guard, not a second path.

    The branch that is not taken is the test continuing; the branch that is
    taken fails the test. Neither is a path that passes without asserting.
    """
    if not file.tokens[keyword].is_name("if") or keyword + 1 >= len(file):
        return False
    cursor = file.skip_group(keyword + 1)  # past the condition
    if cursor >= len(file):
        return False
    braced = file.tokens[cursor].is_op("{")
    if braced:
        cursor += 1
    if not file.tokens[cursor].is_name("throw"):
        return False
    end = statement_end(file, cursor)
    if braced:
        if end >= len(file) or not file.tokens[end].is_op("}"):
            return False  # the block does more than throw
        end += 1
    return end >= len(file) or not file.tokens[end].is_name("else")


def _in_finally(file: TsFile, index: int, callback) -> bool:
    """True when ``index`` sits in a `finally` block of ``callback``'s own body.

    A `finally` runs after the assertions above it have already passed or
    failed, so a conditional restore there cannot hide one.
    """
    for probe in body_indices(callback):
        if probe >= index:
            return False
        if file.tokens[probe].is_name("finally") and probe + 1 < len(file) \
                and file.tokens[probe + 1].is_op("{") and probe + 1 < index < file.closer(probe + 1):
            return True
    return False


def _check_logic_in_tests(file: TsFile, report: Reporter) -> None:
    for paren, callee, title, callback in _test_calls(file):
        if callback is None:
            continue
        branches = [i for i in body_indices(callback)
                    if file.tokens[i].is_name("if", "switch")
                    and file.enclosing_function(i) is callback
                    and not _is_throw_guard(file, i)
                    and not _in_finally(file, i, callback)]
        if branches:
            report.add(file.tokens[branches[0]].line, "logic_in_test",
                       f"`{callee}('{title}')` branches on a condition",
                       "A test with an `if` in it has a path that asserts nothing, and you cannot "
                       "tell from a green run which path ran. Split it into two tests.", "medium")


def _check_over_mocking(file: TsFile, report: Reporter) -> None:
    mocks = [paren for paren, callee in iter_calls(file)
             if callee.rsplit(".", 1)[-1] in MOCK_CALLS and "." in callee]
    if len(mocks) > MOCK_LIMIT:
        report.add(file.tokens[mocks[0]].line, "over_mocked_test_file",
                   f"{len(mocks)} mock/spy calls in one test file",
                   "At this density the test asserts how the code is wired, not what it does, and "
                   "it will fail on every refactor while missing real breakage. Test through the "
                   "real collaborators and mock only what is slow, non-deterministic, or external.",
                   "low")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    if not is_test_file(file.path):
        return report.findings
    _check_assertionless(file, report)
    _check_focused_and_skipped(file, report)
    _check_async_mistakes(file, report)
    _check_logic_in_tests(file, report)
    _check_over_mocking(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find tests that cannot fail, focused/skipped tests and async assertion mistakes",
        "No test smells found!",
        analyze,
    )
