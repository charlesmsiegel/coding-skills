"""Lexical binding ownership and receiver derivation for coupling analysis.

One inventory owns every write and its extraction/import metadata. Owned-data
derivation and original-receiver identity ask different questions of that same
inventory; neither executes control flow, so every possible write must qualify.
"""

import ast
from collections import defaultdict


def _method_nodes(nodes):
    """A method's nodes in source order, nested functions and lambdas included.

    A nested class is skipped: it is analysed as a class of its own.
    """
    for node in nodes:
        yield node
        if isinstance(node, ast.ClassDef):
            continue
        yield from _method_nodes(ast.iter_child_nodes(node))


_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
           ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
# Builtins whose result is made of their first argument's elements.
_TRANSPARENT = frozenset({"sorted", "list", "tuple", "set", "frozenset", "reversed", "iter"})
_FOREIGN = None  # a binding whose value comes from outside: a parameter, `with ... as`, ...


def _target_names(target):
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))]


class _Derivation:
    """Which names in one method hold the object's own data.

    `settings = self._settings()`, `request = self.job.request`, `ctx, job =
    self.ctx, self.job`, `for job in self._jobs.values()`, and what is built from
    them (`finished = sorted(job for job in self._jobs.values() ...)`): reading
    these is reading self. A name is resolved to the scope that binds it — a
    lambda's `job` parameter is not the loop's `job` — and it is self-derived
    only when every binding in that scope is.
    """

    def __init__(self, func, receiver: str | None) -> None:
        self.parent: dict[int, ast.AST] = {}
        for node in _method_nodes([func]):
            for child in ast.iter_child_nodes(node):
                self.parent[id(child)] = node
        self.receiver_key = (id(func), receiver) if receiver else None
        self.bindings: dict[tuple, list] = defaultdict(list)
        self.nonlocals: dict[tuple, ast.AST] = {}
        self.globals: dict[tuple, tuple] = {}
        self.global_scope = id(func) if isinstance(func, ast.Module) else None
        self.module_keys: set = set()
        self.iterated: set = set()  # id() of the iterables a loop or comprehension binds from
        self.unpacked: set = set()  # binding keys that receive elements, rather than the whole value
        receiver_arg = ([*func.args.posonlyargs, *func.args.args] or [None])[0] if receiver else None
        if self.receiver_key:
            self.bindings[self.receiver_key] = []
        for node in _method_nodes([func]):
            self._collect(node, receiver_arg)
        # Outside-owner fallback is uncertainty, not an actual write. Preserve
        # the latter so enclosing inventories can link class nonlocals too.
        self.nonlocal_writes = {key: list(self.bindings.get(key, [])) for key in self.nonlocals}
        # A read-only global declaration in a nested function does not create
        # a module value (in particular, it cannot shadow a builtin there).
        if isinstance(func, ast.Module):
            for key in set(self.globals.values()):
                if not self.bindings[key]:
                    self.bindings.pop(key)
        self._link_nonlocal_writes()
        self.state = {key: (bool(values) or key == self.receiver_key) and all(v is not _FOREIGN for v in values)
                      for key, values in self.bindings.items()}
        changed = True
        while changed:  # greatest fixpoint: demote any name with a binding not derived
            changed = False
            for key, values in self.bindings.items():
                if self.state[key] and key != self.receiver_key and not all(self.derived(v) for v in values):
                    self.state[key] = False
                    changed = True

    def _scope_of(self, node):
        current = self.parent.get(id(node))
        while current is not None and not isinstance(current, _SCOPES):
            current = self.parent.get(id(current))
        return current

    def _pattern_subject(self, node, whole=False):
        """The matched value; structural patterns extract rather than preserve it."""
        while node is not None and not isinstance(node, ast.Match):
            if whole and isinstance(node, ast.pattern) and not isinstance(node, (ast.MatchAs, ast.MatchOr)):
                return None
            node = self.parent.get(id(node))
        return node.subject if node is not None else None

    def _bind(self, scope, name, value):
        if scope is not None:
            self.bindings[self._binding_key(scope, name)].append(value)

    def _binding_key(self, scope, name):
        key = (id(scope), name)
        return self.globals.get(key, key)

    def _link_nonlocal_writes(self):
        """A closure writes its enclosing binding, not a new local copy.

        Resolve after the inventory is complete: an enclosing assignment may
        appear later. A read-only nonlocal declaration contributes no write.
        """
        for key, scope in self.nonlocals.items():
            enclosing = self._scope_of(scope)
            while enclosing is not None and not isinstance(enclosing, ast.Module):
                owner = (id(enclosing), key[1])
                if owner in self.globals:
                    self.bindings[key].append(_FOREIGN)  # nonlocal cannot cross a global boundary
                    break
                if owner in self.bindings and owner not in self.nonlocals:
                    self.bindings[owner].extend(self.bindings.pop(key, []))
                    if key in self.unpacked:
                        self.unpacked.add(owner)
                    if key in self.module_keys:
                        self.module_keys.add(owner)
                        self.module_keys.discard(key)
                    break
                enclosing = self._scope_of(enclosing)
            else:
                self.bindings[key].append(_FOREIGN)  # the owner is outside this inventory

    def _assign(self, scope, target, value):
        if isinstance(target, ast.Name):
            self._bind(scope, target.id, value)
        elif (isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List))
              and len(target.elts) == len(value.elts)
              and not any(isinstance(e, ast.Starred) for e in (*target.elts, *value.elts))):
            for sub_target, sub_value in zip(target.elts, value.elts):
                self._assign(scope, sub_target, sub_value)
        elif not isinstance(target, (ast.Attribute, ast.Subscript)):
            for name in _target_names(target):  # a part of the value is derived when it is
                self._bind(scope, name, value)
                if scope is not None:
                    self.unpacked.add((id(scope), name))

    def _collect(self, node, receiver_arg) -> None:
        if isinstance(node, ast.arg):  # the node itself, so its annotation can be read
            if node is not receiver_arg:
                self._bind(self._scope_of(node), node.arg, node)
            return
        if isinstance(node, (ast.comprehension, ast.For, ast.AsyncFor)):
            scope = self._scope_of(node)
            self.iterated.add(id(node.iter))
            for name in _target_names(node.target):
                self._bind(scope, name, node.iter)
            return
        scope = self._scope_of(node)
        if isinstance(node, ast.NamedExpr):
            # A walrus inside comprehensions writes the enclosing lexical cell;
            # a nested lambda or function still owns its own assignments.
            while isinstance(scope, _COMPREHENSIONS):
                scope = self._scope_of(scope)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                self._assign(scope, target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            self._assign(scope, node.target, node.value)
        elif (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
              and isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))):
            # Only function annotations own a local cell; namespace annotations do not bind.
            self.bindings.setdefault(self._binding_key(scope, node.target.id), [])
        elif isinstance(node, ast.Delete) and isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Deletion owns a local cell without assigning it a foreign value.
            # Attribute/subscript targets contain no Name with Del context.
            for target in node.targets:
                for name in _target_names(target):
                    self.bindings.setdefault(self._binding_key(scope, name), [])
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                key = self._binding_key(scope, alias.asname or alias.name.split(".")[0])
                self.bindings[key].append(node)
                self.module_keys.add(key)
        elif isinstance(node, ast.AugAssign):
            for name in _target_names(node.target):
                self._bind(scope, name, _FOREIGN)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            for name in _target_names(node.optional_vars):
                self._bind(scope, name, _FOREIGN)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            self._bind(scope, node.name, _FOREIGN)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            # Retain the pattern's narrowing evidence as well as its subject.
            # Whole-value identity and extracted data ask different questions
            # of this same capture; neither should erase the other's evidence.
            self._bind(scope, node.name, node)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            self._bind(scope, node.rest, node)
        elif isinstance(node, ast.Nonlocal):
            for name in node.names:
                self.nonlocals[(id(scope), name)] = scope
        elif isinstance(node, ast.Global):
            for name in node.names:
                key, owner = (id(scope), name), (self.global_scope, name)
                self.globals[key] = owner
                # Keep the lexical boundary visible to scope readers, while
                # every actual global write shares one canonical origin.
                self.bindings[key] = self.bindings[owner]
        elif isinstance(node, ast.ClassDef):
            self._bind(scope, node.name, node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._bind(self._scope_of(node), node.name, _FOREIGN)

    def resolve(self, name_node):
        """The (scope, name) a Name use refers to inside the method, or None if free."""
        child, current = name_node, self.parent.get(id(name_node))
        from_first_iterable = False
        while current is not None:
            if isinstance(current, ast.comprehension):
                from_first_iterable = child is current.iter
            elif isinstance(current, _SCOPES):
                # A comprehension's first iterable is evaluated in the enclosing scope.
                skip = (isinstance(current, _COMPREHENSIONS) and from_first_iterable
                        and child is current.generators[0])
                key = (id(current), name_node.id)
                if not skip and key in self.globals:
                    return self.globals[key]
                if not skip and key in self.bindings:
                    return key
                from_first_iterable = False
            child, current = current, self.parent.get(id(current))
        return None

    def binding_identity(self, expr, origin, global_namespace=None, values_at=None) -> bool:
        """A name alias grounded in one binding, never merely its data.

        The caller checks every write to the origin separately. Cycles among
        other names, extraction, iteration, and even one unknown binding cannot
        certify identity. Unlike owned-data derivation this is a grounded proof.
        """
        # A flat method can contain hundreds of aliases without a deeply
        # nested AST. Walk their proof graph without consuming Python frames.
        pending = [(expr, False)]
        active, proven = set(), set()
        while pending:
            value, leaving = pending.pop()
            if isinstance(value, ast.MatchAs):
                value = self._pattern_subject(value, whole=True)
            if not isinstance(value, ast.Name) or id(value) in self.iterated:
                return False
            key = self.resolve(value)
            if key is None or key in self.unpacked:
                return False
            if (global_namespace is not None and key[0] == self.global_scope
                    and not global_namespace(self, value, key)):
                return False
            if key == origin or key in proven:
                continue
            if leaving:
                active.remove(key)
                proven.add(key)
                continue
            if key in active:
                return False
            values = values_at(key, value) if values_at is not None else self.bindings.get(key, [])
            if not values:
                return False
            active.add(key)
            pending.append((value, True))
            pending.extend((binding, False) for binding in values)
        return True

    def module_namespace(self, expr, imported_namespace=None, global_namespace=None) -> bool:
        """A module alias stays exempt only while every origin write is safe.

        Import writes ground the same iterative identity proof as the receiver.
        A possible foreign replacement in that binding or any alias still wins;
        merely encountering one import cannot certify the other writes.
        """
        return any(self.binding_identity(expr, origin, global_namespace, self.namespace_values) and all(
            isinstance(value, ast.Import)
            or (isinstance(value, ast.ImportFrom) and imported_namespace is not None
                and imported_namespace(value, origin[1])) or self.derived(value)
            or self.binding_identity(value, origin, global_namespace, self.namespace_values)
            for value in self.namespace_values(origin, expr))
            for origin in self.module_keys)

    def _direct_statement(self, expression, scope):
        current = expression
        while current is not None:
            parent = self.parent.get(id(current))
            if parent is scope:
                return current
            current = parent
        return None

    def namespace_values(self, key, expression):
        """A narrow read-position view over this same binding inventory.

        Only a direct read after a completed direct import can ignore direct
        own writes after that read. Reads in loops/branches and writes from
        nested, conditional or ambient owners keep the all-writes policy.
        """
        values = self.bindings.get(key, [])
        scope = self._scope_of(expression)
        read = self._direct_statement(expression, scope) if scope is not None else None
        if not isinstance(read, (ast.Assign, ast.AnnAssign, ast.Return, ast.Expr)):
            return values
        if not any(isinstance(value, (ast.Import, ast.ImportFrom))
                   and self.parent.get(id(value)) is scope
                   and (value.end_lineno, value.end_col_offset) < (read.lineno, read.col_offset)
                   for value in values):
            return values
        visible = []
        for value in values:
            statement = self._direct_statement(value, scope) if isinstance(value, ast.AST) else None
            if (self._scope_of(value) is scope
                    and isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom))
                    and (read.end_lineno, read.end_col_offset) < (statement.lineno, statement.col_offset)):
                continue
            visible.append(value)
        return visible

    def derived(self, expr) -> bool:
        """True when ``expr`` navigates from self or from a self-derived name."""
        while True:
            if isinstance(expr, (ast.Attribute, ast.Subscript)):
                expr = expr.value
            elif isinstance(expr, ast.Call):
                if (isinstance(expr.func, ast.Name) and expr.func.id in _TRANSPARENT
                        and expr.args and self.resolve(expr.func) is None):
                    expr = expr.args[0]
                else:
                    expr = expr.func
            elif isinstance(expr, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
                expr = expr.elt
            elif isinstance(expr, (ast.MatchAs, ast.MatchStar, ast.MatchMapping)):
                # Captures extract the matched subject's data. Keep the pattern
                # as the binding value: extraction does not prove identity.
                expr = self._pattern_subject(expr)
                if expr is None:
                    return False
            elif isinstance(expr, ast.Name):
                key = self.resolve(expr)
                return key is not None and self.state.get(key, False)
            else:
                return False
