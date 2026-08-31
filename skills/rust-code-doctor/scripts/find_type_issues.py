#!/usr/bin/env python3
"""
Find places where the type system was talked out of doing its job.

Rust's escape hatches are quieter than most languages'. There is no `any`; what
there is instead is `as`, which silently truncates between integer widths and
saturates from floats, and a handful of type shapes that admit states the
program does not have — `Option<Option<T>>`, a struct where every field is
`Option`, a `Vec<String>` standing in for a parsed thing.

The `as` checks are the valuable half: a narrowing cast is a data-loss bug that
compiles, and `try_into()` turns it into a `Result` at the same call site.
"""

import re

from common import Reporter, run_file_detector
from rsparse import PRIMITIVES, RsFile

# Integer widths, for deciding whether an `as` cast can lose data.
_WIDTH = {"i8": 8, "u8": 8, "i16": 16, "u16": 16, "i32": 32, "u32": 32,
          "i64": 64, "u64": 64, "i128": 128, "u128": 128, "isize": 64, "usize": 64}
_SIGNED = {"i8", "i16", "i32", "i64", "i128", "isize"}
_FLOATS = {"f32", "f64"}


def _check_numeric_casts(file: RsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("as"):
            continue
        target = file.tok(index + 1)
        if target is None or target.kind != "name" or target.value not in PRIMITIVES:
            continue
        source = _source_type(file, index)
        line = token.line
        if target.value in _FLOATS:
            if source in _WIDTH and _WIDTH[source] >= 32 and target.value == "f32":
                report.add(line, "lossy_int_to_float_cast",
                           f"`{source} as f32` silently loses precision above 2^24",
                           "Use `f64`, or accept the loss explicitly with a comment saying the "
                           "range is bounded.", "medium")
            continue
        if source in _FLOATS:
            report.add(line, "float_to_int_cast",
                       f"`{source} as {target.value}` truncates towards zero and *saturates* at "
                       "the bounds — a NaN becomes 0",
                       "Round deliberately (`.round()`, `.floor()`) and convert with a checked "
                       "path, or use `num_traits`/`try_into` so the out-of-range case is a value "
                       "you handle rather than a clamp you never see.", "high")
            continue
        if not source and target.value in ("u8", "i8", "u16", "i16"):
            previous = file.tok(index - 1)
            if previous is not None and previous.kind == "name":
                report.add(line, "narrowing_cast_candidate",
                           f"`{previous.value} as {target.value}` — this scan could not resolve "
                           f"`{previous.value}`'s type, and a cast to {target.value} truncates "
                           "anything wider with no check",
                           f"Check the source type. If it can exceed {target.value}'s range, "
                           f"`{target.value}::try_from(value)` makes the overflow a `Result` "
                           "instead of a silently wrong number.", "low")
            continue
        if source in _WIDTH and target.value in _WIDTH:
            narrowing = _WIDTH[source] > _WIDTH[target.value]
            sign_change = (source in _SIGNED) != (target.value in _SIGNED)
            if narrowing:
                report.add(line, "narrowing_cast",
                           f"`{source} as {target.value}` drops the high bits with no check",
                           f"`{target.value}::try_from(value)?` — the failure becomes a `Result` "
                           "you handle instead of a wrong number that keeps flowing.", "high")
            elif sign_change:
                report.add(line, "sign_changing_cast",
                           f"`{source} as {target.value}` reinterprets the sign bit",
                           f"`{target.value}::try_from(value)` — a negative value becomes a huge "
                           "positive one here, which is how a length check gets bypassed.",
                           "medium")


def _source_type(file: RsFile, as_index: int) -> str:
    """A best guess at what is being cast: a literal suffix or an annotated local."""
    previous = file.tok(as_index - 1)
    if previous is None:
        return ""
    if previous.kind == "num":
        match = re.search(r"(i8|i16|i32|i64|i128|isize|u8|u16|u32|u64|u128|usize|f32|f64)$",
                          previous.value)
        return match.group(1) if match else ""
    if previous.kind != "name":
        return ""
    name = previous.value
    for index, token in enumerate(file.tokens[:as_index]):
        if not token.is_name("let"):
            continue
        cursor = index + 1
        if file.value(cursor) == "mut":
            cursor += 1
        if file.value(cursor) != name or file.value(cursor + 1) != ":":
            continue
        annotation = file.tok(cursor + 2)
        if annotation is not None and annotation.kind == "name" and annotation.value in PRIMITIVES:
            return annotation.value
    for func in file.functions:
        for param in func.params:
            if param.name == name and param.type_text in PRIMITIVES:
                return param.type_text
    return ""


def _check_type_shapes(file: RsFile, report: Reporter) -> None:
    for definition in file.types:
        if definition.kind == "type" and definition.alias_target.count("<") >= 3:
            report.add(definition.line, "alias_hides_complexity",
                       f"`type {definition.name} = {definition.alias_target[:60]}…`",
                       "An alias over a deeply nested type hides the nesting without simplifying "
                       "it — every error message still shows the full type. A newtype "
                       "(`struct Foo(…)`) gives you a name *and* a place to put the methods.",
                       "low")
        if definition.kind != "struct" or len(definition.fields) < 3:
            continue
        optional = [f for f in definition.fields if f.type_text.replace(" ", "").startswith("Option<")]
        if len(optional) == len(definition.fields):
            report.add(definition.line, "all_optional_struct",
                       f"every field of `{definition.name}` is an `Option`",
                       f"The type permits 2^{len(definition.fields)} states, and the code almost "
                       "certainly has three. Split it into the states that actually occur — an "
                       "enum with a variant per state makes the impossible combinations "
                       "unrepresentable.", "medium")
        for field in definition.fields:
            if field.type_text.replace(" ", "").startswith("Option<Option<"):
                report.add(field.line, "nested_option",
                           f"`{definition.name}.{field.name}: {field.type_text}`",
                           "`Option<Option<T>>` has two different spellings of 'nothing' and "
                           "nobody remembers which is which. Use a three-variant enum with names.",
                           "medium")


def _check_stringly_typed(file: RsFile, report: Reporter) -> None:
    for definition in file.types:
        if definition.kind != "struct" or len(definition.fields) < 3:
            continue
        strings = [f for f in definition.fields
                   if f.type_text.strip() in ("String", "&str") and f.name.isidentifier()]
        if len(strings) >= 4 and len(strings) == len(definition.fields):
            report.add(definition.line, "stringly_typed_struct",
                       f"all {len(strings)} fields of `{definition.name}` are strings",
                       "Any two of them can be swapped at a call site and it still compiles. "
                       "Newtypes (`struct UserId(String)`) make the mistake a type error and cost "
                       "nothing at runtime.", "low")


def _check_visibility_breadth(file: RsFile, report: Reporter) -> None:
    for definition in file.types:
        if definition.kind != "struct" or not definition.is_public:
            continue
        public_fields = [f for f in definition.fields if f.visibility == "pub"]
        if len(public_fields) == len(definition.fields) and len(definition.fields) >= 2:
            has_methods = any(block.type_name.split("<")[0].strip() == definition.name
                              and block.trait_name is None and block.methods
                              for block in file.impls)
            if has_methods:
                report.add(definition.line, "public_fields_with_invariants",
                           f"`{definition.name}` has an inherent `impl` and every field is `pub`",
                           "Anything the methods maintain can be broken from outside by writing "
                           "a field directly. Make the fields private and expose the accessors "
                           "the invariants allow — a plain data carrier with no methods is the "
                           "case where all-`pub` is right.", "medium")


def _check_unit_error_results(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        compact = func.return_type.replace(" ", "")
        if re.match(r"^Result<[^,]*,\(\)>$", compact):
            report.add(func.line, "unit_error_result",
                       f"`{func.qualname}` returns `Result<_, ()>`",
                       "`Option` says the same thing with less ceremony, and if the failure has a "
                       "reason, an error type says what it was. `Result<_, ()>` is the worst of "
                       "both: it demands handling and carries nothing to handle.", "low")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_numeric_casts(file, report)
    _check_type_shapes(file, report)
    _check_stringly_typed(file, report)
    _check_visibility_breadth(file, report)
    _check_unit_error_results(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find casts and type shapes that give up compile-time guarantees",
        "No type-safety problems found!",
        analyze,
    )
