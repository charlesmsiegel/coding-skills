#!/usr/bin/env python3
"""The structural model: one dataclass per kind of declaration, plus TsFile.

TsFile owns the tokens and the bracket map and answers positional questions
about them. It is deliberately free of extraction logic — it is constructed
empty and filled in by tsparse.parse_source, which keeps the model importable
from the extractors without a cycle.
"""

from dataclasses import dataclass, field
from pathlib import Path

from tslex import OPENERS, Token, Tokenizer, match_brackets


# --------------------------------------------------------------------------- #
# Structural model
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Param:
    name: str
    type_text: str
    line: int
    optional: bool = False
    has_default: bool = False
    is_rest: bool = False
    is_destructured: bool = False
    accessibility: str | None = None  # constructor parameter property
    readonly: bool = False


@dataclass(slots=True)
class Func:
    name: str
    kind: str  # function | method | arrow | getter | setter | constructor
    line: int
    start: int
    params_open: int
    params_close: int
    body_open: int  # -1 for an overload signature or a concise arrow body
    body_close: int
    params: list[Param] = field(default_factory=list)
    return_type: str = ""
    is_async: bool = False
    is_generator: bool = False
    is_exported: bool = False
    is_static: bool = False
    is_abstract: bool = False
    accessibility: str | None = None
    owner: str | None = None
    decorators: list[str] = field(default_factory=list)

    @property
    def has_body(self) -> bool:
        return self.body_open >= 0

    @property
    def qualname(self) -> str:
        return f"{self.owner}.{self.name}" if self.owner else self.name


@dataclass(slots=True)
class Prop:
    name: str
    line: int
    index: int
    type_text: str = ""
    accessibility: str | None = None
    is_static: bool = False
    readonly: bool = False
    optional: bool = False
    has_initializer: bool = False
    initializer: str = ""
    decorators: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Class:
    name: str
    line: int
    start: int
    body_open: int
    body_close: int
    extends: str | None = None
    implements: list[str] = field(default_factory=list)
    is_abstract: bool = False
    is_exported: bool = False
    is_default_export: bool = False
    decorators: list[str] = field(default_factory=list)
    methods: list[Func] = field(default_factory=list)
    props: list[Prop] = field(default_factory=list)


@dataclass(slots=True)
class TypeMember:
    name: str
    type_text: str
    line: int
    optional: bool = False
    readonly: bool = False


@dataclass(slots=True)
class TypeDecl:
    kind: str  # interface | type | enum
    name: str
    line: int
    start: int
    end: int
    is_exported: bool = False
    is_const: bool = False
    extends: list[str] = field(default_factory=list)
    members: list[TypeMember] = field(default_factory=list)
    text: str = ""


@dataclass(slots=True)
class Import:
    module: str
    line: int
    kind: str  # import | require | dynamic | export-from
    names: list[str] = field(default_factory=list)
    default_name: str = ""
    namespace_name: str = ""
    is_type_only: bool = False
    side_effect_only: bool = False


@dataclass(slots=True)
class Export:
    name: str
    line: int
    kind: str  # named | default | star | declaration
    is_type_only: bool = False


class TsFile:
    """One tokenized TypeScript source, with its declarations extracted."""

    def __init__(self, path: Path, text: str):
        self.path = path
        self.text = text
        self.lines = text.splitlines()
        self.is_tsx = path.suffix in (".tsx", ".jsx")
        tokenizer = Tokenizer(text, jsx=self.is_tsx)
        tokenizer.run()
        self.tokens = tokenizer.tokens
        self.comments = tokenizer.comments
        self.jsx_text = tokenizer.jsx_text
        self.match = match_brackets(self.tokens)

        self.imports: list[Import] = []
        self.exports: list[Export] = []
        self.classes: list[Class] = []
        self.types: list[TypeDecl] = []
        self.functions: list[Func] = []
        self.type_spans: list[tuple[int, int]] = []

    # -- token helpers ------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self.tokens)

    def tok(self, index: int) -> Token | None:
        if 0 <= index < len(self.tokens):
            return self.tokens[index]
        return None

    def value(self, index: int) -> str:
        token = self.tok(index)
        return token.value if token else ""

    def line_of(self, index: int) -> int:
        token = self.tok(index)
        return token.line if token else 0

    def slice(self, start: int, end: int) -> str:
        """Raw source text spanning tokens [start, end)."""
        if start < 0 or start >= len(self.tokens) or end <= start:
            return ""
        end = min(end, len(self.tokens))
        return self.text[self.tokens[start].start:self.tokens[end - 1].end]

    def snippet(self, line: int, width: int = 70) -> str:
        if 0 < line <= len(self.lines):
            return self.lines[line - 1].strip()[:width]
        return ""

    def closer(self, index: int) -> int:
        """The matching bracket for the token at ``index``, or -1."""
        return self.match.get(index, -1)

    def skip_group(self, index: int) -> int:
        """Index just past the bracket group opening at ``index``."""
        end = self.match.get(index, -1)
        return end + 1 if end >= 0 else index + 1

    def enclosing_function(self, index: int) -> Func | None:
        found = None
        for func in self.functions:
            if func.has_body and func.body_open <= index <= func.body_close:
                if found is None or func.body_open > found.body_open:
                    found = func
        return found

    def enclosing_class(self, index: int) -> Class | None:
        for klass in self.classes:
            if klass.body_open <= index <= klass.body_close:
                return klass
        return None

    def top_level(self, index: int) -> bool:
        """True when the token is not inside any function body or class body."""
        return self.enclosing_function(index) is None and self.enclosing_class(index) is None

    def find_op(self, value: str, start: int, stop: int) -> int:
        """First index in [start, stop) holding ``value`` at bracket depth 0."""
        index = start
        while index < min(stop, len(self.tokens)):
            token = self.tokens[index]
            if token.kind == "op":
                if token.value == value:
                    return index
                if token.value in OPENERS:
                    index = self.skip_group(index)
                    continue
            index += 1
        return -1

    def in_type_position(self, index: int) -> bool:
        """True when the token sits inside a type annotation or declaration.

        `(a: string) => void` is a *type* when it annotates something and a
        *function* when it is the value. Both look identical token by token, so
        the spans every annotation covers are recorded as they are parsed and
        consulted here — that is what keeps a function-typed interface member
        out of the function inventory.
        """
        return any(start <= index <= end for start, end in self.type_spans)

