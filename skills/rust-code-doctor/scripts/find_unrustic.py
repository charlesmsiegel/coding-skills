#!/usr/bin/env python3
"""
Find code that works but does not read as Rust.

This is the analogue of "unpythonic": nothing here is a bug, and a compiler
will never mention any of it. The cost is that a reader has to reconstruct the
intent from a procedure — `match` arms that spell out `unwrap_or`, a
`contains_key` followed by an index, a `.len() == 0` — instead of reading it off
the name of a combinator that already exists.

The rule applied throughout is that the idiomatic form must be *shorter and say
more*, not merely be more fashionable. Where the explicit form is genuinely
clearer (a `match` with real work in both arms), this stays quiet.
"""

import re

from common import Reporter, run_file_detector
from rsparse import (
    OPENERS, RsFile, argument_spans, iter_calls, iter_method_calls, receiver_text,
    split_top_level, statement_start,
)

# A `match` arm body this short is an expression, not a block of work — which is
# what makes the combinator form strictly better.
_SIMPLE_ARM_TOKENS = 8


def _arms(file: RsFile, brace: int, close: int) -> list[tuple[int, str, str]]:
    """(index, pattern, body) for each top-level arm of a `match` block.

    Splitting on commas is not enough: an arm whose body is a block may omit the
    trailing comma, so `true => {} false => {}` is one comma-delimited span and
    two arms.
    """
    out = []
    cursor = brace + 1
    while cursor < close:
        arrow = file.find_op("=>", cursor, close)
        if arrow < 0:
            break
        pattern = file.slice(cursor, arrow).strip().lstrip(",").strip()
        body_start = arrow + 1
        if file.value(body_start) == "{":
            end = file.skip_group(body_start)
        else:
            end = body_start
            while end < close:
                token = file.tokens[end]
                if token.kind == "op":
                    if token.value in OPENERS:
                        end = file.skip_group(end)
                        continue
                    if token.value == ",":
                        break
                end += 1
        out.append((cursor, pattern, file.slice(body_start, end).strip()))
        cursor = end + 1 if file.value(end) == "," else end
        if cursor <= arrow:
            break
    return out


def _match_blocks(file: RsFile):
    """Yield (match_index, subject_text, brace, close) for every `match`."""
    for index, token in enumerate(file.tokens):
        if not token.is_name("match") or file.in_macro_body(index):
            continue
        brace = file.find_op("{", index + 1, min(index + 120, len(file.tokens)))
        if brace < 0:
            continue
        close = file.closer(brace)
        if close < 0:
            continue
        yield index, file.slice(index + 1, brace).strip(), brace, close


def _check_match_idioms(file: RsFile, report: Reporter) -> None:
    for index, subject, brace, close in _match_blocks(file):
        arms = _arms(file, brace, close)
        if len(arms) != 2:
            continue
        patterns = {pattern.split("(")[0].strip(): (pattern, body) for _, pattern, body in arms}
        line = file.tokens[index].line
        long_arm = any(len(body) > _SIMPLE_ARM_TOKENS * 8 for _, _, body in arms)

        if {"Some", "None"} <= set(patterns) and not long_arm:
            some_pattern, some_body = patterns["Some"]
            binding = some_pattern[some_pattern.find("(") + 1:some_pattern.rfind(")")].strip()
            none_body = patterns["None"][1]
            if binding and some_body == binding:
                report.add(line, "manual_unwrap_or",
                           f"`match {subject} {{ Some({binding}) => {binding}, None => … }}`",
                           f"`{subject}.unwrap_or({none_body})` — or `unwrap_or_else(…)` when the "
                           "fallback does work, `unwrap_or_default()` when it is the default.",
                           "low")
            elif none_body in ("None", "()"):
                report.add(line, "manual_option_map",
                           f"`match` on an `Option` whose `None` arm is `{none_body}`",
                           f"`{subject}.map(…)` (or `and_then` when the body returns an `Option`) "
                           "says the same thing in one line, and keeps the `None` case impossible "
                           "to get wrong.", "low")
            else:
                report.add(line, "manual_map_or",
                           "`match` on an `Option` with a one-expression arm on each side",
                           f"`{subject}.map_or(<none>, |v| <some>)` — the reader sees the two "
                           "outcomes without reconstructing them from the arms.", "low")

        if {"Ok", "Err"} <= set(patterns) and not long_arm:
            ok_pattern, ok_body = patterns["Ok"]
            binding = ok_pattern[ok_pattern.find("(") + 1:ok_pattern.rfind(")")].strip()
            err_body = patterns["Err"][1]
            if binding and ok_body == binding and "return" not in err_body:
                report.add(line, "manual_unwrap_or_result",
                           f"`match {subject} {{ Ok({binding}) => {binding}, Err(_) => … }}`",
                           f"`{subject}.unwrap_or_else(|e| …)`, or `?` when the error should "
                           "propagate.", "low")

        if {"true", "false"} <= set(patterns):
            report.add(line, "match_on_bool",
                       f"`match {subject} {{ true => …, false => … }}`",
                       f"`if {subject} {{ … }} else {{ … }}`. A `match` on two variants that "
                       "already have keywords adds ceremony, not exhaustiveness.", "low")


