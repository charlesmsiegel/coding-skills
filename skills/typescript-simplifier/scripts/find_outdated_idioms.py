#!/usr/bin/env python3
"""
Find dated TypeScript and JavaScript: CommonJS in an ESM file, `namespace`,
angle-bracket casts, `arguments`, `let` that never changes, and the pre-2020
spellings of optional chaining and nullish coalescing.

Modernising is not fashion. `?.` and `??` remove whole classes of `undefined`
bug, `const` documents intent the compiler can check, and `namespace` breaks
every bundler's tree shaking.
"""

from common import Reporter, run_file_detector
from tsparse import TsFile, argument_spans, iter_calls

ASSIGN_OPS = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "**=", "||=", "&&=", "??=", "++", "--"})


def _check_module_system(file: TsFile, report: Reporter) -> None:
    for record in file.imports:
        if record.kind == "require":
            report.add(record.line, "commonjs_require",
                       f"`require('{record.module}')` in a TypeScript module",
                       "Use `import`. `require` gives you `any` unless the package ships types the "
                       "old way, and it blocks static analysis, tree shaking and `import type`.",
                       "medium")
    for index, token in enumerate(file.tokens):
        if token.is_name("module") and index + 2 < len(file) \
                and file.tokens[index + 1].is_op(".") and file.tokens[index + 2].is_name("exports"):
            report.add(token.line, "commonjs_exports",
                       "`module.exports` in a TypeScript module",
                       "Use `export`. Mixing the two module systems in one file is the source of "
                       "most `undefined is not a function` on import.", "medium")
        elif token.is_name("namespace", "module") and index + 2 < len(file) \
                and file.tokens[index + 1].kind == "name" and file.tokens[index + 2].is_op("{"):
            if token.is_name("module") and not file.tokens[index + 1].kind == "name":
                continue
            report.add(token.line, "typescript_namespace",
                       f"`{token.value} {file.tokens[index + 1].value}` — the pre-ESM module system",
                       "Use an ES module: one file, named exports. Namespaces merge globally, defeat "
                       "tree shaking, and are rejected by `isolatedModules`.", "medium")


def _check_angle_cast(file: TsFile, report: Reporter) -> None:
    """`<Foo>value` — ambiguous with JSX and deprecated in every style guide."""
    if file.is_tsx:
        return  # the syntax is not even legal there, so nothing to find
    for index, token in enumerate(file.tokens):
        if not token.is_op("<") or index + 2 >= len(file):
            continue
        name, closing = file.tokens[index + 1], file.tokens[index + 2]
        if name.kind != "name" or not closing.is_op(">"):
            continue
        previous = file.tokens[index - 1] if index else None
        if previous is None or not (previous.is_op("=", "(", ",", "[", "=>", ":") or previous.is_name("return")):
            continue
        following = file.tokens[index + 3] if index + 3 < len(file) else None
        if following is None or following.kind not in ("name", "str", "num") and not following.is_op("("):
            continue
        report.add(token.line, "angle_bracket_cast",
                   f"`<{name.value}>value` cast",
                   f"Write `value as {name.value}`. The angle form is unusable in .tsx and reads as a "
                   "generic argument everywhere else.", "medium")


