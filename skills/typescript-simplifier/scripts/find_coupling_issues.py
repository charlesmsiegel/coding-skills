#!/usr/bin/env python3
"""
Measure how tangled classes and modules are: feature envy, low cohesion, and
middle-man classes that only forward.

Cohesion is the useful number here. A class whose methods touch disjoint sets of
fields is two classes sharing a file, and the split it wants is already visible
in which fields each method reads.
"""

from itertools import combinations

from common import Reporter, is_test_file, run_file_detector
from tsparse import TsFile, body_indices, iter_calls

MIN_METHODS_FOR_COHESION = 4
ENVY_RATIO = 2.0
MIN_EXTERNAL_TOUCHES = 4


def _own_fields(file: TsFile, klass) -> set[str]:
    names = {p.name for p in klass.props}
    constructor = next((m for m in klass.methods if m.kind == "constructor"), None)
    if constructor is not None:
        names.update(p.name for p in constructor.params if p.accessibility)
    return names


def _this_members(file: TsFile, method) -> set[str]:
    """Members of `this` a method touches."""
    touched = set()
    for index in body_indices(method):
        token = file.tokens[index]
        if not token.is_name("this") or index + 2 >= len(file):
            continue
        if file.tokens[index + 1].is_op(".", "?.") and file.tokens[index + 2].kind == "name":
            touched.add(file.tokens[index + 2].value)
    return touched


def _check_cohesion(file: TsFile, report: Reporter) -> None:
    for klass in file.classes:
        methods = [m for m in klass.methods
                   if m.has_body and m.kind not in ("constructor", "getter", "setter")]
        if len(methods) < MIN_METHODS_FOR_COHESION:
            continue
        fields = _own_fields(file, klass)
        if len(fields) < 2:
            continue
        touched = {m.name: _this_members(file, m) & fields for m in methods}
        pairs = list(combinations(methods, 2))
        if not pairs:
            continue
        disjoint = sum(1 for a, b in pairs if not (touched[a.name] & touched[b.name]))
        lcom = disjoint / len(pairs)
        if lcom < 0.8:
            continue
        loners = [name for name, used in touched.items() if not used]
        report.add(klass.line, "low_cohesion",
                   f"`{klass.name}`: {disjoint} of {len(pairs)} method pairs share no field "
                   f"(LCOM {lcom:.0%})",
                   "The methods are not working on the same state. Group them by which fields they "
                   "read — those groups are the classes. "
                   + (f"`{', '.join(loners[:3])}` touch no field at all and can be plain functions."
                      if loners else ""),
                   "medium" if lcom < 0.95 else "high")


def _check_feature_envy(file: TsFile, report: Reporter) -> None:
    for klass in file.classes:
        fields = _own_fields(file, klass)
        for method in klass.methods:
            if not method.has_body or method.kind == "constructor":
                continue
            own = len(_this_members(file, method) & fields)
            external: dict[str, int] = {}
            parameters = {p.name for p in method.params}
            for index in body_indices(method):
                token = file.tokens[index]
                if token.kind != "name" or token.value not in parameters:
                    continue
                if index + 1 < len(file) and file.tokens[index + 1].is_op(".", "?."):
                    external[token.value] = external.get(token.value, 0) + 1
            if not external:
                continue
            envied, count = max(external.items(), key=lambda item: item[1])
            if count >= MIN_EXTERNAL_TOUCHES and count > max(own, 1) * ENVY_RATIO:
                report.add(method.line, "feature_envy",
                           f"`{klass.name}.{method.name}` reaches into `{envied}` {count} times but "
                           f"touches its own state {own} time(s)",
                           f"Move the method onto whatever `{envied}` is. A method that is mostly "
                           "about another object belongs to that object — that is the whole "
                           "criterion.", "medium")


def _check_middle_man(file: TsFile, report: Reporter) -> None:
    for klass in file.classes:
        methods = [m for m in klass.methods
                   if m.has_body and m.kind not in ("constructor", "getter", "setter")]
        if len(methods) < 3:
            continue
        forwarding = 0
        for method in methods:
            body = file.slice(method.body_open + 1, method.body_close).strip().rstrip(";")
            if not body.startswith(("return this.", "this.")):
                continue
            calls = list(iter_calls(file, method.body_open, method.body_close))
            if len(calls) == 1 and len(body) < 120:
                forwarding += 1
        if forwarding >= len(methods) * 0.75:
            report.add(klass.line, "middle_man",
                       f"{forwarding} of {len(methods)} methods on `{klass.name}` only forward to a collaborator",
                       "Let callers talk to the collaborator directly and delete the class, or give "
                       "it a reason to exist (a translation, an invariant, a narrowed surface). A "
                       "class that only forwards has to be edited for every change on both sides.",
                       "medium")


def _check_module_fan_out(file: TsFile, report: Reporter) -> None:
    internal = [record for record in file.imports
                if record.module.startswith(".") and not record.is_type_only]
    if len(internal) > 15:
        report.add(1, "high_module_fan_out",
                   f"{file.path.name} imports {len(internal)} other modules in this project",
                   "This module is a hub: it rebuilds and retests whenever any of them changes. "
                   "Look for a group of imports that only one exported function uses — that "
                   "function is a separate module.", "low")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    if is_test_file(file.path):
        return report.findings
    _check_cohesion(file, report)
    _check_feature_envy(file, report)
    _check_middle_man(file, report)
    _check_module_fan_out(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Measure coupling and cohesion: feature envy, LCOM, middle-man classes",
        "No coupling problems found!",
        analyze,
    )
