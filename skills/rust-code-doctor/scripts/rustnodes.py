#!/usr/bin/env python3
"""The structural model: one dataclass per kind of item, plus RsFile.

RsFile owns the tokens and the bracket map and answers positional questions
about them. It is deliberately free of extraction logic — it is constructed
empty and filled in by rsparse.parse_source, which keeps the model importable
from the extractors without a cycle.
"""

from dataclasses import dataclass, field
from pathlib import Path

from rustlex import OPENERS, Token, Tokenizer, is_doc_comment, match_brackets


# --------------------------------------------------------------------------- #
# Structural model
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Param:
    name: str
    type_text: str
    line: int
    is_self: bool = False
    by_ref: bool = False
    is_mut: bool = False


@dataclass(slots=True)
class Func:
    name: str
    kind: str  # fn | method | assoc_fn | trait_method | closure
    line: int
    start: int          # index of the `fn` token (or the closure's first `|`)
    params_open: int
    params_close: int
    body_open: int      # -1 for a trait method with no default body
    body_close: int
    params: list[Param] = field(default_factory=list)
    return_type: str = ""
    generics: str = ""
    is_async: bool = False
    is_unsafe: bool = False
    is_const: bool = False
    is_extern: bool = False
    visibility: str = ""            # "", "pub", "pub(crate)", "pub(super)", …
    owner: str | None = None        # the impl type or trait this belongs to
    trait_name: str | None = None   # set when the owner impl names a trait
    attrs: list[str] = field(default_factory=list)
    doc_lines: int = 0

    @property
    def has_body(self) -> bool:
        return self.body_open >= 0

    @property
    def is_public(self) -> bool:
        """Reachable from outside the crate — plain `pub`, nothing narrower.

        `pub(crate)`, `pub(super)` and `pub(in …)` are internal: they are not
        API-evolution surface, they do not need `# Errors` docs for downstream
        users, and dead-code analysis must not exempt them as if a downstream
        caller could exist.
        """
        return self.visibility == "pub"

    @property
    def is_exported(self) -> bool:
        """Visible beyond its own module, at any restriction level."""
        return self.visibility.startswith("pub") and self.visibility != "pub(self)"

    @property
    def takes_self(self) -> bool:
        return bool(self.params) and self.params[0].is_self

    @property
    def qualname(self) -> str:
        return f"{self.owner}::{self.name}" if self.owner else self.name


@dataclass(slots=True)
class Field:
    name: str
    type_text: str
    line: int
    visibility: str = ""
    attrs: list[str] = field(default_factory=list)

    @property
    def is_public(self) -> bool:
        """Reachable from outside the crate — plain `pub`, nothing narrower."""
        return self.visibility == "pub"

    @property
    def is_exported(self) -> bool:
        """Visible beyond its own module, at any restriction level."""
        return self.visibility.startswith("pub") and self.visibility != "pub(self)"


@dataclass(slots=True)
class Variant:
    name: str
    line: int
    payload: str = ""  # the tuple or struct body, "" for a unit variant


@dataclass(slots=True)
class TypeDef:
    kind: str  # struct | enum | union | type
    name: str
    line: int
    start: int
    body_open: int
    body_close: int
    visibility: str = ""
    generics: str = ""
    derives: list[str] = field(default_factory=list)
    attrs: list[str] = field(default_factory=list)
    fields: list[Field] = field(default_factory=list)
    variants: list[Variant] = field(default_factory=list)
    doc_lines: int = 0
    alias_target: str = ""  # for `type X = …;`

    @property
    def is_public(self) -> bool:
        """Reachable from outside the crate — plain `pub`, nothing narrower."""
        return self.visibility == "pub"

    @property
    def is_exported(self) -> bool:
        """Visible beyond its own module, at any restriction level."""
        return self.visibility.startswith("pub") and self.visibility != "pub(self)"


@dataclass(slots=True)
class Trait:
    name: str
    line: int
    start: int
    body_open: int
    body_close: int
    visibility: str = ""
    generics: str = ""
    supertraits: list[str] = field(default_factory=list)
    is_unsafe: bool = False
    methods: list[Func] = field(default_factory=list)
    attrs: list[str] = field(default_factory=list)
    doc_lines: int = 0

    @property
    def is_public(self) -> bool:
        """Reachable from outside the crate — plain `pub`, nothing narrower."""
        return self.visibility == "pub"

    @property
    def is_exported(self) -> bool:
        """Visible beyond its own module, at any restriction level."""
        return self.visibility.startswith("pub") and self.visibility != "pub(self)"


@dataclass(slots=True)
class Impl:
    type_name: str
    line: int
    start: int
    body_open: int
    body_close: int
    trait_name: str | None = None
    generics: str = ""
    is_unsafe: bool = False
    is_negative: bool = False
    methods: list[Func] = field(default_factory=list)
    attrs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Use:
    path: str
    line: int
    visibility: str = ""
    names: list[str] = field(default_factory=list)  # what the statement binds
    is_glob: bool = False