def _check_arguments_object(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("arguments") or (index and file.tokens[index - 1].is_op(".", "?.")):
            continue
        following = file.tokens[index + 1] if index + 1 < len(file) else None
        if following is not None and following.is_op(":"):
            continue
        report.add(token.line, "arguments_object",
                   "`arguments` — array-like, untyped, and unavailable in arrow functions",
                   "Use a rest parameter: `(...args: string[])`. It is a real array and the compiler "
                   "knows its type.", "medium")


def _check_const_candidates(file: TsFile, report: Reporter) -> None:
    """`let` bindings that are never reassigned."""
    for index, token in enumerate(file.tokens):
        if not token.is_name("let"):
            continue
        name_token = file.tokens[index + 1] if index + 1 < len(file) else None
        if name_token is None or name_token.kind != "name":
            continue
        name = name_token.value
        reassigned = False
        for probe in range(index + 2, len(file)):
            candidate = file.tokens[probe]
            if candidate.kind != "name" or candidate.value != name:
                continue
            if probe and file.tokens[probe - 1].is_op(".", "?."):
                continue
            following = file.tokens[probe + 1] if probe + 1 < len(file) else None
            if following is not None and following.kind == "op" and following.value in ASSIGN_OPS:
                reassigned = True
                break
            if probe and file.tokens[probe - 1].is_op("++", "--"):
                reassigned = True
                break
        if not reassigned:
            report.add(token.line, "let_never_reassigned",
                       f"`let {name}` is never reassigned",
                       "Use `const`. It tells the reader the binding is settled and makes an "
                       "accidental later write a compile error.", "low")


def _check_optional_chaining(file: TsFile, report: Reporter) -> None:
    """`x && x.y` and `x !== null && x.y` predate `?.`."""
    for index, token in enumerate(file.tokens):
        if not token.is_op("&&") or index == 0 or index + 2 >= len(file):
            continue
        left = file.tokens[index - 1]
        right, dot = file.tokens[index + 1], file.tokens[index + 2]
        if left.kind != "name" or right.kind != "name" or not dot.is_op(".", "["):
            continue
        if left.value != right.value:
            continue
        report.add(token.line, "manual_optional_chaining",
                   f"`{left.value} && {left.value}.…` is what `?.` is for",
                   f"Write `{left.value}?.…`. The manual form also treats `0` and `''` as absent, "
                   "which `?.` does not.", "low")


def _check_nullish_default(file: TsFile, report: Reporter) -> None:
    """`x || 0` and `x || ''` silently replace legitimate falsy values."""
    for index, token in enumerate(file.tokens):
        if not token.is_op("||") or index + 1 >= len(file):
            continue
        default = file.tokens[index + 1]
        risky = (default.kind == "num" and default.value in ("0", "0.0")) \
            or (default.kind == "str" and default.value.strip("'\"") == "") \
            or default.is_name("false")
        if not risky:
            continue
        report.add(token.line, "falsy_default_with_or",
                   f"`… || {default.value}` treats `0`, `''` and `false` as missing",
                   "Use `??` when you mean 'null or undefined'. `||` is right only when every falsy "
                   "value really should take the default.", "medium")


def _check_legacy_calls(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        method = callee.rsplit(".", 1)[-1]
        line = file.tokens[paren].line
        if method == "hasOwnProperty" and "." in callee:
            report.add(line, "legacy_has_own_property",
                       f"`{callee}(…)` breaks on objects created with `Object.create(null)`",
                       "Use `Object.hasOwn(obj, key)` (ES2022) or `Object.prototype.hasOwnProperty.call`.",
                       "low")
        elif method == "indexOf" and "." in callee:
            close = file.closer(paren)
            following = file.tokens[close + 1] if 0 < close + 1 < len(file) else None
            if following is not None and following.is_op("!==", "===", ">", "<", "!=", "=="):
                report.add(line, "indexof_membership_test",
                           f"`{callee}(…) {following.value} …` as a membership test",
                           "Use `.includes(x)`. It says what it means and handles `NaN` correctly.",
                           "low")
        elif callee == "Array.prototype.slice.call" or callee.endswith("slice.call"):
            report.add(line, "array_from_workaround",
                       f"`{callee}(…)` to convert an array-like",
                       "Use `Array.from(x)` or `[...x]`.", "low")
        elif method == "apply" and "." in callee:
            spans = argument_spans(file, paren)
            if len(spans) == 2 and file.tokens[spans[0][0]].is_name("null", "undefined"):
                report.add(line, "apply_instead_of_spread",
                           f"`{callee}(null, args)` to spread an array",
                           "Use `fn(...args)`. Spread keeps `this` explicit and stays typed.", "low")
        elif callee == "JSON.parse":
            spans = argument_spans(file, paren)
            if spans and file.slice(*spans[0]).startswith("JSON.stringify"):
                report.add(line, "json_clone",
                           "`JSON.parse(JSON.stringify(x))` as a deep clone",
                           "Use `structuredClone(x)`. The JSON round trip drops `undefined`, "
                           "`Date`, `Map`, `Set` and functions, and throws on cycles.", "low")
        elif callee.endswith("getTime") and "new Date()" in file.snippet(line):
            report.add(line, "date_gettime_for_now",
                       "`new Date().getTime()` allocates a Date to read the clock",
                       "Use `Date.now()`.", "low")


def _check_react_fc(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if token.is_name("FC", "FunctionComponent") and index and file.tokens[index - 1].is_op(".", ":"):
            report.add(token.line, "react_fc_annotation",
                       f"`React.{token.value}` annotation",
                       "Type the props parameter and let the return type infer. `FC` adds an implicit "
                       "`children` in older typings and blocks generic components.", "low")


def _check_array_type_style(file: TsFile, report: Reporter) -> None:
    """`Array<T>` and `T[]` mixed in one file is a coin flip per line."""
    generic = [t.line for i, t in enumerate(file.tokens)
               if t.is_name("Array") and i + 1 < len(file) and file.tokens[i + 1].is_op("<")]
    shorthand = [t.line for i, t in enumerate(file.tokens)
                 if t.is_op("[") and file.closer(i) == i + 1 and i and file.tokens[i - 1].kind == "name"
                 and file.in_type_position(i)]
    if generic and shorthand and len(generic) >= 2 and len(shorthand) >= 2:
        report.add(generic[0], "mixed_array_type_style",
                   f"Both `Array<T>` ({len(generic)}×) and `T[]` ({len(shorthand)}×) are used in this file",
                   "Pick one and apply it everywhere — `T[]` is the common default. Consistency here "
                   "is worth more than either choice.", "low",
                   related=shorthand[:5])


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_module_system(file, report)
    _check_angle_cast(file, report)
    _check_arguments_object(file, report)
    _check_const_candidates(file, report)
    _check_optional_chaining(file, report)
    _check_nullish_default(file, report)
    _check_legacy_calls(file, report)
    _check_react_fc(file, report)
    _check_array_type_style(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find dated idioms: CommonJS, namespace, angle casts, arguments, manual optional chaining",
        "No outdated idioms found!",
        analyze,
    )
