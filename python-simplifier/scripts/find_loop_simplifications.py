#!/usr/bin/env python3
"""
Detect loops that can be simplified, via AST analysis.

Finds:
  - loop_to_comprehension : a loop whose only job is to append to a list
  - string_concat_in_loop : building a string with += inside a loop (O(n^2))
  - manual_any_all        : a loop that sets a flag and breaks, i.e. any()/all()

(loop_to_comprehension / string_concat_in_loop partially overlap Ruff PERF rules;
included so the suite is self-contained.)
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Iterator
from collections import defaultdict


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


def _same_loop_nodes(stmts):
    """Nodes in the same loop body: descend through compound statements but not
    into nested loops or functions."""
    stack = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARIES + (ast.For, ast.AsyncFor, ast.While)):
                continue
            stack.append(child)


def _is_strish(node) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):   # f-string
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"str", "repr", "format"}:
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_strish(node.left) or _is_strish(node.right)
    return False


def _append_target(stmt):
    """If stmt is `name.append(arg)`, return name; else None."""
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        call = stmt.value
        if isinstance(call.func, ast.Attribute) and call.func.attr == "append" and len(call.args) == 1:
            if isinstance(call.func.value, ast.Name):
                return call.func.value.id
    return None


class LoopSimplificationDetector(ast.NodeVisitor):
    def __init__(self, filename, lines, ignore):
        self.filename = filename
        self.lines = lines
        self.ignore = ignore
        self.issues = []

    def _add(self, line, st, desc, sug, sev):
        if st in self.ignore:
            return
        self.issues.append(CodeSmell(self.filename, line, st, desc, sug, sev, _get_line(self.lines, line)))

    def _check_loop(self, node):
        body = node.body

        # loop_to_comprehension: body is a single append, or a single guarded append
        if len(body) == 1:
            stmt = body[0]
            tgt = _append_target(stmt)
            if tgt is not None:
                self._add(node.lineno, "loop_to_comprehension",
                    f"This loop only appends to '{tgt}'",
                    f"Replace with a list comprehension: {tgt} = [... for ... in ...].",
                    "medium")
            elif isinstance(stmt, ast.If) and not stmt.orelse and len(stmt.body) == 1:
                tgt = _append_target(stmt.body[0])
                if tgt is not None:
                    self._add(node.lineno, "loop_to_comprehension",
                        f"This loop only conditionally appends to '{tgt}'",
                        f"Replace with a filtered comprehension: {tgt} = [... for ... in ... if <cond>].",
                        "medium")

        # string_concat_in_loop + manual_any_all (scan same-loop scope)
        for n in _same_loop_nodes(body):
            # s += "..."  or  s = s + "..."
            if isinstance(n, ast.AugAssign) and isinstance(n.op, ast.Add) and isinstance(n.target, ast.Name) and _is_strish(n.value):
                self._add(getattr(n, "lineno", node.lineno), "string_concat_in_loop",
                    f"String '{n.target.id}' is built with += inside a loop (quadratic time)",
                    "Append pieces to a list and ''.join(...) once after the loop.",
                    "medium")
            elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                v = n.value
                if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add) and isinstance(v.left, ast.Name) \
                        and v.left.id == n.targets[0].id and _is_strish(v):
                    self._add(getattr(n, "lineno", node.lineno), "string_concat_in_loop",
                        f"String '{n.targets[0].id}' is built by concatenation inside a loop (quadratic time)",
                        "Append pieces to a list and ''.join(...) once after the loop.",
                        "medium")

        # manual_any_all: an `if cond: flag = True/False; break`
        for stmt in body:
            if isinstance(stmt, ast.If):
                has_break = any(isinstance(s, ast.Break) for s in stmt.body)
                bool_assign = None
                for s in stmt.body:
                    if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name) \
                            and isinstance(s.value, ast.Constant) and isinstance(s.value.value, bool):
                        bool_assign = s.value.value
                if has_break and bool_assign is not None:
                    builtin = "any()" if bool_assign else "all()"
                    self._add(stmt.lineno, "manual_any_all",
                        f"This loop sets a flag and breaks; it is equivalent to {builtin}",
                        f"Replace the loop with {builtin}: e.g. `flag = {builtin[:-2]}(<cond> for x in items)`.",
                        "medium")

    def visit_For(self, node):
        self._check_loop(node)
        self.generic_visit(node)

    def visit_AsyncFor(self, node):
        self._check_loop(node)
        self.generic_visit(node)

    def visit_While(self, node):
        # only the concat / any-all checks make sense for while loops
        for n in _same_loop_nodes(node.body):
            if isinstance(n, ast.AugAssign) and isinstance(n.op, ast.Add) and isinstance(n.target, ast.Name) and _is_strish(n.value):
                self._add(getattr(n, "lineno", node.lineno), "string_concat_in_loop",
                    f"String '{n.target.id}' is built with += inside a loop (quadratic time)",
                    "Append pieces to a list and ''.join(...) once after the loop.",
                    "medium")
        self.generic_visit(node)


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
        d = LoopSimplificationDetector(str(filepath), source.splitlines(), ignore)
        d.visit(tree)
        return d.issues
    except (SyntaxError, Exception):
        return []


def find_python_files(path: Path) -> Iterator[Path]:
    if path.is_file() and path.suffix == ".py":
        yield path
    elif path.is_dir():
        for p in path.rglob("*.py"):
            if ".venv" not in p.parts and "node_modules" not in p.parts and "__pycache__" not in p.parts:
                yield p


def main():
    parser = argparse.ArgumentParser(description="Detect loops that can be simplified")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))
    all_issues.sort(key=lambda x: (x.severity != "high", x.severity != "medium", x.file, x.line))

    if args.format == "json":
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No loop simplifications found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} loop simplification(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        for i in all_issues:
            print(f"{icons[i.severity]} [{i.severity.upper()}] {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            print(f"   → {i.suggestion}\n")


if __name__ == "__main__":
    main()
