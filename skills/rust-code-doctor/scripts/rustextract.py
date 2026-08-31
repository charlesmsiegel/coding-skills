#!/usr/bin/env python3
"""Turn a token stream into items: functions, types, traits, impls, uses, mods.

The pass is linear rather than recursive. Every item keyword is found in one
sweep, bodies are located through the bracket map, and ownership (which impl a
method belongs to) is assigned afterwards by span containment. That is simpler
than a recursive descent and it degrades better: a construct this file does not
understand costs one item, not the rest of the file.

Angle brackets are not in the bracket map — `a < b` and `Vec<T>` are the same
characters — so generic argument lists are stepped over by `skip_generics`,
which counts `<` and `>` only where a type is already expected.
"""

from rustlex import OPENERS, RustSyntaxError
from rustnodes import Binding, Field, Func, Impl, ModDecl, Param, RsFile, Trait, TypeDef, Use, Variant

__all__ = ["extract", "find_body_brace", "skip_generics", "split_top_level",
           "RustSyntaxError"]

# Modifiers that may sit between an attribute and the item keyword.
_LEAD_MODIFIERS = frozenset({"pub", "async", "unsafe", "const", "extern", "default", "move"})

# Keywords that start an item. `type` and `const` need extra checks below.
_ITEM_KEYWORDS = frozenset({
    "fn", "struct", "enum", "union", "trait", "impl", "mod", "use", "type",
    "const", "static", "macro_rules",
})

# Attributes whose presence marks the item as test-only.
_TEST_ATTRS = ("test", "tokio::test", "async_std::test", "bench", "rstest", "proptest")


# --------------------------------------------------------------------------- #
# Small scanning helpers, shared with the detectors through rsparse
# --------------------------------------------------------------------------- #

def skip_generics(file: RsFile, index: int) -> int:
    """Index just past the `<…>` starting at ``index``; ``index`` if it is not one."""
    if file.value(index) != "<":
        return index
    depth = 0
    cursor = index
    while cursor < len(file.tokens):
        token = file.tokens[cursor]
        if token.kind == "op":
            if token.value == "<":
                depth += 1
            elif token.value == "<<":
                depth += 2
            elif token.value in (">", ">="):
                depth -= 1
                if depth <= 0:
                    return cursor + 1
            elif token.value == ">>":
                depth -= 2
                if depth <= 0:
                    return cursor + 1
            elif token.value in OPENERS:
                cursor = file.skip_group(cursor)
                continue
            elif token.value in (";", "}"):
                return index  # not a generic list after all
        cursor += 1
    return index


def split_top_level(file: RsFile, start: int, stop: int, separator: str = ",") -> list[tuple[int, int]]:
    """(start, end) spans between top-level ``separator`` tokens in [start, stop)."""
    spans: list[tuple[int, int]] = []
    cursor, begin, angle = start, start, 0
    while cursor < stop:
        token = file.tokens[cursor]
        if token.kind == "op":
            if token.value in OPENERS:
                cursor = file.skip_group(cursor)
                continue
            # The lexer emits `>>` as one token, so `Vec<Vec<u8>>` closes two
            # levels at once. Decrementing by one leaves the depth stuck above
            # zero and every later comma is swallowed — which merged whole
            # parameter lists and struct field lists into a single entry.
            if token.value == "<":
                angle += 1
            elif token.value == "<<":
                angle += 2
            elif token.value in (">", ">=") and angle:
                angle -= 1
            elif token.value == ">>" and angle:
                angle = max(0, angle - 2)
            elif token.value == separator and not angle:
                if cursor > begin:
                    spans.append((begin, cursor))
                begin = cursor + 1
        cursor += 1
    if stop > begin:
        spans.append((begin, stop))
    return spans


