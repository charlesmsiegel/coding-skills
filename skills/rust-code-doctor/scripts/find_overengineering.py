#!/usr/bin/env python3
"""
Find abstractions that have not earned their keep.

The Rust-specific version of this question is sharper than most languages'.
A trait with one implementor is not needed for testing — a generic parameter
takes any type that fits, and `#[cfg(test)]` can define the fake right there.
A struct full of associated functions and no fields is a module with a `struct`
keyword. A generic parameter used once in a signature is a concrete type wearing
a letter.

Every finding here is a *candidate*: a plugin boundary, a published API and a
mock at an IO edge are all real reasons for the shape. The detector reports the
shape and the reason it is usually wrong; the reference guide has the cases
where it is right.
"""

import re
from collections import defaultdict
from pathlib import Path

from common import Finding, is_test_file, run_tree_detector
from rsproject import load_project

# Traits with a language-level meaning: implementor count says nothing about them.
_STD_TRAITS = frozenset({
    "Debug", "Display", "Clone", "Copy", "Default", "PartialEq", "Eq", "Hash",
    "PartialOrd", "Ord", "From", "Into", "TryFrom", "TryInto", "AsRef", "AsMut",
    "Deref", "DerefMut", "Drop", "Iterator", "IntoIterator", "FromIterator",
    "Error", "Send", "Sync", "Future", "Read", "Write", "Serialize", "Deserialize",
    "FromStr", "Add", "Sub", "Mul", "Div", "Index", "IndexMut", "Borrow", "ToString",
})


def _finding(path, line, smell, description, suggestion, severity, related=None):
    return Finding(file=str(path), line=line, smell_type=smell, description=description,
                   suggestion=suggestion, severity=severity, related_lines=related or [])


def _base(name: str) -> str:
    return name.split("<")[0].strip().rsplit("::", 1)[-1].lstrip("&").strip()


def _check_single_impl_traits(project, findings: list) -> None:
    implementors: dict[str, list] = defaultdict(list)
    for path, rsfile in project.files.items():
        for block in rsfile.impls:
            if block.trait_name:
                implementors[_base(block.trait_name)].append((path, _base(block.type_name)))

    # A trait name declared more than once in the tree — sibling modules, or two
    # workspace crates — cannot be matched to its impls without resolving `use`,
    # which a syntax scan cannot do. Counting them together merged unrelated
    # traits and suppressed both findings; guessing either way would be worse
    # than saying nothing, so ambiguous names are left alone.
    declared: dict[str, int] = defaultdict(int)
    for rsfile in project.files.values():
        for trait in rsfile.traits:
            declared[trait.name] += 1

    for path, rsfile in project.files.items():
        if is_test_file(path):
            continue
        for trait in rsfile.traits:
            if trait.name in _STD_TRAITS or declared[trait.name] > 1:
                continue
            impls = implementors.get(trait.name, [])
            real = [(p, t) for p, t in impls if not is_test_file(p)]
            if len(real) > 1:
                continue
            if not real:
                findings.append(_finding(
                    path, trait.line, "trait_with_no_implementors",
                    f"`trait {trait.name}` has no implementor in this tree",
                    "Either it is implemented downstream (a published extension point — fine, and "
                    "worth saying in the docs) or it was written for an implementation that never "
                    "arrived. Delete it if so.", "medium"))
                continue
            method_count = len(trait.methods)
            findings.append(_finding(
                path, trait.line, "trait_with_one_implementor",
                f"`trait {trait.name}` ({method_count} method(s)) is implemented once, by "
                f"`{real[0][1]}`",
                "Use the concrete type. 'I need the trait to mock it in tests' is not a reason in "
                "Rust: a generic parameter accepts any type that fits, and the fake can be a "
                "`#[cfg(test)]` struct with no trait at all. Keep the trait when it is a public "
                "extension point, when a second implementor is in this same change, or when it "
                "must be a `dyn` object in a collection.", "medium"))


def _check_stateless_structs(project, findings: list) -> None:
    for path, rsfile in project.files.items():
        if is_test_file(path):
            continue
        for definition in rsfile.types:
            if definition.kind != "struct" or definition.fields:
                continue
            methods = [m for block in rsfile.impls
                       if _base(block.type_name) == definition.name and block.trait_name is None
                       for m in block.methods]
            if len(methods) < 2 or any(m.takes_self for m in methods):
                continue
            findings.append(_finding(
                path, definition.line, "stateless_struct_as_namespace",
                f"`struct {definition.name}` has no fields and {len(methods)} associated "
                "functions, none of which take `self`",
                "That is a module with a `struct` keyword. Make it a module and export the "
                "functions — callers write `parser::parse(x)` instead of `Parser::parse(x)`, and "
                "nothing has to be constructed to use it.", "medium"))


