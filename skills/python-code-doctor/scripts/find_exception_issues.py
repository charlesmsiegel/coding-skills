#!/usr/bin/env python3
"""
Detect exception-handling hazards in Python code via AST analysis.

Complements the existing detectors: bare `except:` lives in find_code_smells.
This script covers the cases it misses, and is the single owner of swallowed
exceptions (find_unpythonic defers to it).

Finds:
  - raise_without_from     : raising a new exception inside an `except` block
                             without `from`. Nothing is lost — implicit chaining
                             keeps the original as `__context__` — but the
                             traceback then reads "During handling of the above
                             exception, another exception occurred" rather than
                             "was the direct cause", so the intent goes unstated
                             (Ruff B904). Low severity; a framework HTTPException
                             is only a candidate, since the framework renders it
                             as a response and never prints the chain.
  - unreachable_except     : an `except` clause that can never run because an
                             earlier clause already catches a base class
  - catches_baseexception  : `except BaseException` that neither re-raises nor
                             hands the exception on — it traps KeyboardInterrupt,
                             SystemExit and GeneratorExit
  - swallowed_exception    : a broad catch (bare / Exception / BaseException) that
                             discards the error without logging or re-raising
  - suppressible_exception : a narrow `try: ... except X: pass`, which is
                             `contextlib.suppress(X)` spelled long-hand (Ruff SIM105)
  - assert_for_validation  : `assert` validating a public function's argument in
                             non-test code (assertions are stripped under `python -O`)

A broad catch that the repo's Ruff gate suppresses *with a stated reason*
(`# noqa: BLE001 -- why`) is a candidate rather than a finding: the reason is the
benign explanation, already written down by whoever adjudicated the line.
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import (sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files,
                    looks_like_test, reasoned_noqa, warn_detector_error, warn_unparseable)


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""
    # A scored defect by default. "candidate" marks a lead the downstream merge
    # must not score: it carries no fix and names, in also_caused_by, the ways
    # a healthy codebase produces the same observation.
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


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
    # The seeds obey the same rule as the children; a `def` written directly in
    # the block is a scope boundary whether or not it is nested in an `if`.
    stack = [s for s in stmts
             if not isinstance(s, _SCOPE_BOUNDARIES) and not (stop_types and isinstance(s, stop_types))]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARIES) or (stop_types and isinstance(child, stop_types)):
                continue
            stack.append(child)


def _callee_leaf(call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_public(name: str) -> bool:
    """No leading underscore, or a dunder (``__init__`` validates a caller's input too)."""
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


def _parameter_names(fn) -> set:
    args = fn.args
    every = list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs)
    every += [a for a in (args.vararg, args.kwarg) if a is not None]
    return {a.arg for a in every} - {"self", "cls"}


def _is_narrowing(test) -> bool:
    """`x is not None`, `isinstance(...)`, or an and/or of those: the shape of a
    type checker's narrowing, which usually restates what the annotations and a
    check upstream already promise. A lead to confirm, not proof of validation."""
    if isinstance(test, ast.BoolOp):
        return all(_is_narrowing(v) for v in test.values)
    if isinstance(test, ast.Compare):
        return (len(test.ops) == 1 and isinstance(test.ops[0], ast.IsNot)
                and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value is None)
    if isinstance(test, ast.Call):
        return isinstance(test.func, ast.Name) and test.func.id == "isinstance"
    return False


