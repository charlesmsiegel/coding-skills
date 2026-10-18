#!/usr/bin/env python3
"""
Detect gaps in type annotations on the public API surface in Python code via AST analysis.

Incrementally typed code benefits from mypy/pyright, but only when annotations
are present and specific. This finds public functions/methods with missing or
over-broad annotations, and source lines with unscoped type: ignore comments.

Finds:
  - missing_return_annotation : public function/method lacks a return annotation
                                (and, when its parameters are bare too, says so —
                                one signature is one finding, not two)
  - missing_param_annotation  : public function/method has un-annotated parameters
                                but does annotate its return
  - any_overuse               : annotation uses `Any` or `typing.Any`, defeating static checks
                                (on any public-named def, closures and private
                                classes included: a written `Any` is a choice
                                wherever it sits)
  - broad_type_ignore         : bare `# type: ignore` without an error code in brackets

"Public" means a surface other code calls. Not reported for missing annotations:
closures and anything else defined inside a function, methods of a `_Private`
class, and web route handlers (`@router.get("/x")`, `@app.route(...)`). The
framework calls a handler, not other code, and in FastAPI a return annotation
becomes the response model — adding one changes validation and the published
schema, which is not a typing chore.
"""

import ast
import re
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import project_parts, sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files, warn_detector_error, warn_unparseable, is_route_handler


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


def _is_test_file(filepath: Path) -> bool:
    parts = project_parts(filepath)[:-1]  # below its project, not wherever it is checked out
    if "tests" in parts:
        return True
    name = filepath.name
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return False


# Matches a bare `# type: ignore` not followed by `[`
_BARE_TYPE_IGNORE = re.compile(r"#\s*type:\s*ignore(?!\s*\[)")


def _is_stub_body(node) -> bool:
    """Return True if the function body is only ... / pass / a docstring (stub)."""
    body = node.body
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr):
        val = stmt.value
        # Ellipsis literal
        if isinstance(val, ast.Constant) and val.value is ...:
            return True
        # String constant = docstring-only body
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            return True
    return False


def _api_function_ids(tree) -> set[int]:
    """id() of every def that is part of an API surface, where annotating starts.

    Excluded: anything nested in a function (a closure, or a method of a class
    defined inside one), whose only callers are in that function; and methods
    of a `_Private` class, at any depth, which callers outside the module
    cannot name. Only the missing-annotation checks use this: an `Any` someone
    wrote defeats the checker wherever it sits.
    """
    found: set[int] = set()

    def walk(body, private: bool):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not private:
                    found.add(id(node))
                # Its body is a closure scope: nothing below is API surface.
            elif isinstance(node, ast.ClassDef):
                walk(node.body, private or node.name.startswith("_"))
            else:
                # if TYPE_CHECKING / try-except-ImportError / with blocks at
                # module or class level still define module- or class-level names.
                for field in ("body", "orelse", "finalbody", "handlers"):
                    for child in getattr(node, field, None) or ():
                        if isinstance(child, ast.ExceptHandler):
                            walk(child.body, private)
                        elif isinstance(child, ast.stmt):
                            walk([child], private)

    walk(tree.body, False)
    return found


def _is_any_annotation(annotation) -> bool:
    """Return True if the annotation node is exactly `Any` or `typing.Any`."""
    if annotation is None:
        return False
    if isinstance(annotation, ast.Name) and annotation.id == "Any":
        return True
    if isinstance(annotation, ast.Attribute) and annotation.attr == "Any":
        return True
    return False


