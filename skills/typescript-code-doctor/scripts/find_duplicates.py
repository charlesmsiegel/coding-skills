#!/usr/bin/env python3
"""
Find repetition: duplicated statement blocks, structurally identical types, and
string literals repeated across the codebase.

Duplication is compared after normalising identifiers and literals away, so
copy-paste that has since been renamed still matches — which is most of it.
That also means a match is a *candidate*: two blocks with the same shape can
encode genuinely different rules, and merging those is the wrong abstraction.
The finding names both sites so a reader can decide.
"""

import hashlib
from collections import defaultdict
from pathlib import Path

from common import Finding, is_declaration_file, is_test_file, run_tree_detector
from tsproject import load_project

WINDOW = 50          # tokens; roughly 8-12 statements
MIN_DISTINCT_NAMES = 12
MIN_LITERAL_REPEATS = 5
MIN_LITERAL_LENGTH = 8


def _normalise(tsfile, start: int, stop: int) -> str:
    """Token shape with names and literals erased, so renames still match."""
    parts = []
    for index in range(start, stop):
        token = tsfile.tokens[index]
        if token.kind == "name":
            parts.append("#" if token.value not in _KEYWORDS else token.value)
        elif token.kind in ("str", "num", "template", "regex"):
            parts.append("$")
        else:
            parts.append(token.value)
    return " ".join(parts)


_KEYWORDS = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "default", "break",
    "continue", "return", "throw", "try", "catch", "finally", "new", "delete",
    "typeof", "instanceof", "in", "of", "await", "async", "function", "class",
    "const", "let", "var", "this", "super", "null", "undefined", "true", "false",
    "import", "export", "extends", "implements", "interface", "type", "enum",
})


def _block_windows(tsfile):
    """Yield (start, stop) windows aligned to statement boundaries in bodies."""
    for func in tsfile.functions:
        if not func.has_body:
            continue
        body_start, body_stop = func.body_open + 1, func.body_close
        if body_stop - body_start < WINDOW:
            continue
        # Step by a few tokens rather than one: a one-token shift of the same
        # code is the same duplicate, and stepping keeps this near-linear.
        for start in range(body_start, body_stop - WINDOW + 1, 8):
            yield start, start + WINDOW


def _check_blocks(project, add) -> None:
    seen: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    for path, tsfile in project.files.items():
        # Test files repeat their arrange/act/assert scaffolding on purpose;
        # reporting that buries the duplication in the code under test.
        if is_declaration_file(path) or is_test_file(path):
            continue
        for start, stop in _block_windows(tsfile):
            shape = _normalise(tsfile, start, stop)
            if shape.count("#") < MIN_DISTINCT_NAMES:
                continue  # boilerplate-shaped, e.g. a long chain of punctuation
            digest = hashlib.blake2b(shape.encode("utf-8"), digest_size=12).hexdigest()
            seen[digest].append((path, tsfile.tokens[start].line))

    # A copy-pasted region produces one group per window offset. Report each
    # region once: the first group that claims it wins, the rest are the same
    # finding shifted by a few tokens.
    claimed: set[tuple[Path, int]] = set()
    for occurrences in sorted(seen.values(), key=len, reverse=True):
        if len(occurrences) < 2:
            continue
        # Collapse overlapping windows from the same region of one file.
        distinct: list[tuple[Path, int]] = []
        for path, line in sorted(occurrences):
            if distinct and distinct[-1][0] == path and line - distinct[-1][1] < WINDOW:
                continue
            distinct.append((path, line))
        if len(distinct) < 2:
            continue
        if any((path, line // WINDOW) in claimed for path, line in distinct):
            continue
        for path, line in distinct:
            claimed.add((path, line // WINDOW))
        head = distinct[0]
        others = ", ".join(f"{p.name}:{line}" for p, line in distinct[1:5])
        add(head[0], head[1], "duplicate_block",
            f"This block of ~{WINDOW} tokens also appears at {others}"
            + (f" and {len(distinct) - 5} more" if len(distinct) > 5 else ""),
            "Extract the shared part — but only if the copies are the same *decision*, not just "
            "the same shape. Two rules that happen to look alike will diverge, and a shared "
            "function with a flag parameter is worse than the duplication was.",
            "medium" if len(distinct) > 2 else "low",
            related=[line for _, line in distinct[1:5]])


def _type_shape(decl) -> str:
    members = sorted((m.name, m.type_text.replace(" ", "")) for m in decl.members)
    return "|".join(f"{name}:{text}" for name, text in members)


def _check_types(project, add) -> None:
    shapes: dict[str, list[tuple[Path, int, str]]] = defaultdict(list)
    for path, tsfile in project.files.items():
        for decl in tsfile.types:
            if decl.kind == "enum" or len(decl.members) < 3:
                continue
            shapes[_type_shape(decl)].append((path, decl.line, decl.name))
    for occurrences in shapes.values():
        if len(occurrences) < 2:
            continue
        names = {name for _, _, name in occurrences}
        if len(names) == 1:
            continue  # the same type re-declared, which find_ai_scaffolding reports
        path, line, name = occurrences[0]
        others = ", ".join(f"{n} ({p.name}:{ln})" for p, ln, n in occurrences[1:4])
        add(path, line, "duplicate_type_shape",
            f"`{name}` has the same members as {others}",
            "If they describe the same thing, declare it once and import it. If they describe "
            "different things that happen to match today, leave them apart — structural typing "
            "already lets them interoperate, and merging couples two schedules of change.",
            "medium", related=[ln for _, ln, _ in occurrences[1:4]])


def _check_literals(project, add) -> None:
    occurrences: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    for path, tsfile in project.files.items():
        if is_test_file(path) or is_declaration_file(path):
            continue
        import_lines = {record.line for record in tsfile.imports}
        for token in tsfile.tokens:
            if token.kind != "str" or token.line in import_lines:
                continue
            literal = token.value.strip("'\"")
            if len(literal) < MIN_LITERAL_LENGTH or literal.startswith(("http", "./", "../")):
                continue
            occurrences[literal].append((path, token.line))
    for literal, sites in occurrences.items():
        if len(sites) < MIN_LITERAL_REPEATS:
            continue
        path, line = sites[0]
        add(path, line, "repeated_string_literal",
            f"`{literal[:50]}` is written out {len(sites)} times across {len({p for p, _ in sites})} file(s)",
            "Name it once and import the constant. A repeated literal is a rename waiting to miss "
            "a site, and the compiler cannot help with a typo in a string.", "low",
            related=[ln for _, ln in sites[1:5]])


def analyze(root: Path, ignore: set[str], _args) -> list[Finding]:
    project = load_project(root)
    findings: list[Finding] = []

    def add(path, line, smell, description, suggestion, severity, related=None):
        if smell not in ignore:
            findings.append(Finding(file=str(path), line=line, smell_type=smell,
                                    description=description, suggestion=suggestion,
                                    severity=severity, related_lines=related or []))

    _check_blocks(project, add)
    _check_types(project, add)
    _check_literals(project, add)
    return findings


if __name__ == "__main__":
    run_tree_detector(
        "Find duplicated blocks, identical type shapes and repeated string literals",
        "No duplication found!",
        analyze,
    )
