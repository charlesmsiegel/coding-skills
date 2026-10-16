#!/usr/bin/env python3
"""
Find repetition: duplicated statement blocks, structurally identical types, and
string literals repeated across the codebase.

Duplication is compared after normalising identifiers and literals away, so
copy-paste that has since been renamed still matches — which is most of it.
React hook names are the exception, and a window that is mostly hook state
declarations is not compared at all: `const [a, setA] = useState(…)` five
times over is how a component declares state, not a copied block. A repeated
string is not counted where its repetition is not a missed constant — a CSS
class name shared with a stylesheet, or a member of a literal union in a type,
a comparison or a `case` label, where the compiler already checks the spelling.
That also means a match is a *candidate*: two blocks with the same shape can
encode genuinely different rules, and merging those is the wrong abstraction.
The finding names both sites so a reader can decide.
"""

import hashlib
import re
from collections import defaultdict
from pathlib import Path

from common import Finding, is_declaration_file, is_test_file, run_tree_detector
from tsparse import iter_calls
from tsproject import load_project

WINDOW = 50          # tokens; roughly 8-12 statements
MIN_DISTINCT_NAMES = 12
MIN_LITERAL_REPEATS = 5
MIN_LITERAL_LENGTH = 8

# Where a string is a CSS class list: the value of a JSX class attribute, or an
# argument of a function that joins, merges or builds class lists (clsx and
# classnames, shadcn's `cn`, tailwind-merge, class-variance-authority and
# tailwind-variants).
CLASS_ATTRIBUTES = frozenset({"className", "class"})
CLASS_LIST_HELPERS = frozenset({"clsx", "cn", "cx", "classNames", "classnames", "twMerge", "twJoin", "cva", "tv"})


# A React hook's name is its meaning — `useState` and `useRef` are different
# operations, not two spellings of one — so it survives normalisation.
_HOOK = re.compile(r"use[A-Z]\w*$")
# A window this hook-declaration-heavy is a component declaring its state, the
# language's idiom for it, and two components that each declare five pieces of
# state have not copied each other.
HOOK_DECLARATION_SHARE = 0.6


def _normalise(tsfile, start: int, stop: int) -> str:
    """Token shape with names and literals erased, so renames still match."""
    parts = []
    for index in range(start, stop):
        token = tsfile.tokens[index]
        if token.kind == "name":
            keep = token.value in _KEYWORDS or _HOOK.match(token.value)
            parts.append(token.value if keep else "#")
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


def _mostly_hook_declarations(tsfile, start: int, stop: int) -> bool:
    """True when most lines of the window declare state with a hook call."""
    lines: dict[int, list[int]] = defaultdict(list)
    for index in range(start, stop):
        lines[tsfile.tokens[index].line].append(index)
    hooked = sum(
        1 for indices in lines.values()
        if any(tsfile.tokens[i].kind == "name" and _HOOK.match(tsfile.tokens[i].value)
               and i + 1 < len(tsfile) and tsfile.tokens[i + 1].is_op("(", "<")
               and i >= 1 and tsfile.tokens[i - 1].is_op("=") for i in indices)
    )
    return hooked >= HOOK_DECLARATION_SHARE * len(lines)


def _check_blocks(project, add) -> None:
    seen: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    for path, tsfile in project.analyzable.items():
        # Test files repeat their arrange/act/assert scaffolding on purpose;
        # reporting that buries the duplication in the code under test.
        if is_declaration_file(path) or is_test_file(path):
            continue
        for start, stop in _block_windows(tsfile):
            shape = _normalise(tsfile, start, stop)
            if shape.count("#") < MIN_DISTINCT_NAMES:
                continue  # boilerplate-shaped, e.g. a long chain of punctuation
            if _mostly_hook_declarations(tsfile, start, stop):
                continue
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
    for path, tsfile in project.analyzable.items():
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


