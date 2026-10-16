#!/usr/bin/env python3
"""
Analyze Python code complexity using AST.

Detects, per function: control-flow complexity (cyclomatic complexity, cognitive
complexity and nesting depth), function length and parameter count; per class:
class size (code lines and method count).

One defect, one finding. Cyclomatic complexity, cognitive complexity and
nesting depth are three measures of the same thing — how hard a function's
branching is to follow — so a tangled function is reported once, as
`cyclomatic_complexity` when that measure is over its threshold and otherwise as
the first one that is, at the highest severity any of them reached, with every
exceeded measure named in the description. Charging all three made one tangled
function cost three findings. Length stays its own finding: a long, flat
function is a different defect from a short, branchy one. A class's line count
and method count are likewise one size finding.

What each measure counts, and why:
  - every measure belongs to one function. A nested function or class is
    measured on its own, so its branches, nesting and lines are not charged to
    the function that encloses it as well.
  - cyclomatic complexity (McCabe): +1 per if/elif, loop, except clause,
    boolean operand and comprehension `for`. An `assert` in production code is
    a conditional raise and counts; in a test file it states an expectation,
    and a run of them is one straight path, so it does not.
  - cognitive complexity (Sonar-style): +1 plus the current nesting level for
    if, loop, except, ternary, lambda and list/set/dict comprehension; +1 flat
    for each `elif` and `else`; boolean operands as for cyclomatic. Only
    if/elif/else, loop, except, ternary and comprehension bodies nest — `try`
    and `with` are not decisions and add no level.
  - nesting depth: if, loop, with and try blocks. An `elif` is a sibling branch
    of its `if`, not a block inside it, so a ladder is as deep as its deepest
    branch, not as long as the ladder.
  - function length: code lines — blank lines, comment lines and the
    function's own docstring are documentation, not code.
  - parameter count: what a caller must pass. `self`/`cls` is bound by Python,
    and a pytest test or fixture receives fixtures by name from the framework,
    so neither is an API anybody calls.
  - class size: code lines, without docstrings, comments or blank lines.
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from common import count_parameters, framework_hook_overrides, parameter_severity, signature_is_a_contract, sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files, warn_detector_error


@dataclass
class ComplexityIssue:
    file: str
    line: int
    name: str
    issue_type: str
    value: int
    threshold: int
    severity: str
    suggestion: str
    description: str = ""


@dataclass
class Config:
    max_complexity: int = 10
    max_cognitive: int = 15
    max_nesting: int = 4
    max_function_lines: int = 50
    max_params: int = 5
    max_class_methods: int = 20
    max_class_lines: int = 300


# A nested function or class is its own unit of complexity: it is measured when
# the visitor reaches it, so the enclosing function must not count it again.
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)

# Nesting depth's block statements. `elif` is handled where `If` is.
_BLOCKS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)

# The first positional parameter Python binds for a method.
_BOUND_FIRST = frozenset({"self", "cls", "mcs", "mcls", "metacls"})

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}

# Control-flow measures in reporting order: the first that is over its
# threshold names a function's one control-flow finding.
_CONTROL_FLOW = ("cyclomatic_complexity", "cognitive_complexity", "nesting_depth")
_LABELS = {
    "cyclomatic_complexity": "cyclomatic complexity {value}",
    "cognitive_complexity": "cognitive complexity {value}",
    "nesting_depth": "nesting depth {value}",
    "class_size": "{value} code lines",
    "class_methods": "{value} methods",
}
_SUGGESTIONS = {
    "cyclomatic_complexity": "Extract methods or simplify conditionals",
    "cognitive_complexity": "Reduce nesting and boolean operations",
    "nesting_depth": "Use early returns or extract nested logic",
    "class_size": "Extract responsibilities into separate classes",
    "class_methods": "Split into smaller focused classes",
}


def _looks_like_test(path: Path) -> bool:
    """A test module or test scaffolding, judged by its path alone."""
    if path.name == "conftest.py":
        return True
    if path.stem.startswith("test_") or path.stem.endswith("_test"):
        return True
    return bool({part.lower() for part in path.parts} & {"tests", "test"})


def _decorator_name(decorator: ast.AST) -> str | None:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return None


def _own_nodes(node: ast.AST):
    """Every node of ``node`` except the insides of nested functions and classes."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, _SCOPES):
            continue
        yield child
        stack.extend(ast.iter_child_nodes(child))


