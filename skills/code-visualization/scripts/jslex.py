"""JS/TS lexing, just far enough to tell code from literal text.

imports.py reads import statements out of JS and TS with a regex; this module
says which of its matches are statements. An `import` inside a string, a
template literal or a comment is text, and the difference is decided by what
encloses the match — which no look at its neighbouring characters can see.

Stdlib only.
"""
import re

# In code the scanner jumps between the characters that can open a comment or
# a literal (and, inside a ${...} substitution, the braces that can close it).
_CODE_STOP = re.compile(r"[\"'`/]")
_SUBST_STOP = re.compile(r"[\"'`/{}]")
_STRING = {
    "'": re.compile(r"'(?:[^'\\\n]|\\.)*'", re.S),
    '"': re.compile(r'"(?:[^"\\\n]|\\.)*"', re.S),
}
_TEMPLATE_STOP = re.compile(r"\\.|`|\$\{", re.S)
# No re.S: a regex literal cannot hold a line break, escaped or not.
_REGEX = re.compile(r"/(?![*/])(?:[^/\\\[\n]|\\.|\[(?:[^\]\\\n]|\\.)*\])+/")
# A `/` after one of these opens a regex literal; anywhere else it divides.
_REGEX_AFTER = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_KEYWORDS = frozenset({
    "await", "case", "delete", "do", "else", "in", "instanceof", "new", "of",
    "return", "throw", "typeof", "void", "yield"})


def _regex_allowed(text, i):
    """Whether a `/` at text[i] opens a regex literal, judged by the token
    before it: an operand (identifier, number, `)`, `]`, a literal) makes it
    a division."""
    j = i - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j < 0 or text[j] in _REGEX_AFTER:
        return True
    k = j
    while k >= 0 and (text[k].isalnum() or text[k] in "_$"):
        k -= 1
    return k < j and text[k + 1:j + 1] in _REGEX_KEYWORDS


def scan_js(text):
    """(masked, literals): ``text`` with every comment blanked (newlines kept,
    so offsets and line numbers survive), and the sorted (start, end) spans of
    its string, template and regex literals — a template's ``${...}``
    substitutions excepted, since those are code.

    Deliberately forgiving: a quote with no partner on its line (an
    apostrophe in JSX text) opens nothing, and a would-be regex that does not
    close on its line is a division, so a misread costs one line, never the
    rest of the file.
    """
    n = len(text)
    out, literals = [], []
    resume = []  # brace depth at each open ${ substitution, innermost last
    depth = copied = i = 0

    def template_text(start):
        """Literal text from the backtick or substitution-closing brace at
        ``start`` to the template's end or its next ${; returns where code
        resumes."""
        for m in _TEMPLATE_STOP.finditer(text, start + 1):
            if m.group() in ("`", "${"):
                literals.append((start, m.end()))
                if m.group() == "${":
                    resume.append(depth)
                return m.end()
        literals.append((start, n))
        return n

    while i < n:
        stop = (_SUBST_STOP if resume else _CODE_STOP).search(text, i)
        if stop is None:
            break
        i = stop.start()
        c = text[i]
        if c == "{":
            depth += 1
            i += 1
        elif c == "}":
            if depth == resume[-1]:
                resume.pop()
                i = template_text(i)
            else:
                depth -= 1
                i += 1
        elif c == "`":
            i = template_text(i)
        elif c in _STRING:
            m = _STRING[c].match(text, i)
            if m:
                literals.append(m.span())
            i = m.end() if m else i + 1
        elif text.startswith(("//", "/*"), i):
            if text[i + 1] == "/":
                end = text.find("\n", i)
                end = n if end < 0 else end
            else:
                end = text.find("*/", i + 2)
                end = n if end < 0 else end + 2
            out += [text[copied:i], re.sub(r"[^\n]", " ", text[i:end])]
            copied = i = end
        else:
            m = _REGEX.match(text, i) if _regex_allowed(text, i) else None
            if m:
                literals.append(m.span())
            i = m.end() if m else i + 1
    out.append(text[copied:])
    return "".join(out), literals
