#!/usr/bin/env python3
"""Public facade for the TypeScript parser.

Detectors import this module and nothing below it. The layers underneath are
tslex (characters to tokens), tsnodes (the model), tsextract (declarations) and
tsmodules (imports, exports and type declarations).
"""

from pathlib import Path

from tsextract import (
    extract_arrows, extract_classes, extract_functions, extract_object_methods,
    extract_variable_annotations, skip_to_statement_end,
)
from tslex import CLOSERS, CONTROL_KEYWORDS, OPENERS, RESERVED, Token, TsSyntaxError
from tsmodules import extract_modules, extract_types
from tsnodes import (
    Class, Export, Func, Import, Param, Prop, TsFile, TypeDecl, TypeMember,
)

__all__ = [
    "TsSyntaxError", "Token", "TsFile", "Func", "Param", "Class", "Prop",
    "TypeDecl", "TypeMember", "Import", "Export", "RESERVED",
    "parse_file", "parse_source", "callee_of", "iter_calls", "argument_spans",
    "body_indices", "names_in", "statement_start", "statement_end", "line_indent",
]


def _extract(file: TsFile) -> None:
    extract_modules(file)
    # Order matters: every pass before extract_arrows records the type spans
    # that tell a function type from a function.
    extract_types(file)
    extract_classes(file)
    extract_functions(file)
    extract_variable_annotations(file)
    extract_arrows(file)
    extract_object_methods(file)
    file.functions.sort(key=lambda f: f.start)


def parse_source(text: str, path: Path | str = "<memory>") -> TsFile:
    file = TsFile(Path(path), text)
    _extract(file)
    return file


def parse_file(path: Path) -> TsFile:
    return parse_source(path.read_text(encoding="utf-8", errors="replace"), path)


# --------------------------------------------------------------------------- #
# Query helpers the detectors share
# --------------------------------------------------------------------------- #

def callee_of(file: TsFile, paren: int) -> str:
    """The dotted callee text for the call whose `(` is at ``paren``, or "".

    `a.b.c(` yields "a.b.c"; `foo(` yields "foo"; `(x)(` and `arr[0](` yield ""
    because there is no name to report.
    """
    cursor = paren - 1
    if cursor < 0 or file.tokens[cursor].kind != "name":
        return ""
    parts = [file.tokens[cursor].value]
    cursor -= 1
    while cursor >= 1 and file.tokens[cursor].is_op(".", "?.") and file.tokens[cursor - 1].kind == "name":
        parts.append(file.tokens[cursor - 1].value)
        cursor -= 2
    return ".".join(reversed(parts))


def iter_calls(file: TsFile, start: int = 0, stop: int | None = None):
    """Yield (paren_index, callee_text) for every call expression in a range."""
    stop = len(file.tokens) if stop is None else stop
    for index in range(start, min(stop, len(file.tokens))):
        token = file.tokens[index]
        if not token.is_op("("):
            continue
        callee = callee_of(file, index)
        if not callee:
            continue
        # `if (…)` is not a call, but `p.catch(…)` and `xs.in(…)` are: a control
        # keyword only disqualifies the name when it is not a member access.
        member = index >= 2 and file.tokens[index - 2].is_op(".", "?.")
        if not member and (callee.rsplit(".", 1)[-1] in CONTROL_KEYWORDS
                           or file.tokens[index - 1].is_name("function")):
            continue
        yield index, callee


def argument_spans(file: TsFile, paren: int) -> list[tuple[int, int]]:
    """(start, end) token spans of each top-level argument of a call.

    Nested groups are stepped over, so a callback's body never counts as an
    argument of the outer call — which is the difference between "called with
    two booleans" and "called with a function that happens to contain `true`".
    """
    close = file.match.get(paren, -1)
    if close < 0:
        return []
    spans: list[tuple[int, int]] = []
    cursor = paren + 1
    start = cursor
    while cursor < close:
        token = file.tokens[cursor]
        if token.kind == "op" and token.value in OPENERS:
            cursor = file.skip_group(cursor)
            continue
        if token.is_op(","):
            if cursor > start:
                spans.append((start, cursor))
            start = cursor + 1
        cursor += 1
    if close > start:
        spans.append((start, close))
    return spans


def body_indices(func: Func) -> range:
    """Token indices strictly inside a function body (empty when there is none)."""
    if not func.has_body or func.body_close < 0:
        return range(0)
    return range(func.body_open + 1, func.body_close)


def names_in(file: TsFile, span: range, *values: str) -> list[int]:
    """Indices in ``span`` holding one of the given identifiers."""
    wanted = set(values)
    return [i for i in span if file.tokens[i].kind == "name" and file.tokens[i].value in wanted]


def statement_start(file: TsFile, index: int) -> int:
    """Index of the first token of the statement containing ``index``.

    Walks left to the nearest `;`, `{`, `}` or `)` that closes a control
    clause, which is what distinguishes `foo()` standing alone (its value is
    discarded) from `const x = foo()` (its value is used).
    """
    cursor = index
    while cursor > 0:
        previous = file.tokens[cursor - 1]
        if previous.kind == "op" and previous.value in (";", "{", "}"):
            return cursor
        if previous.kind == "op" and previous.value in CLOSERS:
            opener = file.closer(cursor - 1)
            if opener >= 0:
                head = file.tokens[opener - 1] if opener else None
                if previous.value == ")" and head is not None and head.is_name("if", "for", "while", "switch", "catch"):
                    return cursor
                cursor = opener
                continue
        if previous.is_op("=>"):
            return cursor
        cursor -= 1
    return 0


def statement_end(file: TsFile, index: int, stop: int | None = None) -> int:
    """Index just past the statement starting at ``index``.

    Stops at a `;`, or at the automatic-semicolon-insertion point: a line break
    where neither side is an operator that continues the expression. That second
    rule is what keeps a multi-line `return foo\\n  .bar()` in one piece.
    """
    return skip_to_statement_end(file, index, len(file.tokens) if stop is None else stop)


def line_indent(file: TsFile, line: int) -> int:
    if 0 < line <= len(file.lines):
        raw = file.lines[line - 1]
        return len(raw) - len(raw.lstrip())
    return 0
