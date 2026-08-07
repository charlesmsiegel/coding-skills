#!/usr/bin/env python3
"""
Find leaked internals: state anyone can reach and change from anywhere.

TypeScript makes encapsulation cheap — `private`, `readonly`, `#name`, and a
module boundary that is real rather than conventional — and then defaults to
`public` and mutable, so the cheap thing is the one nobody does. These findings
are about who is allowed to change what, which is the question that decides
whether a refactor is local or global.
"""

from common import Reporter, is_test_file, run_file_detector
from tsparse import TsFile, iter_calls

# Roots whose chains are not a Demeter violation: they are the platform.
CHAIN_ROOT_ALLOWLIST = frozenset({
    "console", "Math", "JSON", "Object", "Array", "String", "Number", "Promise",
    "process", "window", "document", "globalThis", "React", "it", "test", "expect",
})
CHAIN_DEPTH = 4

MUTATING_METHODS = frozenset({"push", "pop", "shift", "unshift", "splice", "sort", "reverse",
                              "fill", "copyWithin", "set", "delete", "clear", "add"})


def _assigned_fields(file: TsFile, klass) -> dict[str, list[int]]:
    """`this.x = …` targets inside the class, mapped to the lines they occur on."""
    assigned: dict[str, list[int]] = {}
    for index in range(klass.body_open, klass.body_close):
        token = file.tokens[index]
        if not token.is_name("this") or index + 3 >= len(file):
            continue
        if not file.tokens[index + 1].is_op(".") or file.tokens[index + 2].kind != "name":
            continue
        operator = file.tokens[index + 3]
        if operator.is_op("=", "+=", "-=", "*=", "/=", "??=", "||=", "&&=", "++", "--"):
            assigned.setdefault(file.tokens[index + 2].value, []).append(token.line)
    return assigned


def _check_class_fields(file: TsFile, report: Reporter) -> None:
    for klass in file.classes:
        assigned = _assigned_fields(file, klass)
        constructor = next((m for m in klass.methods if m.kind == "constructor"), None)
        for prop in klass.props:
            if prop.accessibility in ("private", "protected") or prop.readonly or prop.is_static:
                continue
            report.add(prop.line, "public_mutable_field",
                       f"`{klass.name}.{prop.name}` is public and mutable — any caller can set it",
                       "Mark it `private` (or `readonly` when it is set once in the constructor). A "
                       "public field is an invariant nobody can defend, and every write site becomes "
                       "part of the class's contract.", "medium")
        for prop in klass.props:
            if prop.readonly or prop.is_static or prop.name not in assigned:
                continue
            outside_constructor = [
                line for line in assigned[prop.name]
                if constructor is None or not (constructor.line <= line <= file.line_of(constructor.body_close))
            ]
            if not outside_constructor and (prop.has_initializer or constructor is not None):
                report.add(prop.line, "missing_readonly",
                           f"`{klass.name}.{prop.name}` is only ever assigned in the constructor",
                           "Mark it `readonly`. The compiler then rejects the accidental later write "
                           "rather than leaving it to a reviewer.", "low")


def _check_accessor_pairs(file: TsFile, report: Reporter) -> None:
    """A getter/setter pair that only forwards is a public field with ceremony."""
    for klass in file.classes:
        getters = {m.name: m for m in klass.methods if m.kind == "getter"}
        setters = {m.name: m for m in klass.methods if m.kind == "setter"}
        for name, getter in getters.items():
            setter = setters.get(name)
            if setter is None or not getter.has_body or not setter.has_body:
                continue
            get_body = file.slice(getter.body_open + 1, getter.body_close).strip().rstrip(";")
            set_body = file.slice(setter.body_open + 1, setter.body_close).strip().rstrip(";")
            parameter = setter.params[0].name if setter.params else "?"
            trivial_get = get_body.startswith("return this.")
            trivial_set = set_body.startswith("this.") and set_body.endswith(f"= {parameter}")
            if trivial_get and trivial_set:
                report.add(getter.line, "pass_through_accessors",
                           f"`{klass.name}.{name}` has a getter and setter that only forward",
                           "This is a public field written the long way — it defends nothing and "
                           "costs a call. Make it a public field, and reintroduce accessors when "
                           "there is real logic to put in them.", "low",
                           related=[setter.line])


def _check_exposed_internals(file: TsFile, report: Reporter) -> None:
    """A public method handing back the private collection it wraps."""
    for klass in file.classes:
        private = {p.name for p in klass.props if p.accessibility in ("private", "protected")}
        collections = {p.name for p in klass.props
                       if p.name in private
                       and ("[]" in p.type_text or "Map<" in p.type_text or "Set<" in p.type_text
                            or "Array<" in p.type_text or p.initializer.startswith(("[", "new Map", "new Set")))}
        for method in klass.methods:
            if method.accessibility in ("private", "protected") or not method.has_body:
                continue
            body = file.slice(method.body_open + 1, method.body_close).strip().rstrip(";")
            for name in collections:
                if body == f"return this.{name}":
                    report.add(method.line, "exposes_internal_collection",
                               f"`{klass.name}.{method.name}()` returns the private `{name}` itself",
                               "The caller now holds a live handle to your internals and can mutate "
                               "them behind your back. Return a copy, a `ReadonlyArray`/`ReadonlyMap`, "
                               "or an iterator.", "medium")


