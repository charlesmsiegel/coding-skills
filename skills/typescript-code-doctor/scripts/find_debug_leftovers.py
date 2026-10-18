#!/usr/bin/env python3
"""
Find debugging aids that were never taken back out: `debugger`, console noise,
blocking dialogs nobody acts on, and blanket lint suppressions.

A `debugger` statement in shipped code freezes the tab for anyone with devtools
open. A blanket `eslint-disable` at the top of a file silently exempts every
rule from every future line of that file, which is the most expensive line in
most repositories.
"""

import re

from common import Reporter, is_test_file, run_file_detector
from tsparse import TsFile, iter_calls, statement_end, statement_start

CONSOLE_NOISE = frozenset({"log", "debug", "trace", "dir", "table", "count", "time", "timeEnd", "group"})
BROWSER_DIALOGS = frozenset({"alert", "confirm", "prompt"})

_FILE_DISABLE = re.compile(r"eslint-disable(?!-)(?:\s|$|\*)")
_LINE_DISABLE = re.compile(r"eslint-disable-(?:next-)?line\s*(?P<rules>[^*]*)")


def _check_debugger(file: TsFile, report: Reporter) -> None:
    for token in file.tokens:
        if token.is_name("debugger"):
            report.add(token.line, "debugger_statement",
                       "`debugger` left in the source",
                       "Delete it. It halts execution for every user with devtools open and does "
                       "nothing for everyone else.", "high")


def _check_console(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        if not callee.startswith("console."):
            continue
        method = callee.split(".", 1)[1]
        if method not in CONSOLE_NOISE:
            continue  # console.error / console.warn are legitimate reporting
        report.add(file.tokens[paren].line, "console_leftover",
                   f"`{callee}(…)` in shipped source",
                   "Delete it, or route it through the project's logger so it has a level and can "
                   "be switched off. Console output survives into production bundles.", "medium")


def _result_used(file: TsFile, paren: int, callee: str) -> bool:
    """True unless the call is a whole statement whose value nobody reads."""
    head = paren - 1 - 2 * callee.count(".")
    if statement_start(file, head) != head:
        return True  # `if (!confirm(…))`, `const name = prompt(…)`, `return confirm(…)`
    close = file.closer(paren)
    end = statement_end(file, head)
    return not (close + 1 == end or (close + 1 < len(file) and file.tokens[close + 1].is_op(";")))


def _check_dialogs(file: TsFile, report: Reporter) -> None:
    """Browser dialogs, by what the call is doing.

    A `confirm()` or `prompt()` whose answer gates what happens next
    (`if (!confirm("Delete?")) return`) is deliberate UI, not a leftover; that
    it blocks the page is the reason it was chosen. One whose answer is thrown
    away asks a question nobody reads — that is the leftover. A bare `alert()`
    can be either a forgotten probe or an app's way of surfacing an error, so
    it is a lead.
    """
    for paren, callee in iter_calls(file):
        method = callee.split(".")[-1]
        if not (callee in BROWSER_DIALOGS or callee.startswith("window.") and method in BROWSER_DIALOGS):
            continue
        line = file.tokens[paren].line
        if method == "alert":
            report.candidate(line, "browser_dialog",
                             f"`{callee}(…)` blocks the whole page",
                             ("it is how this app deliberately surfaces an error or a "
                              "confirmation, and it has no dialog component of its own",
                              "the code is an internal tool or prototype where a blocking "
                              "dialog is acceptable"),
                             "low")
        elif not _result_used(file, paren, callee):
            report.add(line, "browser_dialog",
                       f"`{callee}(…)` asks a question and discards the answer",
                       "Delete it, or act on the answer. A blocking dialog nobody reads is a "
                       "debugging aid left behind.", "medium")


def _check_lint_suppressions(file: TsFile, report: Reporter) -> None:
    for comment in file.comments:
        text = comment.value
        line_match = _LINE_DISABLE.search(text)
        if line_match:
            rules = line_match.group("rules").strip().strip("*/ ")
            if not rules or rules.startswith("--"):
                report.add(comment.line, "blanket_line_suppression",
                           "`eslint-disable-line` with no rule named — every rule is off for that line",
                           "Name the single rule you mean, and add a reason after `--`.", "low")
            continue
        if _FILE_DISABLE.search(text) and comment.line <= 5:
            report.add(comment.line, "file_wide_lint_suppression",
                       "File-wide `eslint-disable` — every rule is off for the whole file, forever",
                       "Scope it to the lines and rules that need it. A file-level disable also "
                       "exempts code nobody has written yet.", "medium")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_debugger(file, report)
    _check_lint_suppressions(file, report)
    if not is_test_file(file.path):
        _check_console(file, report)
        _check_dialogs(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find debugger statements, console noise and blanket lint suppressions",
        "No debug leftovers found!",
        analyze,
    )
