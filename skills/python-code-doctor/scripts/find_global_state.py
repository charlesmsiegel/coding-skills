#!/usr/bin/env python3
"""
Detect global-state hazards in Python code via AST analysis.

Module-level mutable state makes code order-dependent, hard to test, and unsafe
under concurrency. This finds the cases that actually bite (mutated shared
globals) rather than every module constant.

Finds:
  - mutated_global : a module-level mutable value (list/dict/set/...) that is
                     mutated at runtime from inside a function. High, unless
                     every detected direct write holds one common, stable
                     module-level threading Lock/RLock. That is a candidate:
                     a process registry or memo has the same shape, and this
                     file cannot establish a lifecycle or reader defect.
  - global_rebind  : a function that uses the `global` statement to reassign
                     module-level variables — one record per function, naming
                     every name it rebinds. A `global` line suppressed with
                     `# noqa: PLW0603 -- <reason>` is a candidate: the repo's
                     gate adjudicated it (an at-fork hook, say, whose return
                     value nobody receives).

A mutation is charged to the module's name only where that name *is* the
module's: a function that binds the same name locally (or inherits it from an
enclosing function) is mutating its own object, whatever it is spelled.
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import Counter, defaultdict
from common import (sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files,
                    reasoned_noqa, warn_detector_error, warn_unparseable)


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
_MUTABLE_BUILTINS = {"list", "dict", "set", "bytearray", "defaultdict",
                     "OrderedDict", "Counter", "deque"}
_MUTATING_METHODS = {
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse",
    "add", "discard", "update", "setdefault", "popitem",
    "appendleft", "popleft", "extendleft", "rotate",
}


def _definition_inputs(node):
    if isinstance(node, ast.ClassDef):
        return node.decorator_list + node.bases + [keyword.value for keyword in node.keywords]
    args = node.args
    params = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    params += [arg for arg in (args.vararg, args.kwarg) if arg]
    return (node.decorator_list + list(args.defaults) + [value for value in args.kw_defaults if value is not None]
            + [arg.annotation for arg in params if arg.annotation is not None]
            + ([node.returns] if node.returns is not None else []))


def _lambda_bound_names(node):
    args = node.args
    params = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    params += [arg for arg in (args.vararg, args.kwarg) if arg]
    names = {arg.arg for arg in params}
    names.update(item.id for item in _module_scope_nodes(ast.Module(body=[node.body], type_ignores=[]))
                 if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store))
    return names


def _lambda_is_generator(node):
    return any(isinstance(item, (ast.Yield, ast.YieldFrom))
               for item in _module_scope_nodes(ast.Module(body=[node.body], type_ignores=[])))


def _eager_children(node):
    """Definition inputs execute here; deferred bodies retain their own walk."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _definition_inputs(node)
    if isinstance(node, ast.Lambda):
        return list(node.args.defaults) + [value for value in node.args.kw_defaults if value is not None]
    if isinstance(node, ast.GeneratorExp):
        return [node.generators[0].iter]
    children = list(ast.iter_child_nodes(node))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Lambda) and not _lambda_is_generator(node.func):
        children.append(node.func.body)
    return children


def _eager_nodes(stmts):
    stack = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(_eager_children(node))


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _is_mutable_value(value) -> bool:
    if isinstance(value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in _MUTABLE_BUILTINS:
        return True
    return False


_LOCK_FACTORIES = {"Lock", "RLock"}


@dataclass(frozen=True)
class _Mutex:
    factory: str
    identity: tuple[int, int]


@dataclass(frozen=True)
class _Factory:
    name: str
    # A direct import captures the attribute's current object. Replacing that
    # module attribute later does not change the saved factory (or its aliases).
    imported_at: tuple[int, int] | None = None


def _same_scope_nodes(stmts):
    # The seeds obey the same rule as the children: a `def` directly in the body
    # is its own scope, and walking into it charged its `global` statements and
    # mutations to the enclosing function as well as to itself.
    stack = [s for s in stmts if not isinstance(s, _SCOPE_BOUNDARIES)]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARIES):
                continue
            stack.append(child)


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


def _module_name(value, names, contexts):
    return (isinstance(value, ast.Name) and value.id in names
            and ("global", value.id) in contexts.get(value, ()))