def find_body_brace(file: RsFile, start: int, stop: int) -> int:
    """The `{` that opens an item body, stepping over generic argument lists.

    `where T: Bound<{ N + 1 }>` contains a brace belonging to a const-generic
    expression. Taking the first `{` made that expression the function body, so
    every detector then analysed the wrong tokens — silently, since a body was
    still found.
    """
    cursor = start
    while cursor < min(stop, len(file.tokens)):
        token = file.tokens[cursor]
        if token.kind == "op":
            if token.value == "<":
                end = skip_generics(file, cursor)
                if end > cursor:
                    cursor = end
                    continue
            if token.value == "{":
                return cursor
            if token.value == ";":
                return -1
            if token.value in ("(", "["):
                cursor = file.skip_group(cursor)
                continue
        cursor += 1
    return -1


def _find_name_kw(file: RsFile, keyword: str, start: int, stop: int) -> int:
    """First index in [start, stop) holding ``keyword`` at bracket depth 0."""
    cursor = start
    while cursor < min(stop, len(file.tokens)):
        token = file.tokens[cursor]
        if token.kind == "op" and token.value in OPENERS:
            cursor = file.skip_group(cursor)
            continue
        if token.is_name(keyword):
            return cursor
        cursor += 1
    return -1


# --------------------------------------------------------------------------- #
# Attributes and modifiers
# --------------------------------------------------------------------------- #

def _attribute_spans(file: RsFile) -> dict[int, tuple[int, str, bool]]:
    """`#` index -> (index just past the `]`, inner text, is_inner_attribute)."""
    spans: dict[int, tuple[int, str, bool]] = {}
    for index, token in enumerate(file.tokens):
        if not token.is_op("#"):
            continue
        inner = file.value(index + 1) == "!"
        bracket = index + (2 if inner else 1)
        if file.value(bracket) != "[":
            continue
        close = file.closer(bracket)
        if close < 0:
            continue
        spans[index] = (close + 1, file.slice(bracket + 1, close).strip(), inner)
    return spans


def _lead(file: RsFile, index: int, attrs: dict) -> tuple[int, dict]:
    """Walk left from an item keyword over its modifiers, visibility and attributes.

    Returns (index of the item's first token, parsed modifiers).
    """
    info = {"visibility": "", "is_async": False, "is_unsafe": False,
            "is_const": False, "is_extern": False, "attrs": []}
    cursor = index
    while cursor > 0:
        previous = file.tokens[cursor - 1]
        if previous.kind == "name" and previous.value in _LEAD_MODIFIERS:
            if previous.value == "pub":
                info["visibility"] = "pub"
            elif previous.value == "async":
                info["is_async"] = True
            elif previous.value == "unsafe":
                info["is_unsafe"] = True
            elif previous.value == "const":
                info["is_const"] = True
            elif previous.value == "extern":
                info["is_extern"] = True
            cursor -= 1
            continue
        if previous.kind == "str" and cursor >= 2 and file.tokens[cursor - 2].is_name("extern"):
            info["is_extern"] = True
            cursor -= 2
            continue
        if previous.is_op(")"):  # `pub(crate)`, `pub(super)`, `pub(in path)`
            opener = file.closer(cursor - 1)
            if opener > 0 and file.tokens[opener - 1].is_name("pub"):
                info["visibility"] = "pub(" + file.slice(opener + 1, cursor - 1).strip() + ")"
                cursor = opener - 1
                continue
            break
        if previous.is_op("]"):  # an attribute; find the `#` that opened it
            opener = file.closer(cursor - 1)
            hash_index = opener - 2 if opener >= 2 and file.tokens[opener - 1].is_op("!") else opener - 1
            if hash_index >= 0 and hash_index in attrs:
                info["attrs"].insert(0, attrs[hash_index][1])
                cursor = hash_index
                continue
            break
        break
    return cursor, info


def _at_item_position(file: RsFile, start: int) -> bool:
    """True when nothing but a statement boundary precedes the item's first token."""
    if start == 0:
        return True
    previous = file.tokens[start - 1]
    if previous.kind == "op" and previous.value in (";", "{", "}", "]"):
        return True
    # `pub use` inside a `mod x { … }` body, `impl` after a doc comment: both
    # already covered. A `>` or `,` before an item keyword means a type position.
    return False


# --------------------------------------------------------------------------- #
# Item parsers
# --------------------------------------------------------------------------- #

