#!/usr/bin/env python3
"""
Find code nothing reaches: unreachable statements, and private items no other
item names.

Unused *imports* are deliberately not here. rustc reports them precisely — it
knows a trait imported only so its methods resolve, which a syntax scan cannot
— so a second, worse answer would be noise. `cargo check` is the authority;
what this file adds is the two questions it does not answer as reliably in a
crate that has silenced `dead_code`.

The private-item pass is the one worth having. rustc's own `dead_code` lint
catches most of it — but only when the crate compiles, and only when nobody has
written `#[allow(dead_code)]`, which is exactly what happens in the repositories
this skill is pointed at.

Unreachable code after a `return` or a `panic!` is reported at high severity for
a different reason: it is almost never dead on purpose. It usually means an
early return was added above code that still looks live to the reader.
"""

import re
from collections import defaultdict
from pathlib import Path

from common import Finding, is_test_file, run_tree_detector
from rsproject import load_project

# Names whose absence proves nothing: a trait impl's methods are called through
# the trait, and these are conventional entry points.
_ALWAYS_LIVE = frozenset({"main", "new", "default", "drop", "fmt", "from", "into",
                          "clone", "eq", "hash", "next", "poll", "deref", "as_ref"})

_DIVERGING = re.compile(r"^\s*(return\b|break\b|continue\b|panic!|todo!|unimplemented!|"
                        r"unreachable!|std::process::exit|process::exit)")


def _finding(path, line, smell, description, suggestion, severity, related=None):
    return Finding(file=str(path), line=line, smell_type=smell, description=description,
                   suggestion=suggestion, severity=severity, related_lines=related or [])


def _check_unreachable_code(project, findings: list) -> None:
    for path, rsfile in project.files.items():
        for func in rsfile.functions:
            if not func.has_body or func.body_close < 0:
                continue
            cursor = func.body_open + 1
            while cursor < func.body_close:
                token = rsfile.tokens[cursor]
                if token.kind == "op" and token.value in ("(", "[", "{"):
                    cursor = rsfile.skip_group(cursor)
                    continue
                if token.kind == "name" and _DIVERGING.match(token.value + " "):
                    end = rsfile.find_op(";", cursor, func.body_close)
                    if end < 0:
                        break
                    following = rsfile.tok(end + 1)
                    if following is not None and not following.is_op("}") \
                            and end + 1 < func.body_close:
                        findings.append(_finding(
                            path, following.line, "unreachable_code",
                            f"statements after `{token.value}` in `{func.qualname}`",
                            "Nothing below this line runs. Usually an early return was added "
                            "above code that still looks live — check which half is the one you "
                            "meant to keep.", "high"))
                        break
                    cursor = end + 1
                    continue
                cursor += 1


def _check_unused_private_items(project, findings: list) -> None:
    """A private item no other file or item in the crate names."""
    mentions = defaultdict(int)
    for path, rsfile in project.files.items():
        for token in rsfile.tokens:
            if token.kind == "name":
                mentions[token.value] += 1

    for path, rsfile in project.files.items():
        if is_test_file(path):
            continue
        for func in rsfile.functions:
            if func.is_public or func.owner is not None or func.kind == "closure":
                continue
            if func.name in _ALWAYS_LIVE or func.name.startswith("_") or not func.name:
                continue
            if any(a.split("(")[0].strip() in ("test", "no_mangle", "export_name")
                   for a in func.attrs):
                continue
            if mentions.get(func.name, 0) > 1:
                continue
            findings.append(_finding(
                path, func.line, "unused_private_function",
                f"private `fn {func.name}` is not named anywhere else in the tree",
                "Delete it. If it is reached through a macro or a `cfg` branch this scan does not "
                "follow, `cargo build` with the relevant features will say so — and rustc's own "
                "`dead_code` lint is the authority here, so check whether an `#[allow(dead_code)]` "
                "is what has been hiding it.", "medium"))

        for definition in rsfile.types:
            if definition.is_public or definition.name in _ALWAYS_LIVE:
                continue
            if mentions.get(definition.name, 0) > 1:
                continue
            findings.append(_finding(
                path, definition.line, "unused_private_type",
                f"private `{definition.kind} {definition.name}` is not named anywhere else",
                "Delete it, or make it `pub(crate)` if it is meant to be used and is not yet.",
                "medium"))


def _check_unused_constants(project, findings: list) -> None:
    mentions = defaultdict(int)
    for rsfile in project.files.values():
        for token in rsfile.tokens:
            if token.kind == "name":
                mentions[token.value] += 1
    for path, rsfile in project.files.items():
        if is_test_file(path):
            continue
        for binding in rsfile.bindings:
            if binding.is_public or mentions.get(binding.name, 0) > 1:
                continue
            findings.append(_finding(
                path, binding.line, "unused_private_constant",
                f"private `{binding.kind} {binding.name}` is never read",
                "Delete it. A constant nobody reads is a value nobody has checked is still "
                "right.", "low"))


def analyze(root: Path, ignore: set[str], args) -> list:
    project = load_project(root)
    findings: list[Finding] = []
    _check_unreachable_code(project, findings)
    _check_unused_private_items(project, findings)
    _check_unused_constants(project, findings)
    return [f for f in findings if f.smell_type not in ignore]


if __name__ == "__main__":
    run_tree_detector(
        "Find unreachable code and unreferenced private items",
        "No dead code found!",
        analyze,
    )
