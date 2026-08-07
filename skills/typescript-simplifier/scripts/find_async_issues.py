#!/usr/bin/env python3
"""
Find promise bugs: the ones that lose errors, lose ordering, or silently no-op.

TypeScript checks shapes. It does not check that a promise is ever observed, so
the whole class of "this compiled and did nothing" lives here. `forEach(async
…)` is the canonical one — it type-checks, runs, and does not wait.
"""

from common import Reporter, run_file_detector
from tsparse import TsFile, body_indices, iter_calls, statement_start

# Array methods whose callback result is *used*: handing them an async callback
# gives them a Promise, and a Promise is always truthy.
PREDICATE_METHODS = frozenset({"filter", "find", "findIndex", "findLast", "some", "every", "sort"})
# Methods that ignore the callback's return value entirely.
DISCARDING_METHODS = frozenset({"forEach"})

# Calls that produce a promise no matter what the surrounding types say.
PROMISE_SOURCES = frozenset({"fetch", "Promise.all", "Promise.allSettled", "Promise.race",
                             "Promise.any", "Promise.resolve", "Promise.reject"})

LOOP_KEYWORDS = frozenset({"for", "while", "do"})


def _async_names(file: TsFile) -> set[str]:
    """Names declared async in this file — the ones we can be sure about."""
    return {func.name for func in file.functions if func.is_async and func.name != "<anonymous>"}


def _callback_is_async(file: TsFile, paren: int) -> int:
    """Line of an `async` callback passed as the first argument, or -1."""
    first = paren + 1
    if first < len(file) and file.tokens[first].is_name("async"):
        return file.tokens[first].line
    return -1


