#!/usr/bin/env python3
"""
Find names that break Rust's conventions, and the ones that mislead.

Casing is the mechanical half — `snake_case` for values and functions,
`UpperCamelCase` for types and traits, `SCREAMING_SNAKE_CASE` for constants —
and rustc warns about most of it already, so it is reported at low severity and
mainly so a repo that has silenced the lint still sees it.

The half rustc says nothing about is the API guidelines' *meaning* rules, where
a wrong name is a wrong promise: `as_` is a cheap borrow, `to_` allocates,
`into_` consumes, and `get_` is not a Rust prefix at all. A method called
`as_bytes` that allocates will be called in a loop by someone who read the name.
"""

import re

from common import Reporter, run_file_detector
from rsparse import RsFile

_SNAKE = re.compile(r"^_?[a-z][a-z0-9_]*$")
_CAMEL = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_SCREAMING = re.compile(r"^_?[A-Z][A-Z0-9_]*$")

# Conversion prefixes and the cost each promises. (Rust API Guidelines, C-CONV.)
_CONVERSIONS = {
    "as_": ("borrow", "takes `&self` and returns a borrow — free"),
    "to_": ("clone", "allocates or otherwise does real work, and takes `&self`"),
    "into_": ("consume", "takes `self` by value and consumes it"),
}

# Words that make a boolean method read as a question.
_BOOL_PREFIXES = (
    "is_", "has_", "can_", "should_", "was_", "will_", "does_", "did_", "needs_",
    "must_", "allows_", "accepts_", "supports_", "contains", "matches", "starts",
    "ends", "any", "all", "equals", "intersects", "eq", "ne", "lt", "gt",
)


def _check_casing(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        if func.kind == "closure" or func.is_extern or func.trait_name is not None:
            continue
        if func.name and not _SNAKE.match(func.name):
            report.add(func.line, "non_snake_case_function",
                       f"`fn {func.name}` is not `snake_case`",
                       f"Rename to `{_to_snake(func.name)}`. Rust's casing is not decoration — a "
                       "reader uses it to tell a type from a value at a glance.", "low")

    for definition in file.types:
        if definition.name and not _CAMEL.match(definition.name):
            report.add(definition.line, "non_camel_case_type",
                       f"`{definition.kind} {definition.name}` is not `UpperCamelCase`",
                       f"Rename to `{_to_camel(definition.name)}`.", "low")
    for trait in file.traits:
        if not _CAMEL.match(trait.name):
            report.add(trait.line, "non_camel_case_type",
                       f"`trait {trait.name}` is not `UpperCamelCase`",
                       f"Rename to `{_to_camel(trait.name)}`.", "low")
    for binding in file.bindings:
        if not _SCREAMING.match(binding.name):
            report.add(binding.line, "non_screaming_case_constant",
                       f"`{binding.kind} {binding.name}` is not `SCREAMING_SNAKE_CASE`",
                       f"Rename to `{binding.name.upper()}` — the casing is how a reader tells a "
                       "constant from a local without looking it up.", "low")


def _check_getters(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.name.startswith("get_") or not func.takes_self:
            continue
        if func.trait_name is not None or func.kind == "trait_method":
            continue  # the trait chose the name, and an impl cannot change it
        stripped = func.name[len("get_"):]
        if not stripped:
            continue
        report.add(func.line, "getter_with_get_prefix",
                   f"`{func.qualname}` uses the `get_` prefix",
                   f"Rust names a getter after the thing: `fn {stripped}(&self)`. `get_` is "
                   "reserved for the fallible/indexed spelling the standard library uses "
                   "(`HashMap::get`, `slice::get`), where it returns an `Option`.", "low")


def _check_conversion_prefixes(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        if func.trait_name is not None or func.kind == "trait_method" or not func.params:
            continue
        first = func.params[0]
        if not first.is_self:
            continue
        for prefix, (kind, meaning) in _CONVERSIONS.items():
            if not func.name.startswith(prefix):
                continue
            takes_by_value = not first.by_ref
            if kind == "consume" and not takes_by_value:
                report.add(func.line, "into_prefix_borrows",
                           f"`{func.qualname}` starts with `into_` but takes `{first.type_text}`",
                           "`into_` promises the receiver is consumed. Take `self`, or rename to "
                           "`to_` (allocating) or `as_` (a free borrow).", "medium")
            elif kind in ("borrow", "clone") and takes_by_value:
                report.add(func.line, "as_or_to_prefix_consumes",
                           f"`{func.qualname}` starts with `{prefix}` but takes `self` by value",
                           f"`{prefix}` promises the receiver survives the call ({meaning}). A "
                           "consuming conversion is `into_`.", "medium")
            elif kind == "borrow" and "String" in func.return_type and "&" not in func.return_type:
                report.add(func.line, "as_prefix_allocates",
                           f"`{func.qualname}` starts with `as_` but returns an owned "
                           f"`{func.return_type}`",
                           "`as_` promises a free borrow, so callers use it in loops. An "
                           "allocating conversion is `to_`.", "medium")
            break


def _check_boolean_names(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        if func.return_type.strip() != "bool" or func.trait_name is not None \
                or func.kind == "trait_method":
            continue
        if not func.is_public or func.name.startswith(_BOOL_PREFIXES):
            continue
        report.add(func.line, "boolean_fn_not_a_question",
                   f"`{func.qualname}` returns `bool` but does not read as a question",
                   f"Prefix it: `is_{func.name}`, `has_{func.name}`, `can_{func.name}`. At the "
                   "call site `if x.valid()` and `if x.validate()` look alike, and only one of "
                   "them is a query.", "low")


def _check_stutter(file: RsFile, report: Reporter) -> None:
    for declaration in file.mods:
        if not declaration.inline or declaration.body_close < 0:
            continue
        prefix = _to_camel(declaration.name)
        for definition in file.types:
            if not (declaration.body_open < definition.start < declaration.body_close):
                continue
            if definition.name.startswith(prefix) and definition.name != prefix and len(prefix) > 2:
                report.add(definition.line, "module_name_stutter",
                           f"`{declaration.name}::{definition.name}` repeats the module name",
                           f"`{declaration.name}::{definition.name[len(prefix):]}` — the path "
                           "already carries the module, and the import can alias it where the "
                           "short name is ambiguous.", "low")


def _to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower().replace("__", "_")


def _to_camel(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in name.split("_") if part)


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_casing(file, report)
    _check_getters(file, report)
    _check_conversion_prefixes(file, report)
    _check_boolean_names(file, report)
    _check_stutter(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find names that break Rust's casing and conversion conventions",
        "No naming problems found!",
        analyze,
    )