def _class_list_strings(tsfile) -> set[int]:
    """Indices of the string tokens that are CSS class lists.

    Repeating utility classes in markup is how Tailwind styles things, and
    Tailwind's own answer to the repetition is a component, not a string
    constant: `"flex items-center gap-2"` written five times is five elements
    styled alike, not one value a rename could miss. Hoisting it into
    `const ROW = "…"` would only hide which element looks how.

    A string is a class list when it is the value of a JSX `className` or
    `class` attribute — directly, or anywhere inside its `{…}` (a ternary, an
    `&&`) — or an argument of a class-list helper. The innermost enclosing
    call decides, so `className={theme.pick("primary")}` passes a lookup key
    to `pick`, and that key is still counted. (A template literal is a single
    token whose interior is never counted at all.)
    """
    tokens = tsfile.tokens
    marked: set[int] = set()
    regions: dict[int, tuple[int, bool]] = {}  # opening index -> (closing index, holds class lists)
    for paren, callee in iter_calls(tsfile):
        close = tsfile.closer(paren)
        if close > paren:
            regions[paren] = (close, callee.rsplit(".", 1)[-1] in CLASS_LIST_HELPERS)
    for index in range(1, len(tokens) - 1):
        if not (tokens[index].is_op("=") and tokens[index - 1].kind == "name"
                and tokens[index - 1].value in CLASS_ATTRIBUTES):
            continue
        value = tokens[index + 1]
        if value.kind == "str":
            marked.add(index + 1)
        elif value.is_op("{") and tsfile.closer(index + 1) > index + 1:
            regions[index + 1] = (tsfile.closer(index + 1), True)
    # Brackets nest, so the open regions form a stack whose top is innermost.
    stack: list[tuple[int, bool]] = []
    for index, token in enumerate(tokens):
        while stack and stack[-1][0] < index:
            stack.pop()
        if index in regions:
            stack.append(regions[index])
        if token.kind == "str" and stack and stack[-1][1]:
            marked.add(index)
    return marked


# Attributes and helpers whose string arguments are CSS class names. Those are
# a vocabulary shared with a stylesheet, not constants the code owns: the
# stylesheet cannot import a TypeScript name, so "import the constant" is not
# the fix, and a repeated `"field-hint"` is how a design system is used.
_CLASS_KEYS = frozenset({"className", "class", "classList"})
_CLASS_LIST_METHODS = frozenset({"add", "remove", "toggle", "contains", "replace"})
# The property names a discriminated union is conventionally tagged with.
_DISCRIMINANT_KEYS = ("kind", "type", "tag")


def _exempt_literal_positions(tsfile) -> set[int]:
    """Token indices of strings whose repetition is not a missed constant.

    Two kinds. A class name (above). And a member of a literal union: a
    string in a type, compared with `===`/`!==`, used as a `case` label,
    narrowed with `as`/`satisfies`, or given as a `kind`/`type`/`tag` value.
    Those slots are typically typed as a union of literals, where a typo is a
    compile error (TS2367, TS2678, TS2322) — the very check the finding says
    the compiler cannot make.
    """
    tokens = tsfile.tokens
    exempt: set[int] = set()

    def cover(start: int, stop: int) -> None:
        exempt.update(i for i in range(max(start, 0), min(stop, len(tokens))) if tokens[i].kind == "str")

    for start, end in tsfile.type_spans:
        cover(start, end + 1)
    for index, token in enumerate(tokens):
        if token.kind == "name" and token.value in _CLASS_KEYS and index + 2 < len(tokens) \
                and tokens[index + 1].is_op("=", ":"):
            value = index + 2
            if tokens[value].kind == "str":
                cover(value, value + 1)
        elif token.is_op("(") and index:
            callee = tokens[index - 1]
            member = index >= 3 and tokens[index - 2].is_op(".") and tokens[index - 3].is_name("classList")
            if callee.kind == "name" and member and callee.value in _CLASS_LIST_METHODS:
                cover(index, tsfile.skip_group(index))
        elif token.kind == "str":
            previous = tokens[index - 1] if index else None
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            compared = (previous is not None and (previous.is_op("===", "!==", "==", "!=")
                                                  or previous.is_name("case"))) \
                or (following is not None and (following.is_op("===", "!==", "==", "!=")
                                               or following.is_name("as", "satisfies")))
            # `{ kind: "campaign", … }`: the value of a discriminant property,
            # checked against the union wherever the object meets its type.
            discriminant = previous is not None and previous.is_op(":") and index >= 2 \
                and tokens[index - 2].is_name(*_DISCRIMINANT_KEYS)
            if compared or discriminant:
                exempt.add(index)
    return exempt


def _check_literals(project, add) -> None:
    occurrences: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    for path, tsfile in project.analyzable.items():
        if is_test_file(path) or is_declaration_file(path):
            continue
        import_lines = {record.line for record in tsfile.imports}
        exempt = _exempt_literal_positions(tsfile) | _class_list_strings(tsfile)
        for index, token in enumerate(tsfile.tokens):
            if token.kind != "str" or token.line in import_lines or index in exempt:
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