def _mutates_name(node, names, contexts):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr in _MUTATING_METHODS and _module_name(node.func.value, names, contexts):
            return node.func.value.id
    if isinstance(node, ast.Delete):
        for t in node.targets:
            if isinstance(t, ast.Subscript) and _module_name(t.value, names, contexts):
                return t.value.id
    if isinstance(node, (ast.Assign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if isinstance(t, ast.Subscript) and _module_name(t.value, names, contexts):
                return t.value.id
    return None


def _context_mutex(expression, locks, contexts):
    origins = {origin for origin in contexts.get(expression, ()) if origin[0] != "global"}
    if not origins or any(kind != "guard" or name not in locks for kind, name in origins):
        return None
    possible = {locks[name] for _, name in origins}
    return next(iter(possible)) if len(possible) == 1 else None


def _blocking_acquire(node, locks, contexts):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "acquire"):
        return False
    kwargs = {keyword.arg: keyword.value for keyword in node.keywords}
    blocking = node.args[0] if node.args else kwargs.get("blocking")
    timeout = node.args[1] if len(node.args) > 1 else kwargs.get("timeout")
    if isinstance(blocking, ast.Constant) and blocking.value in (False, 0):
        return False
    if isinstance(timeout, ast.Constant) and isinstance(timeout.value, (int, float)) and timeout.value >= 0:
        return False
    held = {locks[name].identity for kind, name in contexts.get(node, ()) if kind == "guard" and name in locks}
    return any(locks[name].factory == "Lock" and locks[name].identity in held
               for kind, name in contexts.get(node.func.value, ()) if kind == "guard" and name in locks)


def _guarded_nodes(stmts, locks, contexts, invalid_acquires):
    """Same-scope nodes paired with the known module locks held at each node.

    `locks` names the module-level Lock/RLock objects. A nested function is
    its own scope and its own walk: it runs whenever it is called, which need
    not be while the lock that surrounds its definition is held.
    """
    stack = [(s, frozenset()) for s in stmts]
    while stack:
        node, held = stack.pop()
        yield node, held if held is not None else frozenset()
        if isinstance(node, ast.With):
            # Context expressions are evaluated before their own __enter__,
            # in order: the first expression is not protected by a later lock.
            # A thread mutex cannot certify task exclusion across suspension;
            # Lock also cannot be acquired twice by the same thread. Keep an
            # invalid proof invalid through nested contexts, even a different
            # lock: the surrounding operation can already suspend or deadlock.
            entered = set(held or ())
            invalid = held is None
            for item in node.items:
                if entered and _suspends([item.context_expr]):
                    invalid = True
                if any(part in invalid_acquires for part in _eager_nodes([item.context_expr])):
                    invalid = True
                for kind, name in contexts.get(item.context_expr, ()):
                    if kind == "guard" and name in locks and locks[name].identity in entered and locks[name].factory == "Lock":
                        invalid = True
                entered.update(locks[name].identity for kind, name in contexts.get(item.context_expr, ())
                               if kind == "guard" and name in locks)
            if entered and _suspends(node.body):
                invalid = True
            if any(part in invalid_acquires for part in _eager_nodes(node.body)):
                invalid = True
            if invalid:
                held = None
            children = []
            for item in node.items:
                children.append((item.context_expr, held))
                mutex = _context_mutex(item.context_expr, locks, contexts)
                if held is not None and mutex is not None:
                    held = held | {mutex.identity}
                if item.optional_vars:
                    children.append((item.optional_vars, held))
            children.extend((stmt, held) for stmt in node.body)
            stack.extend(reversed(children))
            continue
        for child in _eager_children(node):
            stack.append((child, held))


def _suspends(stmts):
    """Visible suspension in this execution scope, including implicit awaits."""
    scope = ast.Module(body=list(stmts), type_ignores=[])
    return any(isinstance(node, (ast.Await, ast.Yield, ast.YieldFrom, ast.AsyncFor, ast.AsyncWith))
               or (isinstance(node, ast.comprehension) and node.is_async)
               for node in _module_scope_nodes(scope, include_class_bodies=True))


def _module_scope_nodes(tree, *, include_class_bodies=False):
    """Nodes evaluated in the module's scope, including definition inputs.

    Bodies of functions and classes have their own namespace. Defaults,
    decorators and bases still run here, where a walrus can replace a lock.
    """
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                stack.extend(node.bases)
                stack.extend(k.value for k in node.keywords)
                if include_class_bodies:
                    # Class assignments bind a separate namespace, but an
                    # attribute store may still replace threading.Lock itself.
                    stack.extend(node.body)
            else:
                stack.extend(ast.iter_child_nodes(node.args))
                if node.returns:
                    stack.append(node.returns)
            continue
        if isinstance(node, ast.Lambda):
            stack.extend(node.args.defaults)
            stack.extend(v for v in node.args.kw_defaults if v is not None)
            continue
        if isinstance(node, ast.AnnAssign) and node.value is None:
            stack.append(node.annotation)   # a bare annotation assigns no value
            continue
        if isinstance(node, ast.comprehension):
            stack.extend([node.iter] + list(node.ifs))
            continue
        stack.extend(ast.iter_child_nodes(node))


def _module_bindings(tree) -> Counter:
    """Count bindings and deletes of module names, excluding same-named locals."""
    bound = Counter()
    for node in _module_scope_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound[node.name] += 1
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound[node.id] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names if a.name != "*")
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound[node.name] += 1
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound[node.name] += 1
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound[node.rest] += 1
    return bound