def _check_is_some_then_unwrap(file: RsFile, report: Reporter) -> None:
    for name_index, paren, method in iter_method_calls(file):
        if method not in ("is_some", "is_ok"):
            continue
        head = file.tokens[name_index - 2] if name_index >= 2 else None
        opener = file.find_op("{", paren, min(paren + 12, len(file.tokens)))
        if opener < 0 or head is None:
            continue
        # Only when this is the condition of an `if`, not a stored boolean.
        if not any(file.tokens[i].is_name("if") for i in range(max(0, name_index - 8), name_index)):
            continue
        close = file.closer(opener)
        if close < 0:
            continue
        subject = receiver_text(file, name_index - 1)
        for inner_name, _, inner_method in iter_method_calls(file, opener, close):
            if inner_method != "unwrap":
                continue
            if receiver_text(file, inner_name - 1) != subject:
                continue
            report.add(file.line_of(name_index), "is_some_then_unwrap",
                       f"`if {subject}.{method}()` followed by `{subject}.unwrap()` in the body",
                       f"`if let Some(value) = {subject}` binds the value once and removes the "
                       "unwrap entirely — the two-step form can be desynchronised by an edit "
                       "between them.", "medium")
            break


def _check_len_and_emptiness(file: RsFile, report: Reporter) -> None:
    for name_index, paren, method in iter_method_calls(file):
        if method not in ("len", "count"):
            continue
        close = file.closer(paren)
        if close < 0:
            continue
        operator = file.tok(close + 1)
        operand = file.tok(close + 2)
        if operator is None or operand is None or operand.kind != "num" or operand.value != "0":
            continue
        if not operator.is_op("==", "!=", ">", "<="):
            continue
        subject = receiver_text(file, name_index - 1)
        if operator.value in ("==", "<="):
            report.add(operator.line, "len_zero_instead_of_is_empty",
                       f"`{subject}.{method}() {operator.value} 0`",
                       f"`{subject}.is_empty()` — it is O(1) for every standard collection, and "
                       "for an iterator `count()` consumes the whole thing to answer a question "
                       "`next().is_none()` answers immediately.", "low")
        else:
            report.add(operator.line, "len_nonzero_instead_of_is_empty",
                       f"`{subject}.{method}() {operator.value} 0`",
                       f"`!{subject}.is_empty()`, or `.any(…)` when you are asking whether an "
                       "iterator yields anything.", "low")

    for index, token in enumerate(file.tokens):
        if not token.is_op("==", "!="):
            continue
        following = file.tok(index + 1)
        if following is not None and following.kind == "str" and following.value in ('""', "''"):
            subject = receiver_text(file, index)
            report.add(token.line, "compare_to_empty_string",
                       f'`{subject} {token.value} ""`',
                       f"`{subject}.is_empty()` — comparing to a literal allocates nothing but "
                       "says less.", "low")
        if following is not None and following.is_name("true", "false"):
            report.add(token.line, "compare_to_bool_literal",
                       f"`{token.value} {following.value}` on a value that is already a `bool`",
                       "Use the value itself (or `!value`).", "low")


