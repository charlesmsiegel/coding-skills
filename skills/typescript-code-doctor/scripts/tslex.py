#!/usr/bin/env python3
"""Lexical layer: turn TypeScript source text into tokens.

Everything in this module is about reading characters correctly — strings,
template literals with nested `${...}`, regex literals against division, JSX
element text, and comments — and about proving the brackets balance. Nothing
here knows what a class or an import is; that is tsextract's job.
"""

import re
from dataclasses import dataclass


class TsSyntaxError(Exception):
    """The scanner could not make structural sense of the source."""


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #

_IDENT = re.compile(r"[A-Za-z_$ª-￿][A-Za-z0-9_$ª-￿]*")
_NUMBER = re.compile(
    r"0[xX][0-9a-fA-F_]+n?|0[bB][01_]+n?|0[oO][0-7_]+n?"
    r"|(?:\d[\d_]*)?\.\d[\d_]*(?:[eE][+-]?\d+)?|\d[\d_]*(?:\.[\d_]*)?(?:[eE][+-]?\d+)?n?"
)

# Longest first: the scanner takes the first match, so `>>>=` must precede `>>`.
_PUNCTUATORS = (
    ">>>=",
    "...", "===", "!==", "**=", "<<=", ">>=", ">>>", "&&=", "||=", "??=",
    "=>", "==", "!=", "<=", ">=", "&&", "||", "??", "?.", "++", "--",
    "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "**", "<<",
    "{", "}", "(", ")", "[", "]", ";", ",", "<", ">", "+", "-", "*", "/", "%",
    "&", "|", "^", "!", "~", "?", ":", "=", ".", "@",
)

OPENERS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = {")": "(", "]": "[", "}": "{"}

# After one of these a `/` starts a regex and a `<` may start JSX, because the
# parser is expecting a value rather than looking at one.
_EXPRESSION_KEYWORDS = frozenset({
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "do", "else",
    "case", "void", "throw", "yield", "await", "extends", "default", "from",
    "as", "satisfies", "is", "keyof", "infer",
})

# Tokens that end a value, so a following `/` is division.
_VALUE_ENDING_OPS = frozenset({")", "]", "++", "--"})

RESERVED = frozenset({
    "abstract", "any", "as", "asserts", "async", "await", "boolean", "break",
    "case", "catch", "class", "const", "constructor", "continue", "debugger",
    "declare", "default", "delete", "do", "else", "enum", "export", "extends",
    "false", "finally", "for", "from", "function", "get", "if", "implements",
    "import", "in", "infer", "instanceof", "interface", "is", "keyof", "let",
    "module", "namespace", "never", "new", "null", "number", "object", "of",
    "out", "override", "package", "private", "protected", "public", "readonly",
    "require", "return", "satisfies", "set", "static", "string", "super",
    "switch", "symbol", "this", "throw", "true", "try", "type", "typeof",
    "undefined", "unique", "unknown", "var", "void", "while", "with", "yield",
})

# Keywords that introduce a parenthesised clause, so `name(` after one of these
# is not a function declaration.
CONTROL_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "with", "return", "typeof", "new",
    "await", "yield", "throw", "delete", "void", "in", "of", "case", "do",
})

MEMBER_MODIFIERS = frozenset({
    "public", "private", "protected", "static", "readonly", "abstract",
    "declare", "override", "async", "accessor",
})


@dataclass(slots=True)
class Token:
    kind: str  # name | num | str | template | regex | op | comment | jsx_text
    value: str
    line: int
    start: int
    end: int

    def is_op(self, *values: str) -> bool:
        return self.kind == "op" and self.value in values

    def is_name(self, *values: str) -> bool:
        return self.kind == "name" and self.value in values


