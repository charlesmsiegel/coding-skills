#!/usr/bin/env python3
"""Lexical layer: turn Rust source text into tokens.

Everything in this module is about reading characters correctly — raw strings
with any number of hashes, byte and C strings, block comments that nest, the
`'` that is a char literal in one place and a lifetime in another, numeric
literals with type suffixes and the `1..2` range that must not eat the dot —
and about proving the brackets balance. Nothing here knows what an `impl` or a
`trait` is; that is rustextract's job.

Angle brackets are deliberately *not* matched. `a < b` and `Vec<T>` are the same
two characters, and no amount of local scanning tells them apart; the extractors
step over generic argument lists with a small depth counter instead, where they
already know a type is expected.
"""

import re
from dataclasses import dataclass


class RustSyntaxError(Exception):
    """The scanner could not make structural sense of the source."""


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #

_IDENT = re.compile(r"[A-Za-z_ª-\U0010ffff][A-Za-z0-9_ª-\U0010ffff]*")

# A trailing `(?![.\w])` on the integer branch keeps `1..2` from swallowing the
# first dot of the range and `1.foo()` from parsing as a float.
_NUMBER = re.compile(
    r"0[xX][0-9a-fA-F_]+(?:[iu](?:8|16|32|64|128|size))?"
    r"|0[bB][01_]+(?:[iu](?:8|16|32|64|128|size))?"
    r"|0[oO][0-7_]+(?:[iu](?:8|16|32|64|128|size))?"
    r"|\d[\d_]*\.\d[\d_]*(?:[eE][+-]?\d[\d_]*)?(?:f32|f64)?"
    r"|\d[\d_]*(?:[eE][+-]?\d[\d_]*)(?:f32|f64)?"
    r"|\d[\d_]*(?:[iu](?:8|16|32|64|128|size)|f32|f64)"
    r"|\d[\d_]*\.(?![.\w])"
    r"|\d[\d_]*"
)

# Longest first: the scanner takes the first match, so `..=` must precede `..`.
_PUNCTUATORS = (
    "<<=", ">>=", "...", "..=",
    "::", "->", "=>", "..", "&&", "||", "==", "!=", "<=", ">=",
    "+=", "-=", "*=", "/=", "%=", "^=", "&=", "|=", "<<", ">>",
    "{", "}", "(", ")", "[", "]", ";", ",", "<", ">", "+", "-", "*", "/", "%",
    "&", "|", "^", "!", "~", "?", ":", "=", ".", "#", "@", "$",
)

OPENERS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = {")": "(", "]": "[", "}": "{"}

# Strict keywords plus the ones that matter structurally. `union`, `dyn` and
# `async` are contextual, but treating them as names is fine — the extractors
# match on text, not on a keyword flag.
KEYWORDS = frozenset({
    "as", "async", "await", "break", "const", "continue", "crate", "dyn",
    "else", "enum", "extern", "false", "fn", "for", "if", "impl", "in", "let",
    "loop", "match", "mod", "move", "mut", "pub", "ref", "return", "self",
    "Self", "static", "struct", "super", "trait", "true", "type", "union",
    "unsafe", "use", "where", "while", "yield",
})

# Keywords that introduce a value, so a following `|` opens a closure and a
# following `'` is far more likely a lifetime in a turbofish than a char.
EXPRESSION_KEYWORDS = frozenset({
    "return", "match", "if", "while", "in", "else", "break", "yield", "await",
    "move", "as", "let",
})

# Keywords that take a parenthesised or braced clause, so `name(` after one of
# these is not a call.
CONTROL_KEYWORDS = frozenset({
    "if", "while", "match", "for", "loop", "return", "else", "break", "continue",
})

# The primitive scalar types, used by the cast and parameter detectors.
PRIMITIVES = frozenset({
    "i8", "i16", "i32", "i64", "i128", "isize",
    "u8", "u16", "u32", "u64", "u128", "usize",
    "f32", "f64", "bool", "char", "str",
})

