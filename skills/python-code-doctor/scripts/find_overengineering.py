#!/usr/bin/env python3
"""
Detect over-engineering patterns in Python code.
Finds: YAGNI violations, speculative generality, premature abstraction,
       single-implementation interfaces, excessive layering, thin wrappers.
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from find_coupling_issues import _Derivation, _forwarded_to, _method_nodes
from common import (sort_findings, SEVERITY_ICONS, configure_output, find_python_files,
                    warn_detector_error, warn_unparseable)


@dataclass
class OverEngineeringIssue:
    file: str
    line: int
    issue_type: str
    name: str
    description: str
    suggestion: str
    severity: str
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


class ProjectAnalyzer:
    def __init__(self):
        self.classes: dict[str, dict] = {}
        self.abstract_classes: set[str] = set()
        self.implementations: dict[str, list[str]] = defaultdict(list)
        self.factory_classes: list[tuple[str, str, int]] = []
        self.builder_classes: list[tuple[str, str, int]] = []
        self.strategy_classes: list[tuple[str, str, int]] = []
        self.thin_wrappers: list[tuple[str, str, int, str, tuple[str, ...]]] = []
        self.issues: list[OverEngineeringIssue] = []

    def analyze_file(self, filepath: Path):
        try:
            source = filepath.read_text(encoding='utf-8', errors='replace')
            tree = ast.parse(source, filename=str(filepath))
            visitor = ClassCollector(str(filepath), source.splitlines(), self)
            visitor.visit(tree)
        except (SyntaxError, ValueError, OSError) as exc:
            warn_unparseable(filepath, exc)
        except Exception as exc:
            warn_detector_error(filepath, exc)

    def detect_issues(self):
        # Single-implementation abstract classes
        for base, subclasses in self.implementations.items():
            if base in self.abstract_classes and len(subclasses) == 1:
                info = self.classes.get(base, {})
                self.issues.append(OverEngineeringIssue(
                    file=info.get('file', '?'), line=info.get('line', 0),
                    issue_type="single_implementation",
                    name=base,
                    description=f"Abstract class {base} has only one implementation: {subclasses[0]}",
                    suggestion="Merge abstract and implementation or wait until you need abstraction",
                    severity="medium"
                ))
        
        # Unused abstract classes
        for cls in self.abstract_classes:
            if cls not in self.implementations or len(self.implementations[cls]) == 0:
                info = self.classes.get(cls, {})
                self.issues.append(OverEngineeringIssue(
                    file=info.get('file', '?'), line=info.get('line', 0),
                    issue_type="unused_abstraction",
                    name=cls,
                    description=f"Abstract class {cls} has no implementations",
                    suggestion="Remove unused abstraction (YAGNI)",
                    severity="high"
                ))
        
        # Unnecessary factories
        for name, file, line in self.factory_classes:
            self.issues.append(OverEngineeringIssue(
                file=file, line=line,
                issue_type="unnecessary_factory",
                name=name,
                description=f"Factory class {name} may be unnecessary",
                suggestion="Use direct instantiation unless you need runtime polymorphism",
                severity="low"
            ))
        
        # Unnecessary builders
        for name, file, line in self.builder_classes:
            info = self.classes.get(name, {})
            if len(info.get('methods', [])) < 5:
                self.issues.append(OverEngineeringIssue(
                    file=file, line=line,
                    issue_type="unnecessary_builder",
                    name=name,
                    description=f"Builder {name} may be over-engineering for simple object",
                    suggestion="Use dataclass with defaults or simple constructor",
                    severity="low"
                ))
        
        # Thin wrappers
        for name, file, line, wrapped, behavior in self.thin_wrappers:
            self.issues.append(OverEngineeringIssue(
                file=file, line=line,
                issue_type="thin_wrapper",
                name=name,
                description=(f"Class {name} visibly stores only self.{wrapped} and mostly accesses it; "
                             f"transparent forwarding is unproven for methods {', '.join(behavior)}"
                             if behavior else f"Class {name} is a thin wrapper around {wrapped}"),
                suggestion="" if behavior else "Use the wrapped class directly unless abstraction is needed",
                severity="low",
                kind="candidate" if behavior else "finding",
                also_caused_by=(("A single stored object may contain the class's own state; "
                                 "methods can aggregate, validate, transform, or reset it rather "
                                 "than merely delegate. Inspect the named method bodies.",)
                                if behavior else ()),
            ))
        
        # Premature strategy pattern
        for name, file, line in self.strategy_classes:
            self.issues.append(OverEngineeringIssue(
                file=file, line=line,
                issue_type="premature_strategy",
                name=name,
                description=f"Strategy interface {name} may be premature",
                suggestion="Implement strategies when you have multiple, not before",
                severity="low"
            ))


class ClassCollector(ast.NodeVisitor):
    def __init__(self, filename: str, source_lines: list[str], analyzer: ProjectAnalyzer):
        self.filename = filename
        self.source_lines = source_lines
        self.analyzer = analyzer

    def visit_ClassDef(self, node: ast.ClassDef):
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(base.attr)
        
        methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        
        is_abstract = False
        for base in bases:
            if 'ABC' in base or 'Abstract' in base:
                is_abstract = True
        
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in item.decorator_list:
                    if isinstance(decorator, ast.Name) and decorator.id == 'abstractmethod':
                        is_abstract = True
                    elif isinstance(decorator, ast.Attribute) and decorator.attr == 'abstractmethod':
                        is_abstract = True
        
        self.analyzer.classes[node.name] = {
            'bases': bases, 'methods': methods,
            'file': self.filename, 'line': node.lineno, 'is_abstract': is_abstract
        }
        
        if is_abstract:
            self.analyzer.abstract_classes.add(node.name)
        
        for base in bases:
            self.analyzer.implementations[base].append(node.name)
        
        # Factory pattern
        if 'Factory' in node.name or any('create' in m.lower() for m in methods):
            create_methods = [m for m in methods if 'create' in m.lower()]
            if create_methods and len(methods) <= 3:
                self.analyzer.factory_classes.append((node.name, self.filename, node.lineno))
        
        # Builder pattern
        if 'Builder' in node.name:
            self.analyzer.builder_classes.append((node.name, self.filename, node.lineno))
        
        # Strategy pattern
        if is_abstract and len(methods) == 1 and ('Strategy' in node.name or 'Policy' in node.name):
            self.analyzer.strategy_classes.append((node.name, self.filename, node.lineno))
        
        # Thin wrapper detection
        self._check_thin_wrapper(node)
        
        self.generic_visit(node)

    def _check_thin_wrapper(self, node: ast.ClassDef):
        """A class whose only state is the one object it stores in __init__, and
        whose methods mostly reach through to it. Reaching through alone is a
        candidate: a dictionary accumulator can hold its policy in those methods.
        Only bodies and arguments that transparently forward establish a finding.

        Every attribute the class stores on self counts, in any method and by
        any form of assignment (`self.x = `, `self.x: T = `, `self.a, self.b = `,
        `self.n += `). A class that also keeps a key, a cache or a counter of its
        own owns state the wrapped object does not, so it is not a thin wrapper.
        """
        defs = [item for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
        init_method = next((item for item in defs if item.name == '__init__'), None)
        if not init_method:
            return
        stored = _self_stores(defs)
        if len(stored) != 1 or stored != _self_stores([init_method]):
            return
        (wrapped_attr,) = stored
        methods = [item for item in defs if item.name != '__init__']
        delegate_count = 0
        for method in methods:
            bindings = _Derivation(method, "self")
            if any(isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Attribute)
                   and inner.value.attr == wrapped_attr
                   and isinstance(inner.value.value, ast.Name) and inner.value.value.id == "self"
                   and bindings.resolve(inner.value.value) == bindings.receiver_key
                   for inner in _method_nodes(method.body)):
                delegate_count += 1
        if delegate_count >= len(methods) * 0.7 and len(methods) > 1:
            method_names = {method.name for method in defs}
            behavior = tuple(method.name for method in methods
                             if not _transparent_forward(method, wrapped_attr, method_names))
            self.analyzer.thin_wrappers.append((node.name, self.filename, node.lineno, wrapped_attr, behavior))


def _transparent_forward(method, wrapped_attr: str, method_names: set[str]) -> bool:
    """Reuse the forwarding body recognizer, then require unchanged arguments.

    Normalization, reordering, or constants may express an adapter's policy even
    when the entire body is one return statement. Unknown shapes stay candidates.
    """
    if _forwarded_to(method, "self", method_names) != wrapped_attr:
        return False
    value = method.body[-1].value
    call = value.value if isinstance(value, ast.Await) else value
    params = method.args
    positional = [ast.Name(id=arg.arg, ctx=ast.Load()) for arg in [*params.posonlyargs, *params.args][1:]]
    if params.vararg:
        positional.append(ast.Starred(value=ast.Name(id=params.vararg.arg, ctx=ast.Load()), ctx=ast.Load()))
    keywords = [ast.keyword(arg=arg.arg, value=ast.Name(id=arg.arg, ctx=ast.Load())) for arg in params.kwonlyargs]
    if params.kwarg:
        keywords.append(ast.keyword(arg=None, value=ast.Name(id=params.kwarg.arg, ctx=ast.Load())))
    return ([ast.dump(arg) for arg in call.args] == [ast.dump(arg) for arg in positional]
            and [ast.dump(arg) for arg in call.keywords] == [ast.dump(arg) for arg in keywords])


def _self_stores(defs: list) -> set:
    """Attributes stored on each method's own receiver, including captured self.

    Reuse the coupling detector's lexical resolver: a nested method's self is
    another object, while a closure capturing this method's self owns its state.
    """
    stored = set()
    for method in defs:
        bindings = _Derivation(method, "self")
        stored.update(target.attr for target in _method_nodes(method.body)
                      if isinstance(target, ast.Attribute) and isinstance(target.ctx, ast.Store)
                      and isinstance(target.value, ast.Name) and target.value.id == "self"
                      and bindings.resolve(target.value) == bindings.receiver_key)
    return stored


def analyze_tree(path: Path, ignore: set) -> list:
    """Findings for the whole tree, ordered as main() orders them."""
    analyzer = ProjectAnalyzer()
    for filepath in find_python_files(Path(path)):
        analyzer.analyze_file(filepath)
    analyzer.detect_issues()
    findings = [i for i in analyzer.issues if i.issue_type not in ignore]
    sort_findings(findings)
    return findings


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect over-engineering patterns")
    parser.add_argument('path', nargs='?', default='.', help='File or directory')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--ignore', type=str, default='', help='Comma-separated issue types to ignore')

    args = parser.parse_args()
    ignore = set(args.ignore.split(',')) if args.ignore else set()

    analyzer = ProjectAnalyzer()
    for filepath in find_python_files(Path(args.path)):
        analyzer.analyze_file(filepath)
    analyzer.detect_issues()

    issues = [i for i in analyzer.issues if i.issue_type not in ignore]
    sort_findings(issues)

    if args.format == 'json':
        # Flat findings list, like every other detector (stats live in the
        # text report only — aggregators consume findings, not stats).
        print(json.dumps([asdict(i) for i in issues], indent=2))
    else:
        if not issues:
            print("✅ No over-engineering issues found!")
            print(f"\nStats: {len(analyzer.classes)} classes, {len(analyzer.abstract_classes)} abstract")
            return
        
        severity_icons = SEVERITY_ICONS
        by_type = defaultdict(int)
        for issue in issues:
            if issue.kind == "finding":
                by_type[issue.issue_type] += 1
        
        print(f"Found {sum(by_type.values())} over-engineering issue(s):\n")
        print("Summary:")
        for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {t}: {c}")
        print()
        
        for issue in issues:
            icon = severity_icons[issue.severity]
            label = "? [CANDIDATE]" if issue.kind == "candidate" else f"{icon} [{issue.severity.upper()}]"
            print(f"{label} {issue.file}:{issue.line}")
            print(f"   {issue.issue_type}: {issue.name}")
            print(f"   {issue.description}")
            if issue.suggestion:
                print(f"   → {issue.suggestion}")
            for reason in issue.also_caused_by:
                print(f"   ? also caused by: {reason}")
            print()


if __name__ == '__main__':
    main()
