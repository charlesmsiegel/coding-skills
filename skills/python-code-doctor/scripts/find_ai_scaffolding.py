#!/usr/bin/env python3
"""
Detect placeholder/stub leftovers — code that "looks done but isn't".

AI-generated code often ships with scaffolding that was never replaced with real
implementations: NotImplementedError stubs, empty pass/... bodies, TODO/FIXME
comments demanding implementation, placeholder string literals, and unused
**kwargs catch-alls that silently swallow typos.

Finds:
  - stub_not_implemented : function whose only effective body is raise NotImplementedError
  - empty_stub           : function whose body is only pass or ...
  - todo_implement       : comment with TODO/FIXME + implementation keyword
  - placeholder_value    : string literal whose value is clearly a placeholder —
                           a whole-word token (CHANGEME, REPLACE_ME, your-api-key),
                           or a reserved example.com host / lorem ipsum outside
                           tests (in a test they are the right fixture values)
  - unused_kwargs        : **kwargs that is never referenced inside the body, on a
                           function whose signature is its own to choose (not a
                           callback, an override, an adapter, or `**_ignored`)
"""

import ast
import re
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import (sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files,
                    looks_like_test, warn_detector_error, warn_unparseable)


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decorator_names(func_node):
    """Return the flat name strings for all decorators on a function node."""
    names = []
    for dec in func_node.decorator_list:
        if isinstance(dec, ast.Name):
            names.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.append(dec.attr)
            names.append(f"{ast.unparse(dec)}" if hasattr(ast, "unparse") else "")
    return names


def _is_abstract(func_node):
    """True if decorated with abstractmethod or abc.abstractmethod."""
    for dec in func_node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "abstractmethod":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "abstractmethod":
            return True
    return False


def _is_overload(func_node):
    """True if decorated with @overload or @typing.overload."""
    for dec in func_node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "overload":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "overload":
            return True
    return False


def _class_is_abstract(class_node):
    """True if the class directly inherits from ABC, ABCMeta, or Protocol."""
    abstract_bases = {"ABC", "ABCMeta", "Protocol"}
    for base in class_node.bases:
        if isinstance(base, ast.Subscript):
            base = base.value
        if isinstance(base, ast.Name) and base.id in abstract_bases:
            return True
        if isinstance(base, ast.Attribute) and base.attr in abstract_bases:
            return True
    return False


def _method_in_abstract_class(func_node, class_map):
    """True if func_node is a method inside an abstract class."""
    return class_map.get(id(func_node), False)


def _build_class_map(tree):
    """
    Return a dict mapping id(func_node) -> bool indicating whether that function
    is a DIRECT method of an abstract class (ABC/ABCMeta/Protocol).

    Only iterates node.body (direct children) rather than ast.walk, so helper
    functions nested inside concrete methods of an ABC are NOT marked exempt.
    """
    result = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            is_abs = _class_is_abstract(node)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    result[id(item)] = is_abs
    return result


def _effective_body(func_node):
    """
    Return the list of effective statements in the function body, stripping a
    leading docstring expression if present.
    """
    body = func_node.body
    if not body:
        return body
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]
    return body


def _is_only_raise_not_implemented(func_node):
    """True if the effective body is a single `raise NotImplementedError(...)`."""
    eff = _effective_body(func_node)
    if len(eff) != 1:
        return False
    stmt = eff[0]
    if not isinstance(stmt, ast.Raise):
        return False
    exc = stmt.exc
    if exc is None:
        return False
    # raise NotImplementedError  OR  raise NotImplementedError(...)
    if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
        return True
    if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name) and exc.func.id == "NotImplementedError":
        return True
    return False


def _is_only_pass_or_ellipsis(func_node):
    """True if effective body is only `pass` or `...`."""
    eff = _effective_body(func_node)
    if len(eff) != 1:
        return False
    stmt = eff[0]
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is ...:
        return True
    return False


# ---------------------------------------------------------------------------
# placeholder_value patterns
# ---------------------------------------------------------------------------

# Placeholder tokens, matched as whole words. Unanchored, `replaceme` matched
# inside "replacement" and `replace_me` inside "replace_messages", so every
# find-and-replace rule's `rule["replacement"]` read as a placeholder. A letter
# or digit on either side means the token is part of a longer word; `_` and
# `-` do not, so REPLACE_ME_WITH_KEY still matches.
_PLACEHOLDER_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:your[_\-]api[_\-]key|api[_\-]key[_\-]here|insert[_\-][\w\-]+?[_\-]here|replace[_\-]?me|change[_\-]?me)"
    r"(?![A-Za-z0-9])"
    r"|<your",
    re.IGNORECASE,
)

