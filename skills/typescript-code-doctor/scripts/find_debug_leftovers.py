#!/usr/bin/env python3
"""
Find debugging aids that were never taken back out: `debugger`, console noise,
`alert`, and blanket lint suppressions.

A `debugger` statement in shipped code freezes the tab for anyone with devtools
open. A blanket `eslint-disable` at the top of a file silently exempts every
rule from every future line of that file, which is the most expensive line in
most repositories.
"""

import re

from common import Reporter, is_test_file, run_file_detector
from tsparse import TsFile, iter_calls

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


def _check_dialogs(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        if callee in BROWSER_DIALOGS or callee.startswith("window.") and callee.split(".")[-1] in BROWSER_DIALOGS:
            report.add(file.tokens[paren].line, "browser_dialog",
                       f"`{callee}(…)` blocks the whole page",
                       "Use the application's own dialog. `alert`/`confirm` freeze the event loop "
                       "and are unstyleable and untestable.", "medium")


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
