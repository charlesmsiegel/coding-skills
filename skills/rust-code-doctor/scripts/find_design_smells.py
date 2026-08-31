#!/usr/bin/env python3
"""
Find the classic design smells in their Rust spellings.

The catalog is the familiar one — flag parameters, data clumps, god objects,
feature envy — but the fixes are Rust's. A boolean parameter is an enum with
two named variants. A data clump is a struct, and in Rust that struct costs
nothing at runtime. A method that only reads another type's fields belongs on
that type, or on a trait it implements.
"""

import re
from collections import Counter

from common import Reporter, is_test_file, run_file_detector
from rsparse import RsFile, body_indices

MIN_CLUMP_SIZE = 3
MIN_CLUMP_REPEATS = 3
MAX_IMPL_METHODS = 20
MAX_STRUCT_FIELDS = 12


def _check_flag_parameters(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        if func.trait_name is not None:
            continue
        flags = [p for p in func.params if p.type_text.strip() == "bool"]
        if not flags:
            continue
        names = ", ".join(p.name for p in flags)
        if len(flags) >= 2:
            report.add(func.line, "multiple_flag_parameters",
                       f"`{func.qualname}` takes {len(flags)} `bool` parameters ({names})",
                       "At the call site these are two bare `true`s in a row and nothing says "
                       "which is which. Replace each with a two-variant enum "
                       "(`Overwrite::Yes` / `Overwrite::No`) — same size, and the call site reads.",
                       "medium")
        elif func.is_public and len([p for p in func.params if not p.is_self]) >= 2:
            report.add(func.line, "flag_parameter",
                       f"`{func.qualname}` takes `{names}: bool`, so the call site reads "
                       f"`{func.name}(…, true)`",
                       "Split into two functions when the flag selects behaviour, or take a named "
                       "enum when it selects a mode. A `bool` argument is a comment the compiler "
                       "does not check.", "low")


def _check_data_clumps(file: RsFile, report: Reporter) -> None:
    """The same three-plus parameters travelling together want to be a struct."""
    signatures = Counter()
    lines: dict[tuple, list[int]] = {}
    for func in file.functions:
        names = tuple(sorted(p.name for p in func.params
                             if not p.is_self and p.name.isidentifier()))
        if len(names) < MIN_CLUMP_SIZE:
            continue
        signatures[names] += 1
        lines.setdefault(names, []).append(func.line)
    for names, count in signatures.items():
        if count < MIN_CLUMP_REPEATS:
            continue
        report.add(lines[names][0], "data_clump",
                   f"`({', '.join(names)})` are passed together by {count} functions",
                   "Give the group a name: `struct RequestContext { … }`. In Rust the struct is "
                   "free at runtime, and every one of these signatures gets shorter.", "medium",
                   related=lines[names][1:])


def _check_god_impls(file: RsFile, report: Reporter) -> None:
    for block in file.impls:
        if block.trait_name is not None:
            continue
        if len(block.methods) > MAX_IMPL_METHODS:
            report.add(block.line, "god_impl",
                       f"`impl {block.type_name}` has {len(block.methods)} inherent methods",
                       "Group them by the data they touch and split the type, or move a coherent "
                       "group behind a trait. A type with this many methods usually has two "
                       "responsibilities and one name.", "medium")

    for definition in file.types:
        if definition.kind == "struct" and len(definition.fields) > MAX_STRUCT_FIELDS:
            report.add(definition.line, "god_struct",
                       f"`{definition.name}` has {len(definition.fields)} fields",
                       "Look for the subsets that change together and extract each into its own "
                       "struct. The nesting also makes partial borrows possible, which a flat "
                       "struct of this width fights you on.", "medium")


def _check_feature_envy(file: RsFile, report: Reporter) -> None:
    """A method that mostly reaches through one parameter belongs on that type."""
    for func in file.functions:
        if not func.has_body or func.owner is None or func.trait_name is not None:
            continue
        span = body_indices(func)
        if not span or len(span) < 20:
            continue
        self_uses = sum(1 for i in span if file.tokens[i].is_name("self"))
        others = Counter()
        for param in func.params:
            if param.is_self or not param.name.isidentifier():
                continue
            uses = sum(1 for i in span
                       if file.tokens[i].kind == "name" and file.tokens[i].value == param.name
                       and file.tok(i + 1) is not None and file.tokens[i + 1].is_op(".", "::"))
            if uses:
                others[param.name] = uses
        if not others:
            continue
        name, count = others.most_common(1)[0]
        if count >= 4 and count > self_uses * 2:
            report.add(func.line, "feature_envy",
                       f"`{func.qualname}` reaches into `{name}` {count} times and touches `self` "
                       f"{self_uses} time(s)",
                       f"The behaviour belongs with the data: move it to an `impl` on `{name}`'s "
                       "type, or to a trait that type implements. Rust lets you add an extension "
                       "trait even when the type is not yours.", "medium")


def _check_message_chains(file: RsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_op("."):
            continue
        if index and file.tokens[index - 1].is_op("."):
            continue
        chain, cursor, lines = 0, index, set()
        while cursor < len(file.tokens) and file.tokens[cursor].is_op("."):
            name_index = cursor + 1
            if file.tok(name_index) is None or file.tokens[name_index].kind != "name":
                break
            lines.add(file.tokens[name_index].line)
            cursor = name_index + 1
            if file.value(cursor) == "(":
                cursor = file.skip_group(cursor)
            if file.value(cursor) == "?":
                cursor += 1
            chain += 1
        if chain >= 6 and len(lines) <= 2:
            report.add(token.line, "message_chain",
                       f"a chain of {chain} accesses on one line",
                       "An iterator adaptor chain is fine — this is about `a.b().c().d()` walking "
                       "someone else's object graph, where every step is a coupling to a shape "
                       "you do not own. Ask the first object for what you actually want.", "low")


def _check_temporary_fields(file: RsFile, report: Reporter) -> None:
    """A field only one method ever reads is a local variable in disguise."""
    for definition in file.types:
        if definition.kind != "struct" or len(definition.fields) < 3:
            continue
        methods = [m for block in file.impls
                   if block.type_name.split("<")[0].strip() == definition.name
                   for m in block.methods if m.has_body]
        if len(methods) < 3:
            continue
        for field in definition.fields:
            if not field.name.isidentifier() or field.visibility.startswith("pub"):
                continue
            users = [m for m in methods
                     if re.search(r"self\s*\.\s*" + re.escape(field.name) + r"\b",
                                  file.slice(m.body_open, m.body_close))]
            elsewhere = len(re.findall(r"\b" + re.escape(field.name) + r"\b", file.text)) - 1
            if len(users) == 1 and len(methods) >= 6 and elsewhere <= 2:
                report.add(field.line, "temporary_field",
                           f"`{definition.name}.{field.name}` is used by exactly one of "
                           f"{len(methods)} methods (`{users[0].name}`)",
                           "A field that only one method reads is a local variable that outlives "
                           "its use — and it has to be initialised by every constructor. Pass it "
                           "as a parameter instead.", "low")


def _check_refused_bequest(file: RsFile, report: Reporter) -> None:
    """A trait impl whose methods all panic or do nothing has the wrong trait."""
    for block in file.impls:
        if block.trait_name is None or len(block.methods) < 2:
            continue
        hollow = 0
        for method in block.methods:
            if not method.has_body or method.body_close < 0:
                continue
            body = " ".join(file.slice(method.body_open + 1, method.body_close).split())
            if body in ("", "()") or re.fullmatch(r"(todo!|unimplemented!|panic!)\s*\(.*\)\s*;?", body):
                hollow += 1
        if hollow >= 2 and hollow >= len(block.methods) - 1:
            report.add(block.line, "refused_bequest",
                       f"`impl {block.trait_name} for {block.type_name}` leaves {hollow} of "
                       f"{len(block.methods)} methods unimplemented or empty",
                       "The type does not want this trait. Split the trait so this type can "
                       "implement the part it means, or drop the impl — a trait object that "
                       "panics on half its methods is a runtime error waiting for the right "
                       "caller.", "high")


def _check_primitive_obsession(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.is_public or func.trait_name is not None:
            continue
        plain = [p for p in func.params
                 if not p.is_self and p.type_text.strip() in ("&str", "String", "u32", "u64",
                                                              "i32", "i64", "usize")]
        if len(plain) < 3:
            continue
        types = [p.type_text.strip() for p in plain]
        if len(set(types)) > 1:
            continue
        report.add(func.line, "adjacent_same_typed_parameters",
                   f"`{func.qualname}` takes {len(plain)} adjacent `{types[0]}` parameters",
                   "Any two of them can be swapped at a call site and it still compiles. Newtypes "
                   "(`struct UserId(u64)`) make the swap a type error at zero runtime cost.",
                   "medium")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    if is_test_file(file.path):
        return report.findings
    _check_flag_parameters(file, report)
    _check_data_clumps(file, report)
    _check_god_impls(file, report)
    _check_feature_envy(file, report)
    _check_message_chains(file, report)
    _check_temporary_fields(file, report)
    _check_refused_bequest(file, report)
    _check_primitive_obsession(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find classic design smells in their Rust spellings",
        "No design smells found!",
        analyze,
    )
