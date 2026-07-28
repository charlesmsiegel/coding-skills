#!/usr/bin/env python3
"""
Detect exception-handling hazards in Python code via AST analysis.

Complements the existing detectors: bare `except:` lives in find_code_smells,
and `except: pass` / `except E: pass` live in find_unpythonic. This script
covers the cases those miss.

Finds:
  - raise_without_from    : raising a new exception inside an `except` block
                            without `from`, which discards the original cause
  - unreachable_except    : an `except` clause that can never run because an
                            earlier clause already catches a base class
  - catches_baseexception : `except BaseException` (also traps KeyboardInterrupt,
                            SystemExit, GeneratorExit)
  - assert_for_validation : `assert` used for runtime validation in non-test code
                            (assertions are stripped under `python -O`)
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

# Immediate parent for the common builtin exceptions, used to detect when one
# handler is shadowed by an earlier handler for a base class.
_EXC_PARENT = {
    "Exception": "BaseException",
    "KeyboardInterrupt": "BaseException",
    "SystemExit": "BaseException",
    "GeneratorExit": "BaseException",
    "ArithmeticError": "Exception",
    "ZeroDivisionError": "ArithmeticError",
    "OverflowError": "ArithmeticError",
    "FloatingPointError": "ArithmeticError",
    "LookupError": "Exception",
    "IndexError": "LookupError",
    "KeyError": "LookupError",
    "OSError": "Exception",
    "IOError": "OSError",
    "EnvironmentError": "OSError",
    "FileNotFoundError": "OSError",
    "FileExistsError": "OSError",
    "PermissionError": "OSError",
    "IsADirectoryError": "OSError",
    "NotADirectoryError": "OSError",
    "InterruptedError": "OSError",
    "ProcessLookupError": "OSError",
    "ChildProcessError": "OSError",
    "BlockingIOError": "OSError",
    "ConnectionError": "OSError",
    "BrokenPipeError": "ConnectionError",
    "ConnectionResetError": "ConnectionError",
    "ConnectionAbortedError": "ConnectionError",
    "ConnectionRefusedError": "ConnectionError",
    "TimeoutError": "OSError",
    "ValueError": "Exception",
    "UnicodeError": "ValueError",
    "UnicodeDecodeError": "UnicodeError",
    "UnicodeEncodeError": "UnicodeError",
    "UnicodeTranslateError": "UnicodeError",
    "RuntimeError": "Exception",
    "RecursionError": "RuntimeError",
    "NotImplementedError": "RuntimeError",
    "NameError": "Exception",
    "UnboundLocalError": "NameError",
    "TypeError": "Exception",
    "AttributeError": "Exception",
    "ImportError": "Exception",
    "ModuleNotFoundError": "ImportError",
    "StopIteration": "Exception",
    "StopAsyncIteration": "Exception",
    "AssertionError": "Exception",
    "BufferError": "Exception",
    "EOFError": "Exception",
    "MemoryError": "Exception",
    "ReferenceError": "Exception",
    "SystemError": "Exception",
}


def _exc_ancestors(name):
    """All names `name` is a subclass of (excluding itself), per the known
    builtin hierarchy. Empty set for names we don't recognise."""
    seen = set()
    cur = _EXC_PARENT.get(name)
    while cur and cur not in seen:
        seen.add(cur)
        cur = _EXC_PARENT.get(cur)
    return seen


def _is_subclass_or_same(child, parent):
    return child == parent or parent in _exc_ancestors(child)


def _handler_type_names(handler):
    """Return (names, is_bare): names is the list of exception names this handler
    catches (Name.id / Attribute.attr); is_bare is True for `except:`."""
    if handler.type is None:
        return [], True
    names = []

    def collect(t):
        if isinstance(t, ast.Name):
            names.append(t.id)
        elif isinstance(t, ast.Attribute):
            names.append(t.attr)
        elif isinstance(t, ast.Tuple):
            for elt in t.elts:
                collect(elt)

    collect(handler.type)
    return names, False


def _same_scope_nodes(stmts, stop_types=()):
    """Yield descendants of `stmts` in the same lexical scope (not descending
    into nested functions/lambdas, nor into `stop_types`)."""
    stack = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARIES) or (stop_types and isinstance(child, stop_types)):
                continue
            stack.append(child)