def _is_elif(parent: ast.If, orelse: list[ast.stmt]) -> bool:
    """True when ``orelse`` is written as `elif`, not as `else:` holding an `if`.

    The two parse to the same tree; only the column tells them apart, and only
    the second is a block inside a block.
    """
    return (len(orelse) == 1 and isinstance(orelse[0], ast.If)
            and orelse[0].col_offset == parent.col_offset)


def _docstring_lines(node: ast.AST) -> set[int]:
    """The line numbers of ``node``'s own docstring, if it has one."""
    body = getattr(node, "body", None)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return set(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
    return set()


class ComplexityVisitor(ast.NodeVisitor):
    """AST visitor that calculates complexity metrics."""

    def __init__(self, filename: str, source_lines: list[str], config: Config,
                 ignore: set[str] = frozenset()):
        self.filename = filename
        self.source_lines = source_lines
        self.config = config
        self.ignore = ignore
        self.issues: list[ComplexityIssue] = []
        self.current_class = None
        self.is_test_file = _looks_like_test(Path(filename))
        # Functions written directly in a class body: the ones Python binds a
        # first argument for.
        self._methods: set[int] = set()
        self._hooks: frozenset = frozenset()

    def visit_Module(self, node: ast.Module):
        self._hooks = framework_hook_overrides(node)
        self.generic_visit(node)

    def _severity(self, value: int, threshold: int) -> str:
        ratio = value / threshold
        if ratio >= 2.0:
            return "high"
        elif ratio >= 1.5:
            return "medium"
        return "low"

    # ----------------------------------------------------------------- #
    # Measures
    # ----------------------------------------------------------------- #

    def _cyclomatic(self, node: ast.AST) -> int:
        """Calculate cyclomatic complexity (McCabe)."""
        complexity = 1
        for child in _own_nodes(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
            elif isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                complexity += sum(1 for _ in child.generators)
            elif isinstance(child, ast.Assert) and not self.is_test_file:
                complexity += 1
        return complexity

    def _cognitive(self, node: ast.AST, nesting: int = 0) -> int:
        """Calculate cognitive complexity (Sonar-style) of ``node``'s children."""
        return sum(self._cognitive_of(child, nesting) for child in ast.iter_child_nodes(node))

    def _cognitive_all(self, nodes, nesting: int) -> int:
        return sum(self._cognitive_of(child, nesting) for child in nodes)

    def _cognitive_of(self, node: ast.AST, nesting: int) -> int:
        """Cognitive complexity of one node sitting at ``nesting``."""
        if isinstance(node, _SCOPES):
            return 0
        if isinstance(node, ast.If):
            # The `if` costs its nesting; each `elif` and the `else` cost one
            # flat, and every branch body sits one level in — the ladder is a
            # row of siblings, not a staircase.
            total = 1 + nesting + self._cognitive_of(node.test, nesting + 1)
            total += self._cognitive_all(node.body, nesting + 1)
            current = node
            while current.orelse:
                if _is_elif(current, current.orelse):
                    current = current.orelse[0]
                    total += 1 + self._cognitive_of(current.test, nesting + 1)
                    total += self._cognitive_all(current.body, nesting + 1)
                    continue
                total += 1 + self._cognitive_all(current.orelse, nesting + 1)
                break
            return total
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            return 1 + nesting + self._cognitive(node, nesting + 1)
        if isinstance(node, ast.ExceptHandler):
            return 1 + nesting + self._cognitive(node, nesting + 1)
        if isinstance(node, ast.IfExp):
            return 1 + nesting + self._cognitive(node, nesting + 1)
        if isinstance(node, ast.BoolOp):
            return len(node.values) - 1 + self._cognitive(node, nesting)
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp)):
            return 1 + nesting + self._cognitive(node, nesting)
        # try, with and everything else: no decision, no new level.
        return self._cognitive(node, nesting)

    def _max_nesting(self, node: ast.AST, depth: int = 0) -> int:
        """Find maximum nesting depth of ``node``'s children."""
        return max([depth] + [self._nesting_of(child, depth)
                              for child in ast.iter_child_nodes(node)])

    def _nesting_of(self, node: ast.AST, depth: int) -> int:
        if isinstance(node, _SCOPES) or isinstance(node, ast.Lambda):
            return depth
        if isinstance(node, ast.If):
            inner = depth + 1
            deepest = self._max_nesting_all([node.test, *node.body], inner)
            current = node
            while current.orelse:
                if _is_elif(current, current.orelse):
                    current = current.orelse[0]
                    deepest = max(deepest, self._max_nesting_all([current.test, *current.body], inner))
                    continue
                deepest = max(deepest, self._max_nesting_all(current.orelse, inner))
                break
            return deepest
        if isinstance(node, _BLOCKS):
            return self._max_nesting(node, depth + 1)
        return self._max_nesting(node, depth)

    def _max_nesting_all(self, nodes, depth: int) -> int:
        return max([depth] + [self._nesting_of(child, depth) for child in nodes])

    def _code_lines(self, first: int, last: int, excluded: set[int]) -> int:
        """Non-empty, non-comment lines in [first, last], less ``excluded``."""
        count = 0
        for number in range(first, last + 1):
            if number in excluded or number > len(self.source_lines):
                continue
            line = self.source_lines[number - 1].strip()
            if line and not line.startswith('#'):
                count += 1
        return count

    def _function_lines(self, node) -> int:
        """Count the function's own code lines: no docstring, no nested defs."""
        if not hasattr(node, 'end_lineno') or node.end_lineno is None:
            return 0
        excluded = _docstring_lines(node)
        for child in _own_nodes_and_scopes(node):
            first = min([child.lineno] + [d.lineno for d in child.decorator_list])
            excluded.update(range(first, (child.end_lineno or child.lineno) + 1))
        return self._code_lines(node.lineno, node.end_lineno, excluded)

    def _class_lines(self, node: ast.ClassDef) -> int:
        """Count a class's code lines: its methods are its size, docstrings are not."""
        if not getattr(node, 'end_lineno', None):
            return 0
        excluded: set[int] = set()
        for inner in ast.walk(node):
            if isinstance(inner, _SCOPES):
                excluded |= _docstring_lines(inner)
        return self._code_lines(node.lineno, node.end_lineno, excluded)

    def _parameter_count(self, node) -> int | None:
        """What a caller must pass, or None when no caller passes anything.

        A pytest test or fixture is called by the framework with fixtures it
        resolves by name, so its parameter list is a dependency declaration,
        not an API.
        """
        if any(_decorator_name(d) == "fixture" for d in node.decorator_list):
            return None
        if self.is_test_file and node.name.startswith("test"):
            return None
        args = node.args
        positional = [*args.posonlyargs, *args.args]
        is_static = any(_decorator_name(d) == "staticmethod" for d in node.decorator_list)
        if id(node) in self._methods and not is_static and positional \
                and positional[0].arg in _BOUND_FIRST:
            positional = positional[1:]
        return len(positional) + len(args.kwonlyargs) + bool(args.vararg) + bool(args.kwarg)

    # ----------------------------------------------------------------- #
    # Reporting
    # ----------------------------------------------------------------- #

    def _report_one(self, node, name: str, measured: list[tuple[str, int, int]]):
        """One finding for several measures of the same defect.

        ``measured`` is (issue_type, value, threshold) in reporting order. The
        finding takes the first exceeded measure's type, the highest severity
        any exceeded measure reached, and names every exceeded measure.
        """
        exceeded = [(kind, value, threshold) for kind, value, threshold in measured
                    if value > threshold and kind not in self.ignore]
        if not exceeded:
            return
        kind, value, threshold = exceeded[0]
        severity = max((self._severity(v, t) for _, v, t in exceeded), key=_SEVERITY_RANK.get)
        description = ", ".join(f"{_LABELS[k].format(value=v)} (threshold {t})"
                                for k, v, t in exceeded)
        suggestion = "; ".join(dict.fromkeys(_SUGGESTIONS[k] for k, _, _ in exceeded))
        self.issues.append(ComplexityIssue(
            file=self.filename, line=node.lineno, name=name,
            issue_type=kind, value=value, threshold=threshold,
            severity=severity, suggestion=suggestion, description=description,
        ))

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._analyze_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._analyze_function(node)
        self.generic_visit(node)

    def _analyze_function(self, node):
        name = f"{self.current_class}.{node.name}" if self.current_class else node.name

        self._report_one(node, name, [
            ("cyclomatic_complexity", self._cyclomatic(node), self.config.max_complexity),
            ("cognitive_complexity", self._cognitive(node), self.config.max_cognitive),
            ("nesting_depth", self._max_nesting(node), self.config.max_nesting),
        ])

        # Function length
        lines = self._function_lines(node)
        if lines > self.config.max_function_lines:
            self.issues.append(ComplexityIssue(
                file=self.filename, line=node.lineno, name=name,
                issue_type="function_length", value=lines,
                threshold=self.config.max_function_lines,
                severity=self._severity(lines, self.config.max_function_lines),
                suggestion="Break into smaller functions",
                description=f"{lines} code lines (threshold {self.config.max_function_lines})",
            ))

        # Parameter count
        params = self._parameter_count(node)
        if params is not None and params > self.config.max_params and not signature_is_a_contract(node, self._hooks):
            self.issues.append(ComplexityIssue(
                file=self.filename, line=node.lineno, name=name,
                issue_type="parameter_count", value=params,
                threshold=self.config.max_params,
                severity=self._severity(params, self.config.max_params),
                suggestion="Use a config object or dataclass",
                description=f"{params} parameters (threshold {self.config.max_params})",
            ))

    def visit_ClassDef(self, node: ast.ClassDef):
        old_class = self.current_class
        self.current_class = node.name

        methods = [n for n in node.body if isinstance(n, _FUNCTIONS)]
        self._methods.update(id(m) for m in methods)
        self._report_one(node, node.name, [
            ("class_size", self._class_lines(node), self.config.max_class_lines),
            ("class_methods", len(methods), self.config.max_class_methods),
        ])

        self.generic_visit(node)
        self.current_class = old_class


