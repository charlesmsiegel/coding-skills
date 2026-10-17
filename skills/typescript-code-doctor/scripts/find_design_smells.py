#!/usr/bin/env python3
"""
Find the classic design smells, in their TypeScript spellings: type-switching
where a discriminated union belongs, boolean flag parameters, data clumps,
primitive obsession, temporary fields and refused bequest.

The TypeScript-specific one is the type switch. An `if/else if` ladder on
`x.type === '…'` is checking at runtime what the type system could check at
compile time — and unlike the ladder, an exhaustive `switch` over a
discriminated union fails the build when a new case is added. (`typeof x` is
not one: on a union of primitives it is the narrowing the compiler checks.)
"""

import re
from collections import defaultdict
from itertools import combinations

from common import Reporter, is_test_file, run_file_detector
from tsparse import TsFile, argument_spans, body_indices, iter_calls, statement_end

TYPE_SWITCH_BRANCHES = 3
# Parameter names that, by convention, hold a DOM event.
EVENT_PARAMETER_NAMES = frozenset({"event", "e", "evt", "ev"})
# Keyboard-event properties: strings the platform defines, not a tag the code owns.
EVENT_KEY_PROPERTIES = frozenset({"key", "code"})
CLUMP_SIZE = 3
CLUMP_OCCURRENCES = 3
PRIMITIVE_TYPES = frozenset({"string", "number", "boolean"})
PRIMITIVE_RUN = 3
# React's naming rule for a hook: `use` followed by a capital letter.
HOOK_NAME = re.compile(r"^use[A-Z0-9]")


def _check_type_switches(file: TsFile, report: Reporter) -> None:
    """`x.kind === '…'` ladders with three or more distinct arms.

    Two ladders look like this one and are not it. `typeof x === 'string'` is
    how TypeScript narrows a union of primitives: there is no discriminant to
    switch on, `typeof` *is* the discriminant, and the compiler already checks
    each arm. And `event.key === 'Enter'` compares a string the platform
    defines — the code does not own that set of values and cannot make it a
    union. Neither is reported. Three checks of the *same* literal are not a
    switch either: a switch needs three cases.
    """
    ladders: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for index, token in enumerate(file.tokens):
        if not token.is_op("===", "==") or index + 1 >= len(file):
            continue
        right = file.tokens[index + 1]
        if right.kind != "str":
            continue
        cursor = index - 1
        if cursor < 0 or file.tokens[cursor].kind != "name":
            continue  # `typeof (x)`, a call result, an index: not a tag read off a value
        parts = [file.tokens[cursor].value]
        while cursor >= 2 and file.tokens[cursor - 1].is_op(".") and file.tokens[cursor - 2].kind == "name":
            cursor -= 2
            parts.append(file.tokens[cursor].value)
        parts.reverse()
        if len(parts) < 2 or (cursor >= 1 and file.tokens[cursor - 1].is_name("typeof")):
            continue
        if len(parts) == 2 and parts[1] in EVENT_KEY_PROPERTIES and _is_keyboard_event(file, index, parts[0]):
            continue
        ladders[".".join(parts)].append((token.line, right.value.strip("'\"")))

    for subject, arms in ladders.items():
        distinct = sorted({line for line, _ in arms})
        cases = {literal for _, literal in arms}
        if len(distinct) < TYPE_SWITCH_BRANCHES or len(cases) < TYPE_SWITCH_BRANCHES:
            continue
        if max(distinct) - min(distinct) > 120:
            continue  # spread across the file: not one ladder
        report.add(distinct[0], "type_switch",
                   f"`{subject}` is compared against {len(cases)} string literals on {len(distinct)} lines",
                   "Model the cases as a discriminated union and `switch` on the discriminant with "
                   "an exhaustive `default: assertNever(x)`. The compiler then tells you every place "
                   "to update when a new case appears — the ladder never will.",
                   "medium", related=distinct[1:6])


def _is_keyboard_event(file: TsFile, index: int, name: str) -> bool:
    """True when ``name`` is a parameter, of a function around ``index``, holding a DOM event.

    An annotation decides when there is one (`KeyboardEvent`,
    `React.KeyboardEvent<…>`), so `event: { key: Mode }` — the code's own type —
    is still checked. Unannotated, as in an inline `onKeyDown={(e) => …}`, the
    conventional name (`event`, `e`, `evt`) is the evidence.
    """
    verdict = False
    for func in file.functions:  # ordered by start, so an inner (shadowing) parameter decides last
        if func.has_body:
            inside = func.body_open < index < func.body_close
        else:  # a concise arrow: its body is the expression after `=>`
            inside = func.params_close < index < statement_end(file, func.params_close + 1)
        for param in func.params if inside else ():
            if param.name == name:
                verdict = "KeyboardEvent" in param.type_text if param.type_text else name in EVENT_PARAMETER_NAMES
    return verdict


def _check_boolean_parameters(file: TsFile, report: Reporter) -> None:
    for func in file.functions:
        flags = [p for p in func.params if p.type_text.strip() == "boolean"]
        if not flags:
            continue
        if len(func.params) == 1 and func.name.startswith(("set", "toggle", "is", "has")):
            continue  # a setter's argument is the value, not a mode switch
        if func.kind in ("function", "arrow") and HOOK_NAME.match(func.name):
            # A React hook's parameters are the reactive inputs its effects
            # depend on: `useDismiss(open)` re-subscribes when `open` changes.
            # The boolean is state the hook follows, not a switch between two
            # behaviours — and splitting the hook in two would push the branch
            # into callers, where choosing between hooks breaks the rules of
            # hooks. Class methods are never hooks, so a `useX` method is not
            # excused.
            continue
        for flag in flags:
            report.add(flag.line, "boolean_flag_parameter",
                       f"`{func.qualname}` takes the boolean flag `{flag.name}`",
                       "At the call site `f(x, true)` says nothing. Split the function into the two "
                       "things it does, or take a named option (`{ force: true }`) or a union of "
                       "string literals.", "low")


