#!/usr/bin/env python3
"""
Detect return-statement problems in Python code via AST analysis.

Finds:
  - inconsistent_returns : a function that returns a value on some path but says
                           nothing on another — either a bare `return` beside a
                           `return <value>` (Ruff RET502), or a body that can fall
                           off the end and return None implicitly (Ruff RET503)
  - return_bool_condition : `if cond: return True` / `return False`, which is just
                            `return bool(cond)`

An explicit `return None` is never the problem: PEP 8 asks for exactly that ("any
return statements where no value is returned should explicitly state this as
`return None`"), so a function that returns a value or `return None` on every path
is consistent, whatever its annotation says.

(Partially overlaps Ruff's RET / SIM103 rules; included so the suite is
self-contained and emits findings in the unified format.)
"""

import ast
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


_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


def _same_scope_nodes(stmts):
    # The seeds are filtered by the same rule as the children: a `def` written
    # directly in the body is a scope boundary too, and walking into it charged a
    # nested function's returns to the function that encloses it.
    stack = [s for s in stmts if not isinstance(s, _SCOPE_BOUNDARIES)]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARIES):
                continue
            stack.append(child)


def _is_bare_return(r: ast.Return) -> bool:
    """`return` with no expression. `return None` is not bare: it states its value."""
    return r.value is None


def _returns_a_value(r: ast.Return) -> bool:
    """`return <expr>` where the expression is something other than the None literal."""
    return r.value is not None and not (isinstance(r.value, ast.Constant) and r.value.value is None)