# RFC 2606 reserves these second-level domains for documentation, so in
# shipping code a URL on one is a value nobody replaced. Matched as a host:
# `myexample.com` is somebody's real domain.
_EXAMPLE_HOST = re.compile(r"(?<![A-Za-z0-9-])example\.(?:com|org|net)(?![A-Za-z0-9-])", re.IGNORECASE)

_LOREM = "lorem ipsum"


def _is_placeholder_string(value: str, in_test: bool = False) -> bool:
    """Return True if the string value looks like a placeholder.

    In a test file a reserved example host and lorem-ipsum filler are not
    placeholders: a fake endpoint on example.com is the correct fixture value
    (RFC 2606 reserves it for exactly that, as RFC 6761 reserves `.test` and
    `.invalid`), and filler text is the input under test. "Replace it with a
    real value" is the wrong remedy for either.
    """
    if _PLACEHOLDER_TOKEN.search(value):
        return True
    if in_test:
        return False
    return value.strip().lower().startswith(_LOREM) or bool(_EXAMPLE_HOST.search(value))


# ---------------------------------------------------------------------------
# todo_implement patterns
# ---------------------------------------------------------------------------

_TODO_RE = re.compile(r"(TODO|FIXME)", re.IGNORECASE)
_IMPL_RE = re.compile(
    r"(implement|complete|fill[\s_-]+in|stub|placeholder|finish[\s_-]+this)",
    re.IGNORECASE,
)


def _is_todo_implement_comment(text: str) -> bool:
    """Return True if the comment text contains TODO/FIXME + an implementation keyword."""
    return bool(_TODO_RE.search(text) and _IMPL_RE.search(text))


# ---------------------------------------------------------------------------
# unused_kwargs
# ---------------------------------------------------------------------------

def _names_used_in_body(func_node):
    """Collect all Name ids that appear in Load context anywhere in the body."""
    used = set()
    for node in ast.walk(func_node):
        if node is func_node:
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
    return used


def _is_stub_body(func_node):
    """True if the body is pass/...  or raise NotImplementedError — i.e., nothing real."""
    return _is_only_pass_or_ellipsis(func_node) or _is_only_raise_not_implemented(func_node)


