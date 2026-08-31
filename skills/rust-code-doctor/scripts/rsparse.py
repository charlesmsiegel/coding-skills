#!/usr/bin/env python3
"""Public facade for the Rust parser.

Detectors import this module and nothing below it. The layers underneath are
rustlex (characters to tokens), rustnodes (the model) and rustextract (items).
"""

from pathlib import Path

from rustextract import extract, skip_generics, split_top_level
from rustlex import (
    CLOSERS, CONTROL_KEYWORDS, COPY_TYPES, KEYWORDS, OPENERS, PRIMITIVES, RustSyntaxError,
    Token, is_doc_comment,
)
from rustnodes import Binding, Field, Func, Impl, ModDecl, Param, RsFile, Trait, TypeDef, Use, Variant

__all__ = [
    "RustSyntaxError", "Token", "RsFile", "Func", "Param", "TypeDef", "Field",
    "Variant", "Trait", "Impl", "Use", "ModDecl", "Binding",
    "KEYWORDS", "PRIMITIVES", "COPY_TYPES", "CONTROL_KEYWORDS", "OPENERS", "CLOSERS",
    "parse_file", "parse_source", "is_doc_comment", "skip_generics",
    "split_top_level", "callee_of", "iter_calls", "iter_method_calls",
    "argument_spans", "body_indices", "names_in", "statement_start",
    "statement_end", "receiver_text", "line_indent", "block_of",
]


def parse_source(text: str, path: Path | str = "<memory>") -> RsFile:
    file = RsFile(Path(path), text)
    extract(file)
    file.functions.sort(key=lambda f: f.start)
    return file


def parse_file(path: Path) -> RsFile:
    return parse_source(path.read_text(encoding="utf-8", errors="replace"), path)


# --------------------------------------------------------------------------- #
# Query helpers the detectors share
# --------------------------------------------------------------------------- #

def callee_of(file: RsFile, paren: int) -> str:
    """The path text for the call whose delimiter is at ``paren``, or "".

    `a.b.c(` yields "a.b.c"; `std::fs::read(` yields "std::fs::read"; `(x)(`
    yields "" because there is no name to report. A turbofish is stepped over,
    so `parse::<u32>(` yields "parse".
    """
    cursor = paren - 1
    if cursor >= 0 and file.tokens[cursor].kind == "op" and file.tokens[cursor].value in (">", ">>"):
        opener = _turbofish_start(file, cursor)
        if opener < 0:
            return ""
        cursor = opener - 1
        if cursor >= 0 and file.tokens[cursor].is_op("::"):
            cursor -= 1
    if cursor < 0 or file.tokens[cursor].kind != "name":
        return ""
    parts = [file.tokens[cursor].value]
    cursor -= 1
    while cursor >= 1 and file.tokens[cursor].is_op(".", "::") and file.tokens[cursor - 1].kind == "name":
        parts.append(file.tokens[cursor].value)
        parts.append(file.tokens[cursor - 1].value)
        cursor -= 2
    parts.reverse()
    return "".join(parts)


def _turbofish_start(file: RsFile, close: int) -> int:
    """Walk back from a `>` to the `<` that opened it, or -1."""
    depth = 0
    cursor = close
    while cursor >= 0:
        token = file.tokens[cursor]
        if token.kind == "op":
            if token.value == ">":
                depth += 1
            elif token.value == ">>":
                depth += 2
            elif token.value == "<":
                depth -= 1
                if depth <= 0:
                    return cursor
            elif token.value == "<<":
                depth -= 2
                if depth <= 0:
                    return cursor
            elif token.value in (";", "{", "}"):
                return -1
        cursor -= 1
    return -1


def iter_calls(file: RsFile, start: int = 0, stop: int | None = None):
    """Yield (delimiter_index, callee_text) for every call expression in a range.

    Macro invocations come back with a trailing `!` on the callee, because a
    detector nearly always wants `println!` and `format!` alongside the
    functions — and never wants `if (…)` reported as a call to `if`.
    """
    stop = len(file.tokens) if stop is None else stop
    for index in range(start, min(stop, len(file.tokens))):
        token = file.tokens[index]
        if token.kind != "op" or token.value not in ("(", "[", "{"):
            continue
        head = index - 1
        bang = head >= 0 and file.tokens[head].is_op("!")
        if not bang and token.value != "(":
            continue  # only a macro may be invoked with `[` or `{`
        callee = callee_of(file, head if bang else index)
        if not callee:
            continue
        if not bang and "." not in callee and "::" not in callee \
                and (callee in CONTROL_KEYWORDS or callee in ("fn", "impl", "unsafe", "move")):
            continue
        yield index, (callee + "!" if bang else callee)


