#!/usr/bin/env python3
"""
Check the public surface against the Rust API Guidelines.

A published Rust API is judged on things that are invisible from inside the
crate: whether a type derives the traits callers expect (`Debug` above all —
its absence makes the type unusable in `assert_eq!`, `dbg!` and every error
message), whether `new()` has a `Default`, whether a result the caller must use
is marked `#[must_use]`, whether an enum can gain a variant without breaking
everyone.

These are all cheap to add now and breaking changes to add later, which is why
they belong in review rather than in a follow-up.
"""

import re

from common import Reporter, is_test_file, run_file_detector
from rsparse import RsFile

# Traits nearly every public data type should have. `Debug` is not optional in
# practice: without it the type cannot appear in a panic message or a test
# failure, and neither can anything that contains it.
_EXPECTED_DERIVES = ("Debug",)

# Return types whose value is the whole point of the call.
_MUST_USE_SHAPES = re.compile(r"^(Self|[A-Z]\w*(<.*>)?)$")


def _is_library_file(file: RsFile) -> bool:
    return file.path.name not in ("main.rs", "build.rs") and not is_test_file(file.path)


def _check_derives(file: RsFile, report: Reporter) -> None:
    for definition in file.types:
        if definition.kind not in ("struct", "enum") or not definition.is_public:
            continue
        derives = {d.rsplit("::", 1)[-1] for d in definition.derives}
        manual = {block.trait_name.rsplit("::", 1)[-1] for block in file.impls
                  if block.trait_name and block.type_name.split("<")[0].strip() == definition.name}
        have = derives | manual
        missing = [t for t in _EXPECTED_DERIVES if t not in have]
        if missing:
            report.add(definition.line, "public_type_without_debug",
                       f"public `{definition.kind} {definition.name}` does not implement "
                       f"{', '.join(missing)}",
                       "`#[derive(Debug)]`. Without it the type cannot be printed in a panic, a "
                       "test failure or `dbg!`, and the omission propagates: no struct containing "
                       "it can derive `Debug` either.", "medium")
        if definition.kind == "enum" and "Clone" in have and "Copy" not in have \
                and all(not v.payload for v in definition.variants) and definition.variants:
            report.add(definition.line, "fieldless_enum_without_copy",
                       f"`{definition.name}` is a fieldless enum that derives `Clone` but not "
                       "`Copy`",
                       "A C-like enum is one integer. `#[derive(Clone, Copy)]` removes the "
                       "`.clone()` calls from every call site.", "low")


def _check_new_without_default(file: RsFile, report: Reporter) -> None:
    implements_default = {block.type_name.split("<")[0].strip() for block in file.impls
                          if block.trait_name and block.trait_name.rsplit("::", 1)[-1] == "Default"}
    derives_default = {d.name for d in file.types
                       if any(x.rsplit("::", 1)[-1] == "Default" for x in d.derives)}
    for block in file.impls:
        if block.trait_name is not None:
            continue
        base = block.type_name.split("<")[0].strip()
        for method in block.methods:
            if method.name != "new" or method.takes_self or not method.is_public:
                continue
            if [p for p in method.params if not p.is_self]:
                continue
            if base in implements_default or base in derives_default:
                continue
            report.add(method.line, "new_without_default",
                       f"`{base}::new()` takes no arguments and `{base}` has no `Default`",
                       "Add `#[derive(Default)]` (or an impl that calls `new`). Generic code and "
                       "`..Default::default()` struct updates both need the trait, and callers "
                       "cannot add it themselves.", "low")


def _check_must_use(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.is_public or not func.has_body or func.trait_name is not None:
            continue
        if not func.takes_self or not func.params or func.params[0].by_ref:
            continue
        returns = func.return_type.strip()
        if returns not in ("Self", func.owner or "") and not returns.startswith("Self"):
            continue
        if any(a.startswith("must_use") for a in func.attrs):
            continue
        report.add(func.line, "builder_method_without_must_use",
                   f"`{func.qualname}` consumes `self` and returns `Self` without `#[must_use]`",
                   "A builder step whose result is dropped silently does nothing. `#[must_use]` "
                   "turns that mistake into a warning at every call site.", "low")


def _check_public_docs(file: RsFile, report: Reporter) -> None:
    if not _is_library_file(file):
        return
    undocumented = []
    for func in file.functions:
        if func.is_public and func.trait_name is None and not func.doc_lines and func.name != "new":
            undocumented.append((func.line, f"fn {func.qualname}"))
    for definition in file.types:
        if definition.is_public and not definition.doc_lines:
            undocumented.append((definition.line, f"{definition.kind} {definition.name}"))
    for trait in file.traits:
        if trait.is_public and not trait.doc_lines:
            undocumented.append((trait.line, f"trait {trait.name}"))
    if len(undocumented) < 3:
        return
    lines = sorted(line for line, _ in undocumented)
    report.add(lines[0], "undocumented_public_items",
               f"{len(undocumented)} public items in this file have no doc comment",
               "`///` on a public item is the contract, and `cargo test` runs the examples in it "
               "— a documented API is a tested one. Turn on "
               "`#![warn(missing_docs)]` to keep the next one from slipping in.", "low",
               related=lines[1:])


def _check_non_exhaustive(file: RsFile, report: Reporter) -> None:
    for definition in file.types:
        if definition.kind != "enum" or not definition.is_public:
            continue
        if len(definition.variants) < 3:
            continue
        if any(a.startswith("non_exhaustive") for a in definition.attrs):
            continue
        if not definition.name.endswith(("Error", "Kind", "Event", "Command", "Message")):
            continue
        report.add(definition.line, "public_enum_without_non_exhaustive",
                   f"public `enum {definition.name}` is exhaustive, so adding a variant is a "
                   "breaking change",
                   "`#[non_exhaustive]` forces downstream `match`es to carry a `_` arm, which "
                   "means a new variant is a minor release rather than a major one. Add it before "
                   "1.0, not after.", "low")


def _check_impl_trait_in_public_return(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.is_public or func.trait_name is not None:
            continue
        compact = func.return_type.replace(" ", "")
        if compact.startswith("implIterator") and "+" not in compact:
            report.add(func.line, "impl_trait_without_auto_traits",
                       f"`{func.qualname}` returns `{func.return_type}` with no `+ Send`",
                       "The returned type's auto traits leak from the body: an unrelated change "
                       "inside the function can make the iterator non-`Send` and break callers "
                       "that spawn it. Name the bounds you promise.", "low")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    if is_test_file(file.path):
        return report.findings
    _check_derives(file, report)
    _check_new_without_default(file, report)
    _check_must_use(file, report)
    _check_public_docs(file, report)
    _check_non_exhaustive(file, report)
    _check_impl_trait_in_public_return(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Check the public API against the Rust API Guidelines",
        "No public-API problems found!",
        analyze,
    )
