#!/usr/bin/env python3
"""Public facade for the TypeScript parser.

Detectors import this module and nothing below it. The layers underneath are
tslex (characters to tokens), tsnodes (the model), tsextract (declarations) and
tsmodules (imports, exports and type declarations).
"""

from pathlib import Path

from tsextract import (
    continues_line, extract_arrows, extract_classes, extract_functions, extract_object_methods,
    extract_variable_annotations, skip_to_statement_end,
)
from tslex import CLOSERS, CONTROL_KEYWORDS, MEMBER_MODIFIERS, OPENERS, RESERVED, Token, TsSyntaxError
from tsmodules import extract_modules, extract_types
from tsnodes import (
    Class, Export, Func, Import, Param, Prop, TsFile, TypeDecl, TypeMember,
)

__all__ = [
    "TsSyntaxError", "Token", "TsFile", "Func", "Param", "Class", "Prop",
    "TypeDecl", "TypeMember", "Import", "Export", "RESERVED",
    "parse_file", "parse_source", "callee_of", "iter_calls", "enclosing_call", "argument_spans",
    "callback_target", "body_indices", "names_in", "statement_start", "statement_end", "line_indent",
    "is_definite_assignment", "imported_identity", "name_is_shadowed",
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


def enclosing_call(file: TsFile, index: int) -> tuple[int, str]:
    """(paren, callee) of the call whose argument list directly holds ``index``.

    "Directly" means no `{` or `[` lies between them: a function passed to
    `describe("…", fn)` is an argument of `describe`, but one stored in an
    object literal that is then passed is not, because what the call receives
    is the object. Returns ``(-1, "")`` when the innermost enclosing bracket is
    not a call's `(`, and ``(paren, "")`` for a parenthesised expression or an
    anonymous callee.

    A curried call is named by its head, which is how a reader names it:
    `describe.each(rows)("t", fn)` and `` describe.each`…`("t", fn) `` both
    report `describe.each`.
    """
    cursor = index - 1
    while cursor >= 0:
        token = file.tokens[cursor]
        if token.kind == "op" and token.value in CLOSERS:
            opener = file.closer(cursor)
            cursor = (opener if opener >= 0 else cursor) - 1
            continue
        if token.kind == "op" and token.value in OPENERS:
            if token.value != "(":
                return -1, ""
            return cursor, _call_head(file, cursor)
        cursor -= 1
    return -1, ""


def _call_head(file: TsFile, paren: int) -> str:
    """The callee of the call at ``paren``, looking through one curried head."""
    previous = file.tokens[paren - 1] if paren else None
    if previous is not None and previous.is_op(")"):
        opener = file.closer(paren - 1)
        return callee_of(file, opener) if opener > 0 else ""
    if previous is not None and previous.kind == "template":
        return callee_of(file, paren - 1)  # `tag`…`(` — the template stands in for `(…)`
    callee = callee_of(file, paren)
    member = paren >= 2 and file.tokens[paren - 2].is_op(".", "?.")
    if not member and (callee in CONTROL_KEYWORDS or (paren >= 2 and file.tokens[paren - 2].is_name("function"))):
        return ""  # `if (…)` is not a call, and neither is `function f(…)`
    return callee


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


def callback_target(file: TsFile, func: Func) -> tuple[str, int]:
    """(callee, argument position) of the call ``func`` is passed to directly.

    `useEffect(() => {…}, deps)` yields ("useEffect", 0). A curried call is
    named by its head, so `describe.each(rows)('t', fn)` yields
    ("describe.each", 1). A function that is assigned, returned, or sits inside
    some larger expression is not a direct argument and yields ("", -1).
    """
    start = func.start
    if start <= 0 or not file.tokens[start - 1].is_op("(", ","):
        return "", -1
    after = func.body_close + 1 if func.has_body else -1
    if 0 < after < len(file.tokens) and not file.tokens[after].is_op(",", ")"):
        return "", -1  # `run(() => {…} && x)`: the function is an operand, not the argument
    position = 0
    cursor = start - 1
    while cursor >= 0:
        token = file.tokens[cursor]
        if token.kind == "op" and token.value in CLOSERS:
            cursor = file.closer(cursor) - 1
            continue
        if token.is_op(","):
            position += 1
        elif token.is_op("("):
            break
        elif (token.kind == "op" and token.value in OPENERS) or token.is_op(";"):
            return "", -1  # an array or object literal, or a block — not an argument list
        cursor -= 1
    if cursor < 0:
        return "", -1
    callee = callee_of(file, cursor)
    if callee in CONTROL_KEYWORDS:
        return "", -1  # `return (fn)`, `await (fn)`: parentheses, not a call
    if not callee and cursor > 0 and file.tokens[cursor - 1].is_op(")"):
        opener = file.closer(cursor - 1)
        callee = callee_of(file, opener) if opener > 0 else ""
    return (callee, position) if callee else ("", -1)


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


# What may stand right before the name in `NAME!:` for it to be a declaration.
_DECLARATION_LEADS = frozenset({"let", "var", "const", "declare"}) | MEMBER_MODIFIERS


def is_definite_assignment(file: TsFile, bang: int) -> bool:
    """True when the `!` at ``bang`` is a definite-assignment assertion, `let x!: T`.

    That `!` says "assigned before use" about a declaration; the postfix `x!` in
    an expression says "not null" about a value. Both are a `!` after a name,
    and only what surrounds them differs: a declaration's name opens a `let` /
    `var` list or a class member, and its `!` is followed by the annotation's
    `:`. The one expression shape with `!` before `:` — the ternary
    `c ? x! : y` — always has `?` or another operator in front of the name.
    """
    if bang < 2 or bang + 1 >= len(file.tokens) or not file.tokens[bang + 1].is_op(":"):
        return False
    name, lead = file.tokens[bang - 1], file.tokens[bang - 2]
    if name.kind != "name":
        return False
    if lead.is_op(",", ";", "{", "}") or lead.is_name(*_DECLARATION_LEADS):
        return True
    # A class field in code without semicolons starts its own line after the
    # previous member's last token, which is not an operator.
    return name.line > lead.line and not continues_line(file, bang - 2)


def name_is_shadowed(file: TsFile, name: str, index: int, definition: Func | None = None) -> bool:
    """Refuse a binding proof when an enclosing scope can replace the name.

    This is deliberately conservative about block scope, while writes in sibling
    functions cannot invalidate a binding at this use site.
    """
    active = [func for func in file.functions if func.has_body and func.body_open < index < func.body_close]
    scopes = {None, *(id(func) for func in active)}
    if any(param.name == name for func in active for param in func.params):
        return True
    allowed = statement_start(file, definition.start) if definition is not None else -1
    for func in file.functions:
        parent = file.enclosing_function(func.start)
        if func.owner is None and func.name == name and func is not definition \
                and (id(parent) if parent is not None else None) in scopes:
            return True
    for probe, token in enumerate(file.tokens):
        parent = file.enclosing_function(probe)
        if (id(parent) if parent is not None else None) not in scopes:
            continue
        if token.is_name("const", "let", "var"):
            if statement_start(file, probe) == allowed:
                continue
            end = statement_end(file, probe)
            equals = file.find_op("=", probe + 1, end)
            if any(file.tokens[i].is_name(name) for i in range(probe + 1, equals if equals >= 0 else end)):
                return True
        if not token.is_name(name) or file.is_jsx_attribute(probe):
            continue
        previous = file.tok(probe - 1)
        if previous is not None and previous.is_op(".", "?."):
            continue
        if definition is not None and probe < definition.params_open and statement_start(file, probe) == allowed:
            continue
        after = probe + 1
        while after + 1 < len(file) and file.tokens[after].is_op(".", "?."):
            after += 2
        following = file.tok(after)
        if following is not None and following.is_op("=", "+=", "-=", "++", "--", "||=", "&&=", "??="):
            return True
        if previous is not None and previous.is_name("class"):
            return True
    return False


def imported_identity(file: TsFile, callee: str, index: int) -> str:
    """Canonical imported name at a use site; empty when its binding is uncertain."""
    root, separator, rest = callee.partition(".")
    identities = []
    for record in file.imports:
        if record.kind != "import" or record.is_type_only or record.line > file.tokens[index].line:
            continue
        if root in record.type_names:
            continue
        if root in record.bindings:
            identities.append(record.module + "." + record.bindings[root])
        elif root in (record.default_name, record.namespace_name) and root:
            identities.append(record.module)
    if len(identities) != 1 or name_is_shadowed(file, root, index):
        return ""
    return identities[0] + (separator + rest if separator else "")
