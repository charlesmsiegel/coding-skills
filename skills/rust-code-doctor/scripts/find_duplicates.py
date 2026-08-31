#!/usr/bin/env python3
"""
Find duplication: repeated token blocks, repeated literals, identical type shapes.

Duplication is a *candidate* for extraction, not a defect. Two functions that
look alike because they do the same thing want one implementation; two that look
alike because they happen to have the same shape today want to stay apart, and
merging them produces the wrong abstraction — the expensive kind, because the
next change has to be threaded through a parameter nobody wanted.

So this reports what is mechanically identical and leaves the judgment to
`references/overengineering-and-abstraction.md`. Blocks are compared after
normalising identifiers and literals, so a copy that was renamed still matches.
"""

import hashlib
from collections import defaultdict
from pathlib import Path

from common import Finding, is_test_file, run_tree_detector
from rsproject import load_project

MIN_BLOCK_TOKENS = 70
MIN_LITERAL_REPEATS = 4
MIN_LITERAL_LENGTH = 12
STRIDE = 20


def _finding(path, line, smell, description, suggestion, severity, related=None):
    return Finding(file=str(path), line=line, smell_type=smell, description=description,
                   suggestion=suggestion, severity=severity, related_lines=related or [])


def _normalise(rsfile, start: int, stop: int) -> str:
    """Token shape with identifiers and literals collapsed, so renames still match."""
    parts = []
    for index in range(start, stop):
        token = rsfile.tokens[index]
        if token.kind == "name":
            parts.append("N" if not token.value[:1].isupper() else "T")
        elif token.kind in ("num", "str", "char", "lifetime"):
            parts.append("L")
        else:
            parts.append(token.value)
    return " ".join(parts)


def _check_duplicate_blocks(project, findings: list) -> None:
    buckets: dict[str, list[tuple[Path, int, int, str]]] = defaultdict(list)
    for path, rsfile in project.files.items():
        if is_test_file(path):
            continue  # near-identical test cases are the point of a test table
        for func in rsfile.functions:
            if rsfile.in_test_code(func.start):
                continue
            if not func.has_body or func.body_close < 0:
                continue
            span = func.body_close - func.body_open
            if span < MIN_BLOCK_TOKENS:
                continue
            for start in range(func.body_open + 1, func.body_close - MIN_BLOCK_TOKENS, STRIDE):
                stop = start + MIN_BLOCK_TOKENS
                digest = hashlib.blake2b(
                    _normalise(rsfile, start, stop).encode("utf-8"), digest_size=12).hexdigest()
                buckets[digest].append(
                    (path, rsfile.line_of(start), rsfile.line_of(stop), func.qualname))

    reported: set[tuple] = set()
    for occurrences in buckets.values():
        if len(occurrences) < 2:
            continue
        # One report per set of functions, not per matching window: a long copied
        # block matches at every stride offset and would otherwise be reported
        # once per offset.
        key = tuple(sorted({(str(p), name) for p, _, _, name in occurrences}))
        if len(key) < 2 or key in reported:
            continue
        reported.add(key)
        path, line, end, _ = occurrences[0]
        others = "; ".join(f"{Path(p).name}:{ln}" for p, ln, _, _ in occurrences[1:4])
        findings.append(_finding(
            path, line, "duplicated_block",
            f"a block of ~{end - line + 1} lines also appears at {others}"
            + (f" (+{len(occurrences) - 4} more)" if len(occurrences) > 4 else ""),
            "If the copies are the same *idea*, extract a function — or a generic one, or a "
            "trait method, whichever the varying part suggests. If they are the same shape by "
            "coincidence, leave them: merging them creates a parameter that means 'which caller "
            "am I', which is the wrong abstraction and is harder to undo than the duplication.",
            "medium"))


def _check_repeated_literals(project, findings: list) -> None:
    occurrences: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    for path, rsfile in project.files.items():
        if is_test_file(path):
            continue
        for index, token in enumerate(rsfile.tokens):
            if token.kind != "str" or len(token.value) < MIN_LITERAL_LENGTH:
                continue
            if rsfile.top_level(index):
                continue  # already a `const`
            occurrences[token.value].append((path, token.line))
    for literal, places in occurrences.items():
        if len(places) < MIN_LITERAL_REPEATS:
            continue
        path, line = places[0]
        findings.append(_finding(
            path, line, "repeated_string_literal",
            f"{literal[:40]}… appears {len(places)} times across {len({p for p, _ in places})} "
            "file(s)",
            "Name it once: `const HEADER: &str = …;`. A literal repeated this often is a value "
            "that will need changing in every copy, and the compiler cannot help you find them.",
            "low", related=[ln for _, ln in places[1:]]))


def _check_identical_type_shapes(project, findings: list) -> None:
    shapes: dict[str, list[tuple[Path, int, str]]] = defaultdict(list)
    for path, rsfile in project.files.items():
        for definition in rsfile.types:
            if definition.kind != "struct" or len(definition.fields) < 3:
                continue
            shape = ";".join(sorted(f"{f.name}:{' '.join(f.type_text.split())}"
                                    for f in definition.fields))
            shapes[shape].append((path, definition.line, definition.name))
    for shape, places in shapes.items():
        if len(places) < 2:
            continue
        names = ", ".join(name for _, _, name in places)
        path, line, _ = places[0]
        findings.append(_finding(
            path, line, "identical_struct_shape",
            f"{len(places)} structs have identical fields: {names}",
            "If they mean the same thing, keep one and alias or re-export it. If they mean "
            "different things that happen to look alike — a `UserId` and an `OrderId` — keeping "
            "them separate is the point, and the duplication is the type safety working.",
            "low", related=[ln for _, ln, _ in places[1:]]))


def analyze(root: Path, ignore: set[str], args) -> list:
    project = load_project(root)
    findings: list[Finding] = []
    _check_duplicate_blocks(project, findings)
    _check_repeated_literals(project, findings)
    _check_identical_type_shapes(project, findings)
    return [f for f in findings if f.smell_type not in ignore]


if __name__ == "__main__":
    run_tree_detector(
        "Find duplicated blocks, literals and type shapes",
        "No duplication found!",
        analyze,
    )
