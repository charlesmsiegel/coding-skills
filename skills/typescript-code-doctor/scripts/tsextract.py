#!/usr/bin/env python3
"""Declaration extraction: functions, arrow functions and classes.

The hard case throughout is that TypeScript's type syntax and its value syntax
overlap. `(a: string) => void` is a function when it is a value and a type when
it annotates one, and telling them apart is what the recorded type spans are
for. See TsFile.in_type_position.
"""

from tsnodes import Class, Func, Param, Prop, TsFile
from tslex import MEMBER_MODIFIERS, RESERVED, CLOSERS, CONTROL_KEYWORDS, OPENERS


# --------------------------------------------------------------------------- #
# Declaration extraction
# --------------------------------------------------------------------------- #

_STATEMENT_ENDERS = frozenset({";", "}", "{"})
# Operators that make a line continue onto the next one, so a newline after
# them is not a statement break.
CONTINUATIONS = frozenset({
    ",", "+", "-", "*", "/", "%", "=", "==", "===", "!=", "!==", "<", ">", "<=",
    ">=", "&&", "||", "??", "?", ":", "=>", "|", "&", ".", "(", "[", "{", "...",
})


def modifiers_before(file: TsFile, index: int) -> tuple[int, set[str]]:
    """Walk left over declaration modifiers. Returns (first index, modifiers)."""
    words: set[str] = set()
    cursor = index - 1
    while cursor >= 0:
        token = file.tokens[cursor]
        if token.kind == "name" and token.value in MEMBER_MODIFIERS | {"export", "default", "const", "let", "var"}:
            words.add(token.value)
            cursor -= 1
        else:
            break
    return cursor + 1, words


def _is_exported(file: TsFile, index: int) -> bool:
    _, words = modifiers_before(file, index)
    return "export" in words


# A `{` right after one of these opens an object *type*; anywhere else after a
# complete type it opens the function body (or the next block) instead.
_TYPE_CONTINUING = frozenset({":", "|", "&", "=>", "<", "(", ",", "[", "?", "=", "extends"})


def read_type_after_colon(file: TsFile, colon: int, stop: int) -> tuple[str, int]:
    """Read a type annotation starting at a `:`. Returns (text, index after).

    The span is recorded on the file so `in_type_position` can later tell a
    function type from a function.
    """
    cursor = colon + 1
    start = cursor
    while cursor < stop:
        token = file.tokens[cursor]
        if token.kind == "op":
            if token.value == "{":
                previous = file.tokens[cursor - 1]
                if previous.value not in _TYPE_CONTINUING and not previous.is_name("extends", "keyof", "infer"):
                    break  # this brace opens a body, not an object type
                cursor = file.skip_group(cursor)
                continue
            if token.value in OPENERS:
                cursor = file.skip_group(cursor)
                continue
            if token.value in (",", ";", ")", "]", "}", "="):
                break
        if token.kind == "name" and token.value in ("const", "let", "var", "function", "class", "return"):
            break
        # A newline followed by something that cannot continue a type ends it.
        previous = file.tokens[cursor - 1]
        if cursor > start and token.line > previous.line and previous.value not in CONTINUATIONS \
                and token.value not in CONTINUATIONS and token.kind == "name":
            break
        cursor += 1
    if cursor > start:
        file.type_spans.append((start, cursor - 1))
    return file.slice(start, cursor).strip(), cursor


def _parse_params(file: TsFile, open_index: int, close_index: int) -> list[Param]:
    params: list[Param] = []
    cursor = open_index + 1
    while cursor < close_index:
        chunk_end = cursor
        depth_guard = 0
        while chunk_end < close_index:
            token = file.tokens[chunk_end]
            if token.kind == "op" and token.value in OPENERS:
                chunk_end = file.skip_group(chunk_end)
                depth_guard += 1
                continue
            if token.kind == "op" and token.value == ",":
                break
            chunk_end += 1
        param = _parse_one_param(file, cursor, chunk_end)
        if param is not None:
            params.append(param)
        cursor = chunk_end + 1
    return params


