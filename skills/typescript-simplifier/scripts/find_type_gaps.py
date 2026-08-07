#!/usr/bin/env python3
"""
Find the places where TypeScript has been told to stop checking.

`any`, `as`, `!` and `@ts-ignore` are all escape hatches, and a codebase full of
them is a JavaScript codebase paying a compiler tax. These are the findings that
distinguish "typed" from "type-safe", and none of them need type information to
spot — the escape hatch is written down in the source.
"""

import re

from common import Reporter, is_test_file, run_file_detector
from tsparse import RESERVED, TsFile

# Types that accept more than the author means. `Function` accepts any callable
# and returns `any`; the boxed wrappers are not the primitives; `{}` means
# "anything except null/undefined", which is nearly everything.
UNSAFE_BUILTIN_TYPES = {
    "Function": "accepts any callable and returns `any` — declare the signature you mean, e.g. `(x: string) => void`",
    "Object": "is the boxed wrapper, not a plain object — use `object`, `Record<string, unknown>`, or a real interface",
    "String": "is the boxed wrapper, not the `string` primitive — use `string`",
    "Number": "is the boxed wrapper, not the `number` primitive — use `number`",
    "Boolean": "is the boxed wrapper, not the `boolean` primitive — use `boolean`",
    "Symbol": "is the boxed wrapper — use `symbol`",
}

# Positions where the token before a name means the name is being used as a type.
_TYPE_LEAD = {":", "<", ",", "|", "&", "(", "[", "=>", "extends", "as", "satisfies", "keyof", "readonly"}

_TS_DIRECTIVE = re.compile(r"@ts-(ignore|expect-error|nocheck)\b(.*)")


def _is_component(func) -> bool:
    """React components infer their return type; requiring one is noise."""
    return bool(func.name) and func.name[0].isupper()


def _check_any(file: TsFile, report: Reporter, test: bool) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("any"):
            continue
        previous = file.tokens[index - 1] if index else None
        if previous is None:
            continue
        if previous.is_name("as"):
            report.add(token.line, "as_any",
                       "`as any` switches off checking for this expression entirely",
                       "Narrow with a type guard, or assert to the *specific* type you know it is; "
                       "if the value is genuinely unknown, type it `unknown` and narrow.",
                       "low" if test else "high")
            continue
        if not (previous.kind == "op" and previous.value in _TYPE_LEAD) and not previous.is_name("as", "extends", "keyof"):
            continue
        following = file.tokens[index + 1] if index + 1 < len(file) else None
        is_index_signature = previous.is_op(":") and index >= 2 and file.tokens[index - 2].is_op("]")
        if is_index_signature:
            report.add(token.line, "any_index_signature",
                       "Index signature typed `any` — every property access on this type is unchecked",
                       "Use `unknown` and narrow at the read site, or enumerate the keys "
                       "(`Record<'a' | 'b', T>`).", "high")
            continue
        container = "" if following is None else following.value
        report.add(token.line, "explicit_any",
                   f"Explicit `any`{' in a generic argument' if container == '>' else ''} — "
                   "this value and everything derived from it is unchecked",
                   "Prefer `unknown` plus narrowing when the shape is genuinely unknown; "
                   "otherwise write the real type. `any` propagates silently through calls.",
                   "low" if test else ("high" if file.top_level(index) else "medium"))


def _check_assertions(file: TsFile, report: Reporter, test: bool) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("as"):
            continue
        nxt = file.tokens[index + 1] if index + 1 < len(file) else None
        if nxt is None or nxt.is_name("any"):
            continue  # `as any` already reported, and more severely
        if nxt.is_name("const"):
            continue  # `as const` is the good one: it narrows rather than widens
        if nxt.is_name("unknown") and index + 2 < len(file) and file.tokens[index + 2].is_name("as"):
            report.add(token.line, "double_assertion",
                       "`as unknown as T` — a double assertion, the sanctioned way to lie to the compiler",
                       "This says the two types have nothing in common and you want it anyway. "
                       "Validate at the boundary (a parser or type guard) and return the real type.",
                       "low" if test else "high")
            continue
        if test:
            # Casting a mock into place is how the testing libraries are used;
            # reporting it says nothing about the production types.
            continue
        if _in_type_guard(file, index):
            # `(value as Record<string, unknown>).id` inside a `value is T`
            # predicate is the sanctioned narrowing idiom — the assertion is
            # what the function exists to justify.
            continue
        report.add(token.line, "type_assertion",
                   f"Type assertion `as {nxt.value}` — asserted, not checked",
                   "An assertion moves a compile-time error to runtime. Prefer a type guard, "
                   "a discriminated union, or `satisfies` when you only want the literal checked.", "low")


def _in_type_guard(file: TsFile, index: int) -> bool:
    """True when the token sits in a function declared `(x): x is T`."""
    func = file.enclosing_function(index)
    return func is not None and " is " in f" {func.return_type} "


