"""File-local execution and mutex evidence for global-state analysis.

Bindings track possible object origins and namespace ownership through Python's
visible eager evaluation, with a finite origin join at control-flow boundaries.
Only one stable imported mutex shared by every detected direct write can justify
a candidate; indirect effects, external readers and lifecycle remain unproved.
Finding severity, reporting and the CLI belong to find_global_state instead.
"""

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass


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
    return (getattr(node, "decorator_list", []) + list(args.defaults) + [value for value in args.kw_defaults if value is not None]
            + [arg.annotation for arg in params if arg.annotation is not None]
            + ([node.returns] if getattr(node, "returns", None) is not None else []))


def _callable_bound_names(node):
    if not isinstance(node, ast.Lambda):
        return _bound_names(node)
    args = node.args
    params = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    params += [arg for arg in (args.vararg, args.kwarg) if arg]
    names = {arg.arg for arg in params}
    names.update(item.id for item in _module_scope_nodes(ast.Module(body=[node.body], type_ignores=[]))
                 if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store))
    return names


def _callable_is_deferred(node):
    body = [node.body] if isinstance(node, ast.Lambda) else node.body
    return isinstance(node, ast.AsyncFunctionDef) or any(isinstance(item, (ast.Yield, ast.YieldFrom))
               for item in _module_scope_nodes(ast.Module(body=body, type_ignores=[])))


