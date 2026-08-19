#!/usr/bin/env python3
"""
Detect dead code and unused code in Python.
Finds: unused imports, unused variables, unreachable code, unused functions/classes.
"""

import ast
import io
import json
import argparse
import re
import tokenize
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Set
from collections import defaultdict
from common import cached_parse, configure_output, find_python_files, warn_detector_error, warn_unparseable


_NOQA_CODES = re.compile(
    r"#\s*noqa:\s*([A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)",
    re.IGNORECASE,
)
_DJANGO_REGISTRATION_MODULES = {"checks", "signals"}


def _dotted_name(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


# Issues below this confidence are noise by default. Shared with the runner so
# both entry points apply the same floor.
DEFAULT_MIN_CONFIDENCE = 60


@dataclass
class DeadCodeIssue:
    file: str
    line: int
    issue_type: str
    name: str
    description: str
    confidence: int
    # A scored defect by default. Set to "candidate" for a lead the downstream
    # merge must NOT score: cross-module use is invisible to this file-local
    # analysis, so a public module-level name cannot be *proven* dead here. The
    # key is stripped from JSON when None so a finding stays a plain finding.
    kind: str | None = None


class ScopeTracker(ast.NodeVisitor):
    def __init__(
        self,
        filename: str,
        parents: dict[ast.AST, ast.AST],
        comments: dict[int, list[str]],
    ):
        self.filename = filename
        self.issues: list[DeadCodeIssue] = []
        self.imports: dict[str, int] = {}
        self.from_imports: dict[str, int] = {}
        self.used_names: Set[str] = set()
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        self.dunder_all: Set[str] = set()
        self.called_functions: Set[str] = set()
        self.instantiated_classes: Set[str] = set()
        self.parents = parents
        self.comments = comments
        self.import_origins: dict[str, str] = {}
        self.current_app_config_names: set[str] = set()
        self.django_app_config_classes: set[ast.ClassDef] = set()
        self.function_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def _at_module_scope(self, node: ast.AST) -> bool:
        parent = self.parents.get(node)
        while parent is not None and not isinstance(parent, ast.Module):
            if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return False
            parent = self.parents.get(parent)
        return isinstance(parent, ast.Module)

    def _is_test_file(self) -> bool:
        base = Path(self.filename).name
        return base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py"

    @staticmethod
    def _decorator_name(decorator: ast.AST) -> str | None:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute):
            return target.attr
        if isinstance(target, ast.Name):
            return target.id
        return None

    def _is_decorated(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> bool:
        return bool(node.decorator_list)

    def _is_fixture(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        return any(self._decorator_name(d) == "fixture" for d in node.decorator_list)

    @staticmethod
    def _is_test_function_name(name: str) -> bool:
        # pytest collects functions named test* by default.
        return name.startswith("test")

    def _collect_dunder_all(self, node: ast.Assign | ast.AugAssign) -> None:
        if not self._at_module_scope(node):
            return
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            return
        value = node.value
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            for elt in value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    self.dunder_all.add(elt.value)

    def visit_Assign(self, node: ast.Assign):
        self._collect_dunder_all(node)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign):
        self._collect_dunder_all(node)
        self.generic_visit(node)

    def _remember_imports(self, node: ast.Import | ast.ImportFrom) -> None:
        if not self._at_module_scope(node):
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                target = alias.name if alias.asname else alias.name.split(".")[0]
                self.import_origins[local] = target
                self.current_app_config_names.discard(local)
        elif node.module or node.level:
            for alias in node.names:
                if alias.name != "*":
                    local = alias.asname or alias.name
                    self.current_app_config_names.discard(local)
                    if node.level == 0 and node.module:
                        self.import_origins[local] = f"{node.module}.{alias.name}"
                    else:
                        self.import_origins.pop(local, None)

    def _canonical_name(self, node: ast.AST) -> str | None:
        dotted = _dotted_name(node)
        if not dotted:
            return None
        first, separator, rest = dotted.partition(".")
        imported = self.import_origins.get(first)
        if not imported:
            return None
        return imported + (separator + rest if separator else "")

    def _line_suppresses_f401(self, line: int) -> bool:
        for comment in self.comments.get(line, ()):
            match = _NOQA_CODES.search(comment)
            if match is not None:
                codes = {code.strip().upper() for code in match.group(1).split(",")}
                if "F401" in codes:
                    return True
        return False

    def _suppresses_f401(self, node: ast.Import | ast.ImportFrom, alias: ast.alias) -> bool:
        end_line = getattr(node, "end_lineno", node.lineno)
        closing_line_has_alias = any(
            candidate.lineno <= end_line
            <= getattr(candidate, "end_lineno", candidate.lineno)
            for candidate in node.names
        )
        if self._line_suppresses_f401(node.lineno) or (
            not closing_line_has_alias and self._line_suppresses_f401(end_line)
        ):
            return True
        alias_end = getattr(alias, "end_lineno", alias.lineno)
        return any(
            self._line_suppresses_f401(line)
            for line in range(alias.lineno, alias_end + 1)
        )

    def _in_appconfig_ready(self) -> bool:
        if not self.function_stack:
            return False
        method = self.function_stack[-1]
        parent = self.parents.get(method)
        return method.name == "ready" and isinstance(parent, ast.ClassDef) \
            and parent in self.django_app_config_classes

    @staticmethod
    def _is_registration_import(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> bool:
        if isinstance(node, ast.Import):
            module = alias.name
        else:
            module = node.module or alias.name
            if module.rsplit(".", 1)[-1] not in _DJANGO_REGISTRATION_MODULES:
                module = alias.name
        return module.rsplit(".", 1)[-1] in _DJANGO_REGISTRATION_MODULES

    def visit_Import(self, node: ast.Import):
        self._remember_imports(node)
        for alias in node.names:
            if self._suppresses_f401(node, alias) or \
                    (self._in_appconfig_ready() and self._is_registration_import(node, alias)):
                continue
            name = alias.asname if alias.asname else alias.name.split('.')[0]
            self.imports[name] = node.lineno
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        self._remember_imports(node)
        for alias in node.names:
            if alias.name == '*':
                continue
            if self._suppresses_f401(node, alias) or \
                    (self._in_appconfig_ready() and self._is_registration_import(node, alias)):
                continue
            name = alias.asname if alias.asname else alias.name
            self.from_imports[name] = node.lineno
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Load):
            self.used_names.add(node.id)
        elif isinstance(node.ctx, ast.Store) and self._at_module_scope(node):
            self.import_origins.pop(node.id, None)
            self.current_app_config_names.discard(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if isinstance(node.value, ast.Name):
            self.used_names.add(node.value.id)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if node.col_offset == 0:
            self.functions[node.name] = node
        self._check_unreachable(node)
        self._check_unused_params(node)
        self.function_stack.append(node)
        try:
            self.generic_visit(node)
        finally:
            self.function_stack.pop()
        if self._at_module_scope(node):
            self.import_origins.pop(node.name, None)
            self.current_app_config_names.discard(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        if node.col_offset == 0:
            self.functions[node.name] = node
        self._check_unreachable(node)
        self._check_unused_params(node)
        self.function_stack.append(node)
        try:
            self.generic_visit(node)
        finally:
            self.function_stack.pop()
        if self._at_module_scope(node):
            self.import_origins.pop(node.name, None)
            self.current_app_config_names.discard(node.name)

    def visit_ClassDef(self, node: ast.ClassDef):
        if node.col_offset == 0:
            self.classes[node.name] = node
        is_app_config = any(
            self._canonical_name(base) == "django.apps.AppConfig"
            or (isinstance(base, ast.Name) and base.id in self.current_app_config_names)
            for base in node.bases
        )
        if is_app_config:
            self.django_app_config_classes.add(node)
        self.generic_visit(node)
        if self._at_module_scope(node):
            self.import_origins.pop(node.name, None)
            if is_app_config:
                self.current_app_config_names.add(node.name)
            else:
                self.current_app_config_names.discard(node.name)

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name):
            self.called_functions.add(node.func.id)
            self.instantiated_classes.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            self.called_functions.add(node.func.attr)
        self.generic_visit(node)

    def _check_unreachable(self, node):
        for i, stmt in enumerate(node.body):
            if isinstance(stmt, (ast.Return, ast.Raise)):
                if i < len(node.body) - 1:
                    next_stmt = node.body[i + 1]
                    self.issues.append(DeadCodeIssue(
                        file=self.filename, line=next_stmt.lineno,
                        issue_type="unreachable_code", name="statement",
                        description=f"Code after {'return' if isinstance(stmt, ast.Return) else 'raise'} is unreachable",
                        confidence=100
                    ))

    def _check_unused_params(self, node):
        if node.name.startswith('_'):
            return
        # pytest collects test functions and injects fixtures by name; their
        # parameters are the framework's business, not dead code.
        if self._is_fixture(node) or (self._is_test_file() and self._is_test_function_name(node.name)):
            return

        # Only a free, module-level function chooses its own signature, so an
        # unused parameter there is a provable defect. A method (overrides,
        # ABC/Protocol conformance) or a nested callback/stub (its signature is
        # dictated by the caller it is passed to, e.g. monkeypatch targets) must
        # accept parameters it need not use — those are unscored leads.
        kind = None if self._at_module_scope(node) else "candidate"

        # A leading underscore is the conventional mark of a deliberately unused
        # parameter; honoring it keeps the detector aligned with every linter.
        params = set()
        for arg in node.args.args:
            if arg.arg not in ('self', 'cls') and not arg.arg.startswith('_'):
                params.add(arg.arg)
        for arg in node.args.kwonlyargs:
            if not arg.arg.startswith('_'):
                params.add(arg.arg)

        used = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                used.add(child.id)

        for param in params - used:
            self.issues.append(DeadCodeIssue(
                file=self.filename, line=node.lineno,
                issue_type="unused_parameter", name=param,
                description=f"Parameter '{param}' in {node.name}() is never used",
                confidence=80, kind=kind
            ))

    def finalize(self):
        # An unused import IS provable inside one file — unless the name is
        # re-exported via __all__, in which case it is part of the public API.
        for name, line in self.imports.items():
            if name not in self.used_names and name not in self.dunder_all:
                self.issues.append(DeadCodeIssue(
                    file=self.filename, line=line,
                    issue_type="unused_import", name=name,
                    description=f"Import '{name}' is never used",
                    confidence=90
                ))

        for name, line in self.from_imports.items():
            if name not in self.used_names and name not in self.dunder_all:
                self.issues.append(DeadCodeIssue(
                    file=self.filename, line=line,
                    issue_type="unused_import", name=name,
                    description=f"Import '{name}' is never used",
                    confidence=90
                ))

        # A public module-level function or class cannot be *proven* dead by
        # reading one file: any other module may import and use it. So these are
        # never scored findings.
        for name, node in self.functions.items():
            if name in self.called_functions or name.startswith('_'):
                continue
            if name in self.dunder_all:
                continue  # re-exported: part of the public API, definitely used
            if self._is_test_file() and (self._is_test_function_name(name) or self._is_fixture(node)):
                continue  # collected/injected by pytest — not dead, not a lead
            if self._is_decorated(node):
                continue  # registered or dispatched by its decorator (route, hook, ...)
            self.issues.append(DeadCodeIssue(
                file=self.filename, line=node.lineno,
                issue_type="unused_function", name=name,
                description=f"Function '{name}' appears unused in this file",
                confidence=60, kind="candidate"
            ))

        for name, node in self.classes.items():
            if name in self.instantiated_classes or name in self.used_names:
                continue
            if name.startswith('_') or name in self.dunder_all:
                continue
            if self._is_test_file() and name.startswith("Test"):
                continue  # pytest test class — collected, not dead
            if self._is_decorated(node):
                continue  # registered/dispatched by its decorator
            self.issues.append(DeadCodeIssue(
                file=self.filename, line=node.lineno,
                issue_type="unused_class", name=name,
                description=f"Class '{name}' appears unused in this file",
                confidence=60, kind="candidate"
            ))


class RedundantCodeDetector(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.issues: list[DeadCodeIssue] = []

    def visit_If(self, node: ast.If):
        if isinstance(node.test, ast.Constant):
            if node.test.value is True:
                self.issues.append(DeadCodeIssue(
                    file=self.filename, line=node.lineno,
                    issue_type="constant_condition", name="if True",
                    description="Condition is always True",
                    confidence=100
                ))
            elif node.test.value is False:
                self.issues.append(DeadCodeIssue(
                    file=self.filename, line=node.lineno,
                    issue_type="constant_condition", name="if False",
                    description="Condition is always False, code is unreachable",
                    confidence=100
                ))
        
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass) and not node.orelse:
            self.issues.append(DeadCodeIssue(
                file=self.filename, line=node.lineno,
                issue_type="empty_if", name="if ... pass",
                description="Empty if block does nothing",
                confidence=90
            ))
        self.generic_visit(node)

    def visit_While(self, node: ast.While):
        if isinstance(node.test, ast.Constant) and node.test.value is False:
            self.issues.append(DeadCodeIssue(
                file=self.filename, line=node.lineno,
                issue_type="dead_loop", name="while False",
                description="Loop will never execute",
                confidence=100
            ))
        self.generic_visit(node)


def analyze_file(filepath: Path, ignore: set[str] = frozenset()) -> list[DeadCodeIssue]:
    try:
        source, tree = cached_parse(filepath)
        comments = defaultdict(list)
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                comments[token.start[0]].append(token.string)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        
        tracker = ScopeTracker(str(filepath), parents, comments)
        tracker.visit(tree)
        tracker.finalize()
        
        redundant = RedundantCodeDetector(str(filepath))
        redundant.visit(tree)
        
        return [i for i in tracker.issues + redundant.issues
                if i.issue_type not in ignore]
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def to_record(issue: "DeadCodeIssue") -> dict:
    """The JSON shape this detector emits. Shared with the runner so a pooled
    run and a `find_dead_code.py <path>` run produce the same records.

    `kind` is dropped when unset rather than serialised as null, and severity is
    derived from confidence, which is what this detector ranks by.
    """
    record = asdict(issue)
    if record.get('kind') is None:
        record.pop('kind', None)
    record['severity'] = (
        'high' if issue.confidence >= 90
        else ('medium' if issue.confidence >= 70 else 'low')
    )
    return record


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect dead and unused code")
    parser.add_argument('path', nargs='?', default='.', help='File or directory')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--min-confidence', type=int, default=DEFAULT_MIN_CONFIDENCE)
    parser.add_argument('--ignore', type=str, default='', help='Comma-separated issue types to ignore')

    args = parser.parse_args()
    ignore = set(args.ignore.split(',')) if args.ignore else set()

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))

    all_issues = [i for i in all_issues if i.confidence >= args.min_confidence]
    all_issues.sort(key=lambda x: (-x.confidence, x.file, x.line))

    if args.format == 'json':
        # severity travels with the finding so standalone output renders the
        # same as analyze_all's aggregation (confidence maps onto severity).
        # `kind` is stripped unless set to "candidate", so a scored finding
        # carries no key and the downstream merge scores it as a defect.
        print(json.dumps([to_record(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No dead code found!")
            return
        
        by_type = defaultdict(int)
        for issue in all_issues:
            by_type[issue.issue_type] += 1
        
        print(f"Found {len(all_issues)} potential dead code issue(s):\n")
        print("Summary:")
        for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {t}: {c}")
        print()
        
        def conf_icon(c):
            return '🔴' if c == 100 else ('🟡' if c >= 80 else '🟢')
        
        for issue in all_issues:
            icon = conf_icon(issue.confidence)
            print(f"{icon} [{issue.confidence}%] {issue.file}:{issue.line}")
            print(f"   {issue.issue_type}: {issue.name}")
            print(f"   {issue.description}\n")


if __name__ == '__main__':
    main()