def _check_array_callbacks(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        method = callee.rsplit(".", 1)[-1]
        line = _callback_is_async(file, paren)
        if line < 0 or "." not in callee:
            continue
        if method in DISCARDING_METHODS:
            report.add(line, "async_callback_in_foreach",
                       f"`{callee}(async …)` — forEach ignores the returned promise, so nothing waits",
                       "Use `for (const x of items) { await … }` for sequential work, or "
                       "`await Promise.all(items.map(async x => …))` for parallel work. Rejections "
                       "here become unhandled rejections.", "high")
        elif method in PREDICATE_METHODS:
            report.add(line, "async_callback_in_predicate",
                       f"`{callee}(async …)` — the callback returns a Promise, which is always truthy",
                       f"`{method}` cannot await. Resolve first: "
                       "`const flags = await Promise.all(items.map(check))`, then use the flags.", "high")


def _check_promise_executor(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("Promise") or index == 0 or not file.tokens[index - 1].is_name("new"):
            continue
        paren = index + 1
        if paren >= len(file) or not file.tokens[paren].is_op("("):
            continue
        close = file.closer(paren)
        if close < 0:
            continue
        if file.tokens[paren + 1].is_name("async"):
            report.add(token.line, "async_promise_executor",
                       "`new Promise(async …)` — a rejection inside the executor is swallowed",
                       "The executor's returned promise is discarded, so a throw after the first "
                       "await never reaches `reject`. Drop the wrapper and return the async function's "
                       "promise directly.", "high")
            continue
        wraps_promise = any(
            file.tokens[probe].is_name("then") and file.tokens[probe - 1].is_op(".", "?.")
            for probe in range(paren + 1, close)
        )
        if wraps_promise:
            report.add(token.line, "promise_constructor_antipattern",
                       "`new Promise(…)` wrapping something that already returns a promise",
                       "Return the inner promise (or await it). Re-wrapping loses rejections and "
                       "adds a layer that has to be kept correct by hand.", "medium")


def _shadowing_params(file: TsFile, index: int) -> set[str]:
    """Parameter names in scope at ``index``.

    A file-wide name table has no scopes, so `async function resolve()` at the
    bottom of a module would otherwise make the `resolve` of a Promise executor
    look like a floating promise. A parameter of the same name shadows it.
    """
    names: set[str] = set()
    for func in file.functions:
        if func.has_body and func.body_open < index < func.body_close:
            names.update(param.name for param in func.params)
    return names


def _check_floating_promises(file: TsFile, report: Reporter) -> None:
    """Statement-level promise expressions whose result nobody looks at."""
    known_async = _async_names(file)
    for paren, callee in iter_calls(file):
        name_start = paren - 1 - 2 * callee.count(".")
        method = callee.rsplit(".", 1)[-1]
        if callee.split(".")[0] in _shadowing_params(file, paren):
            continue
        is_promise = (
            callee in PROMISE_SOURCES
            or method in ("then", "catch", "finally") and "." in callee
            or callee in known_async
            or method in known_async and "." not in callee
        )
        if not is_promise:
            continue
        # The call has to *be* the statement. Anything to its left — `await`,
        # `return`, `const x =`, `cache =` — means the value is used.
        statement = statement_start(file, name_start)
        if statement != name_start:
            continue
        head = file.tokens[statement]
        if head.is_name("return", "await", "void", "yield", "export", "const", "let", "var"):
            continue
        # `() => doThing()` *returns* the promise; whoever receives that arrow
        # decides whether it is handled. Deciding that needs the callee's type,
        # which is typescript-eslint's no-misused-promises, not this scanner's.
        if statement and file.tokens[statement - 1].is_op("=>"):
            continue
        close = file.closer(paren)
        if close < 0:
            continue
        following = file.tokens[close + 1] if close + 1 < len(file) else None
        if following is not None and (following.is_op(".", "?.", ",", ")", "]", "=>", "+", ":", "=")
                                      or following.kind == "name"):
            continue  # the value is consumed by a chain, an argument, or an assignment
        if method == "catch" or _chain_has(file, statement, close, "catch"):
            continue
        report.add(head.line, "floating_promise",
                   f"`{callee}(…)` returns a promise that is never awaited or handled",
                   "Await it, return it, or attach a `.catch`. A floating promise runs out of order "
                   "and its rejection becomes an unhandled rejection (a hard crash under Node's "
                   "default in v15+).", "high")


def _chain_has(file: TsFile, start: int, stop: int, method: str) -> bool:
    return any(
        file.tokens[i].is_name(method) and file.tokens[i - 1].is_op(".", "?.")
        for i in range(start, min(stop + 1, len(file)))
    )


def _check_swallowed_rejections(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        if callee.rsplit(".", 1)[-1] != "catch":
            continue
        # `fetch(x).catch(...)` gives a bare callee of "catch" because the chain
        # starts at a call rather than a name, so check the dot directly.
        if not (paren >= 2 and file.tokens[paren - 2].is_op(".", "?.")):
            continue
        close = file.closer(paren)
        if close < 0:
            continue
        inner = file.slice(paren + 1, close).strip()
        if inner in ("() => {}", "() => { }", "() => undefined", "() => null", "()=>{}"):
            report.add(file.tokens[paren].line, "swallowed_rejection",
                       "`.catch(() => {})` discards the error and reports success",
                       "Log it with context and re-throw, or handle it concretely. An empty catch "
                       "turns a failure into a silent wrong answer.", "high")


def _check_await_in_loop(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if token.kind != "name" or token.value not in LOOP_KEYWORDS:
            continue
        if token.is_name("for") and file.tokens[index + 1].is_name("await"):
            continue  # `for await (… of …)` is the correct sequential form
        cursor = index + 1
        if cursor < len(file) and file.tokens[cursor].is_op("("):
            cursor = file.skip_group(cursor)
        if cursor >= len(file) or not file.tokens[cursor].is_op("{"):
            continue
        close = file.closer(cursor)
        if close < 0:
            continue
        awaits = [i for i in range(cursor, close)
                  if file.tokens[i].is_name("await") and _same_function(file, i, cursor)]
        if awaits:
            report.add(token.line, "await_in_loop",
                       f"`await` inside a {token.value} loop — {len(awaits)} sequential round trip(s) per iteration",
                       "If the iterations are independent, collect the promises and "
                       "`await Promise.all(...)` once. If each iteration depends on the last, or the "
                       "target rate-limits, keep the loop and say so in a comment.", "medium")


def _same_function(file: TsFile, index: int, loop_body: int) -> bool:
    """True when the await belongs to the loop rather than a nested callback."""
    enclosing = file.enclosing_function(index)
    outer = file.enclosing_function(loop_body)
    return enclosing is outer or (enclosing is not None and outer is not None
                                  and enclosing.body_open == outer.body_open)


def _check_async_without_await(file: TsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.is_async or not func.has_body:
            continue
        body = body_indices(func)
        nested = [(f.body_open, f.body_close) for f in file.functions
                  if f is not func and f.has_body and func.body_open < f.body_open < func.body_close]
        has_await = any(
            file.tokens[i].is_name("await") and not any(a < i < b for a, b in nested)
            for i in body
        )
        if not has_await and func.name != "<anonymous>":
            report.add(func.line, "async_without_await",
                       f"`{func.qualname}` is declared async but never awaits",
                       "Drop `async` (and return the promise directly if it returns one). An async "
                       "wrapper with no await only adds a microtask and hides where the waiting is.",
                       "low")


def _check_then_chains(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        if callee.rsplit(".", 1)[-1] != "then" or "." not in callee:
            continue
        close = file.closer(paren)
        if close < 0:
            continue
        nested = any(
            file.tokens[i].is_name("then") and file.tokens[i - 1].is_op(".", "?.")
            for i in range(paren + 1, close)
        )
        if nested:
            report.add(file.tokens[paren].line, "nested_then_chain",
                       "`.then(…)` nested inside another `.then(…)`",
                       "Rewrite as `await`. Nested continuations reintroduce the callback pyramid "
                       "the promise API was meant to remove, and error paths get missed at each level.",
                       "medium")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_array_callbacks(file, report)
    _check_promise_executor(file, report)
    _check_floating_promises(file, report)
    _check_swallowed_rejections(file, report)
    _check_await_in_loop(file, report)
    _check_async_without_await(file, report)
    _check_then_chains(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find promise bugs: floating promises, async callbacks that never wait, swallowed rejections",
        "No async/promise problems found!",
        analyze,
    )
