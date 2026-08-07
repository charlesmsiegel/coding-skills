#!/usr/bin/env python3
"""
Detect code smells in Python code via AST analysis.
Finds: magic numbers, bare excepts, mutable defaults, type comparisons,
       god classes, data classes, long parameter lists.
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import SEVERITY_ICONS, configure_output, find_python_files, warn_detector_error, warn_unparseable


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""


# Classes with these decorators/bases are data containers on purpose.
DATA_CLASS_MARKERS = frozenset({
    "dataclass", "attrs", "attr", "define", "frozen",
    "NamedTuple", "TypedDict", "Protocol",
    "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag",
    "BaseModel",  # pydantic
})


def _unwrap_negative(node: ast.AST) -> ast.AST:
    """-5 parses as UnaryOp(USub, Constant(5)); return the inner constant."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return node.operand
    return node


class CodeSmellDetector(ast.NodeVisitor):
    # Values so conventional a name would add nothing.
    MAGIC_NUMBER_WHITELIST = frozenset({
        10, 100, 1000, 24, 60, 365,
        16, 32, 64, 128, 255, 256, 512, 1024,
    })

    def __init__(self, filename: str, source_lines: list[str], ignore: set[str] = None):
        self.filename = filename
        self.source_lines = source_lines
        self.issues: list[CodeSmell] = []
        self.ignore = ignore or set()
        self.current_class = None
        self.class_info: dict[str, dict] = {}
        self._named_constants: set[int] = set()

    def visit_Module(self, node: ast.Module):
        # A literal is only "magic" when nothing names it. Collect the node
        # ids of literals that already have a name or a self-explaining
        # position, so visit_Constant can skip them:
        #   NAME = 30              the assignment names it
        #   def f(timeout=30)      the parameter names it
        #   get(url, timeout=30)   the keyword names it
        #   text[:200]             slice bounds
        #   [1, 30, 5] / {...}     data tables, not control values
        for parent in ast.walk(node):
            if isinstance(parent, (ast.Assign, ast.AnnAssign)):
                targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                value = _unwrap_negative(getattr(parent, "value", None))
                if isinstance(value, ast.Constant) and any(isinstance(t, ast.Name) for t in targets):
                    self._named_constants.add(id(value))
            elif isinstance(parent, ast.keyword):
                value = _unwrap_negative(parent.value)
                if isinstance(value, ast.Constant):
                    self._named_constants.add(id(value))
            elif isinstance(parent, ast.arguments):
                for default in [*parent.defaults, *parent.kw_defaults]:
                    default = _unwrap_negative(default) if default else default
                    if isinstance(default, ast.Constant):
                        self._named_constants.add(id(default))
            elif isinstance(parent, ast.Subscript):
                for sub in ast.walk(parent.slice):
                    if isinstance(sub, ast.Constant):
                        self._named_constants.add(id(sub))
            elif isinstance(parent, (ast.List, ast.Tuple, ast.Set)):
                for elt in parent.elts:
                    elt = _unwrap_negative(elt)
                    if isinstance(elt, ast.Constant):
                        self._named_constants.add(id(elt))
            elif isinstance(parent, ast.Dict):
                for elt in [*parent.keys, *parent.values]:
                    elt = _unwrap_negative(elt) if elt else elt
                    if isinstance(elt, ast.Constant):
                        self._named_constants.add(id(elt))
        self.generic_visit(node)

    def _get_line(self, lineno: int) -> str:
        if 0 < lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].strip()[:60]
        return ""

    def _add(self, line: int, smell_type: str, desc: str, suggestion: str, severity: str = "medium"):
        if smell_type in self.ignore:
            return
        self.issues.append(CodeSmell(
            file=self.filename, line=line, smell_type=smell_type,
            description=desc, suggestion=suggestion, severity=severity,
            code_snippet=self._get_line(line)
        ))

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._check_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._check_function(node)
        self.generic_visit(node)

    def _check_function(self, node):
        # Mutable defaults
        for default in node.args.defaults + node.args.kw_defaults:
            if default is None:
                continue
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self._add(node.lineno, "mutable_default",
                    f"Mutable default argument in {node.name}",
                    "Use None and create inside function", "high")
            elif isinstance(default, ast.Call):
                if isinstance(default.func, ast.Name) and default.func.id in ('list', 'dict', 'set'):
                    self._add(node.lineno, "mutable_default",
                        f"Mutable default from {default.func.id}() in {node.name}",
                        "Use None and create inside function", "high")

        # Long parameter list
        total_params = len(node.args.args) + len(node.args.kwonlyargs)
        if total_params > 6:
            self._add(node.lineno, "long_parameter_list",
                f"{node.name} has {total_params} parameters",
                "Group into dataclass or config object", "medium")

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.type is None:
            self._add(node.lineno, "bare_except",
                "Bare except catches all exceptions including KeyboardInterrupt",
                "Use 'except Exception:' or specific types", "high")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant):
        if (isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
                and id(node) not in self._named_constants
                and node.value not in self.MAGIC_NUMBER_WHITELIST
                and abs(node.value) > 2):
            self._add(node.lineno, "magic_number",
                f"Magic number {node.value}",
                "Extract to named constant", "low")
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare):
        for comparator in node.comparators:
            if isinstance(comparator, ast.Call):
                if isinstance(comparator.func, ast.Name) and comparator.func.id == 'type':
                    self._add(node.lineno, "type_comparison",
                        "Using type() for comparison",
                        "Use isinstance() instead", "medium")
        if isinstance(node.left, ast.Call):
            if isinstance(node.left.func, ast.Name) and node.left.func.id == 'type':
                self._add(node.lineno, "type_comparison",
                    "Using type() for comparison",
                    "Use isinstance() instead", "medium")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        old_class = self.current_class
        self.current_class = node.name
        
        methods = []
        attributes = set()
        
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append(item.name)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        attributes.add(target.id)
        
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == '__init__':
                for stmt in ast.walk(item):
                    if isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, ast.Attribute):
                                if isinstance(target.value, ast.Name) and target.value.id == 'self':
                                    attributes.add(target.attr)
        
        # God class
        non_dunder = [m for m in methods if not m.startswith('_')]
        if len(methods) > 15 and len(attributes) > 10:
            self._add(node.lineno, "god_class",
                f"Class {node.name} has {len(methods)} methods and {len(attributes)} attributes",
                "Split into smaller focused classes", "high")
        
        # Data class — but a class already declared as one (@dataclass,
        # NamedTuple, TypedDict, Enum, pydantic model, ...) is the fix applied,
        # not the smell.
        marker_names = set()
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Name):
                marker_names.add(target.id)
            elif isinstance(target, ast.Attribute):
                marker_names.add(target.attr)
        for base in node.bases:
            if isinstance(base, ast.Name):
                marker_names.add(base.id)
            elif isinstance(base, ast.Attribute):
                marker_names.add(base.attr)
            elif isinstance(base, ast.Subscript) and isinstance(base.value, ast.Name):
                marker_names.add(base.value.id)
        is_declared_data = bool(marker_names & DATA_CLASS_MARKERS)
        if len(attributes) > 3 and len(non_dunder) == 0 and not is_declared_data:
            self._add(node.lineno, "data_class",
                f"Class {node.name} has only data, no behavior methods",
                "Consider using @dataclass or namedtuple", "low")
        
        self.generic_visit(node)
        self.current_class = old_class

    def visit_Call(self, node: ast.Call):
        bool_args = sum(1 for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, bool))
        if bool_args >= 2:
            self._add(node.lineno, "boolean_blindness",
                "Multiple boolean arguments make code hard to read",
                "Use keyword arguments or Enums", "low")
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp):
        if isinstance(node.op, ast.Mod):
            if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                if '%' in node.left.value:
                    self._add(node.lineno, "old_string_format",
                        "Using %-formatting",
                        "Use f-strings instead", "low")
        self.generic_visit(node)


