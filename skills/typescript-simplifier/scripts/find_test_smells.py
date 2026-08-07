#!/usr/bin/env python3
"""
Find tests that cannot fail, tests that were switched off, and the async
mistakes that make a suite green for the wrong reason.

A test with no assertion passes as long as nothing throws. A missing `await` on
`expect(...).rejects` passes *always*. Both are worse than no test: they occupy
the slot where a real one would go and report the code as covered.
"""

from common import Reporter, is_test_file, run_file_detector
from tsparse import TsFile, argument_spans, body_indices, iter_calls

TEST_DEFINERS = frozenset({"it", "test", "specify", "bench"})
SUITE_DEFINERS = frozenset({"describe", "suite", "context"})
ASSERTION_NAMES = frozenset({"expect", "assert", "should", "chai", "toThrow", "verify", "assertThat"})
# Testing Library's `getBy*` / `findBy*` queries throw when nothing matches, so
# they assert even though no `expect` appears. Treating them as non-assertions
# would report most of a well-written component suite.
QUERY_PREFIXES = ("getBy", "getAllBy", "findBy", "findAllBy")
MOCK_CALLS = frozenset({"mock", "spyOn", "doMock", "mockRestore", "unstable_mockModule"})

# Assertions that pass for almost any value.
WEAK_MATCHERS = frozenset({"toBeDefined", "toBeTruthy", "toBeFalsy", "toBeNull", "toBeUndefined"})
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


def _check_assertionless(file: TsFile, report: Reporter) -> None:
    for paren, callee, title, callback in _test_calls(file):
        if callback is None:
            continue
        body = body_indices(callback)
        if not body:
            report.add(file.tokens[paren].line, "empty_test",
                       f"`{callee}('{title}')` has an empty body",
                       "Finish it or delete it. An empty test is a green tick for nothing.", "high")
            continue
        asserts = [i for i in body if file.tokens[i].kind == "name"
                   and (file.tokens[i].value in ASSERTION_NAMES
                        or file.tokens[i].value.startswith(QUERY_PREFIXES))]
        if not asserts:
            report.add(file.tokens[paren].line, "test_without_assertion",
                       f"`{callee}('{title}')` makes no assertion",
                       "It passes as long as nothing throws, which is a much weaker claim than the "
                       "name promises. Assert the value the code is supposed to produce.", "high")
            continue
        matchers = set()
        for index in asserts:
            cursor = index + 1
            if cursor < len(file) and file.tokens[cursor].is_op("("):
                cursor = file.skip_group(cursor)  # step over expect(...)
            while cursor + 1 < len(file) and file.tokens[cursor].is_op(".", "?."):
                matchers.add(file.tokens[cursor + 1].value)
                cursor += 2
                if cursor < len(file) and file.tokens[cursor].is_op("("):
                    cursor = file.skip_group(cursor)
        weak = matchers & WEAK_MATCHERS
        if matchers and matchers <= WEAK_MATCHERS:
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

    for paren, callee, title, callback in _test_calls(file):
        if callback is None or not callback.is_async:
            continue
        if not any(file.tokens[i].is_name("await") for i in body_indices(callback)):
            report.add(file.tokens[paren].line, "async_test_never_awaits",
                       f"`{callee}('{title}')` is async but never awaits anything",
                       "Either drop `async`, or await the thing under test. An async test that "
                       "forgets to await finishes before the assertion it was meant to make.",
                       "medium")


def _check_logic_in_tests(file: TsFile, report: Reporter) -> None:
    for paren, callee, title, callback in _test_calls(file):
        if callback is None:
            continue
        branches = [i for i in body_indices(callback)
                    if file.tokens[i].is_name("if", "switch")
                    and file.enclosing_function(i) is callback]
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