# Types that are `Copy`, so `.clone()` on one is a no-op with extra syntax.
COPY_TYPES = PRIMITIVES - {"str"}


@dataclass(slots=True)
class Token:
    kind: str  # name | num | str | char | lifetime | op | comment
    value: str
    line: int
    start: int
    end: int

    def is_op(self, *values: str) -> bool:
        return self.kind == "op" and self.value in values

    def is_name(self, *values: str) -> bool:
        return self.kind == "name" and self.value in values


def is_doc_comment(token: Token) -> bool:
    """True for `///`, `//!`, `/** … */` and `/*! … */`."""
    text = token.value
    if text.startswith("///"):
        return not text.startswith("////")  # `////` is a rule, not documentation
    return text.startswith(("//!", "/**", "/*!")) and text != "/**/"


class Tokenizer:
    def __init__(self, text: str):
        self.text = text
        self.i = 0
        self.line = 1
        self.tokens: list[Token] = []
        self.comments: list[Token] = []

    # -- low level ---------------------------------------------------------- #

    def _emit(self, kind: str, start: int, end: int, line: int) -> None:
        token = Token(kind, self.text[start:end], line, start, end)
        (self.comments if kind == "comment" else self.tokens).append(token)

    def _advance(self, end: int) -> None:
        """Move the cursor to ``end``, counting the newlines passed over."""
        self.line += self.text.count("\n", self.i, end)
        self.i = end

    def _skip_space(self) -> None:
        text, n = self.text, len(self.text)
        while self.i < n and text[self.i] in " \t\r\n\f\v ﻿":
            if text[self.i] == "\n":
                self.line += 1
            self.i += 1

    def _expects_value(self) -> bool:
        """True when the parser is where a value may start, not where one just ended."""
        if not self.tokens:
            return True
        previous = self.tokens[-1]
        if previous.kind == "op":
            return previous.value not in (")", "]", "}", "?")
        if previous.kind == "name":
            return previous.value in EXPRESSION_KEYWORDS
        return False

    # -- literal scanners --------------------------------------------------- #

    def _string(self, prefix: int) -> None:
        """A normal (possibly `b`/`c`-prefixed) string literal."""
        text, n = self.text, len(self.text)
        start, line = self.i, self.line
        j = self.i + prefix + 1
        while j < n:
            char = text[j]
            if char == "\\":
                j += 2
                continue
            if char == '"':
                j += 1
                break
            j += 1
        else:
            j = n
        self._advance(j)
        self._emit("str", start, j, line)

    def _raw_string(self, prefix: int, hashes: int) -> None:
        """`r"…"`, `r#"…"#`, `br##"…"##` — no escapes, closed by `"` + N hashes."""
        start, line = self.i, self.line
        opening = self.i + prefix + hashes + 1  # just past the opening quote
        terminator = '"' + "#" * hashes
        end = self.text.find(terminator, opening)
        end = len(self.text) if end == -1 else end + len(terminator)
        self._advance(end)
        self._emit("str", start, end, line)

    def _raw_string_hashes(self, prefix: int) -> int | None:
        """Hash count for a raw string starting at the cursor, or None if it is not one."""
        j = self.i + prefix
        hashes = 0
        while j + hashes < len(self.text) and self.text[j + hashes] == "#":
            hashes += 1
        if self.text[j + hashes:j + hashes + 1] == '"':
            return hashes
        return None

    def _char_or_lifetime(self) -> None:
        """`'a'` is a char, `'a` is a lifetime, and only the tail tells them apart."""
        text, n = self.text, len(self.text)
        start, line = self.i, self.line

        if text[self.i + 1:self.i + 2] == "\\":  # an escape is always a char literal
            j = self.i + 3  # past the backslash and the character it escapes
            while j < n and text[j] != "'":
                j += 1
            j = min(j + 1, n)
            self._advance(j)
            self._emit("char", start, j, line)
            return

        match = _IDENT.match(text, self.i + 1)
        if match and text[match.end():match.end() + 1] != "'":
            self._advance(match.end())  # `'static`, `'a`, `'_`: a lifetime or a label
            self._emit("lifetime", start, match.end(), line)
            return
        if text[self.i + 2:self.i + 3] == "'":  # `'x'`
            self._advance(self.i + 3)
            self._emit("char", start, start + 3, line)
            return
        if match:  # `'a'` where the ident ran to the closing quote
            end = match.end() + 1
            self._advance(end)
            self._emit("char", start, end, line)
            return
        # A lone `'` — an unterminated literal. Emit it and move on rather than stall.
        self._advance(self.i + 1)
        self._emit("op", start, start + 1, line)

    def _comment(self) -> None:
        text, n = self.text, len(self.text)
        start, line = self.i, self.line
        if text[self.i + 1] == "/":
            end = text.find("\n", self.i)
            end = n if end == -1 else end
        else:
            # Rust block comments nest: `/* /* */ */` is one comment.
            depth, j = 1, self.i + 2
            while j < n and depth:
                if text.startswith("/*", j):
                    depth += 1
                    j += 2
                elif text.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            end = j
        self._advance(end)
        self._emit("comment", start, end, line)

    # -- driver ------------------------------------------------------------- #

    def _step(self) -> bool:
        """Scan exactly one token. Returns False at end of input."""
        self._skip_space()
        text, n = self.text, len(self.text)
        if self.i >= n:
            return False
        char = text[self.i]
        start, line = self.i, self.line

        if char == "/" and text[self.i + 1:self.i + 2] in ("/", "*"):
            self._comment()
            return True
        if char == '"':
            self._string(0)
            return True
        if char == "'":
            self._char_or_lifetime()
            return True
        # Prefixed literals: r"", b"", c"", br"", cr"", and their raw forms.
        for prefix_text in ("br", "cr", "r", "b", "c"):
            if not text.startswith(prefix_text, self.i):
                continue
            # `raw` and `bytes` are identifiers, not prefixes: only a quote or a
            # hash immediately after the prefix makes it one.
            prefix = len(prefix_text)
            if prefix_text.endswith("r"):
                hashes = self._raw_string_hashes(prefix)
                if hashes is not None:
                    self._raw_string(prefix, hashes)
                    return True
            if text[self.i + prefix:self.i + prefix + 1] == '"':
                self._string(prefix)
                return True
        if char.isdigit():
            match = _NUMBER.match(text, self.i)
            if match:
                self._advance(match.end())
                self._emit("num", start, match.end(), line)
                return True
        match = _IDENT.match(text, self.i)
        if match:
            self._advance(match.end())
            self._emit("name", start, match.end(), line)
            return True
        for punctuator in _PUNCTUATORS:
            if text.startswith(punctuator, self.i):
                self._advance(self.i + len(punctuator))
                self._emit("op", start, start + len(punctuator), line)
                return True
        self._advance(self.i + 1)  # an unknown byte; skip it rather than stall
        return True

    def run(self) -> None:
        while self._step():
            pass


def match_brackets(tokens: list[Token]) -> dict[int, int]:
    """Two-way index map for (), [] and {}. Raises on an unbalanced file."""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for index, token in enumerate(tokens):
        if token.kind != "op":
            continue
        if token.value in OPENERS:
            stack.append(index)
        elif token.value in CLOSERS:
            if not stack:
                raise RustSyntaxError(f"unmatched '{token.value}' on line {token.line}")
            opener = stack.pop()
            if OPENERS[tokens[opener].value] != token.value:
                raise RustSyntaxError(
                    f"'{tokens[opener].value}' on line {tokens[opener].line} "
                    f"closed by '{token.value}' on line {token.line}"
                )
            pairs[opener] = index
            pairs[index] = opener
    if stack:
        opener = tokens[stack[-1]]
        raise RustSyntaxError(f"unclosed '{opener.value}' opened on line {opener.line}")
    return pairs