def _parse_one_param(file: TsFile, start: int, stop: int) -> Param | None:
    if start >= stop:
        return None
    cursor = start
    accessibility = None
    readonly = False
    decorated = True
    while decorated and cursor < stop:
        token = file.tokens[cursor]
        if token.is_op("@"):
            cursor += 1
            if cursor < stop and file.tokens[cursor].kind == "name":
                cursor += 1
            if cursor < stop and file.tokens[cursor].is_op("("):
                cursor = file.skip_group(cursor)
        elif token.is_name("public", "private", "protected"):
            accessibility = token.value
            cursor += 1
        elif token.is_name("readonly"):
            readonly = True
            cursor += 1
        else:
            decorated = False
    if cursor >= stop:
        return None
    token = file.tokens[cursor]
    is_rest = token.is_op("...")
    if is_rest:
        cursor += 1
        token = file.tokens[cursor] if cursor < stop else token
    destructured = token.is_op("{", "[")
    if destructured:
        name = file.slice(cursor, min(file.skip_group(cursor), stop))[:60]
        cursor = file.skip_group(cursor)
    elif token.kind == "name":
        name = token.value
        cursor += 1
    else:
        return None
    optional = cursor < stop and file.tokens[cursor].is_op("?")
    if optional:
        cursor += 1
    type_text = ""
    if cursor < stop and file.tokens[cursor].is_op(":"):
        type_text, cursor = read_type_after_colon(file, cursor, stop)
    has_default = cursor < stop and file.tokens[cursor].is_op("=")
    return Param(
        name=name, type_text=type_text, line=file.tokens[start].line, optional=optional,
        has_default=has_default, is_rest=is_rest, is_destructured=destructured,
        accessibility=accessibility, readonly=readonly,
    )


def _finish_function(file: TsFile, func: Func) -> int:
    """Fill in params, return type and body span. Returns the index after it.

    Callers need that index: an overload or `abstract` signature has no body,
    so the return type is the last thing consumed and resuming at the closing
    paren would re-read it as another member.
    """
    func.params = _parse_params(file, func.params_open, func.params_close)
    cursor = func.params_close + 1
    if cursor < len(file) and file.tokens[cursor].is_op(":"):
        func.return_type, cursor = read_type_after_colon(file, cursor, len(file))
    if cursor < len(file) and file.tokens[cursor].is_op("{"):
        func.body_open = cursor
        func.body_close = file.closer(cursor)
        return func.body_close + 1
    func.body_open = -1
    func.body_close = -1
    return cursor + 1 if cursor < len(file) and file.tokens[cursor].is_op(";") else cursor


def extract_functions(file: TsFile) -> None:
    tokens = file.tokens
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.is_name("function"):
            start, words = modifiers_before(file, index)
            cursor = index + 1
            generator = tokens[cursor].is_op("*") if cursor < len(tokens) else False
            if generator:
                cursor += 1
            name = ""
            if cursor < len(tokens) and tokens[cursor].kind == "name":
                name = tokens[cursor].value
                cursor += 1
            cursor = skip_type_parameters(file, cursor)
            if cursor < len(tokens) and tokens[cursor].is_op("("):
                func = Func(
                    name=name or "<anonymous>", kind="function", line=token.line, start=start,
                    params_open=cursor, params_close=file.closer(cursor),
                    body_open=-1, body_close=-1,
                    is_async="async" in words, is_generator=generator,
                    is_exported="export" in words,
                )
                _finish_function(file, func)
                file.functions.append(func)
        index += 1


def skip_type_parameters(file: TsFile, index: int) -> int:
    """Step over a `<T, U extends V>` generic clause, if one is present.

    `<` and `>` are not bracket-matched (they are also comparison operators), so
    this counts them, stopping at the `(` that starts the parameter list.
    """
    if index >= len(file) or not file.tokens[index].is_op("<"):
        return index
    depth = 0
    cursor = index
    while cursor < len(file):
        token = file.tokens[cursor]
        if token.is_op("<"):
            depth += 1
        elif token.is_op(">"):
            depth -= 1
            if depth == 0:
                return cursor + 1
        elif token.is_op(">>", ">>>"):
            depth -= len(token.value)
            if depth <= 0:
                return cursor + 1
        elif token.is_op("(", "{", "["):
            cursor = file.skip_group(cursor)
            continue
        elif token.is_op(";") or token.value == "=>":
            return index
        cursor += 1
    return index