def _check_pass_through_wrappers(project, findings: list) -> None:
    for path, rsfile in project.files.items():
        if is_test_file(path):
            continue
        for definition in rsfile.types:
            if definition.kind != "struct" or len(definition.fields) != 1:
                continue
            field = definition.fields[0]
            methods = [m for block in rsfile.impls
                       if _base(block.type_name) == definition.name and block.trait_name is None
                       for m in block.methods if m.has_body and m.body_close > 0]
            if len(methods) < 3:
                continue
            forwarding = 0
            for method in methods:
                body = " ".join(rsfile.slice(method.body_open + 1, method.body_close).split())
                if body.startswith(f"self . {field.name}") or body.startswith(f"self.{field.name}"):
                    if body.count(";") <= 1:
                        forwarding += 1
            if forwarding >= len(methods) - 1 and forwarding >= 3:
                findings.append(_finding(
                    path, definition.line, "pass_through_newtype",
                    f"`{definition.name}` wraps one field and {forwarding} of {len(methods)} "
                    "methods do nothing but forward to it",
                    "A newtype that only forwards adds a name and a maintenance cost. Either give "
                    "it an invariant the wrapper enforces (validate in the constructor, keep the "
                    "field private) — which is what newtypes are for — or delete it and use the "
                    "inner type. `Deref` is the middle option when the wrapper exists only for "
                    "trait coherence.", "low"))


def _check_single_use_generics(project, findings: list) -> None:
    for path, rsfile in project.files.items():
        if is_test_file(path):
            continue
        for func in rsfile.functions:
            generics = func.generics.strip("<>").strip()
            if not generics or func.trait_name is not None:
                continue
            # `fn f<T: AsRef<str>>(x: T)` uses `T` once on purpose: the bound is
            # what lets any convertible type be passed. Only an *unbounded*
            # parameter used once is a concrete type wearing a letter.
            parameters = [p.strip() for p in generics.split(",")
                          if p.strip() and not p.strip().startswith("'") and ":" not in p]
            signature = " ".join(p.type_text for p in func.params) + " " + func.return_type
            # A `where P: AsRef<Path>` clause is a bound too, and it sits between
            # the parameter list and the body rather than inside `<…>`.
            tail = rsfile.slice(func.params_close,
                                func.body_open if func.body_open > 0 else func.params_close + 1)
            for parameter in parameters:
                if len(parameter) > 2 or not parameter.isidentifier():
                    continue
                uses = sum(1 for word in signature.replace("<", " ").replace(">", " ")
                           .replace(",", " ").replace("&", " ").split()
                           if word.strip() == parameter)
                if uses == 1 and ("impl " + parameter) not in signature \
                        and not re.search(r"\b" + re.escape(parameter) + r"\s*:", tail):
                    findings.append(_finding(
                        path, func.line, "single_use_type_parameter",
                        f"`{func.qualname}` declares `{parameter}` and uses it once",
                        "A type parameter that appears in one position is a concrete type with a "
                        "letter for a name, and it costs a monomorphised copy per caller. Name "
                        "the type, or take `impl Trait` in argument position if the bound is the "
                        "point.", "low"))
                    break


def _check_builders_for_small_types(project, findings: list) -> None:
    for path, rsfile in project.files.items():
        if is_test_file(path):
            continue
        for definition in rsfile.types:
            if not definition.name.endswith("Builder") or definition.kind != "struct":
                continue
            target = definition.name[:-len("Builder")]
            built = next((d for f in project.files.values() for d in f.types
                          if d.name == target and d.kind == "struct"), None)
            if built is None or len(built.fields) > 2:
                continue
            findings.append(_finding(
                path, definition.line, "builder_for_small_struct",
                f"`{definition.name}` builds `{target}`, which has {len(built.fields)} field(s)",
                "A builder earns its place when there are many optional fields or the "
                "construction can fail partway. For two fields, `Target::new(a, b)` — or "
                "`..Default::default()` in a struct literal — says the same thing with no second "
                "type to keep in sync.", "low"))


def _check_deep_trait_hierarchies(project, findings: list) -> None:
    supertraits: dict[str, list[str]] = {}
    lines: dict[str, tuple] = {}
    for path, rsfile in project.files.items():
        for trait in rsfile.traits:
            supertraits[trait.name] = [_base(s) for s in trait.supertraits]
            lines[trait.name] = (path, trait.line)
    for name in supertraits:
        depth, cursor, seen = 0, name, set()
        while cursor in supertraits and cursor not in seen and depth < 10:
            seen.add(cursor)
            parents = [p for p in supertraits[cursor] if p in supertraits]
            if not parents:
                break
            cursor = parents[0]
            depth += 1
        if depth >= 3:
            path, line = lines[name]
            findings.append(_finding(
                path, line, "deep_trait_hierarchy",
                f"`{name}` sits {depth} levels down a chain of local supertraits",
                "Every implementor now has to satisfy the whole chain, and a reader has to open "
                f"{depth} files to learn what `{name}` requires. Flatten it: traits compose by "
                "being implemented side by side, and a bound can list several.", "low"))


def analyze(root: Path, ignore: set[str], args) -> list:
    project = load_project(root)
    findings: list[Finding] = []
    _check_single_impl_traits(project, findings)
    _check_stateless_structs(project, findings)
    _check_pass_through_wrappers(project, findings)
    _check_single_use_generics(project, findings)
    _check_builders_for_small_types(project, findings)
    _check_deep_trait_hierarchies(project, findings)
    return [f for f in findings if f.smell_type not in ignore]


if __name__ == "__main__":
    run_tree_detector(
        "Find abstractions that have not earned their keep",
        "No over-engineering found!",
        analyze,
    )