def _check_data_clumps(file: TsFile, report: Reporter) -> None:
    """Parameter groups that travel together across several functions."""
    groups: dict[tuple[str, ...], list[tuple[str, int]]] = defaultdict(list)
    for func in file.functions:
        names = tuple(sorted(p.name for p in func.params
                             if not p.is_destructured and not p.is_rest and p.accessibility is None))
        if len(names) < CLUMP_SIZE or len(names) > 8:
            continue
        # Every subset, not every contiguous run: a clump that travels together
        # is rarely written in the same order at every call site.
        for size in range(CLUMP_SIZE, min(len(names), 5) + 1):
            for clump in combinations(names, size):
                groups[clump].append((func.qualname, func.line))

    reported: set[tuple[str, ...]] = set()
    for clump, sites in sorted(groups.items(), key=lambda item: -len(item[0])):
        if len(sites) < CLUMP_OCCURRENCES:
            continue
        if any(set(clump) <= set(seen) for seen in reported):
            continue
        reported.add(clump)
        where = ", ".join(f"{name}()" for name, _ in sites[:4])
        report.candidate(sites[0][1], "data_clump",
                         f"`{', '.join(clump)}` are passed together to {len(sites)} functions ({where})",
                         ("the parameters are a documented positional API that callers depend on",
                          "the functions are overloads or adapters of one external signature",
                          "the group is passed through unchanged to a third-party call"),
                         "medium", related=[line for _, line in sites[1:5]])


def _check_primitive_obsession(file: TsFile, report: Reporter) -> None:
    for func in file.functions:
        run, longest = 0, 0
        for param in func.params:
            if param.type_text.strip() in PRIMITIVE_TYPES:
                run += 1
                longest = max(longest, run)
            else:
                run = 0
        if longest >= PRIMITIVE_RUN:
            report.candidate(func.line, "primitive_obsession",
                             f"`{func.qualname}` takes {longest} adjacent parameters of primitive type",
                             ("the values are validated at the boundary and the function is internal",
                              "the signature mirrors a third-party API the code cannot change",
                              "the parameters are of distinct semantic types the compiler could only "
                              "express with branding the project has chosen not to adopt"),
                             "medium" if longest >= PRIMITIVE_RUN + 1 else "low")


def _check_temporary_fields(file: TsFile, report: Reporter) -> None:
    """A field only one method sets and one method reads is a parameter."""
    for klass in file.classes:
        for prop in klass.props:
            if prop.is_static or prop.has_initializer:
                continue
            touching = []
            for method in klass.methods:
                if not method.has_body:
                    continue
                uses = any(
                    file.tokens[i].kind == "name" and file.tokens[i].value == prop.name
                    and i and file.tokens[i - 1].is_op(".")
                    and i >= 2 and file.tokens[i - 2].is_name("this")
                    for i in body_indices(method)
                )
                if uses:
                    touching.append(method)
            if len(touching) == 2 and all(m.kind == "method" for m in touching):
                report.add(prop.line, "temporary_field",
                           f"`{klass.name}.{prop.name}` is touched only by "
                           f"`{touching[0].name}` and `{touching[1].name}`",
                           "Pass it between them instead. A field that is meaningful during one "
                           "call and stale afterwards makes every other method's contract depend on "
                           "call order.", "medium", related=[m.line for m in touching])


def _check_refused_bequest(file: TsFile, report: Reporter) -> None:
    for klass in file.classes:
        if not klass.extends:
            continue
        for method in klass.methods:
            if not method.has_body or method.kind == "constructor":
                continue
            body = file.slice(method.body_open + 1, method.body_close).strip().rstrip(";")
            if body.startswith("throw ") and len(body) < 140:
                report.add(method.line, "refused_bequest",
                           f"`{klass.name}.{method.name}` overrides its base only to throw",
                           f"`{klass.name}` is not really a `{klass.extends}` — it cannot honour the "
                           "contract. Use composition: hold an instance and expose the operations "
                           "that do apply.", "high")


def _check_long_option_objects(file: TsFile, report: Reporter) -> None:
    """A call with a huge inline object literal is a signature nobody reviewed."""
    for paren, callee in iter_calls(file):
        for start, end in argument_spans(file, paren):
            if not file.tokens[start].is_op("{") or file.closer(start) != end - 1:
                continue
            keys = sum(1 for i in range(start + 1, end - 1)
                       if file.tokens[i].is_op(":") and file.enclosing_function(i) is file.enclosing_function(start))
            if keys >= 10:
                report.add(file.tokens[paren].line, "large_inline_options",
                           f"`{callee}(…)` is passed an inline object with ~{keys} properties",
                           "Name the object and give it a type. A literal this large is a data "
                           "structure the compiler is checking positionally, at a call site nobody "
                           "reads to the end.", "low")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    if is_test_file(file.path):
        return report.findings
    _check_type_switches(file, report)
    _check_boolean_parameters(file, report)
    _check_data_clumps(file, report)
    _check_primitive_obsession(file, report)
    _check_temporary_fields(file, report)
    _check_refused_bequest(file, report)
    _check_long_option_objects(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find classic design smells: type switches, flag parameters, data clumps, temporary fields",
        "No design smells found!",
        analyze,
    )