@dataclass(slots=True)
class ModDecl:
    name: str
    line: int
    start: int
    visibility: str = ""
    inline: bool = False
    body_open: int = -1
    body_close: int = -1
    attrs: list[str] = field(default_factory=list)

    @property
    def is_test_mod(self) -> bool:
        return any("cfg(test)" in a.replace(" ", "") for a in self.attrs)

    @property
    def is_public(self) -> bool:
        """Reachable from outside the crate — plain `pub`, nothing narrower."""
        return self.visibility == "pub"

    @property
    def is_exported(self) -> bool:
        """Visible beyond its own parent module, at any restriction level."""
        return self.visibility.startswith("pub") and self.visibility != "pub(self)"


@dataclass(slots=True)
class Binding:
    """A `const` or `static` item."""

    name: str
    kind: str  # const | static
    line: int
    start: int
    type_text: str = ""
    value_text: str = ""
    visibility: str = ""
    is_mut: bool = False
    attrs: list[str] = field(default_factory=list)

    @property
    def is_public(self) -> bool:
        """Reachable from outside the crate — plain `pub`, nothing narrower."""
        return self.visibility == "pub"

    @property
    def is_exported(self) -> bool:
        """Visible beyond its own module, at any restriction level."""
        return self.visibility.startswith("pub") and self.visibility != "pub(self)"


class RsFile:
    """One tokenized Rust source, with its items extracted."""

    def __init__(self, path: Path, text: str):
        self.path = path
        self.text = text
        self.lines = text.splitlines()
        tokenizer = Tokenizer(text)
        tokenizer.run()
        self.tokens = tokenizer.tokens
        self.comments = tokenizer.comments
        self.match = match_brackets(self.tokens)

        self.uses: list[Use] = []
        self.mods: list[ModDecl] = []
        self.functions: list[Func] = []
        self.types: list[TypeDef] = []
        self.traits: list[Trait] = []
        self.impls: list[Impl] = []
        self.bindings: list[Binding] = []
        self.macro_bodies: list[tuple[int, int]] = []  # `macro_rules!` spans
        self.inner_attrs: list[str] = []               # `#![…]` at file scope
        # Token index ranges that sit inside a `#[cfg(test)]` module.
        self.test_spans: list[tuple[int, int]] = []

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

    # -- structural queries ------------------------------------------------- #

    def enclosing_function(self, index: int) -> Func | None:
        """The innermost function whose body contains ``index``."""
        found = None
        for func in self.functions:
            if func.has_body and func.body_open <= index <= func.body_close:
                if found is None or func.body_open > found.body_open:
                    found = func
        return found

    def enclosing_impl(self, index: int) -> Impl | None:
        for block in self.impls:
            if block.body_open <= index <= block.body_close:
                return block
        return None

    def top_level(self, index: int) -> bool:
        """True when the token is inside no function, impl, trait or type body."""
        if self.enclosing_function(index) is not None or self.enclosing_impl(index) is not None:
            return False
        for trait in self.traits:
            if trait.body_open <= index <= trait.body_close:
                return False
        return True

    def in_macro_body(self, index: int) -> bool:
        """True inside a `macro_rules!` definition, where the text is a pattern."""
        return any(start <= index <= end for start, end in self.macro_bodies)

    def in_test_code(self, index: int) -> bool:
        """True inside a `#[cfg(test)]` module or a `#[test]`/`#[bench]` function."""
        if any(start <= index <= end for start, end in self.test_spans):
            return True
        func = self.enclosing_function(index)
        while func is not None:
            if any(a.startswith(("test", "tokio::test", "bench", "rstest", "proptest",
                                 "async_std::test", "should_panic"))
                   for a in func.attrs):
                return True
            func = self.enclosing_function(func.start - 1) if func.start > 0 else None
        return False

    # -- comments ----------------------------------------------------------- #

    def comments_on_lines(self, first: int, last: int) -> list[Token]:
        return [c for c in self.comments if first <= c.line <= last]

    def doc_lines_before(self, line: int) -> int:
        """Count of contiguous `///`/`//!` comment lines immediately above ``line``."""
        by_line = {c.line: c for c in self.comments}
        count, probe = 0, line - 1
        while probe > 0:
            comment = by_line.get(probe)
            if comment is None:
                # Attributes sit between the docs and the item; step over them.
                text = self.lines[probe - 1].strip() if probe <= len(self.lines) else ""
                if text.startswith("#["):
                    probe -= 1
                    continue
                break
            if not is_doc_comment(comment):
                break
            count += 1
            probe -= 1
        return count
