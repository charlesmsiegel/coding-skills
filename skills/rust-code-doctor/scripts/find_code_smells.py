#!/usr/bin/env python3
"""
Find the everyday smells: the ones that are cheap to see and cheap to fix.

Nothing here needs an architectural argument. A `#[allow]` with no reason, a
magic number nobody named, an `if` ladder that is a `match`, an integer literal
doing arithmetic that will wrap in release and panic in debug — each is a
local fix that makes the next reading easier.
"""

import re

from common import Reporter, is_test_file, run_file_detector
from rsparse import RsFile, body_indices, split_top_level

# Numbers that carry their own meaning and need no name.
_UNREMARKABLE = {"0", "1", "2", "-1", "100", "10", "0.0", "1.0", "3", "8", "16",
                 "32", "64", "255", "1024"}

# Blanket suppressions: they hide every future instance too, not just this one.
_BLANKET_ALLOWS = ("dead_code", "unused", "warnings", "clippy::all",
                   "unused_variables", "unused_imports", "clippy::pedantic")


def _check_allow_attributes(file: RsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_op("#"):
            continue
        inner = file.value(index + 1) == "!"
        bracket = index + (2 if inner else 1)
        if file.value(bracket) != "[":
            continue
        close = file.closer(bracket)
        if close < 0:
            continue
        text = file.slice(bracket + 1, close).replace(" ", "")
        if not text.startswith("allow("):
            continue
        lints = text[len("allow("):-1] if text.endswith(")") else text
        scope = "the whole crate" if inner else "this item"
        if any(lint in lints for lint in _BLANKET_ALLOWS):
            report.add(token.line, "blanket_lint_suppression",
                       f"`#{'!' if inner else ''}[allow({lints})]` silences a whole lint class for "
                       f"{scope}",
                       "Fix the instances, or narrow the suppression to the one line that needs "
                       "it with a comment saying why. A crate-level `allow` also hides every "
                       "future occurrence, which is how the lint stops being worth turning on.",
                       "medium" if inner else "low")
        elif not _reason_nearby(file, token.line):
            report.add(token.line, "unexplained_lint_suppression",
                       f"`#[allow({lints})]` with no reason given",
                       "Add a comment (or `#[expect(…)]`, which fails once the lint stops firing) "
                       "so the next reader knows whether the suppression is still needed.", "low")


def _reason_nearby(file: RsFile, line: int) -> bool:
    return any(c.value.strip("/*! ").strip() for c in file.comments_on_lines(max(1, line - 2), line))


def _check_magic_numbers(file: RsFile, report: Reporter) -> None:
    if is_test_file(file.path):
        return
    seen: dict[str, list[int]] = {}
    for index, token in enumerate(file.tokens):
        if token.kind != "num" or token.value in _UNREMARKABLE:
            continue
        if file.in_test_code(index) or file.in_macro_body(index):
            continue
        if file.top_level(index):
            continue  # a `const` initialiser is where a number belongs
        previous = file.tok(index - 1)
        if previous is not None and previous.is_op("[", ".", "::"):
            continue  # an index or a tuple field
        seen.setdefault(token.value, []).append(token.line)
    for value, lines in seen.items():
        if len(lines) < 2:
            continue
        report.add(lines[0], "repeated_magic_number",
                   f"`{value}` appears {len(lines)} times with no name",
                   f"`const SOMETHING: … = {value};` — one place to change it, and the name says "
                   "what it measures.", "low", related=lines[1:])


def _check_if_ladders(file: RsFile, report: Reporter) -> None:
    """An `else if` chain comparing the same value is a `match`."""
    for index, token in enumerate(file.tokens):
        if not token.is_name("if") or (index and file.tokens[index - 1].is_name("else")):
            continue
        subjects: list[str] = []
        cursor = index
        rungs = 0
        while cursor < len(file.tokens) and rungs < 12:
            brace = file.find_op("{", cursor + 1, min(cursor + 40, len(file.tokens)))
            if brace < 0:
                break
            condition = " ".join(file.slice(cursor + 1, brace).split())
            match = re.match(r"^([\w.]+)\s*==\s*", condition)
            if not match:
                break
            subjects.append(match.group(1))
            rungs += 1
            close = file.closer(brace)
            if close < 0:
                break
            if not (file.tok(close + 1) is not None and file.tokens[close + 1].is_name("else")
                    and file.tok(close + 2) is not None and file.tokens[close + 2].is_name("if")):
                break
            cursor = close + 2
        if rungs >= 3 and len(set(subjects)) == 1:
            report.add(token.line, "if_ladder_on_one_value",
                       f"{rungs} `else if` rungs all comparing `{subjects[0]}`",
                       f"`match {subjects[0]} {{ … }}`. On an enum the compiler then proves you "
                       "covered every case, which an `if` ladder can never do.", "medium")


def _check_shadowing_across_types(file: RsFile, report: Reporter) -> None:
    """Rebinding a name is idiomatic; doing it three times in one body is not."""
    for func in file.functions:
        span = body_indices(func)
        if not span or file.in_test_code(func.start):
            continue  # `let args = parse(…); assert_eq!(…)` repeated is a test table
        counts: dict[str, list[int]] = {}
        for index in span:
            if not file.tokens[index].is_name("let"):
                continue
            cursor = index + 1
            if file.value(cursor) == "mut":
                cursor += 1
            name = file.tok(cursor)
            if name is None or name.kind != "name" or name.value == "_":
                continue
            counts.setdefault(name.value, []).append(name.line)
        for name, lines in counts.items():
            if len(lines) >= 5:
                report.add(lines[0], "repeated_shadowing",
                           f"`{name}` is rebound {len(lines)} times inside `{func.qualname}`",
                           "One or two shadows to convert a value (`let text = text.trim();`) "
                           "are idiomatic Rust. This many means a debugger breakpoint no longer "
                           "tells you which `name` you are looking at — give the stages different "
                           "names.",
                           "low", related=lines[1:])


def _check_wildcard_matches(file: RsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("match"):
            continue
        brace = file.find_op("{", index + 1, min(index + 120, len(file.tokens)))
        if brace < 0:
            continue
        close = file.closer(brace)
        if close < 0:
            continue
        arms = split_top_level(file, brace + 1, close)
        if len(arms) < 3:
            continue
        last_start, last_end = arms[-1]
        arrow = file.find_op("=>", last_start, last_end)
        if arrow < 0:
            continue
        pattern = file.slice(last_start, arrow).strip()
        if pattern != "_":
            continue
        subject = " ".join(file.slice(index + 1, brace).split())
        report.add(file.line_of(last_start), "wildcard_match_arm",
                   f"`match {subject}` ends in a `_` arm",
                   "A wildcard turns 'a new variant was added' from a compile error into silent "
                   "wrong behaviour. Match the variants explicitly where the type is yours; keep "
                   "`_` for a genuinely open set (an integer, an `#[non_exhaustive]` enum).",
                   "low")


def _check_integer_arithmetic(file: RsFile, report: Reporter) -> None:
    """Arithmetic on a parsed length or index wraps in release and panics in debug."""
    for index, token in enumerate(file.tokens):
        if not token.is_op("-"):
            continue
        left = file.tok(index - 1)
        right = file.tok(index + 1)
        if left is None or right is None:
            continue
        if not (left.is_op(")") and _is_len_call(file, index - 1)):
            continue
        if right.kind != "num":
            continue
        report.add(token.line, "unchecked_length_subtraction",
                   f"`.len() - {right.value}` panics in debug and wraps to a huge `usize` in "
                   "release when the collection is empty",
                   "`checked_sub` / `saturating_sub`, or iterate instead: `.iter().rev().take(n)` "
                   "needs no arithmetic on the length at all.", "high")


def _is_len_call(file: RsFile, close: int) -> bool:
    opener = file.closer(close)
    return opener > 0 and file.tok(opener - 1) is not None and file.tokens[opener - 1].is_name("len")


def _check_panicking_indexing(file: RsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_op("["):
            continue
        head = file.tok(index - 1)
        if head is None or head.kind != "name" or head.value[:1].isupper():
            continue
        if file.in_test_code(index) or is_test_file(file.path):
            continue
        close = file.closer(index)
        if close < 0:
            continue
        inner = file.slice(index + 1, close).strip()
        if not inner or ".." in inner:
            continue  # `&xs[a..b]` is a slice, whose bounds check reads differently
        if not re.fullmatch(r"[A-Za-z_]\w*", inner):
            continue  # only a bare variable index; a literal or expression is not this
        func = file.enclosing_function(index)
        if func is None or not func.is_public:
            continue  # the caller supplied the data, so the bound is theirs to break
        if not func.return_type.replace(" ", "").startswith(("Result<", "Option<")):
            continue
        report.add(token.line, "panicking_index_in_fallible_fn",
                   f"`{head.value}[{inner}]` panics on an out-of-range index, inside a function "
                   "that returns a fallible type",
                   f"`{head.value}.get({inner}).ok_or(…)?` — the function is already allowed to "
                   "fail, so a missing element should be an error rather than a crash.", "medium")


def _check_needless_bool(file: RsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("if"):
            continue
        brace = file.find_op("{", index + 1, min(index + 30, len(file.tokens)))
        if brace < 0:
            continue
        close = file.closer(brace)
        if close < 0 or close - brace > 4:
            continue
        then = file.slice(brace + 1, close).strip()
        if then not in ("true", "false"):
            continue
        if not (file.tok(close + 1) is not None and file.tokens[close + 1].is_name("else")):
            continue
        else_brace = close + 2
        if file.value(else_brace) != "{":
            continue
        else_close = file.closer(else_brace)
        if else_close < 0:
            continue
        otherwise = file.slice(else_brace + 1, else_close).strip()
        if otherwise not in ("true", "false") or otherwise == then:
            continue
        condition = " ".join(file.slice(index + 1, brace).split())
        report.add(token.line, "needless_bool",
                   f"`if {condition} {{ {then} }} else {{ {otherwise} }}`",
                   f"`{condition}` (or `!({condition})`). The condition is already the answer.",
                   "low")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_allow_attributes(file, report)
    _check_magic_numbers(file, report)
    _check_if_ladders(file, report)
    _check_shadowing_across_types(file, report)
    _check_wildcard_matches(file, report)
    _check_integer_arithmetic(file, report)
    _check_panicking_indexing(file, report)
    _check_needless_bool(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find the everyday Rust code smells",
        "No code smells found!",
        analyze,
    )
