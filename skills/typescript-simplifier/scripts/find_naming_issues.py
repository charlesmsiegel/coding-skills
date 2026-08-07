#!/usr/bin/env python3
"""
Check naming against the conventions TypeScript codebases actually converge on,
and flag the names that shadow browser globals — which is a bug, not a style
question, because `name`, `location` and `status` already exist on `window`.
"""

import re

from common import Reporter, run_file_detector
from tsparse import TsFile

PASCAL_CASE = re.compile(r"^[A-Z][A-Za-z0-9]*$")
CAMEL_CASE = re.compile(r"^[a-z][A-Za-z0-9]*$")
CONSTANT_CASE = re.compile(r"^[A-Z][A-Z0-9_]*$")
INTERFACE_PREFIX = re.compile(r"^I[A-Z][A-Za-z0-9]*$")

# Globals on `window` that a binding of the same name shadows. `location`,
# `history`, `status` and `event` are left out on purpose: shadowing them is
# routine and deliberate (`const location = useLocation()`), so reporting them
# would bury the ones below, which nobody shadows knowingly.
BROWSER_GLOBALS = frozenset({
    "name", "length", "top", "self", "parent", "origin",
    "closed", "external", "frames", "opener",
})

BOOLEAN_PREFIXES = ("is", "has", "should", "can", "will", "did", "was", "are", "no", "allow", "enable", "disable")


def _check_declaration_case(file: TsFile, report: Reporter) -> None:
    for decl in file.types:
        if not PASCAL_CASE.match(decl.name):
            report.add(decl.line, "non_pascal_case_type",
                       f"{decl.kind} `{decl.name}` is not PascalCase",
                       "Types, interfaces, enums and classes are PascalCase; values are camelCase. "
                       "The case is how a reader tells a type from a value at a glance.", "low")
        if decl.kind == "interface" and INTERFACE_PREFIX.match(decl.name):
            report.add(decl.line, "hungarian_interface_prefix",
                       f"Interface `{decl.name}` uses the `I` prefix",
                       "Drop it. TypeScript's own style guide and the DOM typings do not use it, and "
                       "the prefix stops meaning anything the moment the interface becomes a type "
                       "alias.", "low")
    for klass in file.classes:
        if klass.name != "<anonymous>" and not PASCAL_CASE.match(klass.name):
            report.add(klass.line, "non_pascal_case_type",
                       f"class `{klass.name}` is not PascalCase",
                       "Classes are PascalCase.", "low")


def _check_function_case(file: TsFile, report: Reporter) -> None:
    for func in file.functions:
        name = func.name
        if name in ("<anonymous>", "constructor") or func.kind in ("getter", "setter"):
            continue
        if name.startswith("#") or name.startswith("["):
            continue
        if CAMEL_CASE.match(name) or PASCAL_CASE.match(name) or CONSTANT_CASE.match(name):
            continue
        report.add(func.line, "non_camel_case_function",
                   f"`{name}` is neither camelCase nor PascalCase",
                   "camelCase for functions and methods; PascalCase only for classes, components "
                   "and type-like factories.", "low")


def _check_shadowed_globals(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("const", "let", "var"):
            continue
        name_token = file.tokens[index + 1] if index + 1 < len(file) else None
        if name_token is None or name_token.kind != "name" or name_token.value not in BROWSER_GLOBALS:
            continue
        # Only module scope: a binding inside a function shadows the global for
        # a few lines, one at the top of the file shadows it for every reader.
        if not file.top_level(index):
            continue
        report.add(name_token.line, "shadows_browser_global",
                   f"`{name_token.value}` shadows the browser global of the same name",
                   f"Rename it. Inside this scope `{name_token.value}` no longer refers to "
                   f"`window.{name_token.value}`, and the two are easy to confuse when reading a "
                   "diff out of context.", "medium")


def _check_boolean_names(file: TsFile, report: Reporter) -> None:
    for decl in file.types:
        for member in decl.members:
            if member.type_text.strip() != "boolean" or member.name == "[index]":
                continue
            if member.name.lower().startswith(BOOLEAN_PREFIXES):
                continue
            report.add(member.line, "boolean_without_predicate_name",
                       f"`{decl.name}.{member.name}` is a boolean but does not read as a question",
                       f"Name it `is{member.name[:1].upper()}{member.name[1:]}` / `has…` / `should…`. "
                       "A bare noun leaves the call site ambiguous about which way `true` points.",
                       "low")


def _check_private_naming(file: TsFile, report: Reporter) -> None:
    for klass in file.classes:
        for prop in klass.props:
            if prop.name.startswith("_") and prop.accessibility is None:
                report.add(prop.line, "underscore_without_private",
                           f"`{klass.name}.{prop.name}` is named as private but declared public",
                           "Use the `private` modifier (or a `#name` field). A naming convention "
                           "cannot be enforced; the modifier can.", "low")


def _check_file_name(file: TsFile, report: Reporter) -> None:
    """A default export whose name disagrees with its file is a grep trap."""
    default = next((e for e in file.exports if e.kind == "default"), None)
    if default is None:
        return
    stem = file.path.name.split(".")[0]
    if stem in ("index",):
        return
    named = [d for d in file.classes if d.is_default_export]
    subject = named[0].name if named else None
    if subject is None:
        for func in file.functions:
            if func.is_exported and func.name not in ("<anonymous>",) and func.line == default.line:
                subject = func.name
                break
    if subject and subject.lower() != stem.lower().replace("-", "").replace("_", ""):
        report.add(default.line, "default_export_name_mismatch",
                   f"`{file.path.name}` default-exports `{subject}`",
                   "Match the file name to the export (or use a named export). A default export can "
                   "be imported under any name, so a mismatch means the same thing appears under "
                   "several names across the codebase.", "low")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_declaration_case(file, report)
    _check_function_case(file, report)
    _check_shadowed_globals(file, report)
    _check_boolean_names(file, report)
    _check_private_naming(file, report)
    _check_file_name(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Check naming conventions and globals shadowed by locals",
        "No naming issues found!",
        analyze,
    )