def _check_non_null(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_op("!") or index == 0:
            continue
        previous = file.tokens[index - 1]
        postfix = previous.kind in ("name", "num", "str", "template") or previous.is_op(")", "]")
        if not postfix or previous.is_name(*RESERVED - {"this", "super", "null", "undefined"}):
            continue
        report.add(token.line, "non_null_assertion",
                   "Non-null assertion `!` — claims a value is present without checking",
                   "If it can be absent, handle it (`?.`, a guard, a default). If it truly cannot, "
                   "make the type say so rather than overriding it at every use site.", "medium")


def _check_directives(file: TsFile, report: Reporter) -> None:
    for comment in file.comments:
        match = _TS_DIRECTIVE.search(comment.value)
        if not match:
            continue
        kind, tail = match.group(1), match.group(2).strip(" -:*/")
        if kind == "nocheck":
            report.add(comment.line, "ts_nocheck",
                       "`@ts-nocheck` disables type checking for the whole file",
                       "Fix or narrow the errors; a file-wide opt-out hides every future error too.", "high")
        elif kind == "ignore":
            report.add(comment.line, "ts_ignore",
                       "`@ts-ignore` silences the next line forever, including errors that appear later",
                       "Use `@ts-expect-error` with a reason instead: it fails the build once the "
                       "underlying error is fixed, so the suppression cannot outlive its cause.", "medium")
        elif not tail:
            report.add(comment.line, "unexplained_suppression",
                       "`@ts-expect-error` with no explanation",
                       "Say what the error is and why suppressing it is right — a reviewer cannot "
                       "otherwise tell a known compiler gap from a real bug.", "low")


def _check_builtin_types(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if token.kind != "name" or token.value not in UNSAFE_BUILTIN_TYPES:
            continue
        following = file.tokens[index + 1] if index + 1 < len(file) else None
        if following is not None and following.is_op("(", ".", "?."):
            continue  # `String(x)` / `Object.keys` are values, not type positions
        previous = file.tokens[index - 1] if index else None
        if previous is None:
            continue
        lead = (previous.kind == "op" and previous.value in _TYPE_LEAD) \
            or previous.is_name("as", "extends", "satisfies", "keyof")
        if not lead or not file.in_type_position(index):
            continue
        report.add(token.line, "unsafe_builtin_type",
                   f"`{token.value}` as a type — it {UNSAFE_BUILTIN_TYPES[token.value]}",
                   f"Replace `{token.value}` with the precise type.", "medium")


def _check_empty_object_type(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_op("{") or file.closer(index) != index + 1:
            continue
        previous = file.tokens[index - 1] if index else None
        if previous is None or not (previous.is_op(":", "<", "|", "&", "=") or previous.is_name("extends")):
            continue
        # `x ? { a } : {}` and `const x = {}` are empty object *values*; only a
        # recorded annotation span makes this the `{}` type.
        if not file.in_type_position(index):
            continue
        report.add(token.line, "empty_object_type",
                   "`{}` as a type means 'anything except null and undefined', not 'an empty object'",
                   "Use `object`, `Record<string, never>`, or the interface you actually mean.", "medium")


def _check_signatures(file: TsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.is_exported or func.kind in ("constructor", "setter"):
            continue
        for param in func.params:
            if not param.type_text and not param.has_default and not param.is_destructured:
                report.add(param.line, "untyped_parameter",
                           f"Exported {func.kind} `{func.qualname}` takes `{param.name}` with no type",
                           "Annotate it. Without `noImplicitAny` this is silently `any`; with it, "
                           "the build breaks for every caller instead of here.", "medium")
        if not func.return_type and not _is_component(func) and func.has_body:
            report.add(func.line, "missing_return_type",
                       f"Exported {func.kind} `{func.qualname}` has no declared return type",
                       "Declare it. An inferred return type is a contract nobody wrote down, and it "
                       "changes silently when the body changes.", "low")


def _check_catch_clauses(file: TsFile, report: Reporter) -> None:
    """`catch (e: any)` exactly — not `.catch(cb)`, which is a method call."""
    for index, token in enumerate(file.tokens):
        if not token.is_name("catch") or index + 4 >= len(file):
            continue
        if index and file.tokens[index - 1].is_op(".", "?."):
            continue
        if not file.tokens[index + 1].is_op("("):
            continue
        binding, colon, annotation = file.tokens[index + 2], file.tokens[index + 3], file.tokens[index + 4]
        if binding.kind == "name" and colon.is_op(":") and annotation.is_name("any"):
            report.add(token.line, "catch_typed_any",
                       f"`catch ({binding.value}: any)` — the caught value is unchecked",
                       "Type it `unknown` and narrow (`e instanceof Error`). A thrown value can be "
                       "anything, so `any` here hides exactly the case that surprises you.", "medium")


def _check_optional_soup(file: TsFile, report: Reporter) -> None:
    for decl in file.types:
        members = [m for m in decl.members if m.name != "[index]"]
        if len(members) < 4:
            continue
        optional = [m for m in members if m.optional]
        if len(optional) / len(members) >= 0.8:
            report.add(decl.line, "all_optional_type",
                       f"`{decl.name}` has {len(optional)} of {len(members)} members optional — "
                       "the type permits an empty object",
                       "Split it into the states that actually occur (a discriminated union), or make "
                       "the required fields required. A type where everything is optional checks nothing.",
                       "medium")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    # A test file casts mocks into place on purpose. The escape hatches are
    # still worth listing, but they are not the same claim about the product.
    test = is_test_file(file.path)
    _check_any(file, report, test)
    _check_assertions(file, report, test)
    _check_non_null(file, report)
    _check_directives(file, report)
    _check_builtin_types(file, report)
    _check_empty_object_type(file, report)
    _check_catch_clauses(file, report)
    _check_optional_soup(file, report)
    if not is_test_file(file.path):
        _check_signatures(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find type-safety escape hatches: any, assertions, non-null, @ts-ignore",
        "No type-safety escape hatches found!",
        analyze,
    )