_UNKNOWN_ORIGINS = frozenset({("unknown", "")})
_SETATTR_ORIGINS = frozenset({("builtin", "setattr")})


def _merged_origins(*paths):
    names = set().union(*(set(path) for path in paths))
    return {name: frozenset().union(*(path.get(name, _UNKNOWN_ORIGINS | {("global", name)}) for path in paths))
            for name in names}


class _LockInterference(ast.NodeVisitor):
    """Read direct aliases in one execution namespace, without executing calls.

    Bindings hold possible origins so alternatives can only weaken a mutex
    proof. Class bodies bind in statement order; a nested class falls back to
    the enclosing function/module, never to another class's namespace.
    """

    def __init__(self, bindings, replaced, released, contexts, *, module=False, class_outer=None,
                 held=frozenset(), local_names=frozenset()):
        self.bindings = {"setattr": _SETATTR_ORIGINS} if module else dict(bindings)
        self.replaced, self.released = replaced, released
        self.module_seeds = dict(bindings) if module else {}
        self.class_outer = class_outer
        self.contexts = contexts
        self.possible_exits = []
        self.expression_origins = {}
        self.held = held
        self.local_names = local_names
        self.global_names = set()

    def _origins(self, value):
        if isinstance(value, ast.Name):
            missing = _UNKNOWN_ORIGINS if value.id in self.local_names else _UNKNOWN_ORIGINS | {("global", value.id)}
            return self._scoped_origins(value.id, self.bindings.get(value.id, missing))
        if isinstance(value, ast.IfExp):
            return self.expression_origins.get(value, _UNKNOWN_ORIGINS)
        if isinstance(value, ast.Attribute) and value.attr == "setattr":
            if ("module", "builtins") in self._origins(value.value):
                return _SETATTR_ORIGINS
        return _UNKNOWN_ORIGINS

    def _scoped_origins(self, name, origins):
        # A function local cannot fall back to the module on an unassigned
        # branch. Class names can, so their missing-path marker is retained.
        if name in self.local_names and name not in self.global_names:
            return origins - {("global", name)}
        return origins

    def _set(self, name, origins, *, local_binding=True):
        origins = self._scoped_origins(name, origins)
        if self.class_outer is not None and local_binding and name not in self.global_names:
            origins -= {("global", name)}
        if name in self.global_names or (self.class_outer is None and name not in self.local_names):
            origins |= {("global", name)}
        self.bindings[name] = origins
        for possible in self.possible_exits:
            possible[name] = possible.get(name, frozenset()) | origins

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.contexts[node] = self.contexts.get(node, frozenset()) | self._origins(node)

    def visit_Global(self, node):
        self.global_names.update(node.names)
        for name in node.names:
            self._set(name, _UNKNOWN_ORIGINS)

    def _bind(self, target, origins=_UNKNOWN_ORIGINS):
        if isinstance(target, ast.Name):
            self._set(target.id, origins)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._bind(item)
        else:
            self.visit(target)

    def _body(self, body):
        for stmt in body:
            self.visit(stmt)

    def _body_origins(self, body):
        """Possible bindings at any early exit, including a break or exception."""
        possible = dict(self.bindings)
        self.possible_exits.append(possible)
        self._body(body)
        self.possible_exits.pop()
        return possible

    def _alternatives(self, bodies):
        before = self.bindings
        outcomes = []
        for body in bodies:
            self.bindings = dict(before)
            self._body(body)
            outcomes.append(self.bindings)
        self.bindings = _merged_origins(*outcomes)

    def visit_Assign(self, node):
        self.visit(node.value)
        origins = self._origins(node.value)
        for target in node.targets:
            # The constructor reader already proved these stable module seeds.
            seed = self.module_seeds.get(target.id, origins) if isinstance(target, ast.Name) else origins
            self._bind(target, seed)

    def visit_AnnAssign(self, node):
        self.visit(node.annotation)
        if node.value is not None:
            self.visit_Assign(ast.Assign(targets=[node.target], value=node.value))

    def visit_NamedExpr(self, node):
        self.visit(node.value)
        self._bind(node.target, self._origins(node.value))

    def visit_AugAssign(self, node):
        self.visit(node.value)
        self._bind(node.target)

    def visit_Delete(self, node):
        for target in node.targets:
            if isinstance(target, ast.Name) and self.class_outer is not None:
                fallback = self.class_outer.get(target.id, _UNKNOWN_ORIGINS | {("global", target.id)})
                self._set(target.id, fallback, local_binding=False)
            else:
                self._bind(target)

    def visit_Import(self, node):
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            self._set(name, frozenset({("module", alias.name)}) if alias.name in {"threading", "builtins"} else _UNKNOWN_ORIGINS)

    def visit_ImportFrom(self, node):
        if any(alias.name == "*" for alias in node.names):
            self.bindings.clear()
            return
        for alias in node.names:
            origins = _SETATTR_ORIGINS if node.level == 0 and node.module == "builtins" and alias.name == "setattr" else _UNKNOWN_ORIGINS
            self._set(alias.asname or alias.name, origins)

    def visit_Attribute(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr in _LOCK_FACTORIES:
            if ("module", "threading") in self._origins(node.value):
                self.replaced.add((node.attr, (node.lineno, node.col_offset)))
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Lambda):
            self._invoke_lambda(node)
            return
        # Capture the callable/receiver before arguments can rebind names, and
        # capture each argument's value before evaluating the next one.
        self.visit(node.func)
        callee = self._origins(node.func)
        receiver = self._origins(node.func.value) if isinstance(node.func, ast.Attribute) else frozenset()
        actual = []
        for value in node.args:
            self.visit(value)
            actual.append(self._origins(value))
        self._body(keyword.value for keyword in node.keywords)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "acquire":
            self.contexts[node.func.value] = self.contexts.get(node.func.value, frozenset()) | receiver
            self.contexts[node] = self.contexts.get(node, frozenset()) | self.held
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"release", "__exit__"}:
            self.released.update(name for kind, name in receiver if kind == "guard")
        if ("builtin", "setattr") in callee and len(node.args) == 3:
            _, attribute, _ = node.args
            if ("module", "threading") in actual[0] and isinstance(attribute, ast.Constant):
                if isinstance(attribute.value, str) and attribute.value in _LOCK_FACTORIES:
                    self.replaced.add((attribute.value, (node.lineno, node.col_offset)))

    def visit_If(self, node):
        self.visit(node.test)
        self._alternatives([node.body, node.orelse])

    def visit_IfExp(self, node):
        self.visit(node.test)
        before = self.bindings
        outcomes, origins = [], frozenset()
        for value in (node.body, node.orelse):
            self.bindings = dict(before)
            self.visit(value)
            origins |= self._origins(value)
            outcomes.append(self.bindings)
        self.bindings = _merged_origins(*outcomes)
        self.expression_origins[node] = origins

    def visit_While(self, node):
        self._loop(node, dict(self.bindings))

    def _loop(self, node, before):
        # Every origin is one of this file's finite seeds or unknown. Joining
        # carried bindings therefore reaches a fixed point without a cutoff,
        # and contexts in later iterations cannot reuse first-iteration proof.
        entry = dict(self.bindings)
        while True:
            self.bindings = dict(entry)
            if isinstance(node, ast.While):
                self.visit(node.test)
            else:
                self._bind(node.target)
            possible = self._body_origins(node.body)
            carried = _merged_origins(entry, possible, self.bindings)
            if carried == entry:
                break
            entry = carried
        self._body(node.orelse)
        self.bindings = _merged_origins(before, possible, self.bindings)

    def visit_For(self, node):
        self.visit(node.iter)
        # An iteration variable shadows a module object in the body; a loop
        # that never runs leaves its earlier binding possible afterwards.
        before = dict(self.bindings)
        self._bind(node.target)
        self._loop(node, before)

    visit_AsyncFor = visit_For

    def visit_With(self, node):
        before = self.held
        for item in node.items:
            self.visit(item.context_expr)
            origins = self._origins(item.context_expr)
            self.contexts[item.context_expr] = self.contexts.get(item.context_expr, frozenset()) | origins
            self.held |= origins
            if item.optional_vars:
                self._bind(item.optional_vars)
        self._body(node.body)
        self.held = before

    visit_AsyncWith = visit_With

    def visit_Try(self, node):
        # Any statement in the try body can fail. A handler/finally can see a
        # binding installed before that failure, even if a later assignment
        # would replace it on the normal path.
        possible = self._body_origins(node.body)
        self._body(node.orelse)
        outcomes = [self.bindings, possible]
        for handler in node.handlers:
            self.bindings = dict(possible)
            if handler.type:
                self.visit(handler.type)
            if handler.name:
                self._set(handler.name, _UNKNOWN_ORIGINS)
            self._body(handler.body)
            outcomes.append(self.bindings)
        self.bindings = _merged_origins(*outcomes)
        self._body(node.finalbody)

    visit_TryStar = visit_Try

    def visit_Match(self, node):
        self.visit(node.subject)
        subject = self._origins(node.subject)
        before = self.bindings
        outcomes = [before]  # matching no case is possible too
        for case in node.cases:
            self.bindings = dict(before)
            for pattern in ast.walk(case.pattern):
                if isinstance(pattern, (ast.MatchAs, ast.MatchStar)) and pattern.name:
                    self._set(pattern.name, _UNKNOWN_ORIGINS)
                elif isinstance(pattern, ast.MatchMapping) and pattern.rest:
                    self._set(pattern.rest, _UNKNOWN_ORIGINS)
            if isinstance(case.pattern, ast.MatchAs) and case.pattern.name:
                self._set(case.pattern.name, subject)
            if case.guard:
                self.visit(case.guard)
            self._body(case.body)
            outcomes.append(self.bindings)
        self.bindings = _merged_origins(*outcomes)

    def visit_FunctionDef(self, node):
        self._body(_definition_inputs(node))
        self._set(node.name, _UNKNOWN_ORIGINS)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self._body(node.args.defaults)
        self._body(value for value in node.args.kw_defaults if value is not None)

    def visit_GeneratorExp(self, node):
        self.visit(node.generators[0].iter)

    def _comprehension(self, node):
        self.visit(node.generators[0].iter)
        targets = {item.id for generator in node.generators for item in ast.walk(generator.target)
                   if isinstance(item, ast.Name)}
        outer = self.bindings if self.class_outer is None else self.class_outer
        child = _LockInterference(outer, self.replaced, self.released, self.contexts, held=self.held,
                                  local_names=self.local_names | targets)
        for index, generator in enumerate(node.generators):
            if index:
                child.visit(generator.iter)
            child._bind(generator.target)
            child._body(generator.ifs)
        child._body([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt])
        for item in _module_scope_nodes(ast.Module(body=[node], type_ignores=[])):
            if isinstance(item, ast.NamedExpr) and isinstance(item.target, ast.Name):
                name = item.target.id
                self._set(name, self._origins(item.target) | child.bindings.get(name, _UNKNOWN_ORIGINS))

    visit_ListComp = _comprehension
    visit_SetComp = _comprehension
    visit_DictComp = _comprehension

    def _invoke_lambda(self, call):
        # Defaults and actual arguments run in the enclosing namespace; the
        # body has its own parameters and walrus locals. A merely created
        # lambda does not execute its body.
        node = call.func
        positional = list(node.args.posonlyargs) + list(node.args.args)
        defaults = {}
        for param, value in zip(positional[len(positional) - len(node.args.defaults):], node.args.defaults):
            self.visit(value)
            defaults[param.arg] = self._origins(value)
        for param, value in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if value is not None:
                self.visit(value)
                defaults[param.arg] = self._origins(value)
        actual = []
        for value in call.args:
            self.visit(value)
            actual.append(self._origins(value))
        keywords = {}
        for keyword in call.keywords:
            self.visit(keyword.value)
            keywords[keyword.arg] = self._origins(keyword.value)
        if _lambda_is_generator(node):
            return
        outer = self.bindings if self.class_outer is None else self.class_outer
        child = _LockInterference(outer, self.replaced, self.released, self.contexts, held=self.held,
                                  local_names=self.local_names | _lambda_bound_names(node))
        params = positional + list(node.args.kwonlyargs) + [p for p in (node.args.vararg, node.args.kwarg) if p]
        for param in params:
            child._set(param.arg, defaults.get(param.arg, _UNKNOWN_ORIGINS))
        for param, origins in zip(positional, actual):
            child._set(param.arg, origins)
        for name, origins in keywords.items():
            if name is not None:
                child._set(name, origins)
        local = _lambda_bound_names(node)
        for name in local - {param.arg for param in params}:
            child._set(name, _UNKNOWN_ORIGINS)
        child.visit(node.body)

    def visit_ClassDef(self, node):
        self._body(_definition_inputs(node))
        outer = self.bindings if self.class_outer is None else self.class_outer
        child = _LockInterference(outer, self.replaced, self.released, self.contexts, class_outer=dict(outer),
                                  held=self.held, local_names=self.local_names)
        child._body(node.body)
        self._set(node.name, _UNKNOWN_ORIGINS)