def extract_variable_annotations(file: TsFile) -> None:
    """Record the type spans of `const x: T = ...` so arrows inside them are types."""
    tokens = file.tokens
    for index, token in enumerate(tokens):
        if not token.is_name("const", "let", "var"):
            continue
        name = index + 1
        if name + 1 < len(tokens) and tokens[name].kind == "name" and tokens[name + 1].is_op(":"):
            read_type_after_colon(file, name + 1, len(tokens))


def extract_arrows(file: TsFile) -> None:
    tokens = file.tokens
    # Descending, so an arrow's own parameter types are recorded before the
    # nested `=>` inside them is considered — that nested arrow is a type.
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index]
        if not token.is_op("=>") or file.in_type_position(index):
            continue
        located = _arrow_parameter_list(file, index)
        if located is None:
            continue
        params_open, params_close = located
        anchor = params_open if params_open >= 0 else params_close
        name, kind, is_async, exported, start = _arrow_name(file, anchor)
        func = Func(
            name=name, kind=kind, line=token.line, start=start,
            params_open=params_open, params_close=params_close,
            body_open=-1, body_close=-1, is_async=is_async, is_exported=exported,
        )
        if params_open >= 0:
            func.params = _parse_params(file, params_open, params_close)
        else:
            func.params = [Param(name=tokens[params_close].value, type_text="", line=token.line)]
        body = index + 1
        if body < len(tokens) and tokens[body].is_op("{"):
            func.body_open = body
            func.body_close = file.closer(body)
        file.functions.append(func)


def _arrow_parameter_list(file: TsFile, arrow: int) -> tuple[int, int] | None:
    """Locate the parameter list of the arrow function whose `=>` is at ``arrow``."""
    tokens = file.tokens
    previous = arrow - 1
    if previous < 0:
        return None
    if tokens[previous].is_op(")"):
        opener = file.closer(previous)
        return (opener, previous) if opener >= 0 else None
    # `(x): ReturnType => ...` — walk left over the return type to its `:`.
    cursor, steps = previous, 0
    while cursor >= 0 and steps < 80:
        token = tokens[cursor]
        if token.kind == "op" and token.value in CLOSERS:
            opener = file.closer(cursor)
            cursor = (opener if opener >= 0 else cursor) - 1
            steps += 1
            continue
        if token.is_op(":"):
            before = cursor - 1
            if before >= 0 and tokens[before].is_op(")"):
                opener = file.closer(before)
                return (opener, before) if opener >= 0 else None
            break
        if token.is_op(";", "=>", "{"):
            break
        cursor -= 1
        steps += 1
    if tokens[previous].kind == "name" and tokens[previous].value not in RESERVED:
        return (-1, previous)  # single identifier parameter, no parentheses
    return None


def _arrow_name(file: TsFile, anchor: int) -> tuple[str, str, bool, bool, int]:
    """Name, kind, async-ness, exportedness and start index of an arrow."""
    tokens = file.tokens
    cursor = anchor - 1
    is_async = False
    while cursor >= 0 and tokens[cursor].is_name("async"):
        is_async = True
        cursor -= 1
    cursor = _rewind_type_parameters(file, cursor)
    start = cursor + 1 if cursor + 1 <= anchor else anchor
    if cursor < 0:
        return "<anonymous>", "arrow", is_async, False, start
    token = tokens[cursor]
    if token.is_op("=", ":") and cursor - 1 >= 0 and tokens[cursor - 1].kind in ("name", "str"):
        name = tokens[cursor - 1].value.strip("'\"")
        declaration = cursor - 2
        exported = False
        begin = cursor - 1
        if declaration >= 0 and tokens[declaration].is_name("const", "let", "var"):
            begin = declaration
            exported = _is_exported(file, declaration)
        return name, "arrow", is_async, exported, begin
    return "<anonymous>", "arrow", is_async, False, start


def _rewind_type_parameters(file: TsFile, index: int) -> int:
    """Step left over a `<T>` clause that precedes an arrow's parameter list."""
    if index < 0 or not file.tokens[index].is_op(">"):
        return index
    depth = 0
    cursor = index
    while cursor >= 0:
        token = file.tokens[cursor]
        if token.is_op(">"):
            depth += 1
        elif token.is_op("<"):
            depth -= 1
            if depth == 0:
                return cursor - 1
        elif token.is_op(";", "{", "}"):
            return index
        cursor -= 1
    return index


