#!/usr/bin/env python3
"""
Find abstractions that never earned their keep: interfaces with one
implementation, abstract classes with one subclass, factories that build one
type, all-static "service" classes, and constructors that only call `super`.

Whole-tree on purpose. "Implemented once" is a fact about the project, not about
a file, and the whole point of this detector is that it can state it rather than
guess. A mock is not a second implementation — if that were the bar, every
interface would pass.
"""

from collections import defaultdict
from pathlib import Path

from common import Finding, is_test_file, run_tree_detector
from tsproject import load_project

CEREMONY_SUFFIXES = ("Service", "Manager", "Helper", "Util", "Utils", "Handler",
                     "Provider", "Controller", "Factory", "Impl", "Wrapper")
DEEP_INHERITANCE = 3


def analyze(root: Path, ignore: set[str], _args) -> list[Finding]:
    project = load_project(root)
    findings: list[Finding] = []

    def add(path, line, smell, description, suggestion, severity, related=None):
        if smell not in ignore:
            findings.append(Finding(file=str(path), line=line, smell_type=smell,
                                    description=description, suggestion=suggestion,
                                    severity=severity, related_lines=related or []))

    implementers: dict[str, list[tuple[Path, str]]] = defaultdict(list)
    subclasses: dict[str, list[tuple[Path, str]]] = defaultdict(list)
    type_uses: dict[str, int] = defaultdict(int)
    for path, tsfile in project.files.items():
        for klass in tsfile.classes:
            for name in klass.implements:
                implementers[name].append((path, klass.name))
            if klass.extends:
                subclasses[klass.extends].append((path, klass.name))
        for token in tsfile.tokens:
            if token.kind == "name":
                type_uses[token.value] += 1

    for path, tsfile in project.files.items():
        if is_test_file(path):
            continue
        _check_interfaces(tsfile, path, implementers, add)
        _check_classes(tsfile, path, subclasses, add)
        _check_wrapper_module(tsfile, path, add)
        _check_single_use_generics(tsfile, path, add)
    return findings


def _check_interfaces(tsfile, path: Path, implementers, add) -> None:
    for decl in tsfile.types:
        if decl.kind != "interface" or not decl.members:
            continue
        impls = implementers.get(decl.name, [])
        if len(impls) != 1:
            continue
        where, class_name = impls[0]
        if not any(m.type_text.startswith("(") or "=>" in m.type_text for m in decl.members):
            continue  # a data shape with one class using it is fine
        add(path, decl.line, "single_implementation_interface",
            f"`{decl.name}` declares behaviour and is implemented only by `{class_name}` "
            f"({where.name})",
            "Delete the interface and use the class type. TypeScript is structurally typed, so a "
            "test double does not need the interface to exist — and an interface with one "
            "implementation only adds a file to keep in sync.", "medium")


