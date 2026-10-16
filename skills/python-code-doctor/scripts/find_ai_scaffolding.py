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
  - placeholder_value    : string literal whose value is clearly a placeholder
  - unused_kwargs        : **kwargs that is never referenced inside the body
"""

import ast
import re
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files, warn_detector_error, warn_unparseable


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

_PLACEHOLDER_EXACT = {
    "lorem ipsum",
    "your-api-key",
    "your_api_key",
    "api_key_here",
    "api-key-here",
    "replace_me",
    "replaceme",
    "changeme",
    "change_me",
}

_PLACEHOLDER_CONTAINS = [
    "example.com",
]

_PLACEHOLDER_STARTSWITH = [
    "lorem ipsum",
    "<your",
]

# The replace-me words count only as whole tokens: "a very long replacement"
# contains `replaceme` and is prose. Underscore and hyphen separate tokens here
# (`API_KEY_CHANGEME` is a placeholder), letters and digits do not.
_PLACEHOLDER_REGEX = re.compile(
    r"(your[_\-]api[_\-]key|api[_\-]key[_\-]here|insert_.+_here|<your"
    r"|(?<![a-z0-9])(?:replace_me|replaceme|changeme|change_me)(?![a-z0-9]))",
    re.IGNORECASE,
)


def _is_placeholder_string(value: str) -> bool:
    """Return True if the string value looks like a placeholder."""
    v = value.strip().lower()
    if v in _PLACEHOLDER_EXACT:
        return True
    for pat in _PLACEHOLDER_CONTAINS:
        if pat in v:
            return True
    for pat in _PLACEHOLDER_STARTSWITH:
        if v.startswith(pat):
            return True
    if _PLACEHOLDER_REGEX.search(value):
        return True
    return False


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
            if kwarg_name not in used:
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
    # Skip docstring constants — the first Expr(Constant(str)) in the body of
    # a Module, ClassDef, FunctionDef, or AsyncFunctionDef.
    # -----------------------------------------------------------------------
    _docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body:
                first = body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    _docstring_ids.add(id(first.value))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        if not isinstance(node.value, str):
            continue
        if id(node) in _docstring_ids:
            continue
        if _is_placeholder_string(node.value):
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
