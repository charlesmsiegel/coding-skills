#!/usr/bin/env python3
"""
Detect unpythonic patterns in Python code.
Finds: non-idiomatic patterns that have cleaner Pythonic alternatives.
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import cached_parse, SEVERITY_ICONS, configure_output, find_python_files, warn_detector_error, warn_unparseable


@dataclass
class UnpythonicPattern:
    file: str
    line: int
    pattern_type: str
    description: str
    before: str
    after: str
    severity: str


def _is_keys_call(expr: ast.AST) -> bool:
    """True for a bare `<something>.keys()` call."""
    return (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
            and expr.func.attr == 'keys' and not expr.args and not expr.keywords)


class UnpythonicDetector(ast.NodeVisitor):
    def __init__(self, filename: str, source_lines: list[str]):
        self.filename = filename
        self.source_lines = source_lines
        self.issues: list[UnpythonicPattern] = []

    def _add(self, line: int, pattern_type: str, desc: str, before: str, after: str, severity: str = "low"):
        self.issues.append(UnpythonicPattern(
            file=self.filename, line=line, pattern_type=pattern_type,
            description=desc, before=before, after=after, severity=severity
        ))

    def visit_For(self, node: ast.For):
        # range(len(x)) pattern
        if isinstance(node.iter, ast.Call):
            if isinstance(node.iter.func, ast.Name) and node.iter.func.id == 'range':
                if node.iter.args:
                    first_arg = node.iter.args[0]
                    if isinstance(first_arg, ast.Call):
                        if isinstance(first_arg.func, ast.Name) and first_arg.func.id == 'len':
                            self._add(node.lineno, "range_len_loop",
                                "Using range(len()) instead of enumerate or direct iteration",
                                "for i in range(len(x)): x[i]",
                                "for item in x: ... or for i, item in enumerate(x):",
                                "medium")
        
        # Manual index tracking. A bare `count += 1` is an accumulator, not an
        # index — only flag when the incremented name actually subscripts
        # something inside the same loop.
        for stmt in node.body:
            if (isinstance(stmt, ast.AugAssign) and isinstance(stmt.op, ast.Add)
                    and isinstance(stmt.value, ast.Constant) and stmt.value.value == 1
                    and isinstance(stmt.target, ast.Name)):
                counter = stmt.target.id
                used_as_index = any(
                    isinstance(sub, ast.Subscript)
                    and isinstance(sub.slice, ast.Name) and sub.slice.id == counter
                    for sub in ast.walk(node)
                )
                if used_as_index:
                    self._add(node.lineno, "manual_index",
                        "Manual index tracking instead of enumerate()",
                        "i = 0; for x in items: use(seq[i]); i += 1",
                        "for i, x in enumerate(items):",
                        "low")
                    break

        # Iterating d.keys() directly
        if _is_keys_call(node.iter):
            self._add(node.lineno, "dict_keys_iteration",
                "Iterating .keys() is unnecessary — iterating a dict yields its keys",
                "for k in d.keys():", "for k in d:", "low")

        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension):
        if _is_keys_call(node.iter):
            self._add(node.iter.lineno, "dict_keys_iteration",
                "Iterating .keys() is unnecessary — iterating a dict yields its keys",
                "[f(k) for k in d.keys()]", "[f(k) for k in d]", "low")
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare):
        for op, comparator in zip(node.ops, node.comparators):
            # Membership against .keys() — `k in d` does the same, faster.
            if isinstance(op, (ast.In, ast.NotIn)) and _is_keys_call(comparator):
                self._add(node.lineno, "dict_keys_iteration",
                    "Membership test against .keys() is unnecessary — test the dict",
                    "if k in d.keys():", "if k in d:", "low")
            if isinstance(op, ast.Eq):
                if isinstance(comparator, ast.Constant):
                    if comparator.value is True:
                        self._add(node.lineno, "compare_to_true",
                            "Comparing to True explicitly",
                            "if x == True:", "if x:", "low")
                    elif comparator.value is False:
                        self._add(node.lineno, "compare_to_false",
                            "Comparing to False explicitly",
                            "if x == False:", "if not x:", "low")
            
            if isinstance(op, (ast.Eq, ast.NotEq)):
                if isinstance(comparator, ast.Constant) and comparator.value is None:
                    op_name = '==' if isinstance(op, ast.Eq) else '!='
                    is_name = 'is' if isinstance(op, ast.Eq) else 'is not'
                    self._add(node.lineno, "compare_none_equality",
                        f"Using {op_name} None instead of {is_name} None",
                        f"if x {op_name} None:", f"if x {is_name} None:", "medium")
        
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == 'sorted' and node.args:
            if _is_keys_call(node.args[0]):
                self._add(node.lineno, "sorted_dict_keys",
                    "sorted(d.keys()) is redundant",
                    "sorted(d.keys())", "sorted(d)", "low")
        self.generic_visit(node)

    # Exception-swallowing (bare/narrow `except: pass`) is detected by
    # find_exception_issues.py (swallowed_exception) — the dedicated owner —
    # so it is intentionally not duplicated here.
    #
    # `x = x + y` is deliberately NOT rewritten to `x += y`: for lists and
    # arrays += mutates in place through every alias, so the "cleanup" changes
    # behavior. A rule this skill cannot apply safely is not worth reporting.

    def visit_Import(self, node: ast.Import):
        if len(node.names) > 1:
            self._add(node.lineno, "multiple_imports",
                "Multiple imports on one line",
                "import os, sys",
                "import os\\nimport sys", "low")
        self.generic_visit(node)


def analyze_file(filepath: Path, ignore: set[str] = frozenset()) -> list[UnpythonicPattern]:
    try:
        source, tree = cached_parse(filepath)
        lines = source.splitlines()
        detector = UnpythonicDetector(str(filepath), lines)
        detector.visit(tree)
        return [i for i in detector.issues if i.pattern_type not in ignore]
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect unpythonic patterns")
    parser.add_argument('path', nargs='?', default='.', help='File or directory')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--ignore', type=str, default='', help='Comma-separated pattern types to ignore')

    args = parser.parse_args()
    ignore = set(args.ignore.split(',')) if args.ignore else set()

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))

    all_issues.sort(key=lambda x: (x.severity != 'high', x.severity != 'medium', x.file, x.line))
    
    if args.format == 'json':
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No unpythonic patterns found!")
            return
        
        by_type = defaultdict(int)
        for issue in all_issues:
            by_type[issue.pattern_type] += 1
        
        print(f"Found {len(all_issues)} unpythonic pattern(s):\n")
        print("Summary:")
        for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {t}: {c}")
        print()
        
        severity_icons = SEVERITY_ICONS
        
        for issue in all_issues:
            icon = severity_icons[issue.severity]
            print(f"{icon} [{issue.severity.upper()}] {issue.file}:{issue.line}")
            print(f"   {issue.pattern_type}: {issue.description}")
            print(f"   Before: {issue.before}")
            print(f"   After:  {issue.after}\n")


if __name__ == '__main__':
    main()