def _check_classes(tsfile, path: Path, subclasses, add) -> None:
    for klass in tsfile.classes:
        members = [*klass.methods, *klass.props]
        public_methods = [m for m in klass.methods
                          if m.accessibility != "private" and m.kind not in ("constructor", "getter", "setter")]
        instance_state = [p for p in klass.props if not p.is_static]
        constructor = next((m for m in klass.methods if m.kind == "constructor"), None)
        injected = [p for p in (constructor.params if constructor else []) if p.accessibility]

        if klass.is_abstract:
            children = subclasses.get(klass.name, [])
            if len(children) <= 1:
                detail = f"only `{children[0][1]}`" if children else "nothing"
                add(path, klass.line, "abstract_class_with_one_subclass",
                    f"`{klass.name}` is abstract and extended by {detail}",
                    "Collapse the hierarchy into one class. An abstract base exists to hold what "
                    "several subclasses share; with one, it is a class split across two files.",
                    "medium")

        if members and all(m.is_static for m in members):
            add(path, klass.line, "all_static_class",
                f"`{klass.name}` has only static members — it is a namespace with a `class` keyword",
                "Export the functions directly from the module. A class that is never instantiated "
                "adds a name to every call site and nothing else.", "medium")
            continue

        if len(public_methods) == 1 and not instance_state and not injected \
                and klass.name.endswith(CEREMONY_SUFFIXES):
            add(path, klass.line, "stateless_single_method_class",
                f"`{klass.name}` has one public method and no state",
                f"That is a function: `export function {public_methods[0].name}(…)`. The class adds "
                "a construction step and an object identity nothing uses.", "medium")

        if constructor is not None and constructor.has_body and not constructor.params:
            body = tsfile.slice(constructor.body_open + 1, constructor.body_close).strip().rstrip(";")
            if body in ("", "super()"):
                add(path, constructor.line, "useless_constructor",
                    f"`{klass.name}` declares a constructor that only {'calls super' if body else 'does nothing'}",
                    "Delete it. The implicit constructor does exactly this.", "low")

        depth, chain = 1, klass.extends
        seen = {klass.name}
        while chain and chain not in seen and depth < 10:
            seen.add(chain)
            parent = _find_class(tsfile, chain)
            if parent is None:
                break
            depth += 1
            chain = parent.extends
        if depth >= DEEP_INHERITANCE:
            add(path, klass.line, "deep_inheritance",
                f"`{klass.name}` sits {depth} levels deep in an inheritance chain",
                "Prefer composition. At this depth a reader has to open every ancestor to know what "
                "one method call does, and a change to the base reaches code nobody listed.",
                "medium")


def _find_class(tsfile, name: str):
    return next((k for k in tsfile.classes if k.name == name), None)


def _check_wrapper_module(tsfile, path: Path, add) -> None:
    """A module whose exported functions only forward to one other module."""
    exported = [f for f in tsfile.functions if f.is_exported and f.has_body]
    if len(exported) < 2:
        return
    forwarding = 0
    for func in exported:
        body = tsfile.slice(func.body_open + 1, func.body_close).strip().rstrip(";")
        if body.startswith("return ") and body.count("(") == 1 and len(body) < 120:
            forwarding += 1
    if forwarding == len(exported):
        add(path, exported[0].line, "pass_through_module",
            f"All {len(exported)} exported functions in {path.name} only forward a call",
            "Import the underlying module directly. A pass-through layer has to be updated for "
            "every signature change and hides where the work happens.", "medium")


def _check_single_use_generics(tsfile, path: Path, add) -> None:
    """A generic parameter used once in the signature is not generic."""
    for func in tsfile.functions:
        if not func.has_body:
            continue
        start = func.start
        if start >= len(tsfile.tokens):
            continue
        # Find `<T>` between the name and the parameter list.
        opener = -1
        for index in range(start, func.params_open):
            if tsfile.tokens[index].is_op("<"):
                opener = index
                break
        if opener < 0:
            continue
        names = [tsfile.tokens[i].value for i in range(opener + 1, func.params_open)
                 if tsfile.tokens[i].kind == "name"]
        if len(names) != 1:
            continue
        parameter = names[0]
        if len(parameter) > 2:
            continue  # a descriptive name suggests a deliberate, documented generic
        uses = sum(1 for i in range(func.params_open, func.body_open if func.has_body else func.params_close)
                   if tsfile.tokens[i].kind == "name" and tsfile.tokens[i].value == parameter)
        if uses <= 1:
            add(path, func.line, "single_use_type_parameter",
                f"`{func.qualname}<{parameter}>` uses `{parameter}` once in its signature",
                "A type parameter that appears once relates nothing to anything — it is `any` with "
                "extra syntax, and callers can pass whatever they like. Use the concrete type, or "
                "`unknown`.", "medium")


if __name__ == "__main__":
    run_tree_detector(
        "Find abstractions with one implementation, ceremony classes and pass-through layers",
        "No over-engineering found!",
        analyze,
    )
