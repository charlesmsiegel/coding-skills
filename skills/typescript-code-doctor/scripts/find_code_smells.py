#!/usr/bin/env python3
"""
Detect the everyday smells: magic numbers, `==`, `var`, nested ternaries,
switches with no default, god classes, and returns that disagree with themselves.
"""

from common import Reporter, is_test_file, run_file_detector
from tsparse import TsFile, argument_spans, body_indices, callee_of, callback_target, iter_calls

# Values so conventional a name would add nothing.
MAGIC_NUMBER_ALLOWED = {0, 1, 2, -1, 10, 100, 1000, 24, 60, 365, 12, 7,
                        16, 24, 32, 64, 128, 180, 255, 256, 360, 512, 1024}

GOD_CLASS_METHODS, GOD_CLASS_MEMBERS = 15, 10

# React hooks whose first argument is an effect: it returns nothing, or a cleanup.
EFFECT_HOOKS = frozenset({"useEffect", "useLayoutEffect", "useInsertionEffect"})


def _check_magic_numbers(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if token.kind != "num":
            continue
        try:
            value = float(token.value.replace("_", "").rstrip("n"))
        except ValueError:
            continue
        if value in MAGIC_NUMBER_ALLOWED or abs(value) > 10_000_000:
            continue
        previous = file.tokens[index - 1] if index else None
        following = file.tokens[index + 1] if index + 1 < len(file) else None
        if previous is None:
            continue
        # A literal that already has a name, a key, or a position is not magic.
        if previous.is_op("=", ":", ",", "[", "(", "-") or previous.is_name("case", "return"):
            continue
        if following is not None and following.is_op(",", "]", "}", ")"):
            continue
        report.add(token.line, "magic_number",
                   f"Magic number {token.value} in an expression",
                   "Name it: `const RETRY_LIMIT = 3`. A named constant is greppable and says why "
                   "the value is what it is.", "low")


def _check_loose_equality(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_op("==", "!="):
            continue
        neighbours = (file.tokens[index - 1] if index else None,
                      file.tokens[index + 1] if index + 1 < len(file) else None)
        # `x == null` is the idiomatic "null or undefined" check and is exempt
        # in every style guide that bans `==`.
        if any(n is not None and n.is_name("null", "undefined") for n in neighbours):
            continue
        report.add(token.line, "loose_equality",
                   f"`{token.value}` performs type coercion (`'' == 0` is true)",
                   f"Use `{token.value}=`. Reserve `== null` for the deliberate null-or-undefined check.",
                   "medium")


def _check_var(file: TsFile, report: Reporter) -> None:
    for token in file.tokens:
        if token.is_name("var"):
            report.add(token.line, "var_declaration",
                       "`var` is function-scoped and hoisted",
                       "Use `const`, or `let` when it is genuinely reassigned. `var` leaks out of "
                       "blocks, which is a bug waiting for a loop.", "medium")


def _check_nested_ternary(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_op("?") or index + 1 >= len(file) or file.tokens[index + 1].is_op(":", ")", ","):
            continue
        colon = file.find_op(":", index + 1, len(file))
        if colon < 0:
            continue
        nested = any(
            file.tokens[probe].is_op("?") and probe + 1 < len(file)
            and not file.tokens[probe + 1].is_op(":", ")", ",")
            for probe in range(index + 1, colon)
        )
        if nested:
            report.add(token.line, "nested_ternary",
                       "Nested ternary — the branches no longer read left to right",
                       "Extract to a small function with early returns, or a lookup table keyed by "
                       "the discriminant.", "medium")


def _check_switches(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("switch"):
            continue
        cursor = index + 1
        if cursor < len(file) and file.tokens[cursor].is_op("("):
            cursor = file.skip_group(cursor)
        if cursor >= len(file) or not file.tokens[cursor].is_op("{"):
            continue
        close = file.closer(cursor)
        if close < 0:
            continue
        if not any(file.tokens[probe].is_name("default") for probe in range(cursor, close)):
            report.add(token.line, "switch_without_default",
                       "`switch` with no `default` branch",
                       "Add a `default` that throws on the unexpected value. On a union type, an "
                       "exhaustive switch with `default: assertNever(x)` makes the compiler tell you "
                       "when a new member is added.", "medium")


# Mock-call assertions spelled as a function rather than an `expect` matcher
# (Sinon's `assert.calledWith(spy, …)`). Their arguments restate a call that was
# made elsewhere; they are not a call anybody has to read.
_CALL_ASSERTIONS = frozenset({
    "calledWith", "calledWithExactly", "calledOnceWith", "calledOnceWithExactly",
    "alwaysCalledWith", "alwaysCalledWithExactly", "neverCalledWith", "notCalledWith",
})


def _is_assertion_call(file: TsFile, paren: int, callee: str) -> bool:
    """`expect(spy).toHaveBeenCalledWith(…, true, false)` and its relatives.

    A matcher's arguments are the expected values of an assertion: the boolean
    literals in them describe the call under test, which is charged where it
    is made, and the matcher has no parameters to name.
    """
    if callee.rsplit(".", 1)[-1] in _CALL_ASSERTIONS:
        return True
    head = paren - 1 - 2 * callee.count(".")  # first token of the dotted callee
    if head < 2 or not file.tokens[head - 1].is_op(".") or not file.tokens[head - 2].is_op(")"):
        return False
    opener = file.closer(head - 2)
    return opener > 0 and callee_of(file, opener).split(".")[0] == "expect"


def _flagged_signatures(file: TsFile) -> set[str]:
    """Functions declared here whose `boolean` parameter boolean_flag_parameter reports.

    That detector skips test files and a one-argument setter, so this does
    too: a name it does not charge is one whose call sites still carry the
    blindness alone.
    """
    if is_test_file(file.path):
        return set()
    return {
        func.name for func in file.functions
        if any(p.type_text.strip() == "boolean" for p in func.params)
        and not (len(func.params) == 1 and func.name.startswith(("set", "toggle", "is", "has")))
    }


def _check_call_shapes(file: TsFile, report: Reporter) -> None:
    # Boolean blindness is a property of a signature, so it is charged once per
    # callee per file, at its first call site, with the rest as related lines —
    # and not at all when this file declares the signature and
    # boolean_flag_parameter already charges it there.
    blind: dict[str, list[tuple[int, int]]] = {}
    flagged = _flagged_signatures(file)
    for paren, callee in iter_calls(file):
        close = file.closer(paren)
        if close < 0:
            continue
        method = callee.rsplit(".", 1)[-1]
        spans = argument_spans(file, paren)
        booleans = [s for s in spans
                    if s[1] == s[0] + 1 and file.tokens[s[0]].is_name("true", "false")]
        if len(booleans) >= 2 and method not in flagged and not _is_assertion_call(file, paren, callee):
            blind.setdefault(callee, []).append((file.tokens[paren].line, len(booleans)))
        if method == "parseInt" and len(spans) == 1:
            report.add(file.tokens[paren].line, "parseint_without_radix",
                       "`parseInt` without a radix",
                       "Pass 10 explicitly, or use `Number(x)`. Leading-zero and `0x` inputs "
                       "otherwise parse in a base you did not choose.", "medium")
        if callee == "Array" and paren and file.tokens[paren - 2].is_name("new"):
            report.add(file.tokens[paren].line, "array_constructor",
                       "`new Array(n)` creates holes, and `new Array(x)` changes meaning with arity",
                       "Use a literal `[]`, `Array.from({ length: n }, …)`, or `Array.of(x)`.", "low")
    for callee, sites in blind.items():
        most = max(count for _, count in sites)
        description = (
            f"`{callee}(…)` is called with {most} boolean literals" if len(sites) == 1
            else f"`{callee}(…)` is called with up to {most} boolean literals at {len(sites)} call sites"
        )
        report.add(sites[0][0], "boolean_blindness", description,
                   "Nobody can read `f(true, false)`. Take an options object, or split the "
                   "function so each name says what it does.", "low",
                   related=[line for line, _ in sites[1:6]])


def _check_class_shape(file: TsFile, report: Reporter) -> None:
    for klass in file.classes:
        members = len(klass.props) + len(klass.methods)
        if len(klass.methods) > GOD_CLASS_METHODS and members > GOD_CLASS_MEMBERS:
            report.add(klass.line, "god_class",
                       f"`{klass.name}` has {len(klass.methods)} methods and {len(klass.props)} fields",
                       "Find the fields that only some methods touch — that subset is the class "
                       "trying to get out.", "high")
        behaviour = [m for m in klass.methods if m.kind not in ("constructor", "getter", "setter")]
        if len(klass.props) >= 4 and not behaviour and not klass.decorators:
            report.add(klass.line, "data_class",
                       f"`{klass.name}` holds {len(klass.props)} fields and no behaviour",
                       "In TypeScript a bag of data is an `interface` or a `type` — no constructor, "
                       "no instantiation, structurally typed. Keep a class only when it has "
                       "invariants to defend.", "low")


def _is_effect_callback(file: TsFile, func) -> bool:
    """The setup function of a React effect, whose contract is "return nothing or a cleanup".

    `if (!open) return` beside `return () => clearTimeout(t)` is that contract
    followed exactly, and the `EffectCallback` type already rejects any other
    value, so mixing the two is not an inconsistency.
    """
    callee, position = callback_target(file, func)
    return position == 0 and callee.rsplit(".", 1)[-1] in EFFECT_HOOKS


def _check_returns(file: TsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.has_body:
            continue
        if _is_effect_callback(file, func):
            _check_boolean_return(file, func, report)
            continue
        body = body_indices(func)
        nested = [(f.body_open, f.body_close) for f in file.functions
                  if f is not func and f.has_body and func.body_open < f.body_open < func.body_close]
        returns = [i for i in body
                   if file.tokens[i].is_name("return") and not any(a < i < b for a, b in nested)]
        if len(returns) < 2:
            continue
        with_value, bare = [], []
        for index in returns:
            following = file.tokens[index + 1] if index + 1 < len(file) else None
            # A line break straight after `return` is an automatic semicolon
            # (a restricted production): `if (!x) return` then `const y = …` on
            # the next line returns undefined, not `const`.
            if following is None or following.is_op(";", "}") or following.line > file.tokens[index].line:
                bare.append(file.tokens[index].line)
            else:
                with_value.append(file.tokens[index].line)
        if with_value and bare:
            report.add(file.tokens[returns[0]].line, "inconsistent_returns",
                       f"`{func.qualname}` returns a value on some paths and nothing on others",
                       "Return the same shape everywhere. Callers otherwise have to guard against "
                       "`undefined` that the signature never mentioned.", "medium",
                       related=with_value + bare)
        _check_boolean_return(file, func, report)


def _check_boolean_return(file: TsFile, func, report: Reporter) -> None:
    """`if (c) return true; else return false;` — the condition is the answer."""
    for index in body_indices(func):
        if not file.tokens[index].is_name("return"):
            continue
        following = file.tokens[index + 1] if index + 1 < len(file) else None
        if following is None or not following.is_name("true", "false"):
            continue
        for probe in range(index + 2, min(index + 10, func.body_close)):
            token = file.tokens[probe]
            if token.is_name("return") and file.tokens[probe + 1].is_name("true", "false") \
                    and file.tokens[probe + 1].value != following.value:
                report.add(following.line, "boolean_return_conditional",
                           "Returning `true` on one branch and `false` on the other",
                           "Return the condition itself: `return items.length > 0;`.", "low")
                return
            if token.is_name("if", "for", "while", "switch"):
                return


def _check_empty_bodies(file: TsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.has_body or func.body_close != func.body_open + 1 or func.is_abstract:
            continue
        if func.kind == "constructor" and any(p.accessibility for p in func.params):
            continue  # parameter properties: the empty body is the point
        if file.comments_between(func.body_open, func.body_close):
            continue  # an explained no-op
        if func.name in ("<anonymous>",):
            continue
        report.add(func.line, "empty_function_body",
                   f"`{func.qualname}` has an empty body",
                   "Delete it, or say why it does nothing. An empty override that silently "
                   "cancels the base behaviour is a bug that looks like a stub.", "low")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_loose_equality(file, report)
    _check_var(file, report)
    _check_nested_ternary(file, report)
    _check_switches(file, report)
    _check_call_shapes(file, report)
    _check_class_shape(file, report)
    _check_returns(file, report)
    if not is_test_file(file.path):
        _check_magic_numbers(file, report)
        _check_empty_bodies(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Detect everyday code smells in TypeScript",
        "No code smells found!",
        analyze,
    )