def _check_combinator_chains(file: RsFile, report: Reporter) -> None:
    for name_index, paren, method in iter_method_calls(file):
        close = file.closer(paren)
        if close < 0:
            continue
        spans = argument_spans(file, paren)
        argument = file.slice(*spans[0]).strip() if spans else ""
        line = file.line_of(name_index)
        following = file.tok(close + 1)
        follower = file.tok(close + 2)

        if method in ("unwrap_or", "ok_or", "or") and argument and "(" in argument \
                and not argument.startswith(("Some", "Ok", "Err", "None")) \
                and not re.match(r"^[A-Za-z_][\w:]*$", argument):
            report.add(line, "eager_fallback_argument",
                       f"`.{method}({argument})` evaluates the fallback even on the success path",
                       f"`.{method}_else(|| {argument})` — the argument form runs the work every "
                       "time, including when it is thrown away.", "medium")

        if method == "map" and following is not None and following.is_op(".") \
                and follower is not None and follower.kind == "name":
            if follower.value == "unwrap_or":
                report.add(line, "map_then_unwrap_or",
                           "`.map(f).unwrap_or(d)`",
                           "`.map_or(d, f)` — one call, and the default is impossible to apply to "
                           "the wrong branch.", "low")
            elif follower.value == "flatten":
                report.add(line, "map_then_flatten",
                           "`.map(f).flatten()`",
                           "`.and_then(f)` for `Option`/`Result`, `.flat_map(f)` for an iterator.",
                           "low")
        if method == "filter" and following is not None and following.is_op(".") \
                and follower is not None and follower.is_name("next"):
            report.add(line, "filter_then_next",
                       "`.filter(p).next()`",
                       "`.find(p)` — same result, and it names what the chain is for.", "low")
        if method == "collect" and following is not None and following.is_op(".") \
                and follower is not None and follower.is_name("iter", "into_iter"):
            report.add(line, "collect_then_iterate",
                       "`.collect()` immediately followed by iterating the collection again",
                       "Drop the `collect()` and keep the iterator — the intermediate "
                       "allocation exists only to be walked once.", "medium")
        if method == "cloned" and following is not None and following.is_op(".") \
                and follower is not None and follower.is_name("collect"):
            subject = receiver_text(file, name_index - 1)
            if subject.endswith(".iter"):
                report.add(line, "iter_cloned_collect",
                           "`.iter().cloned().collect()` to copy a slice into a `Vec`",
                           "`.to_vec()` (or `.to_owned()`) says the intent and skips the "
                           "iterator machinery.", "low")


def _check_redundant_closures(file: RsFile, report: Reporter) -> None:
    for name_index, paren, method in iter_method_calls(file):
        if method not in ("map", "filter", "for_each", "and_then", "map_err", "inspect"):
            continue
        spans = argument_spans(file, paren)
        if not spans:
            continue
        text = " ".join(file.slice(*spans[0]).split())
        match = re.fullmatch(r"\|\s*(\w+)\s*\|\s*([\w:.]+)\s*\(\s*\1\s*\)", text)
        if match and match.group(2) not in ("Some", "Ok", "Err"):
            report.add(file.line_of(name_index), "redundant_closure",
                       f"`.{method}(|{match.group(1)}| {match.group(2)}({match.group(1)}))` wraps a "
                       "function that could be passed directly",
                       f"`.{method}({match.group(2)})`.", "low")


