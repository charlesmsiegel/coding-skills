"""File-local execution and mutex evidence for global-state analysis.

Bindings track possible object origins and namespace ownership through Python's
visible eager evaluation, with a finite origin join at control-flow boundaries.
Only one stable imported mutex shared by every detected direct write can justify
a candidate; indirect effects, external readers and lifecycle remain unproved.
Finding severity, reporting and the CLI belong to find_global_state instead.
"""

import ast
from collections import defaultdict
from dataclasses import dataclass

import global_state_scopes as scopes
import global_state_execution as execution


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
    # sys.modules replacement affects subsequent imports, not a module/factory
    # object captured by an earlier import and copied to another stable alias.
    module_at: tuple[int, int] | None = None


_UNKNOWN_ORIGINS = frozenset({("unknown", "")})
_ATTRIBUTE_BUILTINS = {"setattr": 3, "delattr": 2}
_ITERATOR_CONSUMERS = {"list", "tuple", "set", "dict", "sum", "any", "all", "min", "max", "sorted", "next"}
_LAZY_CALLBACKS = {"map", "filter"}
_BUILTINS = set(_ATTRIBUTE_BUILTINS) | _ITERATOR_CONSUMERS | _LAZY_CALLBACKS | {"vars", "iter"}
_LAZY_KINDS = {"generator", "deferred", "callback_iterator", "uncertain_iterator"}
_NAMESPACE_KEYS = {"threading": _LOCK_FACTORIES, "imports": {"threading"}}
# In a proven threading namespace, setdefault cannot replace the present factory.
# The imports namespace need not contain threading yet, so it differs below.
# Other methods must be known readers or the receiver can lose its factory.
_NAMESPACE_READERS = {"get", "keys", "values", "items", "copy", "fromkeys", "setdefault",
                      "__getitem__", "__contains__", "__iter__", "__reversed__", "__len__",
                      "__repr__", "__str__", "__class__", "__eq__", "__ne__", "__ge__", "__gt__", "__le__", "__lt__"}


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
                 global_bindings=None, skipped=frozenset(), execution_at=None,
                 iterator_calls=None, active_iterators=None):
        self.bindings = {name: frozenset({("builtin", name)}) for name in _BUILTINS} if module else dict(bindings)
        self.replaced, self.released = replaced, released
        self.module_seeds = dict(bindings) if module else {}
        self.class_outer = class_outer
        self.contexts = contexts
        self.possible_exits = []
        self.expression_origins = {}
        self.returned_origins = frozenset()
        self.held = held
        self.local_names = local_names
        self.global_names = set()
        self.global_writes = set()
        self.module = module
        self.callable_values = {} if callable_values is None else callable_values
        self.active_calls = set() if active_calls is None else active_calls
        self.global_bindings = bindings if global_bindings is None else global_bindings
        self.skipped = skipped
        self.execution_at = execution_at
        self.iterator_calls = {} if iterator_calls is None else iterator_calls
        self.active_iterators = set() if active_iterators is None else active_iterators

    def visit(self, node):
        if node not in self.skipped:
            return super().visit(node)

    def _child(self, bindings, *, class_outer=None, local_names=frozenset(), execution_at=None):
        return _LockInterference(bindings, self.replaced, self.released, self.contexts,
                                 class_outer=class_outer, held=self.held, local_names=local_names,
                                 callable_values=self.callable_values, active_calls=self.active_calls,
                                 global_bindings=self.global_bindings, skipped=self.skipped,
                                 execution_at=self.execution_at if execution_at is None else execution_at,
                                 iterator_calls=self.iterator_calls, active_iterators=self.active_iterators)

    def _effect_position(self, node):
        # A visible invocation runs its body now, not at its definition's line.
        return self.execution_at or (node.lineno, node.col_offset)

    def _decline_mutex_proof(self):
        for origins in list(self.bindings.values()) + list(self.global_bindings.values()) + [self.held]:
            self.released.update(name for kind, name in origins if kind == "guard")

    def _origins(self, value):
        if isinstance(value, ast.Name):
            missing = _UNKNOWN_ORIGINS if value.id in self.local_names else _UNKNOWN_ORIGINS | {("global", value.id)}
            return self._scoped_origins(value.id, self.bindings.get(value.id, missing))
        if isinstance(value, (ast.IfExp, ast.BoolOp, ast.NamedExpr, ast.Call, ast.Subscript)):
            return self.expression_origins.get(value, _UNKNOWN_ORIGINS)
        if isinstance(value, ast.Lambda):
            return frozenset({("callable", value)})
        if isinstance(value, ast.GeneratorExp):
            return frozenset({("generator", value)})
        if isinstance(value, ast.Attribute) and value.attr == "__dict__":
            if ("module", "threading") in self._origins(value.value):
                return frozenset({("namespace", "threading")})
        if isinstance(value, ast.Attribute) and value.attr == "modules" and ("module", "sys") in self._origins(value.value):
            return frozenset({("namespace", "imports")})
        if isinstance(value, ast.Attribute):
            namespaces = frozenset(("namespace_method", (name, value.attr))
                                   for kind, name in self._origins(value.value) if kind == "namespace"
                                   and (value.attr not in _NAMESPACE_READERS or (name == "imports" and value.attr == "setdefault")))
            if namespaces:
                return namespaces
        if isinstance(value, ast.Attribute) and value.attr in {"release", "__exit__", "acquire", "__enter__"}:
            operation = "release" if value.attr in {"release", "__exit__"} else "acquire"
            origins = frozenset((operation, name) for kind, name in self._origins(value.value) if kind == "guard")
            return origins or _UNKNOWN_ORIGINS
        if isinstance(value, ast.Attribute) and value.attr in _BUILTINS:
            if ("module", "builtins") in self._origins(value.value):
                return frozenset({("builtin", value.attr)})
        return _UNKNOWN_ORIGINS

    def _scoped_origins(self, name, origins):
        # A function local cannot fall back to the module on an unassigned
        # branch. Class names can, so their missing-path marker is retained.
        if name in self.local_names and name not in self.global_names:
            return origins - {("global", name)}
        return origins

    def _set(self, name, origins, *, local_binding=True, assignment=True):
        if assignment and name in self.global_names:
            self.global_writes.add(name)
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

    def _literal_value(self, value):
        self.visit(value)
        self.contexts[value] = self.contexts.get(value, frozenset()) | self._origins(value)

    def visit_Tuple(self, node):
        for value in node.elts:
            self._literal_value(value)

    visit_List = visit_Tuple

    def visit_Dict(self, node):
        for key, value in zip(node.keys, node.values):
            if key is not None:
                self.visit(key)
            self._literal_value(value)

    def visit_Global(self, node):
        self.global_names.update(node.names)
        for name in node.names:
            self._set(name, _UNKNOWN_ORIGINS, assignment=False)

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
            if isinstance(target, (ast.Tuple, ast.List)):
                self._assign_literal(target, node.value)
                continue
            # The constructor reader already proved these stable module seeds.
            seed = self.module_seeds.get(target.id, origins) if isinstance(target, ast.Name) else origins
            self._bind(target, seed)

    def _assign_literal(self, target, value):
        if isinstance(target, (ast.Tuple, ast.List)):
            if (isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts)
                    and not any(isinstance(item, ast.Starred) for item in target.elts + value.elts)):
                for item, element in zip(target.elts, value.elts):
                    self._assign_literal(item, element)
                return
            # Unresolved shapes cannot prove that a visible guard disappeared.
            for item in ast.walk(value):
                self.released.update(name for kind, name in self.contexts.get(item, ()) if kind == "guard")
        self._bind(target, self.contexts.get(value, self._origins(value)))

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
        self.visit(node.target)
        receiver = self._origins(node.target)
        self.visit(node.value)
        namespaces = [name for kind, name in receiver if kind == "namespace"]
        if namespaces:
            for namespace in namespaces:
                keys = (self._namespace_write_keys(namespace, "__ior__", [node.value], [])
                        if isinstance(node.op, ast.BitOr) else _NAMESPACE_KEYS[namespace])
                self._replace_namespace(namespace, keys, node)
            self._bind(node.target, receiver)
        else:
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
            self._set(name, frozenset({("module", alias.name)}) if alias.name in {"threading", "builtins", "sys"} else _UNKNOWN_ORIGINS)

    def visit_ImportFrom(self, node):
        if any(alias.name == "*" for alias in node.names):
            self.bindings.clear()
            return
        for alias in node.names:
            origins = (frozenset({("builtin", alias.name)}) if node.level == 0 and node.module == "builtins"
                       and alias.name in _BUILTINS else _UNKNOWN_ORIGINS)
            if node.level == 0 and node.module == "sys" and alias.name == "modules":
                origins = frozenset({("namespace", "imports")})
            self._set(alias.asname or alias.name, origins)

    def visit_Attribute(self, node):
        self.visit(node.value)
        if isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr in _LOCK_FACTORIES:
            if ("module", "threading") in self._origins(node.value):
                self.replaced.add((node.attr, self._effect_position(node)))
        if (isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr == "modules"
                and ("module", "sys") in self._origins(node.value)):
            self._replace_namespace("imports", {"threading"}, node)

    def visit_Subscript(self, node):
        self.visit(node.value)
        namespace = self._origins(node.value)
        self.visit(node.slice)
        selected = scopes.literal_index_values(node.value, node.slice)
        if selected is not None:
            values, exact = selected
            if not exact:
                values = [part for value in values for part in ast.walk(value)]
            self.expression_origins[node] = frozenset().union(
                *(self.contexts.get(value, self._origins(value)) for value in values)) | (frozenset() if exact else _UNKNOWN_ORIGINS)
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            for kind, name in namespace:
                if kind == "namespace":
                    keys = {node.slice.value} & _NAMESPACE_KEYS[name] if isinstance(node.slice, ast.Constant) else _NAMESPACE_KEYS[name]
                    self._replace_namespace(name, keys, node)

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
                if scopes.callable_is_deferred(value):
                    self.expression_origins[node] = self.expression_origins.get(node, frozenset()) | {("deferred", value)}
                returned = self._invoke_callable(value, actual, keywords, any(isinstance(arg, ast.Starred) for arg in node.args),
                                                 self._effect_position(node))
                if returned:
                    self.expression_origins[node] = self.expression_origins.get(node, frozenset()) | returned
        lazy = any(("builtin", name) in callee for name in _LAZY_CALLBACKS)
        if lazy and len(actual) >= 2:
            # Origins contain only AST identities. Joining nested runtime-like
            # descriptors would grow forever when a loop wraps its own iterator.
            incoming = {"callback": actual[0]}
            incoming.update((f"input{index}", value) for index, value in enumerate(actual[1:]))
            for index, value in enumerate(node.args[1:]):
                elements = (frozenset().union(*(self.contexts.get(item, self._origins(item)) for item in value.elts))
                            if isinstance(value, (ast.Tuple, ast.List)) and value.elts else _UNKNOWN_ORIGINS)
                incoming[f"element{index}"] = elements
            previous = self.iterator_calls.get(node)
            self.iterator_calls[node] = _merged_origins(previous, incoming) if previous else incoming
            self.expression_origins[node] = frozenset({("callback_iterator", node)})
        if ("builtin", "vars") in callee and len(actual) == 1 and ("module", "threading") in actual[0]:
            self.expression_origins[node] = frozenset({("namespace", "threading")})
        if any(("builtin", name) in callee for name in _ITERATOR_CONSUMERS):
            if actual:
                self._consume_values(node, actual[0])
        elif ("builtin", "iter") in callee:
            if len(actual) == 1 and not keywords and not any(isinstance(arg, ast.Starred) for arg in node.args):
                self.expression_origins[node] = actual[0]
            else:
                self._decline_mutex_proof()
        elif not lazy and not any(kind == "callable" for kind, _ in callee):
            for origins in actual + list(keywords.values()):
                # An opaque wrapper may retain/return an iterator. Expanding its
                # body now would invent both consumption and its mutex state.
                self._escape_deferred(origins)
        if any(kind == "acquire" for kind, _ in callee):
            self.contexts[node] = self.contexts.get(node, frozenset()) | self.held
        self.released.update(name for kind, name in callee if kind == "release")
        for kind, value in callee:
            if kind == "namespace_method":
                namespace, operation = value
                self._replace_namespace(namespace, self._namespace_write_keys(namespace, operation, node.args, node.keywords), node)
        if any(("builtin", name) in callee and len(node.args) == arity for name, arity in _ATTRIBUTE_BUILTINS.items()):
            attribute = node.args[1]
            if ("module", "threading") in actual[0] and isinstance(attribute, ast.Constant):
                if isinstance(attribute.value, str) and attribute.value in _LOCK_FACTORIES:
                    self.replaced.add((attribute.value, self._effect_position(node)))
            if ("module", "sys") in actual[0] and isinstance(attribute, ast.Constant) and attribute.value == "modules":
                self._replace_namespace("imports", {"threading"}, node)

    def _replace_namespace(self, namespace, keys, node):
        effects = {"@threading-import"} if namespace == "imports" and keys else keys
        self.replaced.update((name, self._effect_position(node)) for name in effects)

    def _namespace_write_keys(self, namespace, operation, args, keywords):
        affected = _NAMESPACE_KEYS[namespace]
        if operation in {"pop", "setdefault", "__delitem__", "__setitem__"}:
            if args and isinstance(args[0], ast.Constant):
                return {args[0].value} & affected
            return affected
        if operation not in {"update", "__ior__", "__init__"}:
            return affected
        keys = {keyword.arg for keyword in keywords}
        if None in keys or len(args) > 1:
            return affected
        if args:
            value = args[0]
            if not isinstance(value, ast.Dict) or any(not isinstance(key, ast.Constant) for key in value.keys):
                return affected
            keys.update(key.value for key in value.keys)
        return keys & affected

    def _escape_deferred(self, origins):
        if any(kind in _LAZY_KINDS for kind, _ in origins):
            self._decline_mutex_proof()

    def visit_Return(self, node):
        if node.value is not None:
            self.visit(node.value)
            self._return_deferred(self._origins(node.value), node)

    def _return_deferred(self, origins, node):
        values = frozenset((kind, value) for kind, value in origins if kind in _LAZY_KINDS)
        if values:
            # Creation/return does not consume a body. Its escaped closure and
            # iterator state are unsupported at a later consumer, so carry an
            # uncertainty marker instead of guessing an eager execution edge.
            self.returned_origins |= values | {("uncertain_iterator", node)}

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

    def visit_BoolOp(self, node):
        self.expression_origins[node] = self._conditional_steps(node.values[:1], node.values[1:])

    def visit_Compare(self, node):
        self._conditional_steps([node.left] + node.comparators[:1], node.comparators[1:])

    def visit_Assert(self, node):
        # Assertions may be disabled; the message only runs on failure.
        self._conditional_steps([], [node.test] + ([node.msg] if node.msg is not None else []))

    def _conditional_steps(self, mandatory, conditional):
        origins = frozenset()
        for value in mandatory:
            self.visit(value)
            origins |= self._origins(value)
        for value in conditional:
            skipped = dict(self.bindings)
            self.visit(value)
            origins |= self._origins(value)
            self.bindings = _merged_origins(skipped, self.bindings)
        return origins

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
                self._bind_iteration(node)
            possible = self._body_origins(node.body)
            carried = _merged_origins(entry, possible, self.bindings)
            if carried == entry:
                break
            entry = carried
        self._body(node.orelse)
        self.bindings = _merged_origins(before, possible, self.bindings)

    def visit_For(self, node):
        self.visit(node.iter)
        self._consume_values(node, self._origins(node.iter))
        # An iteration variable shadows a module object in the body; a loop
        # that never runs leaves its earlier binding possible afterwards.
        before = dict(self.bindings)
        self._bind_iteration(node)
        self._loop(node, before)

    visit_AsyncFor = visit_For

    def _bind_iteration(self, node):
        if isinstance(node.iter, (ast.Tuple, ast.List)) and node.iter.elts:
            before, outcomes = dict(self.bindings), []
            for value in node.iter.elts:
                self.bindings = dict(before)
                self._assign_literal(node.target, value)
                outcomes.append(self.bindings)
            self.bindings = _merged_origins(*outcomes)
        else:
            self._bind(node.target)

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

    def visit_TypeAlias(self, node):
        self._bind(node.name)

    def visit_Lambda(self, node):
        self._record_callable(node)

    def _record_callable(self, node):
        # Defaults capture values now; free local names remain late-bound in
        # this lexical scope when a visible callable value is called later.
        positional = list(node.args.posonlyargs) + list(node.args.args)
        defaults = {}
        values = dict(zip(node.args.defaults, positional[len(positional) - len(node.args.defaults):]))
        values.update((value, param) for param, value in zip(node.args.kwonlyargs, node.args.kw_defaults) if value is not None)
        decorators = []
        for value in scopes.definition_inputs(node):
            self.visit(value)
            if value in getattr(node, "decorator_list", ()):
                decorators.append(self._origins(value))
            if value in values:
                defaults[values[value].arg] = self._origins(value)
        previous = self.callable_values.get(node, ({}, self.local_names))[0]
        self.callable_values[node] = (_merged_origins(previous, defaults) if previous else defaults, self.local_names)
        self._apply_decorators(node, decorators)

    def _apply_decorators(self, node, decorators):
        if len(decorators) > 1:
            # Applying the next decorator to a transformed value would need a
            # return-value interpreter. Do not invent positive mutex evidence.
            self._decline_mutex_proof()
        for origins in reversed(decorators):
            # External calls remain an indirect-effect review limit. A visible
            # decorator, however, has a body whose effects execute right here.
            if any(kind == "callable" for kind, _ in origins):
                self._apply_callbacks(node, origins, [frozenset({("callable", node)})])

    def _apply_callbacks(self, site, origins, actual):
        for kind, value in origins:
            if kind == "global":
                continue
            if kind != "callable" or scopes.callable_is_deferred(value):
                self._decline_mutex_proof()
                continue
            self.contexts[site] = self.contexts.get(site, frozenset()) | {("callback_body", value)}
            self._invoke_callable(value, actual, {}, False, self._effect_position(site))

    def visit_GeneratorExp(self, node):
        self.visit(node.generators[0].iter)
        self.callable_values[node] = ({}, self.local_names)

    def _consume_values(self, node, origins):
        for kind, value in origins:
            if kind in {"deferred", "uncertain_iterator"}:
                self._decline_mutex_proof()
            elif kind == "generator":
                self.contexts[node] = self.contexts.get(node, frozenset()) | {("consume", value)}
                _, closure_names = self.callable_values.get(value, ({}, frozenset()))
                visible = self.bindings if self.class_outer is None else self.class_outer
                outer = dict(visible if self.module else self.global_bindings)
                outer.update((name, visible.get(name, _UNKNOWN_ORIGINS)) for name in closure_names)
                child = self._child(outer, local_names=closure_names, execution_at=self._effect_position(node))
                child._comprehension(value, outer_evaluated=True)
            elif kind == "callback_iterator":
                if value in self.active_iterators:
                    self._decline_mutex_proof()
                    continue
                self.active_iterators.add(value)
                try:
                    inputs = self.iterator_calls.get(value, {})
                    for name, items in inputs.items():
                        if name.startswith("input"):
                            self._consume_values(node, items)
                    actual = [inputs.get(f"element{index}", _UNKNOWN_ORIGINS) for index in range(len(value.args) - 1)]
                    self._apply_callbacks(node, inputs.get("callback", _UNKNOWN_ORIGINS), actual)
                finally:
                    self.active_iterators.remove(value)

    def _comprehension(self, node, outer_evaluated=False):
        if not outer_evaluated:
            self.visit(node.generators[0].iter)
        targets = {item.id for generator in node.generators for item in ast.walk(generator.target)
                   if isinstance(item, ast.Name)}
        outer = self.bindings if self.class_outer is None else self.class_outer
        child = self._child(outer, local_names=self.local_names | targets)
        for index, generator in enumerate(node.generators):
            if index:
                child.visit(generator.iter)
            child._bind_iteration(generator)
            child._body(generator.ifs)
        child._body([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt])
        for item in scopes.module_scope_nodes(ast.Module(body=[node], type_ignores=[])):
            if isinstance(item, ast.NamedExpr) and isinstance(item.target, ast.Name):
                name = item.target.id
                self._set(name, self._origins(item.target) | child.bindings.get(name, _UNKNOWN_ORIGINS))

    visit_ListComp = _comprehension
    visit_SetComp = _comprehension
    visit_DictComp = _comprehension

    def _invoke_callable(self, node, actual, keywords, unpacked, position):
        # These call shapes cannot supply positive evidence of safe retention.
        # Async/generator creation itself defers the body; awaiting/consuming it
        # is outside this synchronous call expansion.
        variadic = {arg.arg for arg in (node.args.vararg, node.args.kwarg) if arg is not None}
        body = [node.body] if isinstance(node, ast.Lambda) else node.body
        reads_variadic = bool(variadic) and any(
            isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load) and item.id in variadic
            for item in scopes.module_scope_nodes(ast.Module(body=body, type_ignores=[])))
        if (node in self.active_calls or unpacked or None in keywords
                or getattr(node, "decorator_list", ()) or reads_variadic):
            self._decline_mutex_proof()
            return
        if scopes.callable_is_deferred(node):
            return
        positional = list(node.args.posonlyargs) + list(node.args.args)
        defaults, closure_names = self.callable_values.get(node, ({}, frozenset()))
        visible = self.bindings if self.class_outer is None else self.class_outer
        outer = dict(visible if self.module else self.global_bindings)
        outer.update((name, visible.get(name, _UNKNOWN_ORIGINS)) for name in closure_names)
        child = self._child(outer, local_names=closure_names | scopes.callable_bound_names(node), execution_at=position)
        params = positional + list(node.args.kwonlyargs) + [p for p in (node.args.vararg, node.args.kwarg) if p]
        for param in params:
            child._set(param.arg, defaults.get(param.arg, _UNKNOWN_ORIGINS))
        for param, origins in zip(positional, actual):
            child._set(param.arg, origins)
        for name, origins in keywords.items():
            if name is not None:
                child._set(name, origins)
        nonlocals = set() if isinstance(node, ast.Lambda) else {
            name for item in scopes.module_scope_nodes(ast.Module(body=node.body, type_ignores=[]))
            if isinstance(item, ast.Nonlocal) for name in item.names}
        local = scopes.callable_bound_names(node) - nonlocals
        for name in local - {param.arg for param in params}:
            child._set(name, _UNKNOWN_ORIGINS)
        self.active_calls.add(node)
        try:
            child._body([node.body] if isinstance(node, ast.Lambda) else node.body)
            if isinstance(node, ast.Lambda):
                child._return_deferred(child._origins(node.body), node)
            self._apply_scope_effects(child, nonlocals)
        finally:
            self.active_calls.remove(node)
        return child.returned_origins

    def visit_ClassDef(self, node):
        decorators = []
        for value in scopes.definition_inputs(node):
            self.visit(value)
            if value in node.decorator_list:
                decorators.append(self._origins(value))
        outer = self.bindings if self.class_outer is None else self.class_outer
        child = self._child(outer, class_outer=dict(outer), local_names=self.local_names)
        child._body(node.body)
        nonlocals = {name for item in scopes.module_scope_nodes(ast.Module(body=node.body, type_ignores=[]))
                     if isinstance(item, ast.Nonlocal) for name in item.names}
        self._apply_scope_effects(child, nonlocals)
        self._apply_decorators(node, decorators)
        self._set(node.name, _UNKNOWN_ORIGINS)

    def _apply_scope_effects(self, child, nonlocals):
        for name in nonlocals:
            self._set(name, child.bindings.get(name, _UNKNOWN_ORIGINS))
        if child.global_writes:
            self._decline_mutex_proof()