def _lock_interference(tree, threading_modules, lock_names, contexts, builtin_origins):
    """Visible factory replacement/release, with ordinary lexical alias bindings."""
    known = {name: frozenset({("module", "threading")}) for name in threading_modules}
    known.update((name, frozenset({("guard", name)})) for name in lock_names)
    known.update(builtin_origins)
    replaced, released = set(), set()
    _LockInterference(known, replaced, released, contexts, module=True)._body(tree.body)
    if ("setattr" not in _module_bindings(tree)
            and not any(isinstance(node, ast.Global) and "setattr" in node.names for node in ast.walk(tree))
            and not any(isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
                        for node in _module_scope_nodes(tree))):
        known["setattr"] = _SETATTR_ORIGINS
    for fn, enclosing in _function_scopes(tree):
        global_names = {name for node in _module_scope_nodes(ast.Module(body=fn.body, type_ignores=[]))
                        if isinstance(node, ast.Global) for name in node.names}
        local = (_bound_names(fn) | enclosing) - global_names
        _LockInterference({name: origins for name, origins in known.items() if name not in local},
                          replaced, released, contexts, local_names=local)._body(fn.body)
    return replaced, released


def _module_names(stmt):
    """Direct assignment/import targets understood by the module identity proof."""
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return [(alias.asname or alias.name).split(".")[0] for alias in stmt.names]
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
    return [target.id for target in targets if isinstance(target, ast.Name)]


