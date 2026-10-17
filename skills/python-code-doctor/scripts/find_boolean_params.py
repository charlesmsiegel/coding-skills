#!/usr/bin/env python3
"""
Detect boolean-flag parameters in function *definitions* via AST analysis.

A boolean parameter usually means the function does two things; call sites read
as `do_thing(True)` with no clue what True means. This complements the existing
call-site `boolean_blindness` check in find_code_smells by catching the smell at
the definition.

Finds:
  - multiple_boolean_flags : a function with >= 2 boolean-default parameters that
    can be passed positionally. When the flags are keyword-only — the remedy
    boolean_positional_param asks for — every call site already names them, and
    "it does several things" is a guess the signature cannot prove, so it is a
    candidate.
  - boolean_positional_param : a boolean parameter that can be passed positionally

Not checked, because the signature is not the author's to choose:
  - a CLI command (`@app.command()`, `@click.command`, ...): each boolean is a
    command-line flag, and the framework builds the CLI from the signature
  - a method of a class defined in a test file: a fake mirrors the signature of
    the production object it stands in for
A web route handler (`@app.get(...)`, `@router.post(...)`, `@bp.route(...)`) is
called by its framework with keyword arguments bound from the request, so its
parameters are keyword-only in practice: no call site can write `handler(True)`.
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import is_cli_command, is_test_file, sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files, warn_detector_error, warn_unparseable, is_route_handler


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


_SKIP = {"self", "cls"}

_KEYWORD_FLAGS_REASONS = (
    "every flag is keyword-only, so each call site names what it turns on, and "
    "options that tune one operation are not several operations",
)


def _is_bool_const(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, bool)


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


class BooleanParamDetector(ast.NodeVisitor):
    def __init__(self, filename, lines, ignore, in_tests=False):
        self.filename = filename
        self.lines = lines
        self.ignore = ignore
        self.issues = []
        self.in_tests = in_tests
        self._methods: set[int] = set()  # id() of defs directly in a class body

    def _add(self, line, st, desc, sug, sev):
        if st in self.ignore:
            return
        self.issues.append(CodeSmell(self.filename, line, st, desc, sug, sev, _get_line(self.lines, line)))

    def _check(self, node):
        if is_cli_command(node) or (self.in_tests and id(node) in self._methods):
            return
        args = node.args
        positional = list(getattr(args, "posonlyargs", [])) + list(args.args)
        defaults = list(args.defaults)
        bool_positional = []   # bool-default params that can be passed positionally
        bool_total = 0
        # The framework binds a route handler's arguments by name.
        keyword_bound = is_route_handler(node)

        if defaults:
            for arg, default in zip(positional[-len(defaults):], defaults):
                if _is_bool_const(default) and arg.arg not in _SKIP:
                    bool_total += 1
                    if not keyword_bound:
                        bool_positional.append(arg.arg)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            if default is not None and _is_bool_const(default) and arg.arg not in _SKIP:
                bool_total += 1   # keyword-only, so not positional

        if len(bool_positional) >= 2:
            self._add(node.lineno, "multiple_boolean_flags",
                f"{node.name} has {bool_total} boolean parameters, {len(bool_positional)} of them "
                "positional; it likely does several things",
                "Split into separate functions, or replace the flags with an Enum / small options object.",
                "medium")
        elif bool_total >= 2 and "multiple_boolean_flags" not in self.ignore:
            self.issues.append(CodeSmell(
                self.filename, node.lineno, "multiple_boolean_flags",
                f"{node.name} has {bool_total} boolean parameters, "
                f"{bool_total - len(bool_positional)} of them keyword-only",
                "", "low", _get_line(self.lines, node.lineno),
                kind="candidate", also_caused_by=_KEYWORD_FLAGS_REASONS))
        for name in bool_positional:
            self._add(node.lineno, "boolean_positional_param",
                f"Boolean parameter '{name}' in {node.name} can be passed positionally (e.g. {node.name}(..., True))",
                "Make it keyword-only by adding '*,' before it so call sites are self-documenting, or split the function.",
                "low")

    def visit_ClassDef(self, node):
        self._methods.update(id(item) for item in node.body
                             if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)))
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._check(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._check(node)
        self.generic_visit(node)


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source, tree = cached_parse(filepath)
        d = BooleanParamDetector(str(filepath), source.splitlines(), ignore, is_test_file(filepath))
        d.visit(tree)
        return d.issues
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect boolean-flag parameters")
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
            print("✅ No boolean-flag parameters found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} boolean-flag issue(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = SEVERITY_ICONS
        for i in all_issues:
            if i.kind == "candidate":
                print(f"? [CANDIDATE] {i.file}:{i.line}")
            else:
                print(f"{icons[i.severity]} [{i.severity.upper()}] {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            if i.suggestion:
                print(f"   → {i.suggestion}")
            for reason in i.also_caused_by:
                print(f"   ? also caused by: {reason}")
            print()


if __name__ == "__main__":
    main()
