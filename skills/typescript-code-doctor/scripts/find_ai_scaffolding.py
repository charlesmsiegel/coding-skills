#!/usr/bin/env python3
"""
Find the tells of generated-but-unfinished code: not-implemented stubs,
placeholder values, options nothing reads, symbols defined twice, and merge
markers.

These are not "AI smells" in some mystical sense. They are what any code written
faster than it was read looks like: plausible shape, missing middle. The two
that cost the most are the silent ones — a second declaration of the same name,
where the later one wins and the earlier one is dead; and an options parameter
the body never destructures, so every caller's setting is ignored.
"""

import re
from collections import defaultdict

from common import Reporter, is_test_file, run_file_detector
from tsparse import TsFile, body_indices

NOT_IMPLEMENTED = re.compile(r"not\s*implemented|unimplemented|todo:?\s*implement", re.IGNORECASE)
# Deliberately narrow. `foo`, `sample` and `placeholder` are ordinary words in a
# test or a UI string, so only the phrases that no finished code contains count.
PLACEHOLDER_VALUES = re.compile(
    r"^(?:lorem ipsum.*|your[_-]?\w+[_-]?here|replace[_-]?me|change[_-]?me|"
    r"insert[_-]?\w+[_-]?here|xxxx+|tbd|dummy value|<[a-z_ ]+>)$",
    re.IGNORECASE,
)
CONFLICT_MARKER = re.compile(r"^(<{7}|={7}|>{7})(?:\s|$)")


def _check_stubs(file: TsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.has_body or func.is_abstract:
            continue
        body = list(body_indices(func))
        if not body:
            continue
        text = file.slice(func.body_open + 1, func.body_close).strip()
        if NOT_IMPLEMENTED.search(text) and "throw" in text:
            report.add(func.line, "not_implemented_stub",
                       f"`{func.qualname}` throws 'not implemented'",
                       "Finish it or delete it. A stub that compiles is indistinguishable from "
                       "working code until it runs, and it satisfies every interface check on the "
                       "way there.", "high")
            continue
        if len(body) <= 3 and text.startswith("return") and not func.name.startswith(("get", "is", "has")):
            literal = file.tokens[body[1]] if len(body) > 1 else None
            if literal is not None and literal.is_name("null", "undefined") and func.return_type \
                    and "null" not in func.return_type and "undefined" not in func.return_type:
                report.add(func.line, "placeholder_return",
                           f"`{func.qualname}` returns `{literal.value}` but its type says "
                           f"`{func.return_type}`",
                           "Either the body is unfinished or the return type is wrong. Both compile "
                           "only because something upstream is loose.", "medium")


def _check_unused_options(file: TsFile, report: Reporter) -> None:
    """An options parameter the body never reads: every caller's setting is ignored."""
    for func in file.functions:
        if not func.has_body:
            continue
        for param in func.params:
            if param.is_destructured or param.is_rest:
                continue
            looks_like_options = param.name in ("options", "opts", "config", "settings", "params") \
                or param.type_text.strip().startswith("{")
            if not looks_like_options:
                continue
            used = any(
                file.tokens[i].kind == "name" and file.tokens[i].value == param.name
                for i in body_indices(func)
            )
            if not used:
                report.add(func.line, "ignored_options_parameter",
                           f"`{func.qualname}` takes `{param.name}` and never reads it",
                           "Every caller that passes a setting is silently ignored, and the "
                           "signature says otherwise. Use it or remove it.", "high")


def _check_duplicate_definitions(file: TsFile, report: Reporter) -> None:
    """The same name declared twice at module scope — the later one wins."""
    declarations: dict[str, list[int]] = defaultdict(list)
    for func in file.functions:
        # Module scope only. Two nested `function onKeyDown` helpers in two
        # different components are separate bindings, not a redefinition.
        if func.kind == "function" and func.name != "<anonymous>" and func.has_body \
                and file.top_level(func.start):
            declarations[func.name].append(func.line)
    for klass in file.classes:
        if klass.name != "<anonymous>" and file.top_level(klass.start):
            declarations[klass.name].append(klass.line)
    for decl in file.types:
        # Interfaces merge by design; a repeated `type` or `enum` does not.
        if decl.kind != "interface":
            declarations[decl.name].append(decl.line)

    for name, lines in declarations.items():
        if len(lines) < 2:
            continue
        report.add(lines[-1], "duplicate_definition",
                   f"`{name}` is declared {len(lines)} times in this file (lines "
                   f"{', '.join(str(line) for line in lines)})",
                   "Merge them and delete the loser. The last declaration wins at runtime, so the "
                   "earlier body is dead code that still reads as live.", "high",
                   related=lines[:-1])


def _check_conflict_markers(file: TsFile, report: Reporter) -> None:
    for number, line in enumerate(file.lines, 1):
        if CONFLICT_MARKER.match(line):
            report.add(number, "merge_conflict_marker",
                       f"Merge conflict marker `{line[:20].strip()}` left in the file",
                       "Resolve the conflict. This file does not compile, and if it somehow does, "
                       "half of it is from a branch nobody chose.", "high")
            return


def _check_placeholder_values(file: TsFile, report: Reporter) -> None:
    for token in file.tokens:
        if token.kind != "str":
            continue
        literal = token.value.strip("'\"")
        if PLACEHOLDER_VALUES.match(literal):
            report.add(token.line, "placeholder_value",
                       f"Placeholder literal `{literal[:40]}` in source",
                       "Replace it with the real value, or move it into a fixture. A placeholder "
                       "that ships looks like data to everything downstream.", "medium")


def _check_empty_catch_scaffold(file: TsFile, report: Reporter) -> None:
    """`catch { /* handle error */ }` — a comment standing in for the handler."""
    for comment in file.comments:
        text = comment.value.strip("/* \t")
        if re.fullmatch(r"(?:handle|process)\s+(?:the\s+)?(?:error|exception)s?\.?", text, re.IGNORECASE):
            report.add(comment.line, "handler_placeholder_comment",
                       "A comment describing error handling that was never written",
                       "Write the handler, or let the error propagate. The comment makes the empty "
                       "block look deliberate to every future reader.", "medium")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_conflict_markers(file, report)
    _check_stubs(file, report)
    _check_unused_options(file, report)
    _check_duplicate_definitions(file, report)
    _check_empty_catch_scaffold(file, report)
    if not is_test_file(file.path):
        _check_placeholder_values(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find unfinished scaffolding: stubs, placeholders, ignored options, duplicate definitions",
        "No scaffolding or placeholders found!",
        analyze,
    )