def detect(tree, filename, lines, ignore):
    issues = []

    def add(line, st, desc, sug, sev):
        if st in ignore:
            return
        issues.append(CodeSmell(filename, line, st, desc, sug, sev, _get_line(lines, line)))

    # --- broad_type_ignore: scan raw source lines ---
    for lineno, raw in enumerate(lines, start=1):
        if _BARE_TYPE_IGNORE.search(raw):
            add(lineno, "broad_type_ignore",
                "Bare '# type: ignore' suppresses all type errors on this line without specifying which",
                "Narrow to '# type: ignore[specific-error-code]' (run mypy/pyright to get the code), or fix the underlying type error.",
                "medium")

    # Build a set of function AST node ids that are direct children of a ClassDef
    method_ids: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    method_ids.add(id(item))
    surface = _api_function_ids(tree)

    # Walk all function definitions
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        fname = node.name
        is_method = id(node) in method_ids
        # Missing annotations are judged on the API surface only; a route
        # handler is exempt from both kinds.
        judge_missing = id(node) in surface and not is_route_handler(node)

        # Skip all dunder methods
        if fname.startswith("__") and fname.endswith("__"):
            continue

        # Skip private names
        if fname.startswith("_"):
            continue

        # Skip stub bodies
        if _is_stub_body(node):
            continue

        args = node.args
        all_params = list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs)

        # Determine self/cls to exclude from param annotation checks
        exclude_first = False
        if is_method and all_params:
            first_name = all_params[0].arg
            if first_name in ("self", "cls"):
                exclude_first = True

        # Judged below, with the parameters, so a bare signature is charged once.
        missing_return = node.returns is None and judge_missing

        # --- any_overuse in return annotation ---
        if _is_any_annotation(node.returns):
            kind = "method" if is_method else "function"
            add(node.lineno, "any_overuse",
                f"Return annotation of public {kind} '{fname}' is 'Any', defeating static type checking",
                f"Replace 'Any' with a specific type in '{fname}'. If the type is genuinely unknown, document why and consider a TypeVar or Protocol instead.",
                "medium")

        # --- missing_param_annotation and any_overuse in params ---
        check_params = all_params[1:] if exclude_first else all_params
        # Exclude *args and **kwargs
        vararg_names = set()
        if args.vararg:
            vararg_names.add(args.vararg.arg)
        if args.kwarg:
            vararg_names.add(args.kwarg.arg)

        unannotated = []
        for param in check_params:
            if param.arg in vararg_names:
                continue
            if param.annotation is None:
                unannotated.append(param.arg)
            elif _is_any_annotation(param.annotation):
                add(node.lineno, "any_overuse",
                    f"Parameter '{param.arg}' of public {'method' if is_method else 'function'} '{fname}' is annotated as 'Any', defeating static type checking",
                    f"Replace 'Any' with a specific type for '{param.arg}' in '{fname}'. Consider using a TypeVar, Union, or Protocol if the type is polymorphic.",
                    "medium")

        if not judge_missing:
            unannotated = []

        kind = "method" if is_method else "function"
        param_list = ", ".join(unannotated)
        # One signature, one remedy: type it. Two findings would charge the
        # same gap twice — unless the return type is --ignore'd, when the
        # parameters must still be reported on their own.
        if missing_return and unannotated and "missing_return_annotation" not in ignore:
            add(node.lineno, "missing_return_annotation",
                f"Public {kind} '{fname}' has no return type annotation and un-annotated "
                f"parameter(s): {param_list}",
                f"Annotate the signature of '{fname}': '{param_list}' and the return "
                "(e.g. '-> None' or '-> SomeType'). Enable mypy/pyright incrementally; "
                "start with the public API surface.",
                "low")
        elif missing_return and not unannotated:
            add(node.lineno, "missing_return_annotation",
                f"Public {kind} '{fname}' has no return type annotation",
                f"Add a return annotation to '{fname}' (e.g. '-> None' or '-> SomeType'). Enable mypy/pyright incrementally with per-module ignores.",
                "low")
        elif unannotated:
            add(node.lineno, "missing_param_annotation",
                f"Public {kind} '{fname}' has un-annotated parameter(s): {param_list}",
                f"Add type annotations for '{param_list}' in '{fname}'. Enable mypy/pyright incrementally; start with the public API surface.",
                "low")

    return issues


def analyze_file(filepath: Path, ignore: set) -> list:
    if _is_test_file(filepath):
        return []
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
    parser = argparse.ArgumentParser(description="Detect type-annotation gaps in Python public API")
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
            print("✅ No type-annotation gaps found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} type-annotation gap(s):\n\nSummary:")
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