class ExceptionIssueDetector(ast.NodeVisitor):
    def __init__(self, filename: str, source_lines: list, ignore=None):
        self.filename = filename
        self.source_lines = source_lines
        self.issues = []
        self.ignore = ignore or set()
        self.is_test_file = looks_like_test(filename)
        # The try statement each handler belongs to, and the innermost function
        # each statement sits in — both are context a single node does not carry.
        self._try_of = {}
        self._functions = []

    def _get_line(self, lineno: int) -> str:
        if 0 < lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].strip()[:80]
        return ""

    def _add(self, line, smell_type, desc, suggestion, severity="medium", also_caused_by=()):
        if smell_type in self.ignore:
            return
        # A record with benign readings is a candidate, and a candidate carries
        # no fix: the caller passes one or the other, never both.
        self.issues.append(CodeSmell(
            file=self.filename, line=line, smell_type=smell_type,
            description=desc, suggestion="" if also_caused_by else suggestion, severity=severity,
            code_snippet=self._get_line(line),
            kind="candidate" if also_caused_by else "finding",
            also_caused_by=tuple(also_caused_by),
        ))

    def visit_FunctionDef(self, node):
        self._functions.append(node)
        self.generic_visit(node)
        self._functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    # -- unreachable except (ordering across handlers) --------------------
    def visit_TryStar(self, node):
        for handler in node.handlers:
            self._try_of[handler] = node
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try):
        for handler in node.handlers:
            self._try_of[handler] = node
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

    # -- helpers for catches_baseexception --------------------------------
    @staticmethod
    def _hands_on(handler: ast.ExceptHandler) -> bool:
        """True when the handler re-raises or passes the exception on.

        `except BaseException: <cleanup>; raise` traps nothing — catching
        BaseException there is *required*, so the cleanup also runs on Ctrl-C and
        on asyncio.CancelledError. Nor does `fut.set_exception(exc)` or
        `errors.append(exc)`: the exception object is delivered to whoever waits
        on it, the shape of the stdlib's own concurrent.futures worker. Ruff's
        BLE001 exempts a re-raise for the same reason. Logging or formatting the
        exception (`log.warning(..., exc)`, `str(exc)`) is not delivery: the
        Ctrl-C still stops here.
        """
        bound = handler.name

        def forwards(call):
            # Passing an error to an arbitrary function may only inspect or
            # discard it. Recognize the explicit delivery shapes instead.
            return (bound is not None and isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in {"set_exception", "append"}
                    and any(isinstance(value, ast.Name) and value.id == bound
                            for value in [*call.args, *(kw.value for kw in call.keywords)]))

        def propagates(body):
            if not body:
                return False
            if any(isinstance(n, (ast.Return, ast.Break, ast.Continue))
                   for n in _same_scope_nodes(body[:-1])):
                return False
            last = body[-1]
            if isinstance(last, ast.Raise):
                return last.exc is None or (bound is not None and isinstance(last.exc, ast.Name)
                                             and last.exc.id == bound)
            if isinstance(last, ast.If):
                return propagates(last.body) and propagates(last.orelse)
            return isinstance(last, (ast.Expr, ast.Return)) and forwards(last.value)

        return propagates(handler.body)

    # -- BaseException + raise-without-from (per handler) -----------------
    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        names, _ = _handler_type_names(node)
        if "BaseException" in names and not self._hands_on(node):
            reasons = ()
            if reasoned_noqa(self.source_lines, node.lineno, "BLE001"):
                reasons = ("the repo's BLE001 lint gate suppresses this catch with a stated reason "
                           "(an interpreter-level signal is meant to be caught here)",)
            self._add(node.lineno, "catches_baseexception",
                "Catching BaseException without re-raising also traps KeyboardInterrupt, SystemExit and GeneratorExit",
                "Catch 'Exception' (or a specific subclass), or re-raise after the cleanup, unless you truly "
                "need to intercept interpreter-level signals.",
                also_caused_by=reasons)

        for n in _same_scope_nodes(node.body, stop_types=(ast.ExceptHandler,)):
            if isinstance(n, ast.Raise) and n.exc is not None and n.cause is None:
                if isinstance(n.exc, ast.Call):
                    leaf = _callee_leaf(n.exc) or ""
                    if leaf.endswith("HTTPException"):
                        # The handler turns it into a response; nobody reads
                        # its traceback, so the missing `from` changes nothing
                        # anyone sees.
                        self._add(getattr(n, "lineno", node.lineno), "raise_without_from",
                            f"'{leaf}' is raised inside 'except' without 'from'",
                            "", severity="low",
                            also_caused_by=(
                                "web-framework handlers render an HTTPException as a response and "
                                "never print its chain, so the implicit __context__ is never read",
                            ))
                    else:
                        self._add(getattr(n, "lineno", node.lineno), "raise_without_from",
                            "Raising a new exception inside 'except' without 'from': the original survives "
                            "only as implicit __context__, so the traceback reads 'During handling of the "
                            "above exception, another exception occurred' instead of 'was the direct cause'",
                            "State the intent: 'raise NewError(...) from err' when the original is the cause, "
                            "or 'from None' to hide it deliberately.",
                            severity="low")
                elif isinstance(n.exc, ast.Name) and (node.name is None or n.exc.id != node.name):
                    self._add(getattr(n, "lineno", node.lineno), "raise_without_from",
                        "Raising a different exception inside 'except' without 'from': the original survives "
                        "only as implicit __context__, and the traceback does not say it was the cause",
                        "State the intent: 'raise ... from err' when the original is the cause, or 'from None' "
                        "to hide it deliberately.",
                        severity="low")

        # ---------------------------------------------------------------- #
        # swallowed_exception — a broad catch that silently discards the
        # error. This is the single owner of swallow detection.
        # ---------------------------------------------------------------- #
        if (not self._body_has_raise(node.body)
                and not self._body_has_log_call(node.body)
                and self._is_silent_body(node.body)):
            if self._is_broad_catch(node):
                catch_label = "bare except" if node.type is None else f"except {node.type.id}"
                # Ruff names a broad `except Exception` BLE001 and a bare
                # `except:` E722; a stated reason on either is the repo's own
                # adjudication of this exact line.
                codes = ("BLE001", "E722") if node.type is None else ("BLE001",)
                reasons = ()
                if any(reasoned_noqa(self.source_lines, node.lineno, code) for code in codes):
                    reasons = (
                        "the repo's lint gate suppresses this broad catch with a stated reason — "
                        "typically a fail-soft boundary whose caller reads the fallback as 'unavailable'",
                    )
                self._add(node.lineno, "swallowed_exception",
                    f"Broad '{catch_label}:' silently discards the exception without logging or re-raising",
                    "Don't silently swallow — log with context and re-raise, "
                    "or catch a narrow exception type you can actually handle.",
                    "medium", also_caused_by=reasons)
            elif all(isinstance(s, ast.Pass) for s in node.body) and self._suppress_rewrites(node):
                # A narrow `except X: pass` is the handling: the error is
                # expected and ignoring it is the decision. What is left to
                # say is how it is spelled — contextlib.suppress says the same
                # thing in one line, so this is an idiom item, not a defect.
                listed = ", ".join(names)
                self._add(node.lineno, "suppressible_exception",
                    f"'try: ... except {' / '.join(names)}: pass' is contextlib.suppress spelled long-hand",
                    f"Use `with contextlib.suppress({listed}):` around the guarded statements.",
                    "low")

        self.generic_visit(node)

    def _suppress_rewrites(self, handler: ast.ExceptHandler) -> bool:
        """True when `with contextlib.suppress(X):` says exactly what the try says.

        Only a plain `try` with this one handler and no `else`/`finally` (Ruff
        SIM105's shape). With another handler, an `else` or a `finally`, or under
        `except*`, suppress is not a faithful one-step rewrite, and a fix that
        does not apply is not a fix.
        """
        owner = self._try_of.get(handler)
        return (isinstance(owner, ast.Try) and len(owner.handlers) == 1
                and not owner.orelse and not owner.finalbody)

    # -- assert used for validation ---------------------------------------
    def visit_Assert(self, node: ast.Assert):
        if not self.is_test_file:
            fn = self._functions[-1] if self._functions else None
            validates_input = (
                fn is not None
                and _is_public(fn.name)
                and not _is_narrowing(node.test)
                and any(isinstance(n, ast.Name) and n.id in _parameter_names(fn)
                        for n in ast.walk(node.test))
            )
            if validates_input:
                self._add(node.lineno, "assert_for_validation",
                    f"'{fn.name}' validates its argument with assert, which is removed when Python runs with -O",
                    "Replace with an explicit check that raises, e.g. `if not cond: raise ValueError(...)`.")
            else:
                self._add(node.lineno, "assert_for_validation",
                    "assert is removed when Python runs with -O; confirm it states an invariant "
                    "rather than validating input",
                    "",
                    also_caused_by=(
                        "an internal invariant or a type checker's narrowing after a real check — the "
                        "condition already holds, so the code behaves the same under -O",
                        "a precondition on a private helper whose every caller is in this codebase",
                        "a self-check in a build, eval or benchmark harness that never runs under -O",
                    ))
        self.generic_visit(node)


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source, tree = cached_parse(filepath)
        lines = source.splitlines()
        detector = ExceptionIssueDetector(str(filepath), lines, ignore)
        detector.visit(tree)
        return detector.issues
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def to_record(issue: "CodeSmell") -> dict:
    """The JSON shape this detector emits, shared with the runner.

    `kind` is always present, so a consumer never has to read an absent key as
    a classification; the reasons tuple serialises as a list.
    """
    record = asdict(issue)
    record["also_caused_by"] = list(record["also_caused_by"])
    return record


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect exception-handling hazards in Python")
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
            print("✅ No exception-handling issues found!")
            return

        by_type = defaultdict(int)
        for issue in all_issues:
            by_type[issue.smell_type] += 1

        proven = sum(1 for i in all_issues if i.kind != "candidate")
        print(f"Found {proven} exception-handling issue(s), {len(all_issues) - proven} candidate(s):\n")
        print("Summary:")
        for smell, count in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {smell}: {count}")
        print()

        severity_icons = SEVERITY_ICONS
        for issue in all_issues:
            # A severity icon on a lead reads as a verdict the evidence does not support.
            marker = ("? [CANDIDATE]" if issue.kind == "candidate"
                      else f"{severity_icons[issue.severity]} [{issue.severity.upper()}]")
            print(f"{marker} {issue.file}:{issue.line}")
            print(f"   {issue.smell_type}: {issue.description}")
            if issue.code_snippet:
                print(f"   Code: {issue.code_snippet}")
            if issue.suggestion:
                print(f"   → {issue.suggestion}")
            for reason in issue.also_caused_by:
                print(f"   ? also caused by: {reason}")
            print()


if __name__ == "__main__":
    main()