def _module_aliases(tree, known, stable):
    """Propagate direct, stable module aliases without conflating spellings.

    A chain assignment creates one object, and a later `alias = name` retains
    that identity. Only propagate a binding after its statement executes: a
    wildcard may supply an earlier object with the same spelling. Other
    assignment shapes remain outside this bounded proof.
    """
    visible = {}
    for stmt in tree.body:
        value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
        names = _module_names(stmt)
        if isinstance(value, ast.Name) and value.id in visible:
            visible.update((name, visible[value.id]) for name in names if name in stable)
        visible.update((name, known[name]) for name in names if name in known)
    return visible


def _argument_free_call(node):
    """Only explicit empty unpackings are also provably argument-free."""
    return (all(isinstance(arg, ast.Starred) and isinstance(arg.value, (ast.List, ast.Tuple)) and not arg.value.elts
                for arg in node.args)
            and all(keyword.arg is None and isinstance(keyword.value, ast.Dict) and not keyword.value.keys
                    for keyword in node.keywords))


def _module_locks(tree, contexts) -> dict:
    """Stable names bound once to an imported threading Lock/RLock constructor.

    A global declaration anywhere keeps a name out of this bounded proof: it
    permits a later call to replace the object the name originally identified.
    """
    # An unknown export can replace a factory alias, the threading module or
    # an already-created guard. Without resolving that surface none is stable.
    last_wildcard = max(((node.lineno, node.col_offset) for node in _module_scope_nodes(tree)
                         if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)), default=(0, 0))
    bindings = _module_bindings(tree)
    global_names = {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}
    stable = {name for name, count in bindings.items() if count == 1} - global_names
    threading_modules, lock_factories, builtin_origins = set(), {}, {}
    for stmt in tree.body:
        if (stmt.lineno, stmt.col_offset) <= last_wildcard:
            continue
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == "threading":
                    threading_modules.add(alias.asname or "threading")
                elif alias.name == "builtins":
                    builtin_origins[alias.asname or "builtins"] = frozenset({("module", "builtins")})
        elif isinstance(stmt, ast.ImportFrom) and stmt.module == "threading" and stmt.level == 0:
            for alias in stmt.names:
                if alias.name in _LOCK_FACTORIES:
                    lock_factories[alias.asname or alias.name] = _Factory(alias.name, (stmt.lineno, stmt.col_offset))
        elif isinstance(stmt, ast.ImportFrom) and stmt.module == "builtins" and stmt.level == 0:
            for alias in stmt.names:
                if alias.name == "setattr":
                    builtin_origins[alias.asname or alias.name] = _SETATTR_ORIGINS
    threading_modules &= stable
    lock_factories = {name: factory for name, factory in lock_factories.items() if name in stable}
    threading_modules = set(_module_aliases(tree, {name: "threading" for name in threading_modules}, stable))
    lock_factories = _module_aliases(tree, lock_factories, stable)
    builtin_origins = _module_aliases(tree, {name: origin for name, origin in builtin_origins.items() if name in stable}, stable)
    bound_at = {name: (stmt.lineno, stmt.col_offset) for stmt in tree.body for name in _module_names(stmt)}
    locks = {}
    for stmt in tree.body:
        if (stmt.lineno, stmt.col_offset) <= last_wildcard:
            continue
        value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        position = (stmt.lineno, stmt.col_offset)
        is_lock = (
            (isinstance(func, ast.Name) and func.id in lock_factories and bound_at[func.id] < position)
            or (isinstance(func, ast.Attribute) and func.attr in _LOCK_FACTORIES
                and isinstance(func.value, ast.Name) and func.value.id in threading_modules
                and bound_at[func.value.id] < position)
        )
        if not is_lock:
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        origin = _Factory(func.attr) if isinstance(func, ast.Attribute) else lock_factories[func.id]
        if origin.name == "Lock" and not _argument_free_call(value):
            continue
        mutex = _Mutex(origin.name, (value.lineno, value.col_offset))
        locks.update((t.id, (origin, mutex)) for t in targets if isinstance(t, ast.Name) and t.id in stable)
    locks = _module_aliases(tree, locks, stable)
    replaced, released = _lock_interference(tree, threading_modules, set(locks), contexts, builtin_origins)
    released_identities = {locks[name][1].identity for name in released}
    return {name: mutex for name, (origin, mutex) in locks.items()
            if mutex.identity not in released_identities
            and not any(factory == origin.name and (origin.imported_at is None or position < origin.imported_at)
                        for factory, position in replaced)}


