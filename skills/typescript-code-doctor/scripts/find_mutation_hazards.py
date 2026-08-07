#!/usr/bin/env python3
"""
Find shared-mutable-state bugs: reassigned parameters, imported objects mutated
in place, collections changed while being iterated, and props treated as scratch.

Worth stating up front, because reviewers coming from other languages look for
it: a default parameter value is evaluated on every call, so
`function f(items = [])` is safe and is not a finding. The hazard is never the
default — it is the object the caller already owns and still holds a reference to.
"""

from common import Reporter, run_file_detector
from tsparse import TsFile, argument_spans, body_indices, iter_calls

MUTATING_ARRAY_METHODS = frozenset({"push", "pop", "shift", "unshift", "splice", "sort", "reverse", "fill"})
ASSIGN_OPS = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "**=", "||=", "&&=", "??=", "++", "--"})


def _check_parameter_reassignment(file: TsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.has_body:
            continue
        parameters = {p.name for p in func.params if not p.is_destructured and not p.is_rest}
        if not parameters:
            continue
        nested = [(f.body_open, f.body_close) for f in file.functions
                  if f is not func and f.has_body and func.body_open < f.body_open < func.body_close]
        for index in body_indices(func):
            token = file.tokens[index]
            if token.kind != "name" or token.value not in parameters:
                continue
            if any(a < index < b for a, b in nested):
                continue
            following = file.tokens[index + 1] if index + 1 < len(file) else None
            if following is None or not (following.kind == "op" and following.value in ASSIGN_OPS):
                continue
            if index and file.tokens[index - 1].is_op(".", "?."):
                continue  # `x.param = …` is a property write on something else
            report.add(token.line, "parameter_reassigned",
                       f"`{func.qualname}` reassigns its parameter `{token.value}`",
                       "Assign to a new local instead. A reassigned parameter makes the signature "
                       "lie about what the function is working with halfway down the body.", "medium")


def _check_property_writes_on_parameters(file: TsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.has_body:
            continue
        parameters = {p.name for p in func.params if not p.is_destructured}
        for index in body_indices(func):
            token = file.tokens[index]
            if token.kind != "name" or token.value not in parameters or index + 3 >= len(file):
                continue
            if not file.tokens[index + 1].is_op(".") or file.tokens[index + 2].kind != "name":
                continue
            operator = file.tokens[index + 3]
            if not (operator.kind == "op" and operator.value in ASSIGN_OPS):
                continue
            report.add(token.line, "mutates_argument_property",
                       f"`{token.value}.{file.tokens[index + 2].value} = …` writes through an argument",
                       "The caller's object changes as a side effect of a call it may believe is "
                       "pure. Return a new object (`{ ...input, field }`) or make the mutation the "
                       "documented purpose of the function.", "high")


def _check_imported_mutation(file: TsFile, report: Reporter) -> None:
    """Writing to something another module owns."""
    imported = set()
    for record in file.imports:
        if record.is_type_only:
            continue
        imported.update(record.names)
        if record.default_name:
            imported.add(record.default_name)
        if record.namespace_name:
            imported.add(record.namespace_name)
    for index, token in enumerate(file.tokens):
        if token.kind != "name" or token.value not in imported or index + 3 >= len(file):
            continue
        if not file.tokens[index + 1].is_op("."):
            continue
        member, operator = file.tokens[index + 2], file.tokens[index + 3]
        if member.kind == "name" and operator.kind == "op" and operator.value in ASSIGN_OPS:
            report.add(token.line, "mutates_imported_object",
                       f"`{token.value}.{member.value} = …` mutates a module you imported",
                       "The change is global and invisible to every other importer. Keep the "
                       "override local, or ask that module for a supported way to configure it.",
                       "high")


def _check_mutation_during_iteration(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("for"):
            continue
        clause = index + 1
        if clause >= len(file) or not file.tokens[clause].is_op("("):
            continue
        clause_end = file.skip_group(clause)
        subject = ""
        for probe in range(clause + 1, clause_end):
            if file.tokens[probe].is_name("of", "in") and probe + 1 < clause_end:
                subject = file.tokens[probe + 1].value
                break
        if not subject or clause_end >= len(file) or not file.tokens[clause_end].is_op("{"):
            continue
        close = file.closer(clause_end)
        if close < 0:
            continue
        for paren, callee in iter_calls(file, clause_end, close):
            parts = callee.split(".")
            if len(parts) == 2 and parts[0] == subject and parts[1] in MUTATING_ARRAY_METHODS:
                report.add(file.tokens[paren].line, "mutation_during_iteration",
                           f"`{callee}()` changes `{subject}` while the loop is iterating it",
                           "Collect the changes and apply them after the loop, or iterate a copy "
                           "(`[...items]`). Mutating mid-iteration skips or repeats elements.",
                           "high")


def _check_props_mutation(file: TsFile, report: Reporter) -> None:
    """React props are frozen by contract; writing to them is a silent no-op or a bug."""
    for index, token in enumerate(file.tokens):
        if not token.is_name("props") or index + 3 >= len(file):
            continue
        if index and file.tokens[index - 1].is_op(".", "?."):
            continue
        if not file.tokens[index + 1].is_op("."):
            continue
        operator = file.tokens[index + 3]
        if operator.kind == "op" and operator.value in ASSIGN_OPS:
            report.add(token.line, "mutates_props",
                       f"`props.{file.tokens[index + 2].value} = …` — props belong to the caller",
                       "Derive a local value or lift the state. Writing to props does not re-render "
                       "and is undefined behaviour in React's model.", "high")


def _check_frozen_intent(file: TsFile, report: Reporter) -> None:
    """An exported `const` object is only const in its binding, not its contents."""
    for index, token in enumerate(file.tokens):
        if not token.is_name("const") or not file.top_level(index):
            continue
        if not (index and file.tokens[index - 1].is_name("export")):
            continue
        name_token = file.tokens[index + 1] if index + 1 < len(file) else None
        equals = index + 2
        if name_token is None or name_token.kind != "name" or equals >= len(file):
            continue
        if file.tokens[equals].is_op(":"):
            continue
        if not file.tokens[equals].is_op("="):
            continue
        value = file.tokens[equals + 1] if equals + 1 < len(file) else None
        if value is None or not value.is_op("{", "["):
            continue
        end = file.closer(equals + 1)
        following = file.tokens[end + 1] if 0 < end + 1 < len(file) else None
        if following is not None and following.is_name("as"):
            continue  # `as const` already makes it deeply readonly
        if not name_token.value.isupper() and "_" not in name_token.value:
            continue  # only flag the ones written as constants
        report.add(token.line, "mutable_exported_constant",
                   f"`export const {name_token.value}` is a {'array' if value.value == '[' else 'object'} "
                   "whose contents any importer can change",
                   "Append `as const` (or `Object.freeze`). `const` only freezes the binding; the "
                   "shared object underneath is still writable from every module that imports it.",
                   "medium")


def _check_object_assign(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        if callee != "Object.assign":
            continue
        spans = argument_spans(file, paren)
        if not spans:
            continue
        target = file.tokens[spans[0][0]]
        if target.is_op("{"):
            continue  # `Object.assign({}, a, b)` builds a new object — fine
        func = file.enclosing_function(paren)
        if func is None or target.value not in {p.name for p in func.params}:
            continue
        report.add(target.line, "object_assign_onto_argument",
                   f"`Object.assign({target.value}, …)` writes into an argument",
                   "Build a new object instead: `{ ...input, ...changes }`. Assigning onto the "
                   "caller's object changes state they still hold a reference to.", "high")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_parameter_reassignment(file, report)
    _check_property_writes_on_parameters(file, report)
    _check_imported_mutation(file, report)
    _check_mutation_during_iteration(file, report)
    _check_props_mutation(file, report)
    _check_frozen_intent(file, report)
    _check_object_assign(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find shared-mutable-state hazards",
        "No mutation hazards found!",
        analyze,
    )