def _parse_params(file: RsFile, open_paren: int, close_paren: int) -> list[Param]:
    params: list[Param] = []
    for span_start, span_end in split_top_level(file, open_paren + 1, close_paren):
        # Skip a per-parameter attribute such as `#[allow(unused)] x: u8`.
        cursor = span_start
        while file.value(cursor) == "#":
            bracket = cursor + 1
            if file.value(bracket) != "[":
                break
            cursor = file.skip_group(bracket)
        if cursor >= span_end:
            continue
        text = file.slice(cursor, span_end).strip()
        line = file.line_of(cursor)
        normalized = " ".join(text.replace("&", "& ").split())
        if normalized in ("self", "& self", "& mut self", "mut self") \
                or normalized.startswith("& '") and normalized.endswith("self") \
                or normalized.startswith("self :"):
            params.append(Param("self", text, line, is_self=True,
                                by_ref="&" in text, is_mut="mut self" in normalized))
            continue
        colon = -1
        cursor2, angle = cursor, 0
        while cursor2 < span_end:
            token = file.tokens[cursor2]
            if token.kind == "op":
                if token.value in OPENERS:
                    cursor2 = file.skip_group(cursor2)
                    continue
                if token.value == "<":
                    angle += 1
                elif token.value == ">" and angle:
                    angle -= 1
                elif token.value == ":" and not angle:
                    colon = cursor2
                    break
            cursor2 += 1
        if colon < 0:
            params.append(Param(text, "", line))
            continue
        name = file.slice(cursor, colon).strip()
        params.append(Param(name.replace("mut ", "").strip(), file.slice(colon + 1, span_end).strip(),
                            line, is_mut=name.startswith("mut ")))
    return params


def _parse_fn(file: RsFile, index: int, attrs: dict) -> Func | None:
    name_index = index + 1
    if name_index >= len(file.tokens) or file.tokens[name_index].kind != "name":
        return None
    start, info = _lead(file, index, attrs)
    if not _at_item_position(file, start):
        return None
    cursor = name_index + 1
    generics = ""
    if file.value(cursor) == "<":
        end = skip_generics(file, cursor)
        generics = file.slice(cursor, end)
        cursor = end
    if file.value(cursor) != "(":
        return None
    params_open = cursor
    params_close = file.closer(params_open)
    if params_close < 0:
        return None

    limit = len(file.tokens)
    brace = find_body_brace(file, params_close + 1, limit)
    semi = file.find_op(";", params_close + 1, limit)
    where_index = _find_name_kw(file, "where", params_close + 1,
                                min(x for x in (brace, semi, limit) if x >= 0))
    tail_stop = min(x for x in (brace, semi, where_index, limit) if x >= 0)
    arrow = file.find_op("->", params_close + 1, tail_stop)
    return_type = file.slice(arrow + 1, tail_stop).strip() if arrow >= 0 else ""

    body_open, body_close = -1, -1
    if brace >= 0 and (semi < 0 or brace < semi):
        body_open, body_close = brace, file.closer(brace)

    func = Func(
        name=file.value(name_index), kind="fn", line=file.tokens[index].line, start=start,
        params_open=params_open, params_close=params_close,
        body_open=body_open, body_close=body_close,
        params=_parse_params(file, params_open, params_close),
        return_type=return_type, generics=generics,
        is_async=info["is_async"], is_unsafe=info["is_unsafe"],
        is_const=info["is_const"], is_extern=info["is_extern"],
        visibility=info["visibility"], attrs=info["attrs"],
        doc_lines=file.doc_lines_before(file.tokens[start].line),
    )
    return func