def _bound_names(fn) -> set:
    """Names a function binds in its own scope — its locals, as Python decides
    them: parameters, assignment and loop targets, `as` names, imports, and the
    defs and classes it creates. `global` names are the module's and are left
    out; `nonlocal` names are an enclosing function's, so they are kept (they
    are not the module's either)."""
    args = fn.args
    params = list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs)
    params += [a for a in (args.vararg, args.kwarg) if a is not None]
    bound = {a.arg for a in params}
    declared_global = set()
    stack = [s for s in fn.body]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)     # the name is local; the body is another scope
            stack.extend(_definition_inputs(node))
            continue
        if isinstance(node, ast.Lambda):
            stack.extend(_eager_children(node))
            continue
        if isinstance(node, ast.Global):
            declared_global.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names if a.name != "*")
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        if isinstance(node, ast.comprehension):
            # A comprehension's loop variable lives in the comprehension's own
            # scope; only what it iterates (and a walrus in it) reaches ours.
            stack.extend([node.iter] + list(node.ifs))
            continue
        stack.extend(ast.iter_child_nodes(node))
    return bound - declared_global


def _function_scopes(tree):
    """Every function, with the names enclosing *functions* bind (a closure
    sees those, not the module's). Class bodies bind nothing a method sees."""
    def visit(node, enclosing):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child, enclosing
                yield from visit(child, enclosing | _bound_names(child))
            else:
                yield from visit(child, enclosing)
    yield from visit(tree, frozenset())