def _build_override_map(tree):
    """id(method) for every method that overrides a same-named method of a base
    class defined in this file. An override has to accept what its base accepts,
    so an unused **kwargs there is the signature contract, not a catch-all.

    A base this file cannot see (an import) proves nothing either way, so its
    subclasses' methods are not exempted.
    """
    classes = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes[node.name].append(node)

    def methods_of(class_node):
        return {item.name for item in class_node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def inherited(class_node, seen):
        names = set()
        for base in class_node.bases:
            if not isinstance(base, ast.Name):
                continue
            for base_node in classes.get(base.id, ()):
                if id(base_node) in seen:
                    continue
                seen.add(id(base_node))
                names |= methods_of(base_node) | inherited(base_node, seen)
        return names

    overrides = set()
    for nodes in classes.values():
        for class_node in nodes:
            if not class_node.bases:
                continue
            from_bases = inherited(class_node, {id(class_node)})
            for item in class_node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in from_bases:
                    overrides.add(id(item))
    return overrides
def _names_passed_by_reference(tree) -> set:
    """Names loaded anywhere other than as the callee of a call.

    A function named this way is handed to someone — `monkeypatch.setattr(m,
    "f", boom)`, a callback, a registry — and the callee that will invoke it
    dictates its signature. Its `**kwargs` absorbs arguments production code
    passes, written against the real signature; dropping it makes that call
    raise, and there is no typo of the author's for it to hide.
    """
    callees = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    return {node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and id(node) not in callees}


def _methods_of_derived_classes(tree) -> set:
    """Methods with an unresolved base contract; visible local overrides are
    handled separately by _build_override_map, so new local methods still count.
    """
    classes = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes[node.name].append(node)

    def unresolved(node, seen):
        if id(node) in seen:
            return False
        seen = seen | {id(node)}
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "object":
                continue
            local = classes.get(base.id, []) if isinstance(base, ast.Name) else []
            if len(local) != 1 or unresolved(local[0], seen):
                return True
        return False

    return {id(item) for nodes in classes.values() for node in nodes if unresolved(node, set())
            for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _kwargs_owned_by_the_signature(func_node, by_reference, derived_methods, used) -> bool:
    """True when `**kwargs` is there because someone else chose the signature."""
    kwarg = func_node.args.kwarg.arg
    if kwarg.startswith("_"):
        return True                           # `**_ignored`: the declared-unused convention
    if func_node.name in by_reference:
        return True                           # a callback or a monkeypatched replacement
    if id(func_node) in derived_methods:
        return True                           # an override or a protocol implementation
    vararg = func_node.args.vararg
    # `*args, **kwargs` both unused: an adapter that absorbs any call shape.
    return vararg is not None and vararg.arg not in used


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------

def detect(tree, filename, lines, ignore):
    issues = []

    def add(line, st, desc, sug, sev):
        if st in ignore:
            return
        issues.append(CodeSmell(filename, line, st, desc, sug, sev, _get_line(lines, line)))

    class_map = _build_class_map(tree)
    overrides = _build_override_map(tree)
    by_reference = _names_passed_by_reference(tree)
    derived_methods = _methods_of_derived_classes(tree)
    in_test = looks_like_test(filename)

    # -----------------------------------------------------------------------
    # stub_not_implemented, empty_stub, unused_kwargs
    # -----------------------------------------------------------------------
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        in_abc = _method_in_abstract_class(node, class_map)
        is_abs = _is_abstract(node)

        # stub_not_implemented
        if (
            "stub_not_implemented" not in ignore
            and _is_only_raise_not_implemented(node)
            and not is_abs
            and not in_abc
        ):
            add(
                node.lineno,
                "stub_not_implemented",
                f"Function '{node.name}' is a non-abstract stub that only raises NotImplementedError",
                "Implement the function or remove it if it is not needed.",
                "medium",
            )

        # empty_stub
        if (
            "empty_stub" not in ignore
            and _is_only_pass_or_ellipsis(node)
            and not is_abs
            and not in_abc
            and not _is_overload(node)
        ):
            add(
                node.lineno,
                "empty_stub",
                f"Function '{node.name}' has an empty body (pass or ...) without being abstract or overloaded",
                "Implement it or delete the empty placeholder.",
                "low",
            )

        # unused_kwargs — a leading underscore (`**_kwargs`) is the convention
        # for "deliberately unused", and an override keeps its base's signature.
        if (
            "unused_kwargs" not in ignore
            and node.args.kwarg is not None
            and not node.args.kwarg.arg.startswith("_")
            and id(node) not in overrides
            and not _is_stub_body(node)
            and not node.decorator_list
        ):
            kwarg_name = node.args.kwarg.arg
            used = _names_used_in_body(node)
            if kwarg_name not in used and not _kwargs_owned_by_the_signature(
                    node, by_reference, derived_methods, used):
                add(
                    node.lineno,
                    "unused_kwargs",
                    f"Function '{node.name}' accepts **{kwarg_name} but never uses it in the body",
                    "Drop the unused **kwargs; it adds a silent catch-all that hides typos.",
                    "low",
                )

    # -----------------------------------------------------------------------
    # todo_implement  (line scan)
    # -----------------------------------------------------------------------
    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.lstrip()
        if not stripped.startswith("#"):
            continue
        comment_text = stripped[1:].strip()
        if _is_todo_implement_comment(comment_text):
            add(
                lineno,
                "todo_implement",
                f"Comment signals unfinished implementation: {comment_text[:80]}",
                "Finish the implementation before shipping.",
                "medium",
            )

    # -----------------------------------------------------------------------
    # placeholder_value  (AST scan for string Constants)
    # Skip a string that is a whole statement: a docstring, a PEP 257
    # attribute docstring after an assignment, or a string used as a block
    # comment. It has no runtime effect, so it is prose about the code, never
    # a value that ships.
    # -----------------------------------------------------------------------
    _docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            _docstring_ids.add(id(node.value))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, str):
            continue
        if id(node) in _docstring_ids:
            continue
        if _is_placeholder_string(node.value, in_test):
            add(
                node.lineno,
                "placeholder_value",
                f"String literal looks like a placeholder: {node.value!r}",
                "Replace the placeholder with a real value or load it from config/env.",
                "medium",
            )

    return issues


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source, tree = cached_parse(filepath)
        return detect(tree, str(filepath), source.splitlines(), ignore)
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect AI scaffolding leftovers in Python")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))
    sort_findings(all_issues)

    if args.format == "json":
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No AI scaffolding leftovers found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} AI scaffolding issue(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = SEVERITY_ICONS
        for i in all_issues:
            print(f"{icons[i.severity]} [{i.severity.upper()}] {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            print(f"   → {i.suggestion}\n")


if __name__ == "__main__":
    main()