def extract_classes(file: TsFile) -> None:
    tokens = file.tokens
    index = 0
    while index < len(tokens):
        if not tokens[index].is_name("class"):
            index += 1
            continue
        start, words = modifiers_before(file, index)
        cursor = index + 1
        name = ""
        if cursor < len(tokens) and tokens[cursor].kind == "name" and tokens[cursor].value not in ("extends", "implements"):
            name = tokens[cursor].value
            cursor += 1
        cursor = skip_type_parameters(file, cursor)
        extends, implements = None, []
        while cursor < len(tokens) and not tokens[cursor].is_op("{"):
            if tokens[cursor].is_name("extends") and cursor + 1 < len(tokens):
                extends = tokens[cursor + 1].value
            elif tokens[cursor].is_name("implements"):
                cursor += 1
                while cursor < len(tokens) and not tokens[cursor].is_op("{"):
                    if tokens[cursor].kind == "name":
                        implements.append(tokens[cursor].value)
                    cursor += 1
                break
            cursor += 1
        if cursor >= len(tokens) or not tokens[cursor].is_op("{"):
            index += 1
            continue
        body_close = file.closer(cursor)
        klass = Class(
            name=name or "<anonymous>", line=tokens[index].line, start=start,
            body_open=cursor, body_close=body_close,
            extends=extends, implements=implements,
            is_abstract="abstract" in words, is_exported="export" in words,
            is_default_export="export" in words and "default" in words,
            decorators=_decorators_before(file, start),
        )
        _parse_class_body(file, klass)
        file.classes.append(klass)
        index = body_close + 1 if body_close > index else index + 1


def _decorators_before(file: TsFile, index: int) -> list[str]:
    """Decorator names attached above the declaration starting at ``index``."""
    names: list[str] = []
    cursor = index - 1
    while cursor >= 1:
        token = file.tokens[cursor]
        if token.is_op(")"):
            opener = file.closer(cursor)
            if opener < 1 or file.tokens[opener - 1].kind != "name":
                break
            candidate = opener - 1
            while candidate >= 1 and file.tokens[candidate - 1].is_op("."):
                candidate -= 2
            if candidate >= 1 and file.tokens[candidate - 1].is_op("@"):
                names.append(file.tokens[candidate].value)
                cursor = candidate - 2
                continue
            break
        if token.kind == "name" and cursor >= 1 and file.tokens[cursor - 1].is_op("@"):
            names.append(token.value)
            cursor -= 2
            continue
        break
    return list(reversed(names))