# Calls that never return, so a body ending in one cannot fall off the end.
# Matched on the callee's spelling (`sys.exit`, `pytest.fail`, ...), plus any
# function or method in the same module annotated `-> NoReturn` / `-> Never`.
_NORETURN_CALLS = frozenset({
    "exit", "quit", "sys.exit", "os._exit", "os.abort",
    "pytest.fail", "pytest.skip", "pytest.exit", "pytest.xfail",
    "self.fail", "self.skipTest",
    "typing.assert_never", "assert_never", "typing_extensions.assert_never",
})
_NORETURN_ANNOTATIONS = frozenset({"NoReturn", "Never"})


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _noreturn_functions(tree) -> set:
    """Names of this module's functions annotated as never returning."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            leaf = _dotted(node.returns)
            if leaf and leaf.rsplit(".", 1)[-1] in _NORETURN_ANNOTATIONS:
                names.add(node.name)
    return names


def _breaks_out(loop) -> bool:
    """True when a `break` in this loop's own body (not a nested loop's) exits it."""
    stack = list(loop.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Break):
            return True
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARIES + (ast.For, ast.AsyncFor, ast.While)):
                continue
            stack.append(child)
    return False


def _is_true_constant(node) -> bool:
    """`while True:` / `while 1:` — a loop that only a `break` (or a return) leaves."""
    return isinstance(node, ast.Constant) and not isinstance(node.value, (str, bytes)) and bool(node.value)


def _can_fall_off(stmts, noreturn) -> bool:
    """True when control can run past the last statement of ``stmts``.

    Where the shape leaves the answer open it says False (no finding) rather than
    guess: a `match` without a wildcard is trusted to be exhaustive, because mypy
    accepts an exhaustive enum match with no `case _:`, and a call is trusted to
    return unless it is a known never-returning one. A `try` is read through:
    a handler that ends without returning is a path that returns None.
    """
    if not stmts:
        return True
    last = stmts[-1]
    if isinstance(last, (ast.Return, ast.Raise)):
        return False
    if isinstance(last, ast.If):
        return _can_fall_off(last.body, noreturn) or _can_fall_off(last.orelse, noreturn)
    if isinstance(last, (ast.With, ast.AsyncWith)):
        # Read the block's own ending. A suppressing context manager could let a
        # `raise` inside it fall through, but which managers suppress is not
        # visible here, and guessing would charge every `with lock: return x`.
        return _can_fall_off(last.body, noreturn)
    if isinstance(last, (ast.Try, getattr(ast, "TryStar", ast.Try))):
        if last.finalbody and not _can_fall_off(last.finalbody, noreturn):
            return False
        normal = last.orelse if last.orelse else last.body
        if _can_fall_off(normal, noreturn):
            return True
        return any(_can_fall_off(h.body, noreturn) for h in last.handlers)
    if isinstance(last, ast.While):
        if _is_true_constant(last.test):
            return _breaks_out(last)
        return _breaks_out(last) or _can_fall_off(last.orelse, noreturn)
    if isinstance(last, (ast.For, ast.AsyncFor)):
        return _breaks_out(last) or _can_fall_off(last.orelse, noreturn)
    if isinstance(last, ast.Match):
        return any(_can_fall_off(case.body, noreturn) for case in last.cases)
    if isinstance(last, ast.Assert) and isinstance(last.test, ast.Constant) and not last.test.value:
        return False
    if isinstance(last, ast.Expr) and isinstance(last.value, ast.Call):
        callee = _dotted(last.value.func) or ""
        owner, _, leaf = callee.rpartition(".")
        if callee in _NORETURN_CALLS or (owner in ("", "self", "cls") and leaf in noreturn):
            return False
    return True


def _bool_value(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


class ReturnIssueDetector(ast.NodeVisitor):
    def __init__(self, filename, lines, ignore, noreturn=frozenset()):
        self.filename = filename
        self.lines = lines
        self.ignore = ignore
        self.issues = []
        self.noreturn = noreturn

    def _add(self, line, st, desc, sug, sev):
        if st in self.ignore:
            return
        self.issues.append(CodeSmell(self.filename, line, st, desc, sug, sev, _get_line(self.lines, line)))

    def _check_function(self, node):
        scope = list(_same_scope_nodes(node.body))
        # generators legitimately use bare `return`
        if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in scope):
            return
        returns = [n for n in scope if isinstance(n, ast.Return)]
        if not any(_returns_a_value(r) for r in returns):
            return
        # RET503 first: a path that falls off the end is the one that may be a
        # forgotten `return`, which is a wrong answer rather than a spelling.
        if _can_fall_off(node.body, self.noreturn):
            self._add(node.lineno, "inconsistent_returns",
                f"{node.name} returns a value on some paths but can reach the end of its body, "
                "returning None implicitly",
                "End every path with an explicit return: `return None` where 'no result' is meant "
                "(PEP 8), or the value the path forgot.",
                "medium")
        elif any(_is_bare_return(r) for r in returns):
            # RET502: the caller gets None either way, so this is intent rather
            # than behaviour — a bare `return` beside value returns reads as an
            # oversight where `return None` would read as a decision.
            self._add(node.lineno, "inconsistent_returns",
                f"{node.name} mixes 'return <value>' with a bare 'return'",
                "Write `return None` where no value is meant, so every exit states what it returns (PEP 8).",
                "low")

    def visit_FunctionDef(self, node):
        self._check_function(node)
        self._scan_block(node.body)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._check_function(node)
        self._scan_block(node.body)
        self.generic_visit(node)

    def _scan_block(self, stmts):
        """Look for `if cond: return True` followed/elsed by `return False`."""
        for idx, stmt in enumerate(stmts):
            if not isinstance(stmt, ast.If):
                continue
            if len(stmt.body) != 1 or not isinstance(stmt.body[0], ast.Return):
                continue
            b = _bool_value(stmt.body[0].value)
            if b is None:
                continue
            # else-branch form
            other = None
            if len(stmt.orelse) == 1 and isinstance(stmt.orelse[0], ast.Return):
                other = _bool_value(stmt.orelse[0].value)
            # fall-through form: next statement returns the opposite bool
            elif not stmt.orelse and idx + 1 < len(stmts) and isinstance(stmts[idx + 1], ast.Return):
                other = _bool_value(stmts[idx + 1].value)
            if other is not None and other != b:
                self._add(stmt.lineno, "return_bool_condition",
                    "An if/else that returns True/False is just 'return <condition>'",
                    "Replace with `return <condition>` (wrap in bool() if the condition may be non-boolean).",
                    "low")
        # NOTE: nested blocks are reached via generic_visit -> visit_* on inner defs,
        # and via the recursive scan below for compound statements in the same function.
        for stmt in stmts:
            for attr in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, attr, None)
                if isinstance(inner, list) and not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    self._scan_block(inner)
            for handler in getattr(stmt, "handlers", []) or []:
                self._scan_block(handler.body)


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source, tree = cached_parse(filepath)
        d = ReturnIssueDetector(str(filepath), source.splitlines(), ignore,
                                frozenset(_noreturn_functions(tree)))
        d.visit(tree)
        # de-duplicate (recursive scan can revisit a block reached two ways)
        seen, unique = set(), []
        for i in d.issues:
            key = (i.line, i.smell_type)
            if key not in seen:
                seen.add(key)
                unique.append(i)
        return unique
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect return-statement problems")
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
            print("✅ No return-statement problems found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} return-statement issue(s):\n\nSummary:")
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