def _lock_interference(tree, threading_modules, lock_names, contexts, builtin_origins, skipped):
    """Visible factory replacement/release, with ordinary lexical alias bindings."""
    known = {name: frozenset({("module", "threading")}) for name in threading_modules}
    known.update((name, frozenset({("guard", name)})) for name in lock_names)
    known.update(builtin_origins)
    replaced, released = set(), set()
    module = _LockInterference(known, replaced, released, contexts, module=True, skipped=skipped)
    module._body(tree.body)
    bindings = scopes.module_bindings(tree)
    module_globals = {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}
    wildcard = any(isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
                   for node in scopes.module_scope_nodes(tree))
    stable = {name for name, count in bindings.items() if count == 1} - module_globals
    for name, origins in module.bindings.items():
        if name in stable and any(kind in {"callable", "release", "acquire", "guard", "generator", "deferred", "namespace", "namespace_method", "callback_iterator"} for kind, _ in origins):
            known[name] = origins
    for builtin in _BUILTINS:
        if builtin not in bindings and builtin not in module_globals and not wildcard:
            known[builtin] = frozenset({("builtin", builtin)})
    for fn, enclosing in scopes.function_scopes(tree):
        global_names = {name for node in scopes.module_scope_nodes(ast.Module(body=fn.body, type_ignores=[]))
                        if isinstance(node, ast.Global) for name in node.names}
        local = (scopes.bound_names(fn) | enclosing) - global_names
        _LockInterference({name: origins for name, origins in known.items() if name not in local},
                          replaced, released, contexts, local_names=local, global_bindings=known,
                          callable_values=module.callable_values, iterator_calls=module.iterator_calls, skipped=skipped)._body(fn.body)
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