def iter_method_calls(file: RsFile, start: int = 0, stop: int | None = None):
    """Yield (name_index, paren_index, method_name) for `.method(` calls."""
    stop = len(file.tokens) if stop is None else stop
    for index in range(start, min(stop, len(file.tokens))):
        if not file.tokens[index].is_op("."):
            continue
        name_index = index + 1
        if name_index >= len(file.tokens) or file.tokens[name_index].kind != "name":
            continue
        cursor = name_index + 1
        if file.value(cursor) == "::":  # turbofish
            cursor = skip_generics(file, cursor + 1)
        if file.value(cursor) != "(":
            continue
        yield name_index, cursor, file.tokens[name_index].value


# Keywords that can precede an expression without being part of it, so the
# receiver of `if map.get(k)` is `map` and not `if map`.
_RECEIVER_KEYWORDS = KEYWORDS - {"self", "Self", "crate", "super"}


def receiver_text(file: RsFile, dot: int, width: int = 6) -> str:
    """A short rendering of what a `.` at ``dot`` is called on."""
    cursor = dot - 1
    if cursor < 0:
        return ""
    if file.tokens[cursor].kind == "op" and file.tokens[cursor].value in (")", "]", "}"):
        opener = file.closer(cursor)
        cursor = opener - 1 if opener > 0 else cursor
    start = cursor
    steps = 0
    while start > 0 and steps < width:
        previous = file.tokens[start - 1]
        if previous.kind == "name" and previous.value not in _RECEIVER_KEYWORDS:
            start -= 1
            steps += 1
            continue
        if previous.is_op(".", "::", "?"):
            start -= 1
            steps += 1
            continue
        break
    return file.slice(start, cursor + 1).strip()


def argument_spans(file: RsFile, paren: int) -> list[tuple[int, int]]:
    """(start, end) token spans of each top-level argument of a call."""
    close = file.closer(paren)
    if close < 0:
        return []
    return split_top_level(file, paren + 1, close)


def body_indices(func: Func) -> range:
    """Token indices strictly inside a function body (empty when there is none)."""
    if not func.has_body or func.body_close < 0:
        return range(0)
    return range(func.body_open + 1, func.body_close)


def block_of(file: RsFile, index: int) -> tuple[int, int]:
    """The innermost `{ … }` containing ``index``, as (open, close); (-1, -1) if none."""
    best = (-1, -1)
    for opener, closer in file.match.items():
        if opener < closer and file.tokens[opener].is_op("{") and opener < index < closer:
            if best == (-1, -1) or opener > best[0]:
                best = (opener, closer)
    return best


def names_in(file: RsFile, span: range, *values: str) -> list[int]:
    """Indices in ``span`` holding one of the given identifiers."""
    wanted = set(values)
    return [i for i in span if file.tokens[i].kind == "name" and file.tokens[i].value in wanted]


def statement_start(file: RsFile, index: int) -> int:
    """Index of the first token of the statement containing ``index``."""
    cursor = index
    while cursor > 0:
        previous = file.tokens[cursor - 1]
        if previous.kind == "op" and previous.value in (";", "{", "}"):
            return cursor
        if previous.kind == "op" and previous.value in (")", "]"):
            opener = file.closer(cursor - 1)
            if opener >= 0:
                head = file.tokens[opener - 1] if opener else None
                if previous.value == ")" and head is not None and head.is_name("if", "while", "match", "for"):
                    return cursor
                cursor = opener
                continue
        cursor -= 1
    return 0


def statement_end(file: RsFile, index: int, stop: int | None = None) -> int:
    """Index just past the statement starting at ``index`` — its `;` or block end."""
    stop = len(file.tokens) if stop is None else stop
    cursor = index
    while cursor < stop:
        token = file.tokens[cursor]
        if token.kind == "op":
            if token.value == ";":
                return cursor + 1
            if token.value in OPENERS:
                cursor = file.skip_group(cursor)
                continue
            if token.value == "}":
                return cursor
        cursor += 1
    return stop


def line_indent(file: RsFile, line: int) -> int:
    if 0 < line <= len(file.lines):
        raw = file.lines[line - 1]
        return len(raw) - len(raw.lstrip())
    return 0