def _eager_children(node, contexts=None):
    """Definition inputs execute here; deferred bodies retain their own walk."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _definition_inputs(node)
    if isinstance(node, ast.Lambda):
        return list(node.args.defaults) + [value for value in node.args.kw_defaults if value is not None]
    if isinstance(node, ast.GeneratorExp):
        return [node.generators[0].iter]
    children = list(ast.iter_child_nodes(node))
    if isinstance(node, ast.Call):
        callables = {node.func} if isinstance(node.func, ast.Lambda) else {
            value for kind, value in (contexts or {}).get(node.func, ()) if kind == "callable"}
        for value in callables:
            if not _callable_is_deferred(value):
                children.extend([value.body] if isinstance(value, ast.Lambda) else value.body)
    return children


def _eager_nodes(stmts, contexts):
    stack = list(stmts)
    seen = set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        yield node
        stack.extend(_eager_children(node, contexts))


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
    if not isinstance(node, ast.Call):
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
               for kind, name in contexts.get(node.func, ()) if kind == "acquire" and name in locks)


def _guarded_nodes(stmts, locks, contexts, invalid_acquires):
    """Same-scope nodes paired with the known module locks held at each node.

    `locks` names the module-level Lock/RLock objects. A nested function is
    its own scope and its own walk: it runs whenever it is called, which need
    not be while the lock that surrounds its definition is held.
    """
    stack = [(s, frozenset()) for s in stmts]
    seen = set()
    while stack:
        node, held = stack.pop()
        if (node, held) in seen:
            continue
        seen.add((node, held))
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
                if any(part in invalid_acquires for part in _eager_nodes([item.context_expr], contexts)):
                    invalid = True
                for kind, name in contexts.get(item.context_expr, ()):
                    if kind == "guard" and name in locks and locks[name].identity in entered and locks[name].factory == "Lock":
                        invalid = True
                entered.update(locks[name].identity for kind, name in contexts.get(item.context_expr, ())
                               if kind == "guard" and name in locks)
            if entered and _suspends(node.body):
                invalid = True
            if any(part in invalid_acquires for part in _eager_nodes(node.body, contexts)):
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
        for child in _eager_children(node, contexts):
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
_ATTRIBUTE_BUILTINS = {"setattr": 3, "delattr": 2}


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
                 held=frozenset(), local_names=frozenset(), callable_values=None, active_calls=None,
                 global_bindings=None):
        self.bindings = {name: frozenset({("builtin", name)}) for name in _ATTRIBUTE_BUILTINS} if module else dict(bindings)
        self.replaced, self.released = replaced, released
        self.module_seeds = dict(bindings) if module else {}
        self.class_outer = class_outer
        self.contexts = contexts
        self.possible_exits = []
        self.expression_origins = {}
        self.held = held
        self.local_names = local_names
        self.global_names = set()
        self.module = module
        self.callable_values = {} if callable_values is None else callable_values
        self.active_calls = set() if active_calls is None else active_calls
        self.global_bindings = bindings if global_bindings is None else global_bindings

    def _child(self, bindings, *, class_outer=None, local_names=frozenset()):
        return _LockInterference(bindings, self.replaced, self.released, self.contexts,
                                 class_outer=class_outer, held=self.held, local_names=local_names,
                                 callable_values=self.callable_values, active_calls=self.active_calls,
                                 global_bindings=self.global_bindings)

    def _origins(self, value):
        if isinstance(value, ast.Name):
            missing = _UNKNOWN_ORIGINS if value.id in self.local_names else _UNKNOWN_ORIGINS | {("global", value.id)}
            return self._scoped_origins(value.id, self.bindings.get(value.id, missing))
        if isinstance(value, (ast.IfExp, ast.NamedExpr)):
            return self.expression_origins.get(value, _UNKNOWN_ORIGINS)
        if isinstance(value, ast.Lambda):
            return frozenset({("callable", value)})
        if isinstance(value, ast.Attribute) and value.attr == "__dict__":
            if ("module", "threading") in self._origins(value.value):
                return frozenset({("namespace", "threading")})
        if isinstance(value, ast.Attribute) and value.attr in {"release", "__exit__", "acquire", "__enter__"}:
            operation = "release" if value.attr in {"release", "__exit__"} else "acquire"
            origins = frozenset((operation, name) for kind, name in self._origins(value.value) if kind == "guard")
            return origins or _UNKNOWN_ORIGINS
        if isinstance(value, ast.Attribute) and value.attr in _ATTRIBUTE_BUILTINS:
            if ("module", "builtins") in self._origins(value.value):
                return frozenset({("builtin", value.attr)})
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
        origins = self._origins(node.value)
        self.expression_origins[node] = origins
        self._bind(node.target, origins)

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
            origins = (frozenset({("builtin", alias.name)}) if node.level == 0 and node.module == "builtins"
                       and alias.name in _ATTRIBUTE_BUILTINS else _UNKNOWN_ORIGINS)
            self._set(alias.asname or alias.name, origins)

    def visit_Attribute(self, node):
        self.visit(node.value)
        if isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr in _LOCK_FACTORIES:
            if ("module", "threading") in self._origins(node.value):
                self.replaced.add((node.attr, (node.lineno, node.col_offset)))

    def visit_Subscript(self, node):
        self.visit(node.value)
        namespace = self._origins(node.value)
        self.visit(node.slice)
        if (isinstance(node.ctx, (ast.Store, ast.Del)) and ("namespace", "threading") in namespace
                and isinstance(node.slice, ast.Constant) and node.slice.value in _LOCK_FACTORIES):
            self.replaced.add((node.slice.value, (node.lineno, node.col_offset)))

    def visit_Call(self, node):
        # Capture the callable/receiver before arguments can rebind names, and
        # capture each argument's value before evaluating the next one.
        self.visit(node.func)
        callee = self._origins(node.func)
        self.contexts[node.func] = self.contexts.get(node.func, frozenset()) | callee
        actual = []
        for value in node.args:
            self.visit(value)
            actual.append(self._origins(value))
        keywords = {}
        for keyword in node.keywords:
            self.visit(keyword.value)
            keywords[keyword.arg] = self._origins(keyword.value)
        for kind, value in callee:
            if kind == "callable":
                self._invoke_callable(value, actual, keywords, any(isinstance(arg, ast.Starred) for arg in node.args))
        if any(kind == "acquire" for kind, _ in callee):
            self.contexts[node] = self.contexts.get(node, frozenset()) | self.held
        self.released.update(name for kind, name in callee if kind == "release")
        if any(("builtin", name) in callee and len(node.args) == arity for name, arity in _ATTRIBUTE_BUILTINS.items()):
            attribute = node.args[1]
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
        self._record_callable(node)
        self._set(node.name, frozenset({("callable", node)}))

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self._record_callable(node)

    def _record_callable(self, node):
        # Defaults capture values now; free local names remain late-bound in
        # this lexical scope when a visible callable value is called later.
        positional = list(node.args.posonlyargs) + list(node.args.args)
        defaults = {}
        values = dict(zip(node.args.defaults, positional[len(positional) - len(node.args.defaults):]))
        values.update((value, param) for param, value in zip(node.args.kwonlyargs, node.args.kw_defaults) if value is not None)
        for value in _definition_inputs(node):
            self.visit(value)
            if value in values:
                defaults[values[value].arg] = self._origins(value)
        previous = self.callable_values.get(node, ({}, self.local_names))[0]
        self.callable_values[node] = (_merged_origins(previous, defaults) if previous else defaults, self.local_names)

    def visit_GeneratorExp(self, node):
        self.visit(node.generators[0].iter)

    def _comprehension(self, node):
        self.visit(node.generators[0].iter)
        targets = {item.id for generator in node.generators for item in ast.walk(generator.target)
                   if isinstance(item, ast.Name)}
        outer = self.bindings if self.class_outer is None else self.class_outer
        child = self._child(outer, local_names=self.local_names | targets)
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

    def _invoke_callable(self, node, actual, keywords, unpacked):
        # These call shapes cannot supply positive evidence of safe retention.
        # Async/generator creation itself defers the body; awaiting/consuming it
        # is outside this synchronous call expansion.
        if (node in self.active_calls or unpacked or None in keywords
                or getattr(node, "decorator_list", ()) or node.args.vararg or node.args.kwarg):
            self.released.update(name for kind, name in self.held if kind == "guard")
            return
        if _callable_is_deferred(node):
            return
        positional = list(node.args.posonlyargs) + list(node.args.args)
        defaults, closure_names = self.callable_values.get(node, ({}, frozenset()))
        visible = self.bindings if self.class_outer is None else self.class_outer
        outer = dict(visible if self.module else self.global_bindings)
        outer.update((name, visible.get(name, _UNKNOWN_ORIGINS)) for name in closure_names)
        child = self._child(outer, local_names=closure_names | _callable_bound_names(node))
        params = positional + list(node.args.kwonlyargs) + [p for p in (node.args.vararg, node.args.kwarg) if p]
        for param in params:
            child._set(param.arg, defaults.get(param.arg, _UNKNOWN_ORIGINS))
        for param, origins in zip(positional, actual):
            child._set(param.arg, origins)
        for name, origins in keywords.items():
            if name is not None:
                child._set(name, origins)
        local = _callable_bound_names(node)
        for name in local - {param.arg for param in params}:
            child._set(name, _UNKNOWN_ORIGINS)
        self.active_calls.add(node)
        try:
            child._body([node.body] if isinstance(node, ast.Lambda) else node.body)
        finally:
            self.active_calls.remove(node)

    def visit_ClassDef(self, node):
        self._body(_definition_inputs(node))
        outer = self.bindings if self.class_outer is None else self.class_outer
        child = self._child(outer, class_outer=dict(outer), local_names=self.local_names)
        child._body(node.body)
        self._set(node.name, _UNKNOWN_ORIGINS)


def _lock_interference(tree, threading_modules, lock_names, contexts, builtin_origins):
    """Visible factory replacement/release, with ordinary lexical alias bindings."""
    known = {name: frozenset({("module", "threading")}) for name in threading_modules}
    known.update((name, frozenset({("guard", name)})) for name in lock_names)
    known.update(builtin_origins)
    replaced, released = set(), set()
    module = _LockInterference(known, replaced, released, contexts, module=True)
    module._body(tree.body)
    for name, origins in module.bindings.items():
        if any(kind in {"callable", "release", "acquire"} for kind, _ in origins):
            known[name] = origins
    for builtin in _ATTRIBUTE_BUILTINS:
        if (builtin not in _module_bindings(tree)
                and not any(isinstance(node, ast.Global) and builtin in node.names for node in ast.walk(tree))
                and not any(isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
                            for node in _module_scope_nodes(tree))):
            known[builtin] = frozenset({("builtin", builtin)})
    for fn, enclosing in function_scopes(tree):
        global_names = {name for node in _module_scope_nodes(ast.Module(body=fn.body, type_ignores=[]))
                        if isinstance(node, ast.Global) for name in node.names}
        local = (_bound_names(fn) | enclosing) - global_names
        _LockInterference({name: origins for name, origins in known.items() if name not in local},
                          replaced, released, contexts, local_names=local, global_bindings=known,
                          callable_values=module.callable_values)._body(fn.body)
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
                if alias.name in _ATTRIBUTE_BUILTINS:
                    builtin_origins[alias.asname or alias.name] = frozenset({("builtin", alias.name)})
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


def function_scopes(tree):
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


def mutation_guards(tree, candidate_names):
    """Return validated module mutexes and held identities per direct write."""
    contexts = {}
    locks = _module_locks(tree, contexts)
    invalid_acquires = {node for node in contexts if _blocking_acquire(node, locks, contexts)}
    guarded = defaultdict(list)
    for fn, _ in function_scopes(tree):
        for node, held in _guarded_nodes(fn.body, locks, contexts, invalid_acquires):
            name = _mutates_name(node, candidate_names, contexts)
            if name:
                guarded[name].append(held)
    return locks, guarded