def _parse_type_def(file: RsFile, index: int, attrs: dict) -> TypeDef | None:
    keyword = file.value(index)
    name_index = index + 1
    if name_index >= len(file) or file.tokens[name_index].kind != "name":
        return None
    start, info = _lead(file, index, attrs)
    if not _at_item_position(file, start):
        return None
    cursor = name_index + 1
    generics = ""
    if file.value(cursor) == "<":
        end = skip_generics(file, cursor)
        generics = file.slice(cursor, end)
        cursor = end

    derives: list[str] = []
    for attribute in info["attrs"]:
        stripped = attribute.replace(" ", "")
        if stripped.startswith("derive(") and stripped.endswith(")"):
            derives += [d for d in stripped[len("derive("):-1].split(",") if d]

    definition = TypeDef(kind=keyword, name=file.value(name_index), line=file.tokens[index].line,
                         start=start, body_open=-1, body_close=-1,
                         visibility=info["visibility"], generics=generics, derives=derives,
                         attrs=info["attrs"],
                         doc_lines=file.doc_lines_before(file.tokens[start].line))

    limit = len(file.tokens)
    # Find this item's own end first. Searching for `where` across the whole
    # file would otherwise pick up a *later* function's where clause, and the
    # cursor would then jump past this type's body — giving the struct the
    # function's braces and no fields at all.
    brace = find_body_brace(file, cursor, limit)
    paren = file.find_op("(", cursor, limit)
    semi = file.find_op(";", cursor, limit)
    item_end = min(x for x in (brace, paren, semi, limit) if x >= 0)
    where_index = _find_name_kw(file, "where", cursor, item_end)
    if where_index >= 0:
        cursor = where_index
        brace = find_body_brace(file, cursor, limit)
        paren = file.find_op("(", cursor, limit)
        semi = file.find_op(";", cursor, limit)
    if brace >= 0 and (semi < 0 or brace < semi):
        definition.body_open, definition.body_close = brace, file.closer(brace)
        if keyword == "enum":
            definition.variants = _parse_variants(file, brace, definition.body_close)
        else:
            definition.fields = _parse_fields(file, brace, definition.body_close, attrs)
    elif paren >= 0 and (semi < 0 or paren < semi):
        definition.body_open, definition.body_close = paren, file.closer(paren)
        for position, (span_start, span_end) in enumerate(
                split_top_level(file, paren + 1, definition.body_close)):
            text = file.slice(span_start, span_end).strip()
            visibility = "pub" if text.startswith("pub") else ""
            definition.fields.append(Field(str(position), text, file.line_of(span_start), visibility))
    return definition


def _parse_fields(file: RsFile, brace: int, close: int, attrs: dict) -> list[Field]:
    fields: list[Field] = []
    if close < 0:
        return fields
    for span_start, span_end in split_top_level(file, brace + 1, close):
        cursor = span_start
        field_attrs: list[str] = []
        while file.value(cursor) == "#":
            if cursor in attrs:
                field_attrs.append(attrs[cursor][1])
                cursor = attrs[cursor][0]
            else:
                break
        visibility = ""
        if cursor < len(file.tokens) and file.tokens[cursor].is_name("pub"):
            visibility = "pub"
            cursor += 1
            if file.value(cursor) == "(":
                visibility = "pub(" + file.slice(cursor + 1, file.closer(cursor)).strip() + ")"
                cursor = file.skip_group(cursor)
        colon = file.find_op(":", cursor, span_end)
        if colon < 0 or cursor >= span_end:
            continue
        fields.append(Field(file.slice(cursor, colon).strip(),
                            file.slice(colon + 1, span_end).strip(),
                            file.line_of(cursor), visibility, field_attrs))
    return fields


def _parse_variants(file: RsFile, brace: int, close: int) -> list[Variant]:
    variants: list[Variant] = []
    if close < 0:
        return variants
    for span_start, span_end in split_top_level(file, brace + 1, close):
        cursor = span_start
        while file.value(cursor) == "#":
            bracket = cursor + 1
            if file.value(bracket) != "[":
                break
            cursor = file.skip_group(bracket)
        if cursor >= span_end or file.tokens[cursor].kind != "name":
            continue
        variants.append(Variant(file.value(cursor), file.line_of(cursor),
                                file.slice(cursor + 1, span_end).strip()))
    return variants