def detect(tree, filename, lines, ignore):
    issues = []

    def add(line, st, desc, sug, sev, also_caused_by=()):
        if st in ignore:
            return
        issues.append(CodeSmell(filename, line, st, desc, "" if also_caused_by else sug, sev,
                                _get_line(lines, line),
                                "candidate" if also_caused_by else "finding", tuple(also_caused_by)))

    # Pass 1: module-level mutable assignments
    candidates = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and _is_mutable_value(stmt.value):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and not _is_dunder(t.id):
                    candidates[t.id] = stmt.lineno
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None and _is_mutable_value(stmt.value):
            if isinstance(stmt.target, ast.Name) and not _is_dunder(stmt.target.id):
                candidates[stmt.target.id] = stmt.lineno

    # Pass 2: scan every function for `global` rebinds and mutations of candidates
    contexts = {}
    locks = _module_locks(tree, contexts)
    invalid_acquires = {node for node in contexts if _blocking_acquire(node, locks, contexts)}
    guarded = defaultdict(list)     # name -> [stable module lock names held] per mutation site
    candidate_names = set(candidates)
    for fn, enclosing in _function_scopes(tree):
        body = list(_same_scope_nodes(fn.body))
        global_lines = {}
        for n in body:
            if isinstance(n, ast.Global):
                for nm in n.names:
                    global_lines.setdefault(nm, n.lineno)
        globals_here = set(global_lines)
        rebinds = set()
        for n in body:
            if isinstance(n, ast.Assign):
                rebinds.update(t.id for t in n.targets if isinstance(t, ast.Name) and t.id in globals_here)
            elif isinstance(n, ast.AugAssign):
                if isinstance(n.target, ast.Name) and n.target.id in globals_here:
                    rebinds.add(n.target.id)
            elif isinstance(n, ast.AnnAssign):
                if isinstance(n.target, ast.Name) and n.target.id in globals_here and n.value is not None:
                    rebinds.add(n.target.id)
        if rebinds:
            named = ", ".join(f"'{nm}'" for nm in sorted(rebinds))
            plural = "s" if len(rebinds) > 1 else ""
            # Ruff's PLW0603 is reported on the `global` statement; a stated
            # reason there for every name rebound is the repo's adjudication.
            reasons = ()
            if all(reasoned_noqa(lines, global_lines[nm], "PLW0603") for nm in rebinds):
                reasons = ("the repo's PLW0603 lint gate suppresses this `global` with a stated reason — "
                           "e.g. an at-fork or install-once hook whose return value no caller receives",)
            add(fn.lineno, "global_rebind",
                f"Function '{fn.name}' reassigns module-level global{plural} {named} via the 'global' statement",
                "Return the new value and assign it at the call site, or encapsulate the state in a class or closure.",
                "medium", also_caused_by=reasons)
        # Only the module's names: one this function (or an enclosing function)
        # binds locally is a different object that happens to share the spelling.
        for n, held in _guarded_nodes(fn.body, locks, contexts, invalid_acquires):
            m = _mutates_name(n, candidate_names, contexts)
            if m:
                guarded[m].append(held)

    for nm in sorted(guarded):
        common_locks = set.intersection(*(set(held) for held in guarded[nm]))
        if common_locks:
            names = sorted(name for name, mutex in locks.items() if mutex.identity in common_locks)
            add(candidates[nm], "mutated_global",
                f"Every detected direct mutation of module-level mutable '{nm}' holds the same stable "
                f"module lock ({', '.join(names)})",
                "", "medium", also_caused_by=(
                    "an intentional process-wide registry or memo whose direct writes share one threading Lock/RLock",
                    "reads, mutations through aliases, external consumers and fork/reset lifecycle are not proved "
                    "by this file-level write check",
                ))
        else:
            add(candidates[nm], "mutated_global",
                f"Module-level mutable '{nm}' is shared global state that is mutated at runtime",
                "Encapsulate it in a class or pass it explicitly. Module-level mutable state makes execution "
                "order-dependent and hard to test.",
                "high")

    return issues


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source, tree = cached_parse(filepath)
        return detect(tree, str(filepath), source.splitlines(), ignore)
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
    parser = argparse.ArgumentParser(description="Detect global-state hazards in Python")
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
            print("✅ No global-state hazards found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        proven = sum(1 for i in all_issues if i.kind != "candidate")
        print(f"Found {proven} global-state issue(s), {len(all_issues) - proven} candidate(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = SEVERITY_ICONS
        for i in all_issues:
            # A severity icon on a lead reads as a verdict the evidence does not support.
            marker = "? [CANDIDATE]" if i.kind == "candidate" else f"{icons[i.severity]} [{i.severity.upper()}]"
            print(f"{marker} {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            if i.suggestion:
                print(f"   → {i.suggestion}")
            for reason in i.also_caused_by:
                print(f"   ? also caused by: {reason}")
            print()


if __name__ == "__main__":
    main()
