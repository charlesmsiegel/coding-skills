#!/usr/bin/env python3
"""Extraction of the module surface: interfaces, type aliases, enums,
imports and exports.

Split from tsextract because it answers a different question — not "what code
is in this file" but "what does this file promise and require" — which is what
the whole-tree detectors (cycles, dead exports, dependency hygiene) run on.
"""

from tsextract import CONTINUATIONS, modifiers_before, read_type_after_colon, skip_to_statement_end, skip_type_parameters
from tsnodes import Export, Import, TsFile, TypeDecl, TypeMember
from tslex import OPENERS


def extract_types(file: TsFile) -> None:
    tokens = file.tokens
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.is_name("interface") and index + 1 < len(tokens) and tokens[index + 1].kind == "name":
            start, words = modifiers_before(file, index)
            name = tokens[index + 1].value
            cursor = skip_type_parameters(file, index + 2)
            extends = []
            while cursor < len(tokens) and not tokens[cursor].is_op("{"):
                if tokens[cursor].kind == "name" and not tokens[cursor].is_name("extends"):
                    extends.append(tokens[cursor].value)
                cursor += 1
            if cursor < len(tokens) and tokens[cursor].is_op("{"):
                close = file.closer(cursor)
                decl = TypeDecl(
                    kind="interface", name=name, line=token.line, start=start, end=close,
                    is_exported="export" in words, extends=extends,
                    members=_parse_type_members(file, cursor, close),
                    text=file.slice(cursor, close + 1),
                )
                file.types.append(decl)
                file.type_spans.append((start, close))
                index = close + 1
                continue
        elif token.is_name("type") and index + 1 < len(tokens) and tokens[index + 1].kind == "name":
            start, words = modifiers_before(file, index)
            cursor = skip_type_parameters(file, index + 2)
            if cursor < len(tokens) and tokens[cursor].is_op("="):
                end = skip_to_statement_end(file, cursor + 1, len(tokens))
                body = file.find_op("{", cursor, end)
                members = []
                if body >= 0 and file.closer(body) >= 0:
                    members = _parse_type_members(file, body, file.closer(body))
                file.types.append(TypeDecl(
                    kind="type", name=tokens[index + 1].value, line=token.line,
                    start=start, end=end, is_exported="export" in words,
                    members=members, text=file.slice(cursor + 1, end),
                ))
                file.type_spans.append((start, end))
                index = end
                continue
        elif token.is_name("enum") and index + 1 < len(tokens) and tokens[index + 1].kind == "name":
            start, words = modifiers_before(file, index)
            cursor = index + 2
            if cursor < len(tokens) and tokens[cursor].is_op("{"):
                close = file.closer(cursor)
                file.types.append(TypeDecl(
                    kind="enum", name=tokens[index + 1].value, line=token.line,
                    start=start, end=close, is_exported="export" in words,
                    is_const="const" in words, text=file.slice(cursor, close + 1),
                ))
                index = close + 1
                continue
        index += 1


def _parse_type_members(file: TsFile, open_index: int, close_index: int) -> list[TypeMember]:
    members: list[TypeMember] = []
    if close_index < 0:
        return members
    cursor = open_index + 1
    while cursor < close_index:
        token = file.tokens[cursor]
        if token.is_op(";", ",") or token.kind == "op" and token.value == "|":
            cursor += 1
            continue
        readonly = token.is_name("readonly")
        if readonly:
            cursor += 1
            if cursor >= close_index:
                break
            token = file.tokens[cursor]
        if token.is_op("["):  # index signature
            cursor = file.skip_group(cursor)
            name = "[index]"
        elif token.kind in ("name", "str"):
            name = token.value.strip("'\"")
            cursor += 1
        else:
            cursor += 1
            continue
        optional = cursor < close_index and file.tokens[cursor].is_op("?")
        if optional:
            cursor += 1
        type_text = ""
        if cursor < close_index and file.tokens[cursor].is_op(":"):
            type_text, cursor = read_type_after_colon(file, cursor, close_index)
        elif cursor < close_index and file.tokens[cursor].is_op("("):
            type_text = file.slice(cursor, file.skip_group(cursor))
            cursor = file.skip_group(cursor)
            if cursor < close_index and file.tokens[cursor].is_op(":"):
                extra, cursor = read_type_after_colon(file, cursor, close_index)
                type_text += ": " + extra
        members.append(TypeMember(name=name, type_text=type_text, line=token.line,
                                  optional=optional, readonly=readonly))
        cursor = max(cursor, cursor)
        while cursor < close_index and not file.tokens[cursor].is_op(";", ","):
            if file.tokens[cursor].kind == "op" and file.tokens[cursor].value in OPENERS:
                cursor = file.skip_group(cursor)
                continue
            nxt = file.tokens[cursor + 1] if cursor + 1 < close_index else None
            if nxt is not None and nxt.line > file.tokens[cursor].line \
                    and file.tokens[cursor].value not in CONTINUATIONS:
                cursor += 1
                break
            cursor += 1
    return members