def _parse_trait(file: RsFile, index: int, attrs: dict) -> Trait | None:
    name_index = index + 1
    if name_index >= len(file) or file.tokens[name_index].kind != "name":
        return None
    start, info = _lead(file, index, attrs)
    if not _at_item_position(file, start):
        return None
    cursor = name_index + 1
    generics = ""
    if file.value(cursor) == "<":
        end = skip_generics(file, cursor)
        generics = file.slice(cursor, end)
        cursor = end
    brace = find_body_brace(file, cursor, len(file.tokens))
    if brace < 0:
        return None
    colon = file.find_op(":", cursor, brace)
    supertraits: list[str] = []
    if colon >= 0:
        where_index = _find_name_kw(file, "where", colon, brace)
        stop = where_index if where_index >= 0 else brace
        supertraits = [part.strip() for part in file.slice(colon + 1, stop).split("+") if part.strip()]
    return Trait(name=file.value(name_index), line=file.tokens[index].line, start=start,
                 body_open=brace, body_close=file.closer(brace),
                 visibility=info["visibility"], generics=generics, supertraits=supertraits,
                 is_unsafe=info["is_unsafe"], attrs=info["attrs"],
                 doc_lines=file.doc_lines_before(file.tokens[start].line))


def _parse_impl(file: RsFile, index: int, attrs: dict) -> Impl | None:
    start, info = _lead(file, index, attrs)
    if not _at_item_position(file, start):
        return None
    cursor = index + 1
    generics = ""
    if file.value(cursor) == "<":
        end = skip_generics(file, cursor)
        generics = file.slice(cursor, end)
        cursor = end
    brace = find_body_brace(file, cursor, len(file.tokens))
    if brace < 0:
        return None
    where_index = _find_name_kw(file, "where", cursor, brace)
    header_end = where_index if where_index >= 0 else brace
    for_index = _find_name_kw(file, "for", cursor, header_end)
    negative = file.value(cursor) == "!"
    head = file.slice(cursor + (1 if negative else 0), header_end).strip()
    if for_index >= 0:
        trait_name = file.slice(cursor + (1 if negative else 0), for_index).strip()
        type_name = file.slice(for_index + 1, header_end).strip()
    else:
        trait_name, type_name = None, head
    return Impl(type_name=type_name, line=file.tokens[index].line, start=start,
                body_open=brace, body_close=file.closer(brace), trait_name=trait_name,
                generics=generics, is_unsafe=info["is_unsafe"], is_negative=negative,
                attrs=info["attrs"])


def _parse_use(file: RsFile, index: int, attrs: dict) -> Use | None:
    start, info = _lead(file, index, attrs)
    if not _at_item_position(file, start):
        return None
    semi = file.find_op(";", index + 1, len(file.tokens))
    if semi < 0:
        return None
    path = file.slice(index + 1, semi).strip()
    return Use(path=path, line=file.tokens[index].line, visibility=info["visibility"],
               names=_bound_names(file, index + 1, semi), is_glob="*" in path)


def _bound_names(file: RsFile, start: int, stop: int) -> list[str]:
    """Identifiers a `use` statement brings into scope."""
    names: list[str] = []
    cursor = start
    while cursor < stop:
        token = file.tokens[cursor]
        if token.is_name("as") and cursor + 1 < stop:
            if names:
                names.pop()
            names.append(file.value(cursor + 1))
            cursor += 2
            continue
        if token.kind == "name":
            following = file.tokens[cursor + 1] if cursor + 1 < stop else None
            if following is None or not following.is_op("::"):
                if token.value == "self":
                    # `a::b::{self, c}` binds `b`; the segment before the brace.
                    previous = cursor - 1
                    while previous > start and not file.tokens[previous].kind == "name":
                        previous -= 1
                    if previous > start:
                        names.append(file.value(previous))
                elif token.value not in ("crate", "super"):
                    names.append(token.value)
        cursor += 1
    return names


def _parse_mod(file: RsFile, index: int, attrs: dict) -> ModDecl | None:
    name_index = index + 1
    if name_index >= len(file) or file.tokens[name_index].kind != "name":
        return None
    start, info = _lead(file, index, attrs)
    if not _at_item_position(file, start):
        return None
    brace = index + 2
    if file.value(brace) == "{":
        return ModDecl(file.value(name_index), file.tokens[index].line, start,
                       info["visibility"], True, brace, file.closer(brace), info["attrs"])
    return ModDecl(file.value(name_index), file.tokens[index].line, start,
                   info["visibility"], False, -1, -1, info["attrs"])


