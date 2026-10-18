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

A method or class attribute named after a builtin is reported only when the class
body itself goes on to read that name: elsewhere it is reached as `self.set` or
`Store.set`, so no function body loses the builtin. A shadowing line carrying a
reasoned `# noqa: A001`/`A002`/`A003` — the code flake8-builtins and ruff use for
that shadow — is a candidate rather than a finding: somebody already weighed it.
"""

import ast
import builtins
import re
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files, warn_detector_error, warn_unparseable, reasoned_noqa


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""
    # "candidate" for a shadow a reasoned noqa already reviewed: it carries no
    # fix and names, in also_caused_by, why the name may be deliberate.
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


# Builtins worth flagging when shadowed (high-traffic / bug-prone ones), held
# to what this interpreter's builtins module actually defines. Python 2's
# `file` sat on this list for years: no Python 3 name is hidden by a parameter
# called `file`, so every `file: UploadFile` was a finding with nothing behind
# it. Intersecting keeps a stale entry from coming back.
_BUILTINS = frozenset({
    "list", "dict", "set", "tuple", "str", "int", "float", "bool", "bytes",
    "type", "id", "input", "object", "property", "super", "open",
    "sum", "min", "max", "map", "filter", "range", "len", "sorted", "reversed",
    "iter", "next", "zip", "enumerate", "all", "any", "format", "hash", "dir",
    "vars", "repr", "hex", "oct", "bin", "abs", "round", "pow", "divmod",
    "chr", "ord", "callable", "getattr", "setattr", "hasattr", "globals",
    "locals", "exec", "eval", "compile", "copyright", "license", "exit", "quit",
}) & frozenset(dir(builtins))

# flake8-builtins' codes, which ruff reuses: a variable or function (A001), an
# argument (A002), a class attribute or method (A003). A reasoned suppression
# counts only for the code that names the shadow being reported.
_VARIABLE, _ARGUMENT, _ATTRIBUTE = "A001", "A002", "A003"

_REVIEWED_REASONS = (
    "the line carries a reviewed `# noqa` with a reason: the name is fixed by something "
    "outside this code — an HTTP path or query parameter, a keyword argument callers pass "
    "by name, an API or protocol it mirrors",
    "the builtin is never needed in this scope, which the reason may already say",
)

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
def _class_scope_loads(stmts) -> set[str]:
    """Names the class body itself reads, in the statements given.

    Only these see a class attribute as a bare name: a method body, a lambda
    body and a nested class body are scopes of their own and skip the class
    namespace. What a `def` or nested `class` statement evaluates *in* the class
    body — decorators, defaults, annotations, bases — does see it.
    """
    loads: set[str] = set()

    def visit(node) -> None:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loads.add(node.id)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            parts = [*node.decorator_list, *args.defaults, *[d for d in args.kw_defaults if d],
                     *[a.annotation for a in (*args.posonlyargs, *args.args, *args.kwonlyargs,
                                              args.vararg, args.kwarg) if a and a.annotation]]
            if node.returns is not None:
                parts.append(node.returns)
        elif isinstance(node, ast.Lambda):
            parts = [*node.args.defaults, *[d for d in node.args.kw_defaults if d]]
        elif isinstance(node, ast.ClassDef):
            parts = [*node.decorator_list, *node.bases, *[k.value for k in node.keywords]]
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            # Only the first iterable is evaluated in the enclosing scope.
            parts = [node.generators[0].iter]
        else:
            parts = list(ast.iter_child_nodes(node))
        for part in parts:
            visit(part)

    for stmt in stmts:
        visit(stmt)
    return loads


class NamingDetector(ast.NodeVisitor):
    def __init__(self, filename, lines, ignore):
        self.filename = filename
        self.lines = lines
        self.ignore = ignore
        self.issues = []
        self._reported = set()   # (line, name, type) to avoid duplicate shadow reports
        self._read_after: dict[int, set[str]] = {}
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

    def _add(self, line, st, desc, sug, sev, kind="finding", also_caused_by=()):
        if st in self.ignore:
            return
        self.issues.append(CodeSmell(self.filename, line, st, desc, sug, sev, _get_line(self.lines, line),
                                     kind, tuple(also_caused_by)))

    def _shadow(self, name, line, code=_VARIABLE):
        if name in _BUILTINS and (line, name, "shadows_builtin") not in self._reported:
            self._reported.add((line, name, "shadows_builtin"))
            if reasoned_noqa(self.lines, line, code):
                self._add(line, "shadows_builtin",
                    f"'{name}' shadows the Python builtin of the same name; a reasoned "
                    f"`# noqa: {code}` marks it as reviewed",
                    "", "medium", kind="candidate", also_caused_by=_REVIEWED_REASONS)
                return
            self._add(line, "shadows_builtin",
                f"'{name}' shadows the Python builtin of the same name",
                f"Rename it (e.g. '{name}_' or a more descriptive name) so the builtin stays available and intent is clear.",
                "medium")

    def _class_binding_hides(self, stmt, name) -> bool:
        """Whether a binding made directly in a class body hides the builtin anywhere.

        It becomes a class attribute, reached as `self.name` or `Cls.name`; the one
        place it is a bare name is the rest of the class body.
        """
        return name in self._read_after.get(id(stmt), ())

    def visit_FunctionDef(self, node):
        self._check_func(node, method=id(node) in self._methods)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._check_func(node, method=id(node) in self._methods)
        self.generic_visit(node)

    def _check_func(self, node, method=False):
        if not _is_dunder(node.name):
            if id(node) not in self._read_after:
                self._shadow(node.name, node.lineno)
            elif self._class_binding_hides(node, node.name):
                self._shadow(node.name, node.lineno, _ATTRIBUTE)
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
                # The argument's own line, which is where a wrapped signature
                # puts both the name and any suppression of it.
                self._shadow(a.arg, a.lineno, _ARGUMENT)

    def visit_ClassDef(self, node):
        self._methods.update(id(item) for item in node.body
                             if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)))
        self._namespace_classes.update(id(item) for item in node.body
                                       if isinstance(item, ast.ClassDef))
        # Walked from the end so each statement's "read after it" is one union,
        # not a re-walk of the rest of the body.
        after: set[str] = set()
        for stmt in reversed(node.body):
            self._read_after[id(stmt)] = after
            after = after | _class_scope_loads([stmt])
        if id(node) not in self._namespace_classes and not _PASCAL.match(node.name):
            self._add(node.lineno, "non_pascal_case_class",
                f"Class '{node.name}' is not PascalCase",
                "Rename to PascalCase (PEP 8), e.g. 'HttpClient' not 'http_client'.",
                "low")
        self.generic_visit(node)

    def visit_Assign(self, node):
        in_class = id(node) in self._read_after
        for t in node.targets:
            if isinstance(t, ast.Name) and not _is_dunder(t.id):
                if not in_class:
                    self._shadow(t.id, node.lineno)
                elif self._class_binding_hides(node, t.id):
                    self._shadow(t.id, node.lineno, _ATTRIBUTE)
        self.generic_visit(node)

    def visit_For(self, node):
        if isinstance(node.target, ast.Name) and not _is_dunder(node.target.id):
            self._shadow(node.target.id, node.lineno)
        self.generic_visit(node)


def to_record(issue: "CodeSmell") -> dict:
    """The JSON shape this detector emits, shared with the runner.

    `kind` is always present, so a consumer never has to read an absent key as
    a classification; the reasons tuple serialises as a list.
    """
    record = asdict(issue)
    record["also_caused_by"] = list(record["also_caused_by"])
    return record


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
        print(json.dumps([to_record(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No naming problems found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        leads = sum(1 for i in all_issues if i.kind == "candidate")
        print(f"Found {len(all_issues) - leads} naming issue(s), {leads} candidate(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = SEVERITY_ICONS
        for i in all_issues:
            marker = ("? [CANDIDATE]" if i.kind == "candidate"
                      else f"{icons[i.severity]} [{i.severity.upper()}]")
            print(f"{marker} {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            # A candidate has no fix to print; what it has is the readings to rule out.
            if i.suggestion:
                print(f"   → {i.suggestion}")
            for reason in i.also_caused_by:
                print(f"   ? also caused by: {reason}")
            print()


if __name__ == "__main__":
    main()
