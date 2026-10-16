#!/usr/bin/env python3
"""
Find error handling that loses information: swallowed catches, thrown
non-Errors, rethrows without a cause, and `finally` blocks that eat exceptions.

The failure mode these share is that the program keeps going and reports
success. A crash is a bug report; a swallowed error is a wrong answer.
"""

import re

from common import Reporter, run_file_detector
from tsparse import TsFile, argument_spans, iter_calls

# Calls that count as "handled" inside a catch: the error goes somewhere.
REPORTING_CALLS = frozenset({"error", "warn", "captureException", "report", "logError", "fatal"})
# Comment delimiters, so what is left is the comment's prose.
COMMENT_MARKS = re.compile(r"^/[/*]+|\*+/$")
# Comments that mark a handler as not written yet, rather than explain why there is none.
UNFINISHED = re.compile(r"(?:todo|fixme|xxx|hack)\b|(?:handle|process)\s+(?:the\s+)?(?:error|exception)s?\W*$",
                        re.IGNORECASE)
_ASSIGNMENTS = frozenset({"=", "+=", "-=", "*=", "/=", "||=", "&&=", "??="})
# A React state setter: what the catch puts there is what the user sees next.
_STATE_SETTER = re.compile(r"set[A-Z]")


def _substitutions(template: str) -> str:
    """The `${…}` interiors of a template literal, joined."""
    parts, depth, start = [], 0, 0
    index = 0
    while index < len(template):
        char = template[index]
        if char == "\\":
            index += 2
            continue
        if depth == 0 and template.startswith("${", index):
            depth, start = 1, index + 2
            index += 2
            continue
        if depth:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    parts.append(template[start:index])
        index += 1
    return " ".join(parts)


def _mentions(file: TsFile, start: int, stop: int, name: str) -> bool:
    """True when ``name`` is used in [start, stop), template substitutions included."""
    pattern = re.compile(rf"(?<![\w$.]){re.escape(name)}(?![\w$])")
    for index in range(start, stop):
        token = file.tokens[index]
        if token.kind == "name" and token.value == name and not file.tokens[index - 1].is_op(".", "?."):
            return True
        if token.kind == "template" and pattern.search(_substitutions(token.value)):
            return True
    return False


def _records_the_failure(file: TsFile, binding: str, body_open: int, body_close: int) -> bool:
    """A catch that writes the failure down somewhere is handling it.

    `catch (err) { msg = `failed: ${errorText(err)}` }` shows the error;
    `catch { undiscoverable = true }`, `catch { next = null }` and
    `catch { failed += 1 }` set a flag, a sentinel or a count that the code
    after the `try` reads; `catch { setError(…) }` puts it in state the user
    sees; `catch { this.failed = true }` records it on an object. None of
    these is the failure disappearing — that is a catch whose writes, if any,
    nothing ever reads.
    """
    if binding and _mentions(file, body_open + 1, body_close, binding):
        return True
    owner = file.enclosing_function(body_open)
    scope_end = owner.body_close if owner is not None else len(file)
    for index in range(body_open + 1, body_close):
        token = file.tokens[index]
        following = file.tokens[index + 1] if index + 1 < body_close else None
        if token.kind != "name" or following is None:
            continue
        if following.is_op("(") and _STATE_SETTER.match(token.value):
            return True
        assigned = following.kind == "op" and (following.value in _ASSIGNMENTS
                                               or following.value in ("++", "--"))
        if not assigned:
            continue
        if file.tokens[index - 1].is_op(".", "?."):
            return True  # a member write: state on an object that outlives the catch
        if _mentions(file, body_close + 1, scope_end, token.value):
            return True  # a local the code after the `try` reads
    return False


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


def _explains_itself(file: TsFile, body_open: int, body_close: int) -> bool:
    """True when an otherwise empty block holds a comment saying why it is empty.

    `catch { /* no presets is a normal state */ }` is exactly what the
    empty-catch fix asks for. A bare `// TODO` or `/* handle error */` marks the
    handler as missing rather than explaining its absence, so it does not count.
    """
    for comment in file.comments_between(body_open, body_close):
        text = COMMENT_MARKS.sub("", comment.value).strip(" \t\r\n*")
        if text and not UNFINISHED.match(text):
            return True
    return False


def _check_catch_bodies(file: TsFile, report: Reporter) -> None:
    for index, binding, body_open, body_close in _catch_clauses(file):
        line = file.tokens[index].line
        body = list(range(body_open + 1, body_close))
        if not body:
            if _explains_itself(file, body_open, body_close):
                continue  # the comment is the decision, written down: nothing was swallowed silently
            report.add(line, "empty_catch",
                       "`catch` block is empty — the error is discarded and the program continues",
                       "Handle it or let it propagate. If ignoring is genuinely right, say why in a "
                       "comment so a reader knows it was a decision.", "high")
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
        elif not rethrows and not returns and not reports and len(body) <= 4 \
                and not _records_the_failure(file, binding, body_open, body_close):
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