def analyze_file(filepath: Path, ignore: set[str]) -> list[CodeSmell]:
    try:
        source = filepath.read_text(encoding='utf-8', errors='replace')
        tree = ast.parse(source, filename=str(filepath))
        lines = source.splitlines()
        detector = CodeSmellDetector(str(filepath), lines, ignore)
        detector.visit(tree)
        return detector.issues
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect code smells in Python")
    parser.add_argument('path', nargs='?', default='.', help='File or directory')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--ignore', type=str, default='',
        help='Comma-separated smells to ignore')
    
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
            print("✅ No code smells found!")
            return
        
        by_type = defaultdict(int)
        for issue in all_issues:
            by_type[issue.smell_type] += 1
        
        print(f"Found {len(all_issues)} code smell(s):\n")
        print("Summary:")
        for smell, count in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {smell}: {count}")
        print()
        
        severity_icons = SEVERITY_ICONS
        for issue in all_issues:
            icon = severity_icons[issue.severity]
            print(f"{icon} [{issue.severity.upper()}] {issue.file}:{issue.line}")
            print(f"   {issue.smell_type}: {issue.description}")
            if issue.code_snippet:
                print(f"   Code: {issue.code_snippet}")
            print(f"   → {issue.suggestion}\n")


if __name__ == '__main__':
    main()
