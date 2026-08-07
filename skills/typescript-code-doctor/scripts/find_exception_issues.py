#!/usr/bin/env python3
"""
Find error handling that loses information: swallowed catches, thrown
non-Errors, rethrows without a cause, and `finally` blocks that eat exceptions.

The failure mode these share is that the program keeps going and reports
success. A crash is a bug report; a swallowed error is a wrong answer.
"""

from common import Reporter, run_file_detector
from tsparse import TsFile, argument_spans, iter_calls

# Calls that count as "handled" inside a catch: the error goes somewhere.
REPORTING_CALLS = frozenset({"error", "warn", "captureException", "report", "logError", "fatal"})


def _catch_clauses(file: TsFile):
    """Yield (catch_index, binding_name, body_open, body_close) for `catch` blocks."""
    for index, token in enumerate(file.tokens):
        if not token.is_name("catch") or (index and file.tokens[index - 1].is_op(".", "?.")):
            continue
        cursor = index + 1
        binding = ""
        if cursor < len(file) and file.tokens[cursor].is_op("("):
            inner = file.tokens[cursor + 1] if cursor + 1 < len(file) else None
            binding = inner.value if inner is not None and inner.kind == "name" else ""
            cursor = file.skip_group(cursor)
        if cursor < len(file) and file.tokens[cursor].is_op("{") and file.closer(cursor) > 0:
            yield index, binding, cursor, file.closer(cursor)


def _check_catch_bodies(file: TsFile, report: Reporter) -> None:
    for index, binding, body_open, body_close in _catch_clauses(file):
        line = file.tokens[index].line
        body = list(range(body_open + 1, body_close))
        if not body:
            has_comment = any(body_open < c.start < body_close for c in file.comments)
            report.add(line, "empty_catch",
                       "`catch` block is empty — the error is discarded and the program continues",
                       "Handle it or let it propagate. If ignoring is genuinely right, say why in a "
                       "comment and name the binding so a reader knows it was a decision."
                       if not has_comment else
                       "An explained no-op is better than a silent one, but consider whether the "
                       "narrower `try` would remove the need to ignore anything.",
                       "high" if not has_comment else "low")
            continue

        rethrows = any(file.tokens[i].is_name("throw") for i in body)
        returns = any(file.tokens[i].is_name("return") for i in body)
        reports = any(
            callee.rsplit(".", 1)[-1] in REPORTING_CALLS
            for _, callee in iter_calls(file, body_open, body_close)
        )
        logs_only = any(
            callee in ("console.log", "console.info", "console.debug", "print")
            for _, callee in iter_calls(file, body_open, body_close)
        )
        if logs_only and not rethrows and not reports:
            report.add(line, "catch_logs_and_continues",
                       "`catch` logs to the console and carries on as if nothing failed",
                       "`console.log` is not error handling. Re-throw (with context), return a "
                       "typed failure the caller must handle, or report to the error channel that "
                       "is actually watched.", "medium")
        elif not rethrows and not returns and not reports and len(body) <= 4:
            report.add(line, "swallowed_error",
                       "`catch` neither re-throws, returns, nor reports — the failure disappears",
                       "Decide what the failure means here. Silently continuing turns an exception "
                       "into a wrong value further down, where it is much harder to trace.", "high")

        _check_rethrow_cause(file, report, binding, body_open, body_close)


def _check_rethrow_cause(file: TsFile, report: Reporter, binding: str, body_open: int, body_close: int) -> None:
    """`throw new Error(msg)` inside a catch drops the original stack."""
    if not binding:
        return
    for index in range(body_open, body_close):
        token = file.tokens[index]
        if not token.is_name("throw") or index + 2 >= len(file):
            continue
        if not file.tokens[index + 1].is_name("new"):
            continue
        paren = index + 3
        if paren >= len(file) or not file.tokens[paren].is_op("("):
            continue
        close = file.closer(paren)
        if close < 0:
            continue
        mentions_cause = any(
            file.tokens[i].is_name("cause") for i in range(paren, close)
        )
        if not mentions_cause:
            report.add(token.line, "rethrow_without_cause",
                       f"Throws a new error inside a catch without `{{ cause: {binding} }}`",
                       f"`throw new Error(msg, {{ cause: {binding} }})` keeps the original stack and "
                       "message. Without it the root cause is gone from the log you will read at "
                       "3am.", "medium")


def _check_thrown_values(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("throw") or index + 1 >= len(file):
            continue
        thrown = file.tokens[index + 1]
        if thrown.kind in ("str", "template") or thrown.kind == "num":
            report.add(token.line, "throw_non_error",
                       f"Throws a {'string' if thrown.kind != 'num' else 'number'} rather than an Error",
                       "Throw an `Error` (or a subclass). A thrown primitive has no stack trace, and "
                       "`instanceof Error` checks downstream will not match it.", "medium")
        elif thrown.is_op("{"):
            report.add(token.line, "throw_non_error",
                       "Throws an object literal rather than an Error",
                       "Subclass `Error` so the value carries a stack and survives the `instanceof` "
                       "checks that error handlers use.", "medium")


def _check_finally(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("finally") or (index and file.tokens[index - 1].is_op(".", "?.")):
            continue
        block = index + 1
        if block >= len(file) or not file.tokens[block].is_op("{"):
            continue
        close = file.closer(block)
        if close < 0:
            continue
        for probe in range(block, close):
            if file.tokens[probe].is_name("return", "throw", "break", "continue") \
                    and file.enclosing_function(probe) is file.enclosing_function(block):
                report.add(file.tokens[probe].line, "control_flow_in_finally",
                           f"`{file.tokens[probe].value}` inside `finally` discards any in-flight exception",
                           "Move it out. A `finally` that returns overrides the exception the block "
                           "was propagating, and the failure vanishes with no trace at all.", "high")
                break


def _check_reject_values(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        if callee.rsplit(".", 1)[-1] != "reject":
            continue
        spans = argument_spans(file, paren)
        if len(spans) != 1:
            continue
        first = file.tokens[spans[0][0]]
        if first.kind in ("str", "template", "num"):
            report.add(first.line, "reject_non_error",
                       f"`{callee}(…)` rejects with a {'string' if first.kind != 'num' else 'number'}",
                       "Reject with an `Error`. The rejection reaches a `catch` where the handler "
                       "expects a stack, and a bare string leaves no way to find the origin.",
                       "medium")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_catch_bodies(file, report)
    _check_thrown_values(file, report)
    _check_finally(file, report)
    _check_reject_values(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find error handling that loses information",
        "No exception-handling problems found!",
        analyze,
    )