class Tokenizer:
    def __init__(self, text: str, jsx: bool):
        self.text = text
        self.jsx = jsx
        self.i = 0
        self.line = 1
        self.tokens: list[Token] = []
        self.comments: list[Token] = []
        self.jsx_text: list[Token] = []

    # -- low level ---------------------------------------------------------- #

    def _emit(self, kind: str, start: int, end: int, line: int) -> Token:
        token = Token(kind, self.text[start:end], line, start, end)
        if kind == "comment":
            self.comments.append(token)
        elif kind == "jsx_text":
            self.jsx_text.append(token)
        else:
            self.tokens.append(token)
        return token

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

    def _prev_significant(self) -> Token | None:
        return self.tokens[-1] if self.tokens else None

    def _expects_value(self) -> bool:
        """True when the parser is at a position where a value may start."""
        prev = self._prev_significant()
        if prev is None:
            return True
        if prev.kind == "op":
            return prev.value not in _VALUE_ENDING_OPS
        if prev.kind == "name":
            return prev.value in _EXPRESSION_KEYWORDS
        return False  # a literal, a template, a regex — all are values

    # -- literal scanners --------------------------------------------------- #

    def _string(self) -> None:
        text, n = self.text, len(self.text)
        start, line, quote = self.i, self.line, text[self.i]
        j = self.i + 1
        while j < n:
            char = text[j]
            if char == "\\":
                j += 2
                continue
            if char == quote:
                j += 1
                break
            if char == "\n":  # unterminated; stop at the line end rather than run away
                break
            j += 1
        else:
            j = n
        self._advance(j)
        self._emit("str", start, j, line)

    def _template(self) -> None:
        """Scan a whole template literal, `${...}` interiors included.

        The interior is kept as raw text rather than tokenized separately: the
        detectors that care about it (shell interpolation, SQL concatenation)
        want the substituted expression as a string, and the ones that do not
        are better off seeing a single opaque value token.
        """
        text, n = self.text, len(self.text)
        start, line = self.i, self.line
        j = self.i + 1
        depth = 0
        while j < n:
            char = text[j]
            if char == "\\":
                j += 2
                continue
            if depth == 0 and char == "`":
                j += 1
                break
            if depth == 0 and char == "$" and j + 1 < n and text[j + 1] == "{":
                depth = 1
                j += 2
                continue
            if depth:
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                elif char in "'\"`":
                    j = self._skip_nested_string(j)
                    continue
            j += 1
        else:
            j = n
        self._advance(j)
        self._emit("template", start, j, line)

    def _skip_nested_string(self, j: int) -> int:
        """Skip a string that lives inside a template's `${...}`. Returns the end."""
        text, n = self.text, len(self.text)
        quote = text[j]
        k = j + 1
        while k < n:
            if text[k] == "\\":
                k += 2
                continue
            if text[k] == quote:
                return k + 1
            k += 1
        return n

    def _regex(self) -> None:
        text, n = self.text, len(self.text)
        start, line = self.i, self.line
        j = self.i + 1
        in_class = False
        while j < n:
            char = text[j]
            if char == "\\":
                j += 2
                continue
            if char == "\n":
                # A regex literal cannot span lines: this was division after all.
                self._advance(self.i + 1)
                self._emit("op", start, start + 1, line)
                return
            if char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            elif char == "/" and not in_class:
                j += 1
                break
            j += 1
        else:
            j = n
        while j < n and text[j].isalpha():  # flags
            j += 1
        self._advance(j)
        self._emit("regex", start, j, line)

    def _comment(self) -> None:
        text, n = self.text, len(self.text)
        start, line = self.i, self.line
        if text[self.i + 1] == "/":
            end = text.find("\n", self.i)
            end = n if end == -1 else end
        else:
            end = text.find("*/", self.i + 2)
            end = n if end == -1 else end + 2
        self._advance(end)
        self._emit("comment", start, end, line)

    # -- JSX ---------------------------------------------------------------- #

    def _at_jsx_start(self) -> bool:
        if not self.jsx or self.text[self.i] != "<" or not self._expects_value():
            return False
        j = self.i + 1
        if j < len(self.text) and self.text[j] == ">":
            return True  # <> fragment
        match = _IDENT.match(self.text, j)
        if not match:
            return False
        # `<T,>(x) => x` and `<T extends U>(...)` are generic arrows, not JSX.
        k = match.end()
        while k < len(self.text) and self.text[k] in " \t\r\n":
            k += 1
        if self.text[k:k + 1] == ",":
            return False
        return not _IDENT.match(self.text, k) or self.text[k:k + 7] != "extends"

    def _jsx_element(self) -> None:
        """Consume one JSX element (assumes the cursor sits on its `<`)."""
        self._punct("<")
        self._jsx_tag_name()
        while self.i < len(self.text):
            self._skip_space()
            rest = self.text[self.i:self.i + 2]
            if rest.startswith("/>"):
                self._punct("/>")
                return
            if rest.startswith(">"):
                self._punct(">")
                break
            char = self.text[self.i]
            if char == "{":
                self._jsx_expression_container()
            elif char in "'\"":
                self._string()
            elif char == "=":
                self._punct("=")
            elif char == "/" and self.text[self.i:self.i + 2] == "//":
                self._comment()
            else:
                match = _IDENT.match(self.text, self.i)
                if match:
                    self._jsx_tag_name()
                else:
                    self._advance(self.i + 1)  # stray character; do not stall
        self._jsx_children()

    def _jsx_tag_name(self) -> None:
        """A tag or attribute name, which may contain `.`, `:` and `-`."""
        text, n = self.text, len(self.text)
        start, line = self.i, self.line
        j = self.i
        while j < n and (text[j].isalnum() or text[j] in "_$.-:"):
            j += 1
        if j == start:
            return
        self._advance(j)
        self._emit("name", start, j, line)

    def _jsx_expression_container(self) -> None:
        """`{ ... }` inside JSX: ordinary code, scanned by the ordinary loop."""
        self._punct("{")
        depth = 1
        while self.i < len(self.text) and depth:
            before = len(self.tokens)
            if not self._step():
                return
            for token in self.tokens[before:]:
                if token.is_op("{"):
                    depth += 1
                elif token.is_op("}"):
                    depth -= 1
                    if depth == 0:
                        return

    def _jsx_children(self) -> None:
        text, n = self.text, len(self.text)
        while self.i < n:
            start, line = self.i, self.line
            j = self.i
            while j < n and text[j] not in "<{":
                j += 1
            if j > start:
                self._advance(j)
                if text[start:j].strip():
                    self._emit("jsx_text", start, j, line)
            if self.i >= n:
                return
            if text[self.i] == "{":
                self._jsx_expression_container()
                continue
            if text[self.i:self.i + 2] == "</":
                self._punct("</")
                self._jsx_tag_name()
                self._skip_space()
                if self.text[self.i:self.i + 1] == ">":
                    self._punct(">")
                return
            self._jsx_element()

    def _punct(self, literal: str) -> None:
        start, line = self.i, self.line
        self._advance(self.i + len(literal))
        self._emit("op", start, start + len(literal), line)

    # -- driver ------------------------------------------------------------- #

    def _step(self) -> bool:
        """Scan exactly one token. Returns False at end of input."""
        self._skip_space()
        text, n = self.text, len(self.text)
        if self.i >= n:
            return False
        char = text[self.i]
        start, line = self.i, self.line

        if char == "/" and self.i + 1 < n and text[self.i + 1] in "/*":
            self._comment()
            return True
        if char in "'\"":
            self._string()
            return True
        if char == "`":
            self._template()
            return True
        if char == "/" and self._expects_value():
            self._regex()
            return True
        if char == "<" and self._at_jsx_start():
            self._jsx_element()
            return True
        if char == "#":
            match = _IDENT.match(text, self.i + 1)
            if match:
                self._advance(match.end())
                self._emit("name", start, match.end(), line)
                return True
        if char.isdigit() or (char == "." and self.i + 1 < n and text[self.i + 1].isdigit()):
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
                self._punct(punctuator)
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
                raise TsSyntaxError(f"unmatched '{token.value}' on line {token.line}")
            opener = stack.pop()
            if OPENERS[tokens[opener].value] != token.value:
                raise TsSyntaxError(
                    f"'{tokens[opener].value}' on line {tokens[opener].line} "
                    f"closed by '{token.value}' on line {token.line}"
                )
            pairs[opener] = index
            pairs[index] = opener
    if stack:
        opener = tokens[stack[-1]]
        raise TsSyntaxError(f"unclosed '{opener.value}' opened on line {opener.line}")
    return pairs