def _check_module_state(file: TsFile, report: Reporter) -> None:
    """Module-level `let`, and exported bindings anyone can reassign."""
    for index, token in enumerate(file.tokens):
        if not token.is_name("let", "var") or not file.top_level(index):
            continue
        name_token = file.tokens[index + 1] if index + 1 < len(file) else None
        if name_token is None or name_token.kind != "name":
            continue
        exported = index and file.tokens[index - 1].is_name("export")
        written_inside_function = any(
            file.tokens[i].kind == "name" and file.tokens[i].value == name_token.value
            and i + 1 < len(file) and file.tokens[i + 1].is_op("=", "+=", "-=", "++", "--", "??=")
            and file.enclosing_function(i) is not None
            for i in range(index + 2, len(file))
        )
        if exported:
            report.add(token.line, "exported_mutable_binding",
                       f"`export let {name_token.value}` — importers see a binding that changes under them",
                       "Export a getter or a function instead, so the module keeps control of when "
                       "the value changes. A live mutable export is a global with import syntax.",
                       "high")
        elif written_inside_function:
            report.add(token.line, "module_level_mutable_state",
                       f"Module-level `{name_token.value}` is reassigned from inside functions",
                       "This is a global. Pass it in, or move it into an object whose lifetime you "
                       "control — module state makes tests order-dependent and defeats concurrency.",
                       "medium")


def _check_global_writes(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("window", "globalThis", "global") or index + 3 >= len(file):
            continue
        if file.tokens[index + 1].is_op(".") and file.tokens[index + 3].is_op("=", "??=", "||="):
            report.add(token.line, "global_object_write",
                       f"Writes `{token.value}.{file.tokens[index + 2].value}` — a global anyone can read or clobber",
                       "Pass the value through the module graph instead. A global write is invisible "
                       "to every importer and untypeable at the read site.", "medium")


def _check_private_reach(file: TsFile, report: Reporter) -> None:
    """Reaching into another object's `_private` or `#private`."""
    for index, token in enumerate(file.tokens):
        if not token.is_op(".") or index == 0 or index + 1 >= len(file):
            continue
        owner, member = file.tokens[index - 1], file.tokens[index + 1]
        if member.kind != "name" or not member.value.startswith(("_", "#")):
            continue
        if member.value.startswith("__"):
            continue  # `__proto__`, `__typename` and friends are conventions, not privates
        if owner.is_name("this", "super") or owner.kind != "name":
            continue
        report.add(token.line, "reaches_into_private",
                   f"`{owner.value}.{member.value}` reaches past another object's private naming",
                   "Either the behaviour belongs on that object (move the method there), or the "
                   "member is really part of its API (name it so). Reaching in freezes both classes "
                   "together.", "medium")


def _check_message_chains(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        parts = callee.split(".")
        if len(parts) < CHAIN_DEPTH or parts[0] in CHAIN_ROOT_ALLOWLIST:
            continue
        if parts[0] == "this":
            continue
        report.add(file.tokens[paren].line, "message_chain",
                   f"`{callee}(…)` walks {len(parts) - 1} objects deep",
                   "The caller now depends on every type along the path. Ask the first object for "
                   "what you want (`order.shippingCity()`), or destructure once at the boundary.",
                   "low")


def _check_mutating_calls_on_params(file: TsFile, report: Reporter) -> None:
    """A function quietly rearranging its caller's data."""
    for func in file.functions:
        if not func.has_body:
            continue
        parameters = {p.name for p in func.params if not p.is_destructured}
        for paren, callee in iter_calls(file, func.body_open, func.body_close):
            parts = callee.split(".")
            if len(parts) != 2 or parts[0] not in parameters or parts[1] not in MUTATING_METHODS:
                continue
            report.add(file.tokens[paren].line, "mutates_parameter",
                       f"`{callee}()` mutates the `{parts[0]}` argument in place",
                       "The caller did not ask for that. Copy first (`[...items].sort()`), or make "
                       "the mutation the point and say so in the name (`sortInPlace`).", "high")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_accessor_pairs(file, report)
    _check_exposed_internals(file, report)
    _check_private_reach(file, report)
    _check_mutating_calls_on_params(file, report)
    if not is_test_file(file.path):
        # Tests reassign `globalThis.fetch` to install a mock; that is the
        # supported way to stub the platform, not a leak of production state.
        _check_global_writes(file, report)
        _check_class_fields(file, report)
        _check_module_state(file, report)
        _check_message_chains(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find encapsulation failures: public mutable state, leaked internals, globals, deep chains",
        "No encapsulation problems found!",
        analyze,
    )
