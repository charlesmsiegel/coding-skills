#!/usr/bin/env python3
"""
Detect naming problems in Python code via AST analysis.

Finds:
  - shadows_builtin          : a name that shadows a Python builtin (list, dict,
                               id, type, ...), which hides the builtin and confuses readers
  - non_snake_case_function  : a function/method not named in snake_case
  - non_pascal_case_class    : a class not named in PascalCase. A class nested
                               directly in another class body is exempt: it is an
                               attribute namespace (`vae.config.latents_mean`),
                               named for the API it imitates

(Overlaps Ruff's pep8-naming (N) and flake8-builtins (A) rule sets; included so
the suite is self-contained for repos that don't run those.)
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


# Builtins worth flagging when shadowed (high-traffic / bug-prone ones).
_BUILTINS = {
    "list", "dict", "set", "tuple", "str", "int", "float", "bool", "bytes",
    "type", "id", "input", "file", "object", "property", "super", "open",
    "sum", "min", "max", "map", "filter", "range", "len", "sorted", "reversed",
    "iter", "next", "zip", "enumerate", "all", "any", "format", "hash", "dir",
    "vars", "repr", "hex", "oct", "bin", "abs", "round", "pow", "divmod",
    "chr", "ord", "callable", "getattr", "setattr", "hasattr", "globals",
    "locals", "exec", "eval", "compile", "copyright", "license", "exit", "quit",
}

_SNAKE = re.compile(r"^_{0,2}[a-z][a-z0-9_]*_{0,2}$")

# Names a framework calls, so the override must spell them its way:
# BaseHTTPRequestHandler dispatches `do_GET`, ast.NodeVisitor `visit_ClassDef`,
# logging and SAX their camelCase hooks. Renaming one to
# snake_case would silently stop it being called.
# Test-class lifecycle hooks instead require the imported base identity below.
_FRAMEWORK_HOOK = re.compile(r"^(?:do_[A-Z]+|visit_[A-Z]\w*|depart_[A-Z]\w*)$")
_FRAMEWORK_NAMES = frozenset({
    "setUpModule", "tearDownModule",
    "handleError", "createLock",
    "startDocument", "endDocument", "startElement", "endElement", "startElementNS",
    "endElementNS", "startPrefixMapping", "endPrefixMapping", "ignorableWhitespace",
    "processingInstruction", "setDocumentLocator", "skippedEntity",
})
_PASCAL = re.compile(r"^_?[A-Z][a-zA-Z0-9]*$")


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


_TEST_HOOKS = frozenset({"setUp", "tearDown", "setUpClass", "tearDownClass"})
_TEST_BASES = {
    **{f"{module}.{name}": _TEST_HOOKS
       for module in ("unittest", "unittest.case")
       for name in ("TestCase", "FunctionTestCase")},
    **{f"{module}.IsolatedAsyncioTestCase": _TEST_HOOKS | {"asyncSetUp", "asyncTearDown"}
       for module in ("unittest", "unittest.async_case")},
    **{f"{module}.{name}": _TEST_HOOKS | ({"setUpTestData"} if name == "TestCase" else set())
       for module in ("django.test", "django.test.testcases")
       for name in ("SimpleTestCase", "TransactionTestCase", "TestCase", "LiveServerTestCase")},
}


def _stable_imports(tree):
    """Resolve only unconditional imports with no other bindings or mutations."""
    bindings = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == '*':
                    return {}
                bindings[alias.asname or alias.name.split('.')[0]].append(node)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bindings[node.id].append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings[node.name].append(node)
        elif isinstance(node, ast.arg):
            bindings[node.arg].append(node)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            bindings[node.name].append(node)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bindings[node.rest].append(node)
        elif isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            root = node.value
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if isinstance(root, ast.Name):
                bindings[root.id].append(node)
    imports = {}
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.level:
            continue
        for alias in node.names:
            name = alias.asname or alias.name.split('.')[0]
            if bindings[name] == [node]:
                imports[name] = ((f"{node.module}.{alias.name}" if isinstance(node, ast.ImportFrom)
                                 else alias.name if alias.asname else name), node.lineno)
    return imports


def _imported_name(node, imports, lineno):
    suffix = []
    while isinstance(node, ast.Attribute):
        suffix.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name) and node.id in imports:
        path, imported_at = imports[node.id]
        if imported_at < lineno:
            return '.'.join([path, *reversed(suffix)])
    return None


class NamingDetector(ast.NodeVisitor):
    def __init__(self, filename, lines, ignore):
        self.filename = filename
        self.lines = lines
        self.ignore = ignore
        self.issues = []
        self._reported = set()   # (line, name, type) to avoid duplicate shadow reports
        self._namespace_classes = set()  # id() of classes defined directly in a class body
        self._methods: set[int] = set()   # ids of defs directly in a class body
        self._required_hooks = set()

    def visit_Module(self, node):
        imports = _stable_imports(node)
        for cls in ast.walk(node):
            if isinstance(cls, ast.ClassDef):
                hooks = set()
                for base in cls.bases:
                    hooks.update(_TEST_BASES.get(_imported_name(base, imports, cls.lineno), ()))
                self._required_hooks.update(
                    id(method) for method in cls.body
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name in hooks
                )
        self.generic_visit(node)

    def _add(self, line, st, desc, sug, sev):
        if st in self.ignore:
            return
        self.issues.append(CodeSmell(self.filename, line, st, desc, sug, sev, _get_line(self.lines, line)))

    def _shadow(self, name, line):
        if name in _BUILTINS and (line, name, "shadows_builtin") not in self._reported:
            self._reported.add((line, name, "shadows_builtin"))
            self._add(line, "shadows_builtin",
                f"'{name}' shadows the Python builtin of the same name",
                f"Rename it (e.g. '{name}_' or a more descriptive name) so the builtin stays available and intent is clear.",
                "medium")

    def visit_FunctionDef(self, node):
        self._check_func(node, method=id(node) in self._methods)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._check_func(node, method=id(node) in self._methods)
        self.generic_visit(node)

    def _check_func(self, node, method=False):
        if not _is_dunder(node.name):
            # A method is reached as `obj.list`, never as a bare `list`, so
            # naming one after a builtin hides nothing outside its class body.
            if not method:
                self._shadow(node.name, node.lineno)
            if not _SNAKE.match(node.name) and id(node) not in self._required_hooks and not (
                    node.name in _FRAMEWORK_NAMES or _FRAMEWORK_HOOK.match(node.name)):
                self._add(node.lineno, "non_snake_case_function",
                    f"Function '{node.name}' is not snake_case",
                    "Rename to snake_case (PEP 8), e.g. 'parse_value' not 'parseValue'.",
                    "low")
        # Keyword-only parameters are left out: their name is the caller's
        # spelling, the way the standard library's own `print(file=...)`,
        # `sorted(key=...)` and `subprocess.run(input=...)` are, and renaming
        # one is an API change made for the function body's sake.
        for a in list(getattr(node.args, "posonlyargs", [])) + list(node.args.args):
            if a.arg not in {"self", "cls"}:
                self._shadow(a.arg, node.lineno)

    def visit_ClassDef(self, node):
        self._methods.update(id(item) for item in node.body
                             if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)))
        self._namespace_classes.update(id(item) for item in node.body
                                       if isinstance(item, ast.ClassDef))
        if id(node) not in self._namespace_classes and not _PASCAL.match(node.name):
            self._add(node.lineno, "non_pascal_case_class",
                f"Class '{node.name}' is not PascalCase",
                "Rename to PascalCase (PEP 8), e.g. 'HttpClient' not 'http_client'.",
                "low")
        self.generic_visit(node)

    def visit_Assign(self, node):
        for t in node.targets:
            if isinstance(t, ast.Name) and not _is_dunder(t.id):
                self._shadow(t.id, node.lineno)
        self.generic_visit(node)

    def visit_For(self, node):
        if isinstance(node.target, ast.Name) and not _is_dunder(node.target.id):
            self._shadow(node.target.id, node.lineno)
        self.generic_visit(node)


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source, tree = cached_parse(filepath)
        d = NamingDetector(str(filepath), source.splitlines(), ignore)
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
    parser = argparse.ArgumentParser(description="Detect naming problems")
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
            print("✅ No naming problems found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} naming issue(s):\n\nSummary:")
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