def _parse_binding(file: RsFile, index: int, attrs: dict) -> Binding | None:
    keyword = file.value(index)
    cursor = index + 1
    is_mut = False
    if cursor < len(file.tokens) and file.tokens[cursor].is_name("mut"):
        is_mut = True
        cursor += 1
    if cursor >= len(file) or file.tokens[cursor].kind != "name":
        return None
    start, info = _lead(file, index, attrs)
    if not _at_item_position(file, start):
        return None
    semi = file.find_op(";", cursor, len(file.tokens))
    if semi < 0:
        return None
    colon = file.find_op(":", cursor + 1, semi)
    equals = file.find_op("=", cursor + 1, semi)
    type_text = file.slice(colon + 1, equals if equals >= 0 else semi).strip() if colon >= 0 else ""
    value_text = file.slice(equals + 1, semi).strip() if equals >= 0 else ""
    return Binding(file.value(cursor), keyword, file.tokens[index].line, start,
                   type_text, value_text, info["visibility"], is_mut, info["attrs"])


def _parse_type_alias(file: RsFile, index: int, attrs: dict) -> TypeDef | None:
    name_index = index + 1
    if name_index >= len(file) or file.tokens[name_index].kind != "name":
        return None
    start, info = _lead(file, index, attrs)
    if not _at_item_position(file, start):
        return None
    semi = file.find_op(";", name_index, len(file.tokens))
    if semi < 0:
        return None
    equals = file.find_op("=", name_index, semi)
    return TypeDef(kind="type", name=file.value(name_index), line=file.tokens[index].line,
                   start=start, body_open=-1, body_close=-1, visibility=info["visibility"],
                   attrs=info["attrs"],
                   alias_target=file.slice(equals + 1, semi).strip() if equals >= 0 else "",
                   doc_lines=file.doc_lines_before(file.tokens[start].line))


# --------------------------------------------------------------------------- #
# Closures
# --------------------------------------------------------------------------- #

_CLOSURE_LEAD_OPS = frozenset({"(", ",", "=", "{", ";", "=>", "&", "[", "+"})
_CLOSURE_LEAD_NAMES = frozenset({"move", "return", "async", "match", "in"})


def _extract_closures(file: RsFile) -> list[Func]:
    closures: list[Func] = []
    for index, token in enumerate(file.tokens):
        if token.kind != "op" or token.value not in ("|", "||"):
            continue
        previous = file.tokens[index - 1] if index else None
        is_move = previous is not None and previous.is_name("move")
        lead = file.tokens[index - 2] if is_move and index >= 2 else previous
        if lead is not None:
            if lead.kind == "op" and lead.value not in _CLOSURE_LEAD_OPS:
                continue
            if lead.kind == "name" and lead.value not in _CLOSURE_LEAD_NAMES:
                continue
            if lead.kind in ("num", "str", "char"):
                continue
        if token.value == "||":
            params_open = params_close = index
        else:
            closing = _matching_pipe(file, index)
            if closing < 0:
                continue
            params_open, params_close = index, closing
        start = index - (1 if is_move else 0)
        is_async = start >= 1 and file.tokens[start - 1].is_name("async")
        body_open, body_close = -1, -1
        if file.value(params_close + 1) == "{":
            body_open = params_close + 1
            body_close = file.closer(body_open)
        closures.append(Func(name="", kind="closure", line=token.line, start=start,
                             params_open=params_open, params_close=params_close,
                             body_open=body_open, body_close=body_close,
                             params=[] if params_open == params_close
                             else _parse_params(file, params_open, params_close),
                             is_async=is_async, attrs=["move"] if is_move else []))
    return closures


