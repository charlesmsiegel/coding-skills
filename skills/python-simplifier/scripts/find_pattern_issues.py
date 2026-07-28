#!/usr/bin/env python3
"""
Detect design-pattern issues in both directions via AST analysis.

Direction 1 — pattern machinery Python makes unnecessary (the common case):
  handrolled_singleton       - __new__/accessor/metaclass instance-caching (use a module-level object)
  borg_shared_state          - self.__dict__ = shared class attr (Borg/Monostate)
  registry_metaclass         - metaclass that only registers subclasses (use __init_subclass__)
  getter_setter_pair         - Java-style get_x()/set_x() around a plain attribute
  handrolled_lazy_property   - if self._x is None: compute (use functools.cached_property)
  handrolled_memoize         - module-level dict used as a call cache (use functools.lru_cache)
  iterator_class             - __iter__/__next__ class a generator function replaces
  fluent_builder             - chained set_x()-returning-self builder (use keyword arguments)
  stateless_strategy_classes - sibling one-method classes with no state (use plain functions)
  finalizer_del              - __del__ for cleanup (use a context manager / weakref.finalize)

Direction 2 — a pattern (or stronger type) is missing where forces demand one:
  string_state_machine       - self.attr compared/assigned across methods as string literals
                               (use an Enum; consider State/dispatch if behavior branches)
  try_finally_close          - try/finally whose only job is .close()/.release()
                               (use with / contextlib.closing)

Complements find_overengineering.py (single-impl ABCs, one-type factories, thin
wrappers) and find_design_smells.py (type_switch → polymorphism/dispatch). Each
finding is a candidate: the judgment call — whether the forces for a pattern are
real — belongs to references/design-patterns.md.
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Iterator
from collections import defaultdict


@dataclass
class PatternIssue:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""
    # Cross-definition findings list every participating definition here, so
    # diff tooling can keep the finding when *any* participant changed even
    # though `line` anchors at one (possibly unchanged) definition.
    related_lines: list[int] = field(default_factory=list)


CLEANUP_METHODS = frozenset({"close", "release", "disconnect", "shutdown", "terminate"})

# Method names that mark a fluent-builder finisher.
BUILD_FINISHERS = frozenset({"build", "get_result", "result", "create", "finish"})

ABSTRACT_MARKER_BASES = frozenset({"ABC", "Protocol"})


def _walk_same_scope(stmts: list[ast.stmt]) -> Iterator[ast.AST]:
    """Walk statements without descending into nested scopes (defs, classes,
    lambdas) — cache handling there belongs to that scope's own finding."""
    stack: list[ast.AST] = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _stmt_lists(stmts: list[ast.stmt]) -> Iterator[list[ast.stmt]]:
    """Yield every statement list in the same scope (bodies of if/loops/try/
    with), so sibling-statement order can be inspected without crossing into
    nested defs/classes."""
    stack: list[list[ast.stmt]] = [stmts]
    while stack:
        lst = stack.pop()
        yield lst
        for s in lst:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for attr in ("body", "orelse", "finalbody"):
                sub = getattr(s, attr, None)
                if sub:
                    stack.append(sub)
            for handler in getattr(s, "handlers", []):
                stack.append(handler.body)


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> set[str]:
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _methods(node: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _is_self_attr(node: ast.AST, attr: str | None = None) -> bool:
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self" and (attr is None or node.attr == attr))


def _is_cls_attr(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "cls")


def _is_super_call_to(node: ast.AST, method: str) -> bool:
    """Match super().<method>(...) or super(X, cls).<method>(...)."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == method
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "super")


class PatternIssueDetector(ast.NodeVisitor):
    def __init__(self, filename: str, source_lines: list[str], tree: ast.Module,
                 ignore: set[str] = None):
        self.filename = filename
        self.source_lines = source_lines
        self.issues: list[PatternIssue] = []
        self.ignore = ignore or set()
        # Module-level names bound to an empty dict — candidate hand-rolled caches.
        self.module_dicts: set[str] = set()
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                value = stmt.value
                if (isinstance(value, ast.Dict) and not value.keys) or (
                        isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                        and value.func.id == "dict" and not value.args and not value.keywords):
                    self.module_dicts.add(stmt.targets[0].id)
        self.class_defs: dict[str, ast.ClassDef] = {
            n.name: n for n in tree.body if isinstance(n, ast.ClassDef)
        }

    def _get_line(self, lineno: int) -> str:
        if 0 < lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].strip()[:60]
        return ""

    def _add(self, line: int, smell_type: str, desc: str, suggestion: str,
             severity: str = "medium", related: list[int] = None):
        if smell_type in self.ignore:
            return
        self.issues.append(PatternIssue(
            file=self.filename, line=line, smell_type=smell_type,
            description=desc, suggestion=suggestion, severity=severity,
            code_snippet=self._get_line(line),
            related_lines=sorted(related) if related else []
        ))

    def check_module(self):
        self._check_stateless_strategy_hierarchies()

    # ----------------------------------------------------------------- #
    # Classes
    # ----------------------------------------------------------------- #

    def visit_ClassDef(self, node: ast.ClassDef):
        is_metaclass = any(isinstance(b, ast.Name) and b.id == "type" for b in node.bases)
        if is_metaclass:
            self._check_singleton_metaclass(node)
            self._check_registry_metaclass(node)
        else:
            self._check_singleton_new(node)
            self._check_singleton_accessor(node)
            self._check_borg(node)
            self._check_getter_setter_pairs(node)
            self._check_lazy_properties(node)
            self._check_iterator_class(node)
            self._check_fluent_builder(node)
            self._check_finalizer(node)
            self._check_string_state_machine(node)
        self.generic_visit(node)

    # --- Singletons ---------------------------------------------------- #

    def _check_singleton_new(self, node: ast.ClassDef):
        """__new__ that caches the instance on a class attribute."""

        def is_class_attr(t: ast.AST) -> bool:
            return _is_cls_attr(t) or (isinstance(t, ast.Attribute)
                                       and isinstance(t.value, ast.Name)
                                       and t.value.id == node.name)

        for method in _methods(node):
            if method.name != "__new__":
                continue
            cached_attrs = {
                t.attr
                for inner in _walk_same_scope(method.body) if isinstance(inner, ast.Assign)
                and _is_super_call_to(inner.value, "__new__")
                for t in inner.targets if is_class_attr(t)
            }
            # Recording the latest creation (cls.last = super().__new__(cls);
            # return cls.last) still constructs every call — the singleton
            # signature is a guard reading the stored attribute first.
            reuses = any(
                isinstance(inner, ast.If) and any(
                    is_class_attr(n) and n.attr in cached_attrs
                    for n in ast.walk(inner.test))
                for inner in _walk_same_scope(method.body)
            )
            if cached_attrs and reuses:
                self._add(method.lineno, "handrolled_singleton",
                    f"'{node.name}.__new__' caches the instance on a class attribute "
                    "— a hand-rolled Singleton",
                    "A module is already a singleton: create one instance at module level "
                    "and import it (Global Object pattern). If construction must be lazy, "
                    "expose a module-level @functools.cache function instead. Hidden single "
                    "instances are global state — prefer passing the object in", "medium")
                return

    def _check_singleton_accessor(self, node: ast.ClassDef):
        """classmethod get_instance()-style cached accessor: cls._x = cls(...)."""
        for method in _methods(node):
            if "instance" not in method.name.lower():
                continue
            if "classmethod" not in _decorator_names(method):
                continue
            cached_attrs = {
                t.attr
                for inner in _walk_same_scope(method.body) if isinstance(inner, ast.Assign)
                and isinstance(inner.value, ast.Call)
                and isinstance(inner.value.func, ast.Name) and inner.value.func.id == "cls"
                for t in inner.targets if _is_cls_attr(t)
            }
            if not cached_attrs:
                continue
            # An assignment alone could be a factory recording its latest
            # product — and a post-assignment `return cls._x` still hands out a
            # fresh object per call. The singleton signature is a *guard* that
            # reads the stored attribute before deciding to construct.
            reuses = any(
                isinstance(inner, ast.If) and any(
                    _is_cls_attr(n) and n.attr in cached_attrs
                    for n in ast.walk(inner.test))
                for inner in _walk_same_scope(method.body)
            )
            if reuses:
                self._add(method.lineno, "handrolled_singleton",
                    f"'{node.name}.{method.name}' is a get-instance accessor caching a "
                    "single instance — a hand-rolled Singleton",
                    "Create the instance once at module level and import it (Global Object "
                    "pattern), or use a module-level @functools.cache factory function. "
                    "Callers should receive the object, not fetch it", "medium")
                return

    def _check_singleton_metaclass(self, node: ast.ClassDef):
        """Metaclass whose __call__ caches the result of super().__call__."""
        for method in _methods(node):
            if method.name != "__call__":
                continue
            # The instance must land in persistent storage, and the guard must
            # *read that same storage* to skip construction — an unrelated
            # validation branch beside a recorder assignment is not caching.
            storage = {
                ast.unparse(t.value if isinstance(t, ast.Subscript) else t)
                for inner in _walk_same_scope(method.body) if isinstance(inner, ast.Assign)
                and _is_super_call_to(inner.value, "__call__")
                for t in inner.targets if isinstance(t, (ast.Subscript, ast.Attribute))
            }
            gated = any(
                isinstance(inner, ast.If) and any(
                    isinstance(n, (ast.Attribute, ast.Subscript, ast.Name))
                    and ast.unparse(n) in storage
                    for n in ast.walk(inner.test))
                for inner in _walk_same_scope(method.body)
            )
            if storage and gated:
                self._add(node.lineno, "handrolled_singleton",
                    f"Metaclass '{node.name}' caches instances in __call__ — a Singleton metaclass",
                    "A metaclass is the heaviest possible way to get one instance. Create the "
                    "instance at module level and import it (Global Object pattern)", "medium")
                return

    def _check_borg(self, node: ast.ClassDef):
        """self.__dict__ = <shared> in __init__ (Borg / Monostate)."""
        # Only storage that is actually shared counts: a class-level dict
        # attribute or a module-level dict. `self.__dict__ = state` restoring a
        # per-instance snapshot (deserialization) shares nothing.
        class_dicts = {
            t.id
            for stmt in node.body if isinstance(stmt, ast.Assign)
            and (isinstance(stmt.value, ast.Dict)
                 or (isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Name)
                     and stmt.value.func.id == "dict"))
            for t in stmt.targets if isinstance(t, ast.Name)
        }

        def is_shared(value: ast.expr) -> bool:
            if isinstance(value, ast.Attribute):
                return value.attr in class_dicts and (
                    isinstance(value.value, ast.Name)
                    and value.value.id in ("self", "cls", node.name))
            return isinstance(value, ast.Name) and value.id in self.module_dicts

        for method in _methods(node):
            if method.name != "__init__":
                continue
            for inner in _walk_same_scope(method.body):
                if isinstance(inner, ast.Assign) \
                        and any(_is_self_attr(t, "__dict__") for t in inner.targets) \
                        and is_shared(inner.value):
                    self._add(inner.lineno, "borg_shared_state",
                        f"'{node.name}' shares one __dict__ across instances (Borg/Monostate)",
                        "Shared state without shared identity is still hidden global state. "
                        "A single module-level object is simpler and honest about being one thing",
                        "low")
                    return

    # --- Metaclass that only registers subclasses ----------------------- #

    def _check_registry_metaclass(self, node: ast.ClassDef):
        for method in _methods(node):
            if method.name not in ("__new__", "__init__"):
                continue
            # The registry signature is storing the *newly created class* into a
            # mapping — a metaclass that subscript-assigns other things (EnumType
            # building value maps, say) is doing real metaclass work.
            new_cls_names = {
                t.id
                for inner in _walk_same_scope(method.body) if isinstance(inner, ast.Assign)
                and _is_super_call_to(inner.value, "__new__")
                for t in inner.targets if isinstance(t, ast.Name)
            }
            if method.name == "__init__":
                new_cls_names.add(method.args.args[0].arg if method.args.args else "cls")
            # The mapping must outlive the call: an attribute (mcs.REGISTRY) or
            # a non-local name. A `local = {}` filled during validation and
            # discarded on return is not a registry.
            local_names = {
                t.id
                for inner in _walk_same_scope(method.body)
                if isinstance(inner, (ast.Assign, ast.AnnAssign))
                for t in (inner.targets if isinstance(inner, ast.Assign) else [inner.target])
                if isinstance(t, ast.Name)
            }

            def is_persistent(sub: ast.Subscript) -> bool:
                base = sub.value
                if isinstance(base, ast.Attribute):
                    return True
                return isinstance(base, ast.Name) and base.id not in local_names

            def is_registry_write(stmt: ast.stmt) -> bool:
                return (isinstance(stmt, ast.Assign)
                        and any(isinstance(t, ast.Subscript) and is_persistent(t)
                                for t in stmt.targets)
                        and isinstance(stmt.value, ast.Name)
                        and stmt.value.id in new_cls_names)

            # __init_subclass__ only replaces a metaclass that does *nothing
            # but* register: construction, the registry write, and the return
            # must exhaust the body — validation or namespace rewriting is
            # behavior the swap would silently drop.
            def is_construction_or_return(stmt: ast.stmt) -> bool:
                if isinstance(stmt, ast.Assign) and _is_super_call_to(stmt.value, "__new__"):
                    return True
                if isinstance(stmt, ast.Expr) and _is_super_call_to(stmt.value, "__init__"):
                    return True
                return isinstance(stmt, ast.Return) and (
                    stmt.value is None
                    or (isinstance(stmt.value, ast.Name) and stmt.value.id in new_cls_names)
                    or _is_super_call_to(stmt.value, "__new__"))

            stmts = _strip_docstring(method.body)
            if any(is_registry_write(s) for s in stmts) \
                    and all(is_registry_write(s) or is_construction_or_return(s)
                            for s in stmts):
                self._add(node.lineno, "registry_metaclass",
                    f"Metaclass '{node.name}' writes new classes into a registry",
                    "Use __init_subclass__ on the base class for subclass registration — "
                    "same effect, no metaclass, composes with other bases", "low")
                return

    # --- Java-style accessors ------------------------------------------- #

    def _check_getter_setter_pairs(self, node: ast.ClassDef):
        # name suffix -> (method, backing attribute). The pair only collapses to
        # a plain attribute when both sides touch the *same* attribute — a
        # getter reading self.normalized while the setter writes self.raw is
        # doing real work.
        getters: dict[str, tuple[ast.FunctionDef, str]] = {}
        setters: dict[str, tuple[ast.FunctionDef, str]] = {}
        properties = {m.name for m in _methods(node)
                      if _decorator_names(m) & {"property", "cached_property", "setter"}}
        for method in _methods(node):
            # async accessors are an API boundary (callers await them);
            # swapping in a plain attribute would change every call site.
            if not isinstance(method, ast.FunctionDef) or _decorator_names(method):
                continue
            body = _strip_docstring(method.body)
            args = method.args.posonlyargs + method.args.args
            if method.name.startswith("get_") and len(args) == 1 and len(body) == 1 \
                    and isinstance(body[0], ast.Return) and _is_self_attr(body[0].value):
                getters[method.name[4:]] = (method, body[0].value.attr)
            if method.name.startswith("set_") and len(args) == 2 and len(body) == 1 \
                    and isinstance(body[0], ast.Assign) and len(body[0].targets) == 1 \
                    and _is_self_attr(body[0].targets[0]) \
                    and isinstance(body[0].value, ast.Name) \
                    and body[0].value.id == args[1].arg:
                setters[method.name[4:]] = (method, body[0].targets[0].attr)
        for name in sorted(getters.keys() & setters.keys()):
            if name in properties or getters[name][1] != setters[name][1]:
                continue
            self._add(getters[name][0].lineno, "getter_setter_pair",
                f"'{node.name}' has Java-style accessors get_{name}/set_{name} around a plain attribute",
                "Expose the attribute directly — Python is not Java; @property exists for the "
                "day logic is needed, with no caller changes", "medium",
                related=[getters[name][0].lineno, setters[name][0].lineno])

    # --- Lazy property → functools.cached_property ----------------------- #

    def _check_lazy_properties(self, node: ast.ClassDef):
        # A property with a sibling @x.setter/@x.deleter exposes an assignment
        # API that cached_property does not have — swapping would break it.
        has_accessors = {m.name for m in _methods(node)
                         if _decorator_names(m) & {"setter", "deleter"}}
        for method in _methods(node):
            # cached_property on an async def would cache the coroutine object,
            # which cannot be awaited twice — sync methods only.
            if not isinstance(method, ast.FunctionDef) or method.name in has_accessors:
                continue
            if "property" not in _decorator_names(method):
                continue
            body = _strip_docstring(method.body)
            if len(body) != 2 or not isinstance(body[0], ast.If) or body[0].orelse \
                    or not isinstance(body[1], ast.Return):
                continue
            attr = self._none_check_attr(body[0].test)
            if attr is None or not _is_self_attr(body[1].value, attr):
                continue
            assigns = [s for s in body[0].body if isinstance(s, ast.Assign)]
            if any(any(_is_self_attr(t, attr) for t in a.targets) for a in assigns):
                self._add(method.lineno, "handrolled_lazy_property",
                    f"'{node.name}.{method.name}' is a hand-rolled lazy property "
                    f"(None-check on self.{attr})",
                    "Use @functools.cached_property — one decorator, no sentinel field. "
                    "Verify equivalence first: cached_property caches None (this form "
                    f"retries), and invalidation becomes `del obj.{method.name}` instead "
                    f"of resetting self.{attr}", "low")

    @staticmethod
    def _none_check_attr(test: ast.expr) -> str | None:
        """Return attr name for `self.<attr> is None` / `not hasattr(self, '<attr>')`."""
        if isinstance(test, ast.Compare) and len(test.ops) == 1 \
                and isinstance(test.ops[0], ast.Is) \
                and isinstance(test.comparators[0], ast.Constant) \
                and test.comparators[0].value is None and _is_self_attr(test.left):
            return test.left.attr
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) \
                and isinstance(test.operand, ast.Call) \
                and isinstance(test.operand.func, ast.Name) \
                and test.operand.func.id == "hasattr" and len(test.operand.args) == 2 \
                and isinstance(test.operand.args[0], ast.Name) \
                and test.operand.args[0].id == "self" \
                and isinstance(test.operand.args[1], ast.Constant):
            return test.operand.args[1].value
        return None

    # --- Iterator class → generator -------------------------------------- #

    def _check_iterator_class(self, node: ast.ClassDef):
        methods = {m.name: m for m in _methods(node)}
        if "__next__" not in methods or "__iter__" not in methods:
            return
        # Only iterator-*only* classes collapse to a generator. A cursor or
        # protocol object that also exposes execute()/commit()/close() has a
        # life beyond iteration; rewriting it would not preserve behavior.
        if set(methods) - {"__init__", "__iter__", "__next__"}:
            return
        iter_body = _strip_docstring(methods["__iter__"].body)
        if len(iter_body) == 1 and isinstance(iter_body[0], ast.Return) \
                and isinstance(iter_body[0].value, ast.Name) and iter_body[0].value.id == "self":
            self._add(node.lineno, "iterator_class",
                f"'{node.name}' is a hand-rolled iterator class (__iter__ returning self, "
                "__next__ tracking position)",
                "A generator function (yield) implements the Iterator pattern in a few lines — "
                "the language keeps the position for you. Keep a class only if callers need "
                "to inspect or rewind mid-iteration state", "low")

    # --- Fluent builder → keyword arguments ------------------------------- #

    def _check_fluent_builder(self, node: ast.ClassDef):
        chained = []
        has_finisher = False
        for method in _methods(node):
            if method.name in BUILD_FINISHERS:
                has_finisher = True
            # async setters are awaited scheduling boundaries — kwargs cannot
            # replace them without changing every call site.
            if not isinstance(method, ast.FunctionDef):
                continue
            # Only a *narrow* setter — store one attribute, return self — is
            # builder boilerplate. A method that also validates, does I/O, or
            # calls collaborators would not survive collapsing into kwargs.
            body = _strip_docstring(method.body)
            if len(body) != 2 or not isinstance(body[-1], ast.Return):
                continue
            ret = body[-1].value
            if not (isinstance(ret, ast.Name) and ret.id == "self"):
                continue
            if isinstance(body[0], ast.Assign) and len(body[0].targets) == 1 \
                    and _is_self_attr(body[0].targets[0]):
                chained.append(method)
        if len(chained) >= 3 and has_finisher:
            self._add(node.lineno, "fluent_builder",
                f"'{node.name}' is a fluent Builder: {len(chained)} chained setters plus a "
                "finisher method",
                "Keyword arguments (with defaults) or a dataclass replace most Builders in "
                "Python — construction in one call, validation in __post_init__. Keep a builder "
                "only for genuinely staged construction where partial states are meaningful",
                "low")

    # --- __del__ as destructor -------------------------------------------- #

    def _check_finalizer(self, node: ast.ClassDef):
        for method in _methods(node):
            if method.name == "__del__" and _strip_docstring(method.body):
                # Only a __del__ that performs cleanup is a context-manager
                # candidate; a diagnostic finalizer (warnings.warn(...,
                # ResourceWarning)) deliberately reports leaks without cleaning.
                does_cleanup = any(
                    isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr in CLEANUP_METHODS
                    for inner in _walk_same_scope(method.body)
                )
                if not does_cleanup:
                    continue
                self._add(method.lineno, "finalizer_del",
                    f"'{node.name}.__del__' is used for cleanup",
                    "__del__ runs at unpredictable times (or never, on interpreter exit/cycles). "
                    "Make the class a context manager (__enter__/__exit__) or use "
                    "weakref.finalize so cleanup is deterministic", "medium")
                return

    # --- String-typed state machine ----------------------------------------- #

    def _check_string_state_machine(self, node: ast.ClassDef):
        compare_methods: dict[str, set[str]] = defaultdict(set)
        values: dict[str, set[str]] = defaultdict(set)
        transition_assigns: dict[str, int] = defaultdict(int)
        first_line: dict[str, int] = {}
        involved: dict[str, set[int]] = defaultdict(set)
        for method in _methods(node):
            # Same-scope walk: a nested function's compares/assigns belong to
            # that scope, not to this method's state handling.
            for inner in _walk_same_scope(method.body):
                if isinstance(inner, ast.Compare) and len(inner.ops) == 1 \
                        and isinstance(inner.ops[0], (ast.Eq, ast.NotEq)) \
                        and _is_self_attr(inner.left) \
                        and isinstance(inner.comparators[0], ast.Constant) \
                        and isinstance(inner.comparators[0].value, str):
                    attr = inner.left.attr
                    compare_methods[attr].add(method.name)
                    values[attr].add(inner.comparators[0].value)
                    first_line.setdefault(attr, inner.lineno)
                    involved[attr].add(inner.lineno)
                if isinstance(inner, ast.Assign) and isinstance(inner.value, ast.Constant) \
                        and isinstance(inner.value.value, str):
                    for target in inner.targets:
                        if _is_self_attr(target):
                            values[target.attr].add(inner.value.value)
                            involved[target.attr].add(inner.lineno)
                            if method.name != "__init__":
                                transition_assigns[target.attr] += 1
        for attr, methods in compare_methods.items():
            if len(methods) >= 2 and len(values[attr]) >= 3 and transition_assigns[attr] >= 1:
                self._add(first_line[attr], "string_state_machine",
                    f"'self.{attr}' is a string-typed state machine: {len(values[attr])} "
                    f"states compared in {len(methods)} methods and reassigned at runtime",
                    "At minimum make the states an Enum (typo-proof, exhaustiveness-checkable). "
                    "If behavior branches on the state in several methods, dispatch on it — "
                    "a dict keyed by state or the State pattern — so a new state is an entry, "
                    "not another elif", "medium", related=sorted(involved[attr]))

    # ----------------------------------------------------------------- #
    # Stateless single-method hierarchies (module-level pass)
    # ----------------------------------------------------------------- #

    def _check_stateless_strategy_hierarchies(self):
        by_base: dict[str, list[ast.ClassDef]] = defaultdict(list)
        for cls in self.class_defs.values():
            if len(cls.bases) == 1 and isinstance(cls.bases[0], ast.Name) \
                    and cls.bases[0].id in self.class_defs:
                by_base[cls.bases[0].id].append(cls)
        for base_name, subclasses in by_base.items():
            if len(subclasses) < 2:
                continue
            method_names = set()
            ok = True
            for cls in subclasses:
                # A class decorator (registration, DI, transformation) is
                # class-creation behavior that plain functions would lose.
                if cls.decorator_list:
                    ok = False
                    break
                body = _strip_docstring(cls.body)
                # A decorated method (@classmethod, registration) has calling
                # semantics or side effects a plain function would lose.
                if len(body) != 1 \
                        or not isinstance(body[0], (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        or body[0].name.startswith("_") or body[0].decorator_list:
                    ok = False
                    break
                method_names.add(body[0].name)
            if not ok or len(method_names) != 1:
                continue
            method = next(iter(method_names))
            base = self.class_defs[base_name]
            if base.decorator_list:
                continue
            base_extra = [s for s in _strip_docstring(base.body)
                          if not (isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
                                  and s.name == method)
                          and not isinstance(s, ast.Pass)]
            if base_extra:
                continue
            self._add(base.lineno, "stateless_strategy_classes",
                f"{len(subclasses)} stateless subclasses of '{base_name}' each implement only "
                f"'{method}' — a class hierarchy standing in for functions",
                "Functions are first-class in Python: replace each class with a plain function "
                "and select via a dispatch dict (or pass the callable directly). Keep classes "
                "only when strategies carry configuration or state", "medium",
                related=[base.lineno] + [c.lineno for c in subclasses])


def analyze_file(filepath: Path, ignore: set[str]) -> list[PatternIssue]:
    try:
        source = filepath.read_text(encoding='utf-8', errors='replace')
        tree = ast.parse(source, filename=str(filepath))
        lines = source.splitlines()
        detector = PatternIssueDetector(str(filepath), lines, tree, ignore)
        detector.visit(tree)
        detector.check_module()
        detector.issues.extend(_function_level_checks(filepath, tree, lines, detector))
        return detector.issues
    # Only expected per-file failures are skipped; an unexpected detector bug
    # must crash so the aggregators report the category as not-evaluated.
    except (SyntaxError, ValueError, OSError):
        return []


def _function_level_checks(filepath: Path, tree: ast.Module, lines: list[str],
                           detector: PatternIssueDetector) -> list[PatternIssue]:
    issues: list[PatternIssue] = []

    def get_line(lineno: int) -> str:
        return lines[lineno - 1].strip()[:60] if 0 < lineno <= len(lines) else ""

    def add(line: int, smell_type: str, desc: str, suggestion: str, severity: str):
        if smell_type not in detector.ignore:
            issues.append(PatternIssue(str(filepath), line, smell_type, desc,
                                       suggestion, severity, get_line(line)))

    # handrolled_memoize: function that gates on / reads / writes a module-level dict
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for cache in detector.module_dicts:

                def is_cache_sub(n, cache=cache):
                    return (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                            and n.value.id == cache)

                def gate_test(test, op, cache=cache):
                    return (isinstance(test, ast.Compare) and len(test.ops) == 1
                            and isinstance(test.ops[0], op)
                            and isinstance(test.comparators[0], ast.Name)
                            and test.comparators[0].id == cache)

                # Memoization must *reuse* the stored entry on the hit path:
                # either `if k in CACHE: return CACHE[k]` (hit branch returns
                # without reaching the write), or `if k not in CACHE: CACHE[k]
                # = ...` immediately followed by `return CACHE[k]`. Independent
                # gate/write/return booleans would also match functions that
                # unconditionally recompute. Mutating an entry (accumulator
                # dicts: SESSIONS[k].append(...)) disqualifies.
                hit_reuse = writes = mutates_entry = False
                for stmts in _stmt_lists(node.body):
                    for i, stmt in enumerate(stmts):
                        if not isinstance(stmt, ast.If):
                            continue
                        if gate_test(stmt.test, ast.In) and len(stmt.body) == 1 \
                                and isinstance(stmt.body[0], ast.Return) \
                                and is_cache_sub(stmt.body[0].value):
                            hit_reuse = True
                        if gate_test(stmt.test, ast.NotIn) and not stmt.orelse \
                                and any(isinstance(s, ast.Assign)
                                        and any(is_cache_sub(t) for t in s.targets)
                                        for s in stmt.body) \
                                and i + 1 < len(stmts) \
                                and isinstance(stmts[i + 1], ast.Return) \
                                and is_cache_sub(stmts[i + 1].value):
                            hit_reuse = True
                for inner in _walk_same_scope(node.body):
                    if isinstance(inner, ast.Assign) \
                            and any(is_cache_sub(t) for t in inner.targets):
                        writes = True
                    if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                            and is_cache_sub(inner.func.value)) \
                            or (isinstance(inner, ast.AugAssign) and is_cache_sub(inner.target)):
                        mutates_entry = True
                if hit_reuse and writes and not mutates_entry:
                    add(node.lineno, "handrolled_memoize",
                        f"'{node.name}' hand-rolls memoization through module-level dict '{cache}'",
                        "Use @functools.lru_cache (or @functools.cache) if the function is pure "
                        "and its arguments hashable — it also gives you cache_clear() and stats. "
                        "The module dict is hidden global state", "low")
                    break

        # try_finally_close: a finally whose only job is releasing one resource
        if isinstance(node, ast.Try) and len(node.finalbody) == 1:
            stmt = node.finalbody[0]
            # self.release()-style internal lifecycle management is not a drop-in
            # `with` candidate — only flag cleanup of a separate object.
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) \
                    and isinstance(stmt.value.func, ast.Attribute) \
                    and stmt.value.func.attr in CLEANUP_METHODS \
                    and not stmt.value.args and not stmt.value.keywords \
                    and not (isinstance(stmt.value.func.value, ast.Name)
                             and stmt.value.func.value.id in ("self", "cls")):
                cleanup = stmt.value.func.attr
                receiver = ast.unparse(stmt.value.func.value)[:40]
                # contextlib.closing() only ever calls .close() — suggesting it
                # for .release()/.shutdown() would run the wrong (or no) cleanup.
                fallback = (
                    "wrap it in contextlib.closing()" if cleanup == "close"
                    else f"register the call with contextlib.ExitStack "
                         f"(stack.callback({receiver}.{cleanup})) or write a small context manager"
                )
                add(node.lineno, "try_finally_close",
                    f"try/finally exists only to call .{cleanup}()",
                    f"Use a with block — the object is probably already a context manager; "
                    f"if not, {fallback}. The cleanup then cannot be "
                    "forgotten on the next edit", "low")

    return issues


def find_python_files(path: Path) -> Iterator[Path]:
    if path.is_file() and path.suffix == '.py':
        yield path
    elif path.is_dir():
        for p in path.rglob('*.py'):
            if '.venv' not in p.parts and 'node_modules' not in p.parts and '__pycache__' not in p.parts:
                yield p


def main():
    parser = argparse.ArgumentParser(
        description="Detect design-pattern issues: hand-rolled machinery Python provides, "
                    "and missing patterns where forces demand one")
    parser.add_argument('path', nargs='?', default='.', help='File or directory')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--ignore', type=str, default='',
        help='Comma-separated smell types to ignore')

    args = parser.parse_args()
    ignore = set(args.ignore.split(',')) if args.ignore else set()

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))

    all_issues.sort(key=lambda x: (x.severity != 'high', x.severity != 'medium', x.file, x.line))

    if args.format == 'json':
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No design-pattern issues found!")
            return

        by_type = defaultdict(int)
        for issue in all_issues:
            by_type[issue.smell_type] += 1

        print(f"Found {len(all_issues)} design-pattern issue(s):\n")
        print("Summary:")
        for smell, count in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {smell}: {count}")
        print()

        severity_icons = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}
        for issue in all_issues:
            icon = severity_icons[issue.severity]
            print(f"{icon} [{issue.severity.upper()}] {issue.file}:{issue.line}")
            print(f"   {issue.smell_type}: {issue.description}")
            if issue.code_snippet:
                print(f"   Code: {issue.code_snippet}")
            print(f"   → {issue.suggestion}\n")


if __name__ == '__main__':
    main()