def _looks_like_test(path_str: str) -> bool:
    p = Path(path_str)
    if p.name == "conftest.py":
        return True
    stem = p.stem
    if stem.startswith("test_") or stem.endswith("_test"):
        return True
    parts = {part.lower() for part in p.parts}
    return "tests" in parts or "test" in parts


class ExceptionIssueDetector(ast.NodeVisitor):
    def __init__(self, filename: str, source_lines: list, ignore=None):
        self.filename = filename
        self.source_lines = source_lines
        self.issues = []
        self.ignore = ignore or set()
        self.is_test_file = _looks_like_test(filename)

    def _get_line(self, lineno: int) -> str:
        if 0 < lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].strip()[:80]
        return ""

    def _add(self, line, smell_type, desc, suggestion, severity="medium"):
        if smell_type in self.ignore:
            return
        self.issues.append(CodeSmell(
            file=self.filename, line=line, smell_type=smell_type,
            description=desc, suggestion=suggestion, severity=severity,
            code_snippet=self._get_line(line),
        ))

    # -- unreachable except (ordering across handlers) --------------------
    def visit_Try(self, node: ast.Try):
        caught_names = []        # names caught by earlier handlers
        catch_all_label = None   # label of an earlier catch-all (bare / Exception / BaseException)
        for handler in node.handlers:
            names, is_bare = _handler_type_names(handler)
            if is_bare:
                catch_all_label = catch_all_label or "<bare except>"
                continue  # the bare except itself is reported by find_code_smells
            if catch_all_label is not None:
                self._add(handler.lineno, "unreachable_except",
                    f"This 'except {' / '.join(names)}' can never run; an earlier 'except {catch_all_label}' already catches it",
                    "Put specific exception handlers before the catch-all, or remove the redundant clause.",
                    severity="high")
            else:
                for nm in names:
                    base = next((e for e in caught_names if _is_subclass_or_same(nm, e)), None)
                    if base is not None:
                        self._add(handler.lineno, "unreachable_except",
                            f"'except {nm}' can never run because the earlier 'except {base}' already catches it",
                            "List specific exceptions before their base class, or drop the redundant handler.",
                            severity="high")
                        break
            for nm in names:
                caught_names.append(nm)
                if catch_all_label is None and nm in {"Exception", "BaseException"}:
                    catch_all_label = nm
        self.generic_visit(node)

    # -- helpers for swallowed_exception ----------------------------------
    @staticmethod
    def _is_broad_catch(handler: ast.ExceptHandler) -> bool:
        """True for bare except:, except Exception:, or except BaseException:."""
        if handler.type is None:
            return True
        if isinstance(handler.type, ast.Name):
            return handler.type.id in {"Exception", "BaseException"}
        return False

    @staticmethod
    def _body_has_raise(body) -> bool:
        """True if any node in the handler body (same scope) is a Raise."""
        for n in _same_scope_nodes(body, stop_types=(ast.ExceptHandler,)):
            if isinstance(n, ast.Raise):
                return True
        return False

    _LOG_ATTRS = frozenset({"warning", "warn", "error", "exception", "critical", "fatal"})

    @staticmethod
    def _body_has_log_call(body) -> bool:
        """True if the body contains a call whose attribute name looks like warn/error/etc."""
        for n in _same_scope_nodes(body, stop_types=(ast.ExceptHandler,)):
            if (isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Call)
                    and isinstance(n.value.func, ast.Attribute)
                    and n.value.func.attr in ExceptionIssueDetector._LOG_ATTRS):
                return True
        return False

    @staticmethod
    def _is_silent_body(body) -> bool:
        """True when the body is one of the recognized silent shapes:
          - only `pass`
          - only a single return / return None / continue / break
          - only a bare print(...) optionally followed by one control-flow stmt
        """
        stmts = [s for s in body if not isinstance(s, ast.Pass)]
        # pure pass (or empty)
        if len(stmts) == 0:
            return True

        _ctrl = (ast.Return, ast.Continue, ast.Break)

        def _is_ctrl(s):
            if isinstance(s, ast.Return):
                return s.value is None or (
                    isinstance(s.value, ast.Constant) and s.value.value is None
                )
            return isinstance(s, (ast.Continue, ast.Break))

        # single control-flow statement
        if len(stmts) == 1 and _is_ctrl(stmts[0]):
            return True

        # single bare print(...)
        def _is_bare_print(s):
            return (
                isinstance(s, ast.Expr)
                and isinstance(s.value, ast.Call)
                and isinstance(s.value.func, ast.Name)
                and s.value.func.id == "print"
            )

        if len(stmts) == 1 and _is_bare_print(stmts[0]):
            return True
        if len(stmts) == 2 and _is_bare_print(stmts[0]) and _is_ctrl(stmts[1]):
            return True

        return False

    # -- BaseException + raise-without-from (per handler) -----------------
    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        names, _ = _handler_type_names(node)
        if "BaseException" in names:
            self._add(node.lineno, "catches_baseexception",
                "Catching BaseException also traps KeyboardInterrupt, SystemExit and GeneratorExit",
                "Catch 'Exception' (or a specific subclass) unless you truly need to intercept interpreter-level signals.")

        for n in _same_scope_nodes(node.body, stop_types=(ast.ExceptHandler,)):
            if isinstance(n, ast.Raise) and n.exc is not None and n.cause is None:
                if isinstance(n.exc, ast.Call):
                    self._add(getattr(n, "lineno", node.lineno), "raise_without_from",
                        "Raising a new exception inside 'except' without 'from' discards the original traceback and cause",
                        "Use 'raise NewError(...) from err' to chain the original, or 'from None' to deliberately suppress it.")
                elif isinstance(n.exc, ast.Name) and (node.name is None or n.exc.id != node.name):
                    self._add(getattr(n, "lineno", node.lineno), "raise_without_from",
                        "Raising a different exception inside 'except' without 'from' discards the original cause",
                        "Use 'raise ... from err' to chain the original exception, or 'from None' to suppress it.")

        # ---------------------------------------------------------------- #
        # swallowed_exception — a catch that silently discards the error.
        # This is the single owner of swallow detection (broad and narrow).
        # ---------------------------------------------------------------- #
        if (not self._body_has_raise(node.body)
                and not self._body_has_log_call(node.body)
                and self._is_silent_body(node.body)):
            if self._is_broad_catch(node):
                catch_label = "bare except" if node.type is None else f"except {node.type.id}"
                self._add(node.lineno, "swallowed_exception",
                    f"Broad '{catch_label}:' silently discards the exception without logging or re-raising",
                    "Don't silently swallow — log with context and re-raise, "
                    "or catch a narrow exception type you can actually handle.",
                    "medium")
            elif all(isinstance(s, ast.Pass) for s in node.body):
                # narrow `except X: pass` — less dangerous, but still a silent ignore
                self._add(node.lineno, "swallowed_exception",
                    f"'except {' / '.join(names)}:' silently ignores the exception",
                    "If ignoring is intentional, say so explicitly with "
                    "contextlib.suppress(...) and a comment; otherwise log or handle it.",
                    "low")

        self.generic_visit(node)

    # -- assert used for validation ---------------------------------------
    def visit_Assert(self, node: ast.Assert):
        if not self.is_test_file:
            self._add(node.lineno, "assert_for_validation",
                "assert is removed when Python runs with -O, so it must not be relied on for validation or security checks",
                "Replace with an explicit check that raises, e.g. `if not cond: raise ValueError(...)`.")
        self.generic_visit(node)


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
        lines = source.splitlines()
        detector = ExceptionIssueDetector(str(filepath), lines, ignore)
        detector.visit(tree)
        return detector.issues
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
    parser = argparse.ArgumentParser(description="Detect exception-handling hazards in Python")
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
            print("✅ No exception-handling issues found!")
            return

        by_type = defaultdict(int)
        for issue in all_issues:
            by_type[issue.smell_type] += 1

        print(f"Found {len(all_issues)} exception-handling issue(s):\n")
        print("Summary:")
        for smell, count in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {smell}: {count}")
        print()

        severity_icons = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        for issue in all_issues:
            icon = severity_icons[issue.severity]
            print(f"{icon} [{issue.severity.upper()}] {issue.file}:{issue.line}")
            print(f"   {issue.smell_type}: {issue.description}")
            if issue.code_snippet:
                print(f"   Code: {issue.code_snippet}")
            print(f"   → {issue.suggestion}\n")


if __name__ == "__main__":
    main()