def _check_string_construction(file: RsFile, report: Reporter) -> None:
    for index, callee in iter_calls(file):
        line = file.line_of(index)
        spans = argument_spans(file, index)
        argument = file.slice(*spans[0]).strip() if spans else ""
        if callee in ("String::from", "String::new") and argument in ('""', "''"):
            report.add(line, "empty_string_from_literal",
                       f"`{callee}({argument})`",
                       "`String::new()` — no literal to read, and it is `const`.", "low")
        if callee == "format!" and spans and len(spans) == 2:
            template = file.slice(*spans[0]).strip()
            value = file.slice(*spans[1]).strip()
            if template in ('"{}"', '"{ }"'):
                report.add(line, "format_for_to_string",
                           f'`format!("{{}}", {value})`',
                           f"`{value}.to_string()` — same allocation, no format machinery, and it "
                           "fails to compile if the type stops being `Display`.", "low")

    for name_index, paren, method in iter_method_calls(file):
        if method in ("to_string", "to_owned"):
            previous = file.tok(name_index - 2)
            if previous is not None and previous.kind == "str" and previous.value == '""':
                report.add(file.line_of(name_index), "empty_string_from_literal",
                           f'`"".{method}()`', "`String::new()`.", "low")
        elif method == "push_str":
            spans = argument_spans(file, paren)
            argument = file.slice(*spans[0]).strip() if spans else ""
            if re.fullmatch(r'"(?:[^"\\]|\\.)"', argument):
                report.add(file.line_of(name_index), "push_str_single_char",
                           f"`.push_str({argument})` for one character",
                           f"`.push({argument.replace(chr(34), chr(39))})` — a `char` push skips "
                           "the length check and the UTF-8 copy loop.", "low")


def _check_needless_return(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.has_body or func.body_close < 0:
            continue
        # The last statement of the body ends at the `;` before the closing brace.
        semi = func.body_close - 1
        if semi <= func.body_open or not file.tokens[semi].is_op(";"):
            continue
        head = statement_start(file, semi)
        if head > func.body_open and file.tokens[head].is_name("return") and head + 1 < semi:
            report.add(file.line_of(head), "needless_return",
                       "`return` on the last expression of the function",
                       "Drop the `return` and the `;`. Rust functions evaluate to their final "
                       "expression, and mixing the two spellings in one codebase makes the "
                       "reader check which one this is.", "low")


def _check_redundant_field_names(file: RsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_op("{") or index == 0:
            continue
        head = file.tokens[index - 1]
        if head.kind != "name" or not head.value[:1].isupper():
            continue
        close = file.closer(index)
        if close < 0 or close - index > 40:
            continue
        for start, end in split_top_level(file, index + 1, close):
            if end - start != 3:
                continue
            name, colon, value = file.tokens[start], file.tokens[start + 1], file.tokens[start + 2]
            if colon.is_op(":") and name.kind == "name" and value.kind == "name" \
                    and name.value == value.value:
                report.add(name.line, "redundant_field_name",
                           f"`{name.value}: {name.value}` in a struct literal",
                           f"`{name.value}` — field shorthand. The repetition is the kind of "
                           "thing an edit desynchronises.", "low")


def _check_contains_then_index(file: RsFile, report: Reporter) -> None:
    for name_index, paren, method in iter_method_calls(file):
        if method != "contains_key":
            continue
        opener = file.find_op("{", paren, min(paren + 10, len(file.tokens)))
        if opener < 0:
            continue
        close = file.closer(opener)
        subject = receiver_text(file, name_index - 1)
        if close < 0 or not subject:
            continue
        window = file.slice(opener, close)
        if f"{subject}[" in window or f"{subject}.get(" in window:
            report.add(file.line_of(name_index), "contains_key_then_lookup",
                       f"`{subject}.contains_key(k)` followed by a second lookup in the body",
                       f"`if let Some(value) = {subject}.get(k)` hashes the key once and hands you "
                       "the value; the index form also panics if the entry disappears between the "
                       "two lines.", "medium")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_match_idioms(file, report)
    _check_is_some_then_unwrap(file, report)
    _check_len_and_emptiness(file, report)
    _check_combinator_chains(file, report)
    _check_redundant_closures(file, report)
    _check_string_construction(file, report)
    _check_needless_return(file, report)
    _check_redundant_field_names(file, report)
    _check_contains_then_index(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find working code that does not read as Rust",
        "No unidiomatic Rust found!",
        analyze,
    )
