#!/usr/bin/env python3
"""
Detect loops that can be simplified, via AST analysis.

Finds:
  - loop_to_comprehension : a loop whose only job is to append to a list. Not a
    loop that reads the list it builds (`if key not in seen: seen.append(key)`
    de-duplicates, which no comprehension can say), and when the list already
    holds something — assigned non-empty, appended to before the loop, a
    parameter, or filled across an outer loop — the comprehension would clobber
    it, so the advice is `.extend(<generator>)`.
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


def _scope_statements(scope):
    """The statements of ``scope`` itself, not of functions or classes nested in it."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.stmt):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _names(node) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} if node is not None else set()


def _empty_list(value) -> bool:
    return (isinstance(value, ast.List) and not value.elts) or (
        isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
        and value.func.id == "list" and not value.args and not value.keywords)


def _fills(stmt, name) -> bool:
    """True when ``stmt`` adds to list ``name`` (append/extend/insert/+=)."""
    for node in ast.walk(stmt):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("append", "extend", "insert") \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == name:
            return True
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            return True
    return False


class LoopSimplificationDetector(ast.NodeVisitor):
    def __init__(self, filename, lines, ignore):
        self.filename = filename
        self.lines = lines
        self.ignore = ignore
        self.issues = []
        # The innermost scope's statements and how many loops enclose the one
        # being checked, so the advice can tell a fresh list from a running one.
        self._scopes = []
        self._loop_depth = [0]

    def visit_Module(self, node):
        self._scopes.append(node)
        self.generic_visit(node)
        self._scopes.pop()

    def _visit_scope(self, node):
        self._scopes.append(node)
        self._loop_depth.append(0)
        self.generic_visit(node)
        self._loop_depth.pop()
        self._scopes.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _visit_scope

    def _starts_empty(self, loop, name) -> bool:
        """True when ``name`` was last bound to an empty list before ``loop``
        in this scope, nothing filled it since, and no outer loop repeats it."""
        if self._loop_depth[-1]:
            return False
        last_binding, filled_after = None, False
        for stmt in _scope_statements(self._scopes[-1]):
            if stmt is loop or stmt.lineno >= loop.lineno:
                continue
            targets = stmt.targets if isinstance(stmt, ast.Assign) else (
                [stmt.target] if isinstance(stmt, ast.AnnAssign) else [])
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                if last_binding is None or stmt.lineno > last_binding.lineno:
                    last_binding = stmt
        if last_binding is None or not _empty_list(last_binding.value):
            return False
        for stmt in _scope_statements(self._scopes[-1]):
            if last_binding.lineno < stmt.lineno < loop.lineno \
                    and not isinstance(stmt, (ast.For, ast.AsyncFor, ast.While, ast.If, ast.With,
                                              ast.AsyncWith, ast.Try)) and _fills(stmt, name):
                filled_after = True
        return not filled_after

    def _comprehension_advice(self, node, tgt, filtered):
        form = f"[... for ... in ...{' if <cond>' if filtered else ''}]"
        if self._starts_empty(node, tgt):
            kind = "a filtered comprehension" if filtered else "a list comprehension"
            return f"Replace with {kind}: {tgt} = {form}."
        return (f"'{tgt}' already holds items (or gains them across an outer loop), so "
                f"assigning a comprehension would drop them: {tgt}.extend({form[1:-1]}).")

    def _add(self, line, st, desc, sug, sev):
        if st in self.ignore:
            return
        self.issues.append(CodeSmell(self.filename, line, st, desc, sug, sev, _get_line(self.lines, line)))

    def _check_loop(self, node):
        body = node.body

        # loop_to_comprehension: body is a single append, or a single guarded
        # append — unless the loop reads the list it is building (a de-dup, a
        # running window), which a comprehension cannot see while it builds.
        if len(body) == 1:
            stmt = body[0]
            tgt = _append_target(stmt)
            if tgt is not None and tgt not in _names(stmt.value.args[0]) | _names(node.iter):
                self._add(node.lineno, "loop_to_comprehension",
                    f"This loop only appends to '{tgt}'",
                    self._comprehension_advice(node, tgt, filtered=False),
                    "medium")
            elif isinstance(stmt, ast.If) and not stmt.orelse and len(stmt.body) == 1:
                tgt = _append_target(stmt.body[0])
                reads = _names(stmt.test) | _names(node.iter)
                if tgt is not None and tgt not in reads | _names(stmt.body[0].value.args[0]):
                    self._add(node.lineno, "loop_to_comprehension",
                        f"This loop only conditionally appends to '{tgt}'",
                        self._comprehension_advice(node, tgt, filtered=True),
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
        self._loop_depth[-1] += 1
        self.generic_visit(node)
        self._loop_depth[-1] -= 1

    visit_AsyncFor = visit_For

    def visit_While(self, node):
        # only the concat / any-all checks make sense for while loops
        for n in _same_loop_nodes(node.body):
            if isinstance(n, ast.AugAssign) and isinstance(n.op, ast.Add) and isinstance(n.target, ast.Name) and _is_strish(n.value):
                self._add(getattr(n, "lineno", node.lineno), "string_concat_in_loop",
                    f"String '{n.target.id}' is built with += inside a loop (quadratic time)",
                    "Append pieces to a list and ''.join(...) once after the loop.",
                    "medium")
        self._loop_depth[-1] += 1
        self.generic_visit(node)
        self._loop_depth[-1] -= 1


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source, tree = cached_parse(filepath)
        d = LoopSimplificationDetector(str(filepath), source.splitlines(), ignore)
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
    parser = argparse.ArgumentParser(description="Detect loops that can be simplified")
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
            print("✅ No loop simplifications found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} loop simplification(s):\n\nSummary:")
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