def _module_locks(tree, contexts, skipped) -> dict:
    """Stable names bound once to an imported threading Lock/RLock constructor.

    A global declaration anywhere keeps a name out of this bounded proof: it
    permits a later call to replace the object the name originally identified.
    """
    # An unknown export can replace a factory alias, the threading module or
    # an already-created guard. Without resolving that surface none is stable.
    last_wildcard = max(((node.lineno, node.col_offset) for node in scopes.module_scope_nodes(tree)
                         if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)), default=(0, 0))
    bindings = scopes.module_bindings(tree)
    global_names = {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}
    stable = {name for name, count in bindings.items() if count == 1} - global_names
    threading_modules, lock_factories, builtin_origins = {}, {}, {}
    for stmt in tree.body:
        if (stmt.lineno, stmt.col_offset) <= last_wildcard:
            continue
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == "threading":
                    threading_modules[alias.asname or "threading"] = (stmt.lineno, stmt.col_offset)
                elif alias.name in {"builtins", "sys"}:
                    builtin_origins[alias.asname or alias.name] = frozenset({("module", alias.name)})
        elif isinstance(stmt, ast.ImportFrom) and stmt.module == "threading" and stmt.level == 0:
            for alias in stmt.names:
                if alias.name in _LOCK_FACTORIES:
                    position = (stmt.lineno, stmt.col_offset)
                    lock_factories[alias.asname or alias.name] = _Factory(alias.name, position, position)
        elif isinstance(stmt, ast.ImportFrom) and stmt.module == "builtins" and stmt.level == 0:
            for alias in stmt.names:
                if alias.name in _BUILTINS:
                    builtin_origins[alias.asname or alias.name] = frozenset({("builtin", alias.name)})
        elif isinstance(stmt, ast.ImportFrom) and stmt.module == "sys" and stmt.level == 0:
            for alias in stmt.names:
                if alias.name == "modules":
                    builtin_origins[alias.asname or alias.name] = frozenset({("namespace", "imports")})
    threading_modules = {name: position for name, position in threading_modules.items() if name in stable}
    lock_factories = {name: factory for name, factory in lock_factories.items() if name in stable}
    threading_modules = _module_aliases(tree, threading_modules, stable)
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
        origin = (_Factory(func.attr, module_at=threading_modules[func.value.id])
                  if isinstance(func, ast.Attribute) else lock_factories[func.id])
        if origin.name == "Lock" and not _argument_free_call(value):
            continue
        mutex = _Mutex(origin.name, (value.lineno, value.col_offset))
        locks.update((t.id, (origin, mutex)) for t in targets if isinstance(t, ast.Name) and t.id in stable)
    locks = _module_aliases(tree, locks, stable)
    replaced, released = _lock_interference(tree, threading_modules, set(locks), contexts, builtin_origins, skipped)
    released_identities = {locks[name][1].identity for name in released}
    return {name: mutex for name, (origin, mutex) in locks.items()
            if mutex.identity not in released_identities
            and not any(factory == "@threading-import" and (origin.module_at is None or position < origin.module_at)
                        for factory, position in replaced)
            and not any(factory == origin.name and (origin.imported_at is None or position < origin.imported_at)
                        for factory, position in replaced)}


def mutation_guards(tree, candidate_names):
    """Return validated module mutexes and held identities per direct write."""
    contexts = {}
    skipped = scopes.deferred_expressions(tree)
    locks = _module_locks(tree, contexts, skipped)
    # Lexical with evidence cannot certify a plain mutex that may already have
    # been acquired manually. Its call may succeed even in nonblocking mode.
    prior = set()
    for node, held in contexts.items():
        if isinstance(node, ast.Call):
            entered = {locks[name].identity for kind, name in held if kind == "guard" and name in locks}
            prior.update(locks[name].identity for kind, name in contexts.get(node.func, ())
                         if kind == "acquire" and name in locks and locks[name].factory == "Lock"
                         and locks[name].identity not in entered)
    locks = {name: mutex for name, mutex in locks.items() if mutex.identity not in prior}
    invalid_acquires = {node for node in contexts if execution.blocking_acquire(node, locks, contexts)}
    guarded = defaultdict(list)
    for fn, _ in scopes.function_scopes(tree):
        for node, held in execution.guarded_nodes(fn.body, locks, contexts, invalid_acquires, skipped):
            for name in execution.mutated_names(node, candidate_names, contexts):
                guarded[name].append(held)
    return locks, guarded