def _own_nodes_and_scopes(node: ast.AST):
    """The nested functions and classes directly inside ``node``'s own code."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, _SCOPES):
            yield child
            continue
        stack.extend(ast.iter_child_nodes(child))


def analyze_file(filepath: Path, ignore: set[str] = frozenset(),
                 config: Config | None = None) -> list[ComplexityIssue]:
    try:
        source, tree = cached_parse(filepath)
        lines = source.splitlines()
        visitor = ComplexityVisitor(str(filepath), lines, config or Config(), set(ignore))
        visitor.visit(tree)
        return [i for i in visitor.issues if i.issue_type not in ignore]
    except SyntaxError as e:
        return [ComplexityIssue(
            file=str(filepath), line=e.lineno or 0, name="<parse>",
            issue_type="syntax_error", value=0, threshold=0,
            severity="high", suggestion=f"Fix syntax: {e.msg}"
        )]
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Analyze Python code complexity")
    parser.add_argument('path', nargs='?', default='.', help='File or directory')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--max-complexity', type=int, default=10)
    parser.add_argument('--max-nesting', type=int, default=4)
    parser.add_argument('--max-function-lines', type=int, default=50)
    parser.add_argument('--max-params', type=int, default=5)
    parser.add_argument('--ignore', type=str, default='', help='Comma-separated issue types to ignore')

    args = parser.parse_args()
    ignore = set(args.ignore.split(',')) if args.ignore else set()

    config = Config(
        max_complexity=args.max_complexity,
        max_nesting=args.max_nesting,
        max_function_lines=args.max_function_lines,
        max_params=args.max_params
    )

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore, config))

    sort_findings(all_issues)

    if args.format == 'json':
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No complexity issues found!")
            return

        severity_icons = SEVERITY_ICONS
        print(f"Found {len(all_issues)} complexity issue(s):\n")
        for issue in all_issues:
            icon = severity_icons[issue.severity]
            print(f"{icon} [{issue.severity.upper()}] {issue.file}:{issue.line}")
            print(f"   {issue.name}: {issue.issue_type} = {issue.value} (threshold: {issue.threshold})")
            if issue.description:
                print(f"   {issue.description}")
            print(f"   → {issue.suggestion}\n")


if __name__ == '__main__':
    main()