def _matching_pipe(file: RsFile, index: int) -> int:
    """The `|` closing a closure's parameter list, or -1 when it is a bitwise or."""
    cursor = index + 1
    while cursor < min(index + 60, len(file.tokens)):
        token = file.tokens[cursor]
        if token.kind == "op":
            if token.value in OPENERS:
                cursor = file.skip_group(cursor)
                continue
            if token.value == "|":
                return cursor
            if token.value in (";", "}", "{", "||"):
                return -1
        cursor += 1
    return -1


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def extract(file: RsFile) -> None:
    attrs = _attribute_spans(file)
    file.inner_attrs = [text for (_, text, inner) in attrs.values() if inner]

    # `macro_rules!` bodies are patterns, not code: record them and skip inside.
    index = 0
    while index < len(file.tokens):
        if file.tokens[index].is_name("macro_rules") and file.value(index + 1) == "!":
            brace = index + 3 if file.value(index + 3) in ("{", "(", "[") else index + 2
            if file.value(brace) in ("{", "(", "["):
                close = file.closer(brace)
                if close > 0:
                    file.macro_bodies.append((brace, close))
                    index = close
        index += 1

    index = 0
    while index < len(file.tokens):
        token = file.tokens[index]
        if token.kind != "name" or token.value not in _ITEM_KEYWORDS:
            index += 1
            continue
        if file.in_macro_body(index):
            index += 1
            continue

        keyword = token.value
        if keyword == "fn":
            func = _parse_fn(file, index, attrs)
            if func is not None:
                file.functions.append(func)
        elif keyword in ("struct", "enum", "union"):
            # `union` is contextual: `union` as a field name is not an item.
            definition = _parse_type_def(file, index, attrs)
            if definition is not None:
                file.types.append(definition)
        elif keyword == "trait":
            trait = _parse_trait(file, index, attrs)
            if trait is not None:
                file.traits.append(trait)
        elif keyword == "impl":
            block = _parse_impl(file, index, attrs)
            if block is not None:
                file.impls.append(block)
        elif keyword == "use":
            statement = _parse_use(file, index, attrs)
            if statement is not None:
                file.uses.append(statement)
        elif keyword == "mod":
            declaration = _parse_mod(file, index, attrs)
            if declaration is not None:
                file.mods.append(declaration)
        elif keyword == "type":
            alias = _parse_type_alias(file, index, attrs)
            if alias is not None:
                file.types.append(alias)
        elif keyword in ("const", "static"):
            following = file.tok(index + 1)
            if following is not None and not following.is_name("fn", "unsafe", "extern"):
                binding = _parse_binding(file, index, attrs)
                if binding is not None:
                    file.bindings.append(binding)
        index += 1

    file.closures = _extract_closures(file)
    _assign_owners(file)
    _mark_test_spans(file)


def _assign_owners(file: RsFile) -> None:
    """Attach each function to the innermost impl or trait whose body holds it."""
    for func in file.functions:
        best_span = None
        for block in file.impls:
            if block.body_open < func.start < block.body_close:
                width = block.body_close - block.body_open
                if best_span is None or width < best_span[0]:
                    best_span = (width, block.type_name, block.trait_name, block)
        for trait in file.traits:
            if trait.body_open < func.start < trait.body_close:
                width = trait.body_close - trait.body_open
                if best_span is None or width < best_span[0]:
                    best_span = (width, trait.name, None, trait)
        if best_span is None:
            continue
        _, owner, trait_name, container = best_span
        func.owner = owner
        func.trait_name = trait_name
        func.kind = "method" if func.takes_self else "assoc_fn"
        if isinstance(container, Trait):
            func.kind = "trait_method"
            container.methods.append(func)
        else:
            container.methods.append(func)


def _mark_test_spans(file: RsFile) -> None:
    for declaration in file.mods:
        if declaration.inline and declaration.is_test_mod and declaration.body_close > 0:
            file.test_spans.append((declaration.body_open, declaration.body_close))
    for func in file.functions:
        if not func.has_body or func.body_close < 0:
            continue
        if any(a.split("(")[0].strip() in _TEST_ATTRS or "cfg(test)" in a.replace(" ", "")
               for a in func.attrs):
            file.test_spans.append((func.start, func.body_close))