def extract_modules(file: TsFile) -> None:
    tokens = file.tokens
    for index, token in enumerate(tokens):
        if token.is_name("import"):
            nxt = tokens[index + 1] if index + 1 < len(tokens) else None
            if nxt is None:
                continue
            if nxt.is_op("("):  # dynamic import()
                inner = tokens[index + 2] if index + 2 < len(tokens) else None
                if inner is not None and inner.kind in ("str", "template"):
                    file.imports.append(Import(module=_literal(inner.value), line=token.line, kind="dynamic"))
                continue
            if nxt.is_op("."):  # import.meta
                continue
            # `{ import: "default" }` — a property named `import`, not a statement.
            if not (nxt.is_op("{", "*") or nxt.kind in ("str", "template") or nxt.kind == "name"):
                continue
            if index and file.tokens[index - 1].is_op(".", "?.", "{", ","):
                continue
            file.imports.append(_parse_import(file, index))
        elif token.is_name("require") and index + 1 < len(tokens) and tokens[index + 1].is_op("("):
            inner = tokens[index + 2] if index + 2 < len(tokens) else None
            if inner is not None and inner.kind == "str":
                file.imports.append(Import(module=_literal(inner.value), line=token.line, kind="require"))
        elif token.is_name("export"):
            _parse_export(file, index)


def _literal(raw: str) -> str:
    return raw.strip("'\"`")


def _parse_import(file: TsFile, index: int) -> Import:
    tokens = file.tokens
    cursor = index + 1
    record = Import(module="", line=tokens[index].line, kind="import")
    if tokens[cursor].kind in ("str", "template"):
        record.module = _literal(tokens[cursor].value)
        record.side_effect_only = True
        return record
    if tokens[cursor].is_name("type"):
        record.is_type_only = True
        cursor += 1
    while cursor < len(tokens):
        token = tokens[cursor]
        if token.is_name("from"):
            nxt = tokens[cursor + 1] if cursor + 1 < len(tokens) else None
            if nxt is not None and nxt.kind in ("str", "template"):
                record.module = _literal(nxt.value)
            break
        if token.kind in ("str", "template"):
            record.module = _literal(token.value)
            break
        if token.is_op("{"):
            close = file.closer(cursor)
            record.names.extend(_named_bindings(file, cursor, close))
            cursor = close + 1
            continue
        if token.is_op("*"):
            if cursor + 2 < len(tokens) and tokens[cursor + 1].is_name("as"):
                record.namespace_name = tokens[cursor + 2].value
                cursor += 3
                continue
        elif token.kind == "name" and not record.default_name and token.value not in ("as", "type"):
            record.default_name = token.value
        elif token.is_op(";"):
            break
        cursor += 1
    return record


def _named_bindings(file: TsFile, open_index: int, close_index: int) -> list[str]:
    """The local names bound by a `{ a, b as c }` clause."""
    names: list[str] = []
    cursor = open_index + 1
    while cursor < close_index:
        token = file.tokens[cursor]
        if token.kind == "name" and not token.is_name("as", "type"):
            local = token.value
            if cursor + 2 < close_index and file.tokens[cursor + 1].is_name("as"):
                local = file.tokens[cursor + 2].value
                cursor += 2
            names.append(local)
        cursor += 1
    return names


def _parse_export(file: TsFile, index: int) -> None:
    tokens = file.tokens
    cursor = index + 1
    if cursor >= len(tokens):
        return
    line = tokens[index].line
    token = tokens[cursor]
    if token.is_name("default"):
        file.exports.append(Export(name="default", line=line, kind="default"))
        return
    if token.is_op("*"):
        module = ""
        for probe in range(cursor, min(cursor + 6, len(tokens))):
            if tokens[probe].kind == "str":
                module = _literal(tokens[probe].value)
                break
        file.exports.append(Export(name="*", line=line, kind="star"))
        if module:
            file.imports.append(Import(module=module, line=line, kind="export-from"))
        return
    type_only = token.is_name("type")
    if type_only and cursor + 1 < len(tokens) and tokens[cursor + 1].is_op("{"):
        cursor += 1
        token = tokens[cursor]
    if token.is_op("{"):
        close = file.closer(cursor)
        module = ""
        after = close + 1
        if after + 1 < len(tokens) and tokens[after].is_name("from") and tokens[after + 1].kind == "str":
            module = _literal(tokens[after + 1].value)
        for name in _named_bindings(file, cursor, close):
            file.exports.append(Export(name=name, line=line, kind="named", is_type_only=type_only))
        if module:
            file.imports.append(Import(module=module, line=line, kind="export-from"))
        return
    # `export const x = ...`, `export function f()`, `export class C`, ...
    cursor = index + 1
    while cursor < len(tokens) and tokens[cursor].kind == "name" \
            and tokens[cursor].value in {"const", "let", "var", "async", "declare", "abstract", "default"}:
        cursor += 1
    if cursor >= len(tokens):
        return
    keyword = tokens[cursor].value
    name_index = cursor + 1 if keyword in {"function", "class", "interface", "type", "enum", "namespace"} else cursor
    if keyword == "function" and name_index < len(tokens) and tokens[name_index].is_op("*"):
        name_index += 1
    if name_index < len(tokens) and tokens[name_index].kind == "name":
        file.exports.append(Export(name=tokens[name_index].value, line=line, kind="declaration",
                                   is_type_only=keyword in {"interface", "type"}))