def _parse_class_body(file: TsFile, klass: Class) -> None:
    tokens = file.tokens
    cursor = klass.body_open + 1
    while cursor < klass.body_close:
        token = tokens[cursor]
        if token.is_op(";", ","):
            cursor += 1
            continue
        member_start = cursor
        decorators: list[str] = []
        while cursor < klass.body_close and tokens[cursor].is_op("@"):
            cursor += 1
            if cursor < klass.body_close and tokens[cursor].kind == "name":
                decorators.append(tokens[cursor].value)
                cursor += 1
                while cursor < klass.body_close and tokens[cursor].is_op("."):
                    cursor += 2
            if cursor < klass.body_close and tokens[cursor].is_op("("):
                cursor = file.skip_group(cursor)
        modifiers: set[str] = set()
        while cursor < klass.body_close and tokens[cursor].kind == "name" \
                and tokens[cursor].value in MEMBER_MODIFIERS:
            nxt = tokens[cursor + 1] if cursor + 1 < klass.body_close else None
            # `readonly` / `static` used as a member NAME rather than a modifier.
            if nxt is not None and (nxt.is_op("(", "=", ":", ";", "?")):
                break
            modifiers.add(tokens[cursor].value)
            cursor += 1
        accessor_kind = ""
        if cursor + 1 < klass.body_close and tokens[cursor].is_name("get", "set") \
                and (tokens[cursor + 1].kind == "name" or tokens[cursor + 1].is_op("[")):
            accessor_kind = tokens[cursor].value
            cursor += 1
        generator = cursor < klass.body_close and tokens[cursor].is_op("*")
        if generator:
            cursor += 1
        if cursor >= klass.body_close:
            break
        name_token = tokens[cursor]
        if name_token.is_op("["):
            name = file.slice(cursor, file.skip_group(cursor))[:50]
            cursor = file.skip_group(cursor)
        elif name_token.kind in ("name", "str", "num"):
            name = name_token.value.strip("'\"")
            cursor += 1
        else:
            cursor += 1
            continue
        optional = cursor < klass.body_close and tokens[cursor].is_op("?")
        if optional or (cursor < klass.body_close and tokens[cursor].is_op("!")):
            cursor += 1
        cursor = skip_type_parameters(file, cursor)
        if cursor < klass.body_close and tokens[cursor].is_op("("):
            close = file.closer(cursor)
            kind = accessor_kind + "ter" if accessor_kind else ("constructor" if name == "constructor" else "method")
            func = Func(
                name=name, kind=kind, line=name_token.line, start=member_start,
                params_open=cursor, params_close=close, body_open=-1, body_close=-1,
                is_async="async" in modifiers, is_generator=generator,
                is_static="static" in modifiers, is_abstract="abstract" in modifiers,
                accessibility=_accessibility(modifiers, name),
                owner=klass.name, decorators=decorators,
            )
            cursor = _finish_function(file, func)
            klass.methods.append(func)
            file.functions.append(func)
            continue
        prop = Prop(
            name=name, line=name_token.line, index=member_start,
            accessibility=_accessibility(modifiers, name),
            is_static="static" in modifiers, readonly="readonly" in modifiers,
            optional=optional, decorators=decorators,
        )
        if cursor < klass.body_close and tokens[cursor].is_op(":"):
            prop.type_text, cursor = read_type_after_colon(file, cursor, klass.body_close)
        if cursor < klass.body_close and tokens[cursor].is_op("="):
            prop.has_initializer = True
            init_start = cursor + 1
            cursor = skip_to_statement_end(file, cursor + 1, klass.body_close)
            prop.initializer = file.slice(init_start, cursor)[:200].rstrip(";").strip()
        else:
            cursor = skip_to_statement_end(file, cursor, klass.body_close)
        klass.props.append(prop)


def _accessibility(modifiers: set[str], name: str) -> str | None:
    for level in ("private", "protected", "public"):
        if level in modifiers:
            return level
    return "private" if name.startswith("#") else None


def skip_to_statement_end(file: TsFile, index: int, stop: int) -> int:
    """Advance past the end of a statement: a `;`, or an ASI-style line break."""
    cursor = index
    while cursor < stop:
        token = file.tokens[cursor]
        if token.kind == "op" and token.value in OPENERS:
            cursor = file.skip_group(cursor)
            continue
        if token.is_op(";"):
            return cursor + 1
        nxt = file.tokens[cursor + 1] if cursor + 1 < stop else None
        if nxt is not None and nxt.line > token.line \
                and token.value not in CONTINUATIONS and nxt.value not in CONTINUATIONS:
            return cursor + 1
        cursor += 1
    return stop


def extract_object_methods(file: TsFile) -> None:
    """Methods written as `name(args) { ... }` outside a class body.

    Object-literal methods and the shorthand members of a `describe` block are
    real functions with real complexity, and the class walker has already
    claimed the ones inside classes.
    """
    tokens = file.tokens
    claimed = {func.params_open for func in file.functions}
    for index, token in enumerate(tokens):
        if token.kind != "name" or token.value in RESERVED:
            continue
        opener = index + 1
        if opener >= len(tokens):
            break
        opener = skip_type_parameters(file, opener)
        if opener >= len(tokens) or not tokens[opener].is_op("(") or opener in claimed:
            continue
        close = file.closer(opener)
        after = close + 1
        if after < len(tokens) and tokens[after].is_op(":"):
            _, after = read_type_after_colon(file, after, len(tokens))
        if after >= len(tokens) or not tokens[after].is_op("{"):
            continue
        previous = tokens[index - 1] if index else None
        if previous is not None and (previous.is_op(".", "?.", "=", "new") or previous.is_name(*CONTROL_KEYWORDS)):
            continue
        if file.enclosing_class(index) is not None:
            continue
        is_async = bool(previous is not None and previous.is_name("async"))
        func = Func(
            name=token.value, kind="method", line=token.line,
            start=index - 1 if is_async else index,
            params_open=opener, params_close=close,
            body_open=after, body_close=file.closer(after), is_async=is_async,
        )
        func.params = _parse_params(file, opener, close)
        file.functions.append(func)


