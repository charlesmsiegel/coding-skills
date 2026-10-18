"""Lexical import uses in Python expressions and annotation type positions.

This is the one reference reader used by find_dead_code. It owns progressive
namespace origins and deferred enclosing lookups; the caller owns dead-code
policy, confidence, issue formatting and framework registration exemptions.

Origins describe possible bindings: skipped loop/filter paths retain the old
binding, and finally transforms captured exits. Deferred readers retain enclosing
namespaces, while a store in their own invocation wins over a later import.
This is a file-local reading without a call-order simulation. Unknown loop trip
counts use a fixed point over finite typing origins; literal sequences retain
their exact trip count. Only continuing branches contribute to later origins.
"""

import ast
import symtable
from collections import ChainMap
from contextlib import ExitStack, contextmanager


_FALLBACK = object()
_TERMINATED = object()
_TYPING_ORIGINS = {module + suffix for module in ("typing", "typing_extensions")
                   for suffix in ("", ".cast", ".Literal", ".Annotated")}

def dotted_name(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def function_bindings(source: str, filename: str) -> dict[tuple[int, str], tuple[set[str], set[str]]]:
    """Use Python's lexical scope analysis rather than guessing locals from
    traversal order. Classes deliberately remain progressive namespaces."""
    try:
        tables = [symtable.symtable(source, filename, "exec")]
    except SyntaxError:
        # ast.parse accepts some incomplete snippets that symbol analysis
        # rejects. They still get the existing structural/reference checks.
        return {}
    bindings = {}
    while tables:
        table = tables.pop()
        tables.extend(table.get_children())
        if table.get_type() in {"function", "class"} and table.get_name() != "lambda":
            symbols = table.get_symbols()
            bindings[table.get_lineno(), table.get_name()] = (
                {symbol.get_name() for symbol in symbols if symbol.is_local()},
                {symbol.get_name() for symbol in symbols if symbol.is_declared_global()},
            )
    return bindings


class ImportReferences(ast.NodeVisitor):
    """Read ordinary uses and type-bearing strings with their lexical origins."""

    def __init__(self, parents, bindings):
        self.parents = parents
        self.function_bindings = bindings
        self.used_names = set()
        self.value_references = set()
        self.called_functions = set()
        self.instantiated_classes = set()
        self._annotation_scopes = [("module", {}, frozenset())]
        self._deferred_casts = []
        self._deferred_annotations = []
        self._quoted_depth = 0
        self._finishing_references = False
        self._reference_only = False
        self._loop_breaks = []
        self._loop_continues = []
        self._suppressed_prefixes = []
        self._future_annotations = False
        self._expression_values = {}

    def visit_Assign(self, node: ast.Assign):
        # Python evaluates the value with the old binding, then stores each
        # target. In `cast = cast("models.Model", value)` that order decides
        # whether the first argument is a type or ordinary string data.
        self.visit(node.value)
        assignments = [self._assignment_origins(target, node.value) for target in node.targets]
        for target, origins in zip(node.targets, assignments):
            self.visit(target)
            self._store_origins(origins)

    def _binding_namespace(self, name):
        frame = self._annotation_scopes[0] if name in self._annotation_scopes[-1][2] else self._annotation_scopes[-1]
        return frame[0], frame[1]

    def _store_origins(self, assignments):
        for name, origin in assignments.items():
            self._binding_namespace(name)[1][name] = origin

    def _assignment_origins(self, target, value):
        if isinstance(target, ast.Name):
            names = self._value_names(value)
            # Other aliases have ordinary expression semantics. Collapsing
            # those origins also keeps repeated attribute stores a finite
            # domain when a loop's transfer is applied to a fixed point.
            return {target.id: frozenset(name if name in _TYPING_ORIGINS else None for name in names)} if names else {}
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts):
            return {name: origin for left, right in zip(target.elts, value.elts)
                    for name, origin in self._assignment_origins(left, right).items()}
        return {}

    def _value_names(self, node):
        names = self._expression_values.get(node)
        if names is None:
            names = self._canonical_names(node, self._annotation_origins())
        return frozenset(name if name in _TYPING_ORIGINS else "" for name in names) or frozenset({""})

    def visit_NamedExpr(self, node: ast.NamedExpr):
        self.visit(node.value)
        self._expression_values[node] = self._value_names(node.value)
        origins = self._assignment_origins(node.target, node.value)
        # Unlike a comprehension's iteration target, a walrus binds in its
        # containing non-comprehension scope (PEP 572).
        previous = self._annotation_scopes
        end = len(previous)
        while previous[end - 1][0] in {"comprehension", "generator"}:
            end -= 1
        self._annotation_scopes = previous[:end]
        try:
            self.visit(node.target)
            self._store_origins(origins)
        finally:
            self._annotation_scopes = previous

    def visit_For(self, node: ast.For | ast.AsyncFor):
        self.visit(node.iter)
        # Literal values capture before a target or body can rebind their names.
        values = ([self._assignment_origins(node.target, item) for item in node.iter.elts]
                  if isinstance(node.iter, (ast.Tuple, ast.List))
                  and not any(isinstance(item, ast.Starred) for item in node.iter.elts) else None)
        return self._visit_loop(node, node.body, values)

    def visit_While(self, node: ast.While):
        self.visit(node.test)
        return self._visit_loop(node, node.body)

    @staticmethod
    def _loop_entry(node):
        if isinstance(node, ast.While):
            return bool(node.test.value) if isinstance(node.test, ast.Constant) else None
        if isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
            if not node.iter.elts:
                return False
            return True if any(not isinstance(item, ast.Starred) for item in node.iter.elts) else None
        if isinstance(node.iter, ast.Constant) and isinstance(node.iter.value, (str, bytes)):
            return bool(node.iter.value)
        return None

    def _visit_loop(self, node, body, values=None):
        # A break skips else; a continue feeds the next iteration. Literal
        # sequences have an exact trip count. Otherwise join loop-head origins
        # until their finite typing-origin domain reaches a fixed point.
        enters = self._loop_entry(node)
        count = None
        if isinstance(node, (ast.For, ast.AsyncFor)):
            if isinstance(node.iter, (ast.Tuple, ast.List)) and not any(
                    isinstance(item, ast.Starred) for item in node.iter.elts):
                count = len(node.iter.elts)
            elif isinstance(node.iter, ast.Constant) and isinstance(node.iter.value, (str, bytes)):
                count = len(node.iter.value)
        head = self._origin_state()
        exhausted = [head] if enters is not True else []
        breaks = []
        continues = []
        self._loop_breaks.append(breaks)
        self._loop_continues.append(continues)
        try:
            iteration = 0
            while enters is not False:
                self._restore_origins(head)
                if isinstance(node, (ast.For, ast.AsyncFor)):
                    self.visit(node.target)
                    if values is not None:
                        self._store_origins(values[iteration])
                start = len(continues)
                normal = self._visit_statements(body)
                backedges = continues[start:]
                if normal:
                    backedges.append(self._origin_state())
                if not backedges:
                    break
                following = self._joined_origins(backedges)
                iteration += 1
                if count is not None:
                    # Equal states do not skip later, different literal values.
                    if iteration == count or values is None and following == head:
                        exhausted = [following]
                        break
                    head = following
                    continue
                if isinstance(node, ast.While):
                    self._restore_origins(following)
                    self.visit(node.test)
                    following = self._origin_state()
                if not (isinstance(node, ast.While) and enters is True):
                    exhausted.append(following)
                widened = self._joined_origins([head, following])
                if widened == head:
                    break
                head = widened
        finally:
            self._loop_breaks.pop()
            self._loop_continues.pop()
        continuing = list(breaks)
        if exhausted:
            self._restore_origins(self._joined_origins(exhausted))
            if self._visit_statements(node.orelse):
                continuing.append(self._origin_state())
        if not continuing:
            return _TERMINATED
        self._restore_origins(self._joined_origins(continuing))

    def visit_Break(self, node):
        if self._loop_breaks and not self._reference_only:
            self._loop_breaks[-1].append(self._origin_state())
        return _TERMINATED

    def visit_Continue(self, node):
        if self._loop_continues and not self._reference_only:
            self._loop_continues[-1].append(self._origin_state())
        return _TERMINATED

    def visit_Return(self, node):
        self.generic_visit(node)
        return _TERMINATED

    visit_Raise = visit_Return

    @staticmethod
    def _irrefutable_pattern(pattern):
        if isinstance(pattern, ast.MatchAs):
            return pattern.pattern is None or ImportReferences._irrefutable_pattern(pattern.pattern)
        return isinstance(pattern, ast.MatchOr) and any(
            ImportReferences._irrefutable_pattern(option) for option in pattern.patterns)

    def visit_Match(self, node: ast.Match):
        self.visit(node.subject)
        unmatched = [self._origin_state()]
        completed = []
        for case in node.cases:
            if not unmatched:
                break
            self._restore_origins(self._joined_origins(unmatched))
            incoming = self._origin_state()
            self.visit(case.pattern)
            if case.guard is not None:
                self.visit(case.guard)
            captured = self._origin_state()
            catches_all = self._irrefutable_pattern(case.pattern)
            guard = (bool(case.guard.value) if isinstance(case.guard, ast.Constant)
                     else True if case.guard is None else None)
            unmatched = [] if catches_all else [incoming, captured]
            if guard is not True:
                unmatched.append(captured)
            if guard is not False:
                if self._visit_statements(case.body):
                    completed.append(self._origin_state())
        continuing = [*completed, *unmatched]
        if not continuing:
            return _TERMINATED
        self._restore_origins(self._joined_origins(continuing))

    def visit_MatchAs(self, node: ast.MatchAs):
        self.generic_visit(node)
        if node.name:
            self._store_origins({node.name: None})

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node: ast.MatchMapping):
        self.generic_visit(node)
        if node.rest:
            self._store_origins({node.rest: None})

    def visit_If(self, node: ast.If):
        return self._visit_conditional(node.test, node.body, node.orelse)

    def visit_IfExp(self, node: ast.IfExp):
        self._visit_conditional(node.test, [node.body], [node.orelse])
        values = [node.body if node.test.value else node.orelse] if isinstance(node.test, ast.Constant) else [node.body, node.orelse]
        self._expression_values[node] = frozenset().union(*(self._value_names(value) for value in values))

    def _visit_conditional(self, test, body, otherwise):
        # The symbol table already establishes function-local names, including
        # unreachable declarations. Only the executed branch can replace an
        # import's progressive origin, in any scope.
        self.visit(test)
        if isinstance(test, ast.Constant):
            if not self._visit_statements(body if test.value else otherwise):
                return _TERMINATED
        else:
            return self._join_branches((body, otherwise))

    def _visit_statements(self, statements, partial=None):
        if partial is not None:
            partial_scope = tuple(id(origins) for _kind, origins, _globals in self._annotation_scopes)
            self._suppressed_prefixes.append((partial_scope, partial))
        try:
            for statement in statements:
                result = self.visit(statement)
                self._capture_prefix()
                if result is _TERMINATED:
                    return False
            return True
        finally:
            if partial is not None:
                self._suppressed_prefixes.pop()

    def _capture_prefix(self):
        """Handlers and suppressing contexts share prefixes in their own frame."""
        scope = tuple(id(origins) for _kind, origins, _globals in self._annotation_scopes)
        for scopes, prefixes in self._suppressed_prefixes:
            if scope == scopes:
                prefixes.append(self._origin_state())

    def _join_branches(self, alternatives):
        # Neither branch is known to run. Keep each possible origin; the last
        # branch visited is not evidence that the other was skipped. Restore
        # maps in place: queued deferred bodies retain these namespace objects.
        before = self._origin_state()
        branches = []
        for statements in alternatives:
            self._restore_origins(before)
            if self._visit_statements(statements):
                branches.append(self._origin_state())
        if not branches:
            return _TERMINATED
        self._restore_origins(self._joined_origins(branches))

    def visit_With(self, node):
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
        # __exit__/__aexit__ may suppress an exception raised after any body
        # prefix. Such prefixes can reach the following statement as well.
        partial = [self._origin_state()]
        scopes = tuple(id(origins) for _kind, origins, _globals in self._annotation_scopes)
        self._suppressed_prefixes.append((scopes, partial))
        try:
            self._visit_statements(node.body)
        finally:
            self._suppressed_prefixes.pop()
        self._restore_origins(self._joined_origins(partial))

    visit_AsyncWith = visit_With

    def _origin_state(self):
        return [(origins.maps[0] if isinstance(origins, ChainMap) else origins).copy()
                for _kind, origins, _globals in self._annotation_scopes]

    def _restore_origins(self, state):
        for (_kind, origins, _globals), original in zip(self._annotation_scopes, state):
            origins.clear()
            origins.update(original)

    def _joined_origins(self, branches):
        joined = []
        for index in range(len(self._annotation_scopes)):
            origins = {}
            names = set().union(*(branch[index] for branch in branches))
            for name in names:
                origins[name] = frozenset().union(*(
                    self._origin_options(branch[index].get(name, _FALLBACK)) for branch in branches))
            joined.append(origins)
        return joined

    def visit_Try(self, node):
        # An exception can leave after any prefix of the body. Handlers begin
        # from those possible bindings, not from the last handler we traversed.
        # Finally also transforms loop exits captured inside this try. Exits
        # from before it and breaks belonging to completed inner loops differ.
        break_entries = [(exits, len(exits)) for exits in (*self._loop_breaks, *self._loop_continues)]
        partial = [self._origin_state()]
        body_continues = self._visit_statements(node.body, partial)
        handler_entry = self._joined_origins(partial)
        normal_continues = body_continues and self._visit_statements(node.orelse, partial)
        continuing = [self._origin_state()] if normal_continues else []
        for handler in node.handlers if self._body_may_raise(node.body) else ():
            self._restore_origins(handler_entry)
            if handler.type is not None:
                self.visit(handler.type)
            unmatched = self._origin_state()
            if self._visit_exception_handler(handler, partial):
                continuing.append(self._origin_state())
            if isinstance(node, ast.TryStar):
                # Several except* handlers can run in order; an ordinary except
                # chooses only one. The next group may also skip earlier ones.
                handler_entry = self._joined_origins([unmatched, self._origin_state()])
            else:
                handler_entry = unmatched
            if handler.type is None:
                break
        normal = self._joined_origins(continuing) if continuing else self._origin_state()
        self._restore_origins(normal)
        if not node.finalbody:
            return None if continuing else _TERMINATED
        cleanup_continues = self._visit_cleanup(node.finalbody, partial, normal)
        if not self._reference_only:
            after = self._origin_state()
            self._reference_only = True
            try:
                for exits, start in break_entries:
                    transformed = []
                    for state in exits[start:]:
                        self._restore_origins(state)
                        if self._visit_statements(node.finalbody):
                            transformed.append(self._origin_state())
                    exits[start:] = transformed
            finally:
                self._reference_only = False
                self._restore_origins(after)
        if not continuing or not cleanup_continues:
            return _TERMINATED

    visit_TryStar = visit_Try

    @staticmethod
    def _body_may_raise(statements):
        # Only syntactically inert bodies can rule handlers out. Dynamic
        # values, imports and definitions retain the existing uncertainty.
        return any(not isinstance(node, (ast.Pass, ast.Global, ast.Nonlocal, ast.Break, ast.Continue))
                   and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)) for node in statements)

    def _visit_exception_handler(self, handler, partial):
        if handler.name:
            self._store_origins({handler.name: None})
        continues = self._visit_statements(handler.body, partial)
        if handler.name:
            # Python deletes the exception target at handler exit; a class can
            # then fall back to an enclosing binding, unlike a function local.
            kind, origins = self._binding_namespace(handler.name)
            if kind in {"module", "class"}:
                origins.pop(handler.name, None)
            else:
                origins[handler.name] = None
        return continues

    def _visit_cleanup(self, statements, partial, normal):
        if self._reference_only:
            self._restore_origins(self._joined_origins([*partial, normal]))
        continues = self._visit_statements(statements)
        after = self._origin_state()
        if not self._reference_only:
            # Finally also runs while an uncaught exception escapes. Its uses
            # count, but that path cannot supply bindings to later statements.
            # Reference-only nested cleanups are walked once, avoiding an
            # exponential second walk for deeply nested try/finally statements.
            self._reference_only = True
            self._restore_origins(self._joined_origins([*partial, normal]))
            try:
                self._visit_statements(statements)
            finally:
                self._reference_only = False
                self._restore_origins(after)
        return continues

    def visit_BoolOp(self, node: ast.BoolOp):
        self.visit(node.values[0])
        possible = frozenset()
        conditional = False
        for previous, value in zip(node.values, node.values[1:]):
            known = isinstance(previous, ast.Constant)
            if known and bool(previous.value) != isinstance(node.op, ast.And):
                possible |= self._value_names(previous)
                break
            if not known:
                possible |= self._value_names(previous)
            conditional = conditional or not known
            if conditional:
                self._join_branches(([], [value]))
            else:
                self.visit(value)
        else:
            possible |= self._value_names(node.values[-1])
        self._expression_values[node] = possible

    def visit_Compare(self, node):
        self.visit(node.left)
        self.visit(node.comparators[0])
        for value in node.comparators[1:]:
            self._join_branches(([], [value]))

    def visit_Assert(self, node):
        self.visit(node.test)
        if isinstance(node.test, ast.Constant) and node.test.value:
            return
        before = self._origin_state()
        if node.msg is not None:
            self.visit(node.msg)
        self._capture_prefix()  # A caught/suppressed AssertionError keeps the message's stores.
        if isinstance(node.test, ast.Constant):
            return _TERMINATED
        self._restore_origins(before)

    @staticmethod
    def _origin_options(value):
        return value if isinstance(value, frozenset) else frozenset((value,))

    def _mapping_options(self, origins, name):
        if not isinstance(origins, ChainMap):
            return self._origin_options(origins.get(name, _FALLBACK))
        if len(origins.maps) == 1:
            return self._mapping_options(origins.maps[0], name)
        options = self._origin_options(origins.maps[0].get(name, _FALLBACK))
        return ((options - {_FALLBACK}) | self._mapping_options(origins.parents, name)
                if _FALLBACK in options else options)

    def _remember_annotation_imports(self, node):
        origins = self._annotation_scopes[-1][1]
        for alias in node.names:
            if alias.name == "*":
                # Unknown exports may replace any name, but do not prove that
                # an existing typing helper disappeared. Known helpers from a
                # typing star import do replace their corresponding bindings.
                for name, origin in origins.items():
                    origins[name] = self._origin_options(origin) | {None}
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in {"typing", "typing_extensions"}:
                    origins.update((name, f"{node.module}.{name}") for name in ("cast", "Literal", "Annotated"))
                continue
            local = alias.asname or alias.name.split(".")[0]
            if isinstance(node, ast.Import):
                self._store_origins({local: alias.name if alias.asname else local})
            elif node.level == 0 and node.module:
                self._store_origins({local: f"{node.module}.{alias.name}"})
            else:
                self._store_origins({local: None})

    def _annotation_origins(self) -> dict:
        """Visible import origins, with explicit local shadows. A function or
        comprehension does not close over a class's namespace; a class body
        itself can see the names it has already bound."""
        out = {}
        for index, (kind, origins, globals_) in enumerate(self._annotation_scopes):
            if kind != "class" or index == len(self._annotation_scopes) - 1:
                for name in globals_:
                    out[name] = self._mapping_options(self._annotation_scopes[0][1], name)
                for name, value in origins.items():
                    options = self._mapping_options(origins, name)
                    if _FALLBACK in options:
                        # A class branch may leave its local name absent. That
                        # path falls back to the enclosing namespace.
                        options = (options - {_FALLBACK}) | self._origin_options(out.get(name, _FALLBACK))
                    out[name] = options
        return out

    @contextmanager
    def _preserve_annotation_bindings(self, enabled=True):
        saved = [(origins, (origins.maps[0] if isinstance(origins, ChainMap) else origins).copy())
                 for _kind, origins, _globals in self._annotation_scopes] if enabled else []
        try:
            yield
        finally:
            for origins, original in saved:
                origins.clear()
                origins.update(original)

    @contextmanager
    def _deferred_module_namespace(self):
        # Global writes belong to this possible function invocation, while
        # deferred nested reads retain its later writes and the live module.
        # A separate overlay keeps an uninvoked body from changing later code.
        original = self._annotation_scopes[0]
        self._annotation_scopes[0] = (original[0], ChainMap({}, original[1]), original[2])
        try:
            yield
        finally:
            self._annotation_scopes[0] = original

    def _lambda_bindings(self, node: ast.Lambda) -> set[str]:
        names = set()
        for expression in ast.walk(node.body):
            if not isinstance(expression, ast.NamedExpr):
                continue
            child, parent = expression, self.parents.get(expression)
            while parent is not None:
                if isinstance(parent, ast.Lambda) and child is parent.body:
                    if parent is node:
                        names.add(expression.target.id)
                    break
                child, parent = parent, self.parents.get(parent)
        return names

    @contextmanager
    def _annotation_scope(self, node):
        is_class = isinstance(node, ast.ClassDef)
        # Python decides a function's locals for its whole body, including
        # bindings below a nested definition. An empty origin is pending; None
        # means a parameter or an already visited non-import binding.
        local_names, global_names = self.function_bindings.get((node.lineno, getattr(node, "name", "")), ((), ()))
        origins = {} if is_class else {name: "" for name in local_names}
        if isinstance(node, ast.Lambda):
            origins.update((name, "") for name in self._lambda_bindings(node))
        args = getattr(node, "args", None)
        if args is not None:
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
                if arg is not None:
                    origins[arg.arg] = None
        kind = ("class" if is_class else "function" if args is not None else "comprehension")
        if isinstance(node, ast.GeneratorExp):
            kind = "generator"
        self._annotation_scopes.append((kind, origins, global_names))
        try:
            yield
        finally:
            self._annotation_scopes.pop()

    def visit_Module(self, node: ast.Module):
        self._future_annotations = any(
            isinstance(statement, ast.ImportFrom) and statement.module == "__future__"
            and any(alias.name == "annotations" for alias in statement.names)
            for statement in node.body
        )
        self.generic_visit(node)
        # This is a file-local use analysis, not a simulation of call order.
        # Revisit deferred type-bearing calls, retaining earlier proven
        # uses as well as those the completed enclosing namespaces establish.
        self._finishing_references = True
        for call, enclosing in tuple(self._deferred_casts):
            previous = self._annotation_scopes
            self._annotation_scopes = list(enclosing)
            try:
                self._typing_cast_references(call)
            finally:
                self._annotation_scopes = previous
        for annotation, enclosing in tuple(self._deferred_annotations):
            previous = self._annotation_scopes
            self._annotation_scopes = list(enclosing)
            try:
                with self._preserve_annotation_bindings():
                    self._annotation_references(annotation)
            finally:
                self._annotation_scopes = previous

    def _canonical_names(self, node: ast.AST, origins) -> set[str]:
        dotted = dotted_name(node)
        if not dotted:
            return set()
        first, separator, rest = dotted.partition(".")
        # Keep an unknown/non-import branch as an explicit alternative. A
        # possible custom generic still gives its strings type semantics.
        return {imported + (separator + rest if separator else "")
                if isinstance(imported, str) and imported else ""
                for imported in self._origin_options(origins.get(first, _FALLBACK))
                if imported is not _FALLBACK and imported != ""}

    def visit_Attribute(self, node: ast.Attribute):
        if isinstance(node.value, ast.Name):
            self.used_names.add(node.value.id)
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load):
            self._expression_values[node] = self._canonical_names(node, self._annotation_origins())

    def _expression_references(self, node: ast.AST) -> None:
        """Metadata expressions use names, while their strings remain data.

        A quoted annotation has no ordinary AST walk through its contents.
        Literal enum members and Annotated metadata still need their imports,
        without interpreting a string argument as another forward reference.
        """
        self.visit(node)

    def _annotation_references(self, node: ast.AST | None) -> None:
        """Read forward references only in annotation type positions."""
        if node is None:
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                expression = ast.parse(node.value.strip(), mode="eval").body
            except (SyntaxError, ValueError):
                return
            # Quoted expressions need the same parent relationships as the
            # file's AST: a callee is called, not handed out as a callback.
            self.parents.update((child, parent) for parent in ast.walk(expression)
                                for child in ast.iter_child_nodes(parent))
            # A string annotation can contain stores when eventually evaluated,
            # but reading it here does not execute those stores in the source.
            # Restore the existing maps in place so deferred bodies keep their
            # live namespace references for imports encountered later.
            with self._preserve_annotation_bindings():
                self._quoted_depth += 1
                try:
                    self._annotation_references(expression)
                finally:
                    self._quoted_depth -= 1
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            # Match the existing file-local, conservative treatment of names.
            self.used_names.add(node.id)
            self.value_references.add(node.id)
        if isinstance(node, ast.Subscript):
            names = self._canonical_names(node.value, self._annotation_origins())
            self._annotation_references(node.value)
            if self._quoted_depth and not self._finishing_references:
                # Fully quoted forms can resolve against imports below the
                # definition. Decide whether their strings are types or data
                # once that enclosing namespace is complete.
                self._deferred_annotations.append((node, tuple(self._annotation_scopes)))
                if self._unresolved_callee(node.value):
                    return
            if names and names <= {"typing.Literal", "typing_extensions.Literal"}:
                self._expression_references(node.slice)
                return  # String values are data, not forward references.
            if names and names <= {"typing.Annotated", "typing_extensions.Annotated"}:
                args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                self._annotation_references(args[0])
                for metadata in args[1:]:
                    self._expression_references(metadata)
                return  # Remaining arguments are metadata.
            self._annotation_references(node.slice)
            return
        if isinstance(node, ast.Call):
            self._call_references(node)
        for child in ast.iter_child_nodes(node):
            self._annotation_references(child)

    def _signature_references(self, node):
        self._read_annotation(node.returns)
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                    node.args.vararg, node.args.kwarg):
            if arg is not None:
                self._read_annotation(arg.annotation)

    def _read_annotation(self, annotation):
        # Postponed annotations have the same type/data distinction as quoted
        # ones, including helpers supplied by a later enclosing import.
        self._quoted_depth += self._future_annotations
        try:
            self._annotation_references(annotation)
        finally:
            self._quoted_depth -= self._future_annotations

    def visit_AnnAssign(self, node: ast.AnnAssign):
        origins = {}
        if node.value is not None:
            self.visit(node.value)
            origins = self._assignment_origins(node.target, node.value)
        if node.value is not None or not isinstance(node.target, ast.Name):
            self.visit(node.target)
            self._store_origins(origins)
        self._read_annotation(node.annotation)
        self.visit(node.annotation)

    def _type_parameter_references(self, node):
        # PEP 695 bounds/constraints and PEP 696 defaults are type positions,
        # including strings. The visitor names are harmless on Python 3.11,
        # whose parser cannot produce these newer nodes.
        self._annotation_references(getattr(node, "bound", None))
        self._annotation_references(getattr(node, "default_value", None))
        self.generic_visit(node)

    def visit_TypeAlias(self, node):
        self.visit(node.name)
        for parameter in node.type_params:
            self.visit(parameter)
        # PEP 695 values are lazy type expressions, so later enclosing imports
        # can decide whether a string is a forward type or Literal data.
        self._deferred_annotations.append((node.value, tuple(self._annotation_scopes)))

    def visit_Lambda(self, node: ast.Lambda):
        for default in (*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)):
            self.visit(default)
        with self._annotation_scope(node):
            self.visit(node.body)

    def _visit_comprehension(self, node):
        # Only the first iterable executes outside the comprehension's scope.
        # Later iterables/filters see preceding targets, and no target leaks
        # into the enclosing annotation or expression.
        self.visit(node.generators[0].iter)
        # Creating a generator executes its first iterable, but its body and
        # walrus stores remain deferred until somebody advances it.
        with (self._preserve_annotation_bindings(isinstance(node, ast.GeneratorExp)),
              self._annotation_scope(node), ExitStack() as branches):
            for index, generator in enumerate(node.generators):
                if index:
                    self.visit(generator.iter)
                enters = self._loop_entry(generator)
                if enters is False:
                    return
                branches.enter_context(self._optional_origins(enters is None))
                self.visit(generator.target)
                for condition in generator.ifs:
                    self.visit(condition)
                    if isinstance(condition, ast.Constant) and not condition.value:
                        return
                    branches.enter_context(self._optional_origins(not isinstance(condition, ast.Constant)))
            for field in ("elt", "key", "value"):
                expression = getattr(node, field, None)
                if expression is not None:
                    self.visit(expression)

    @contextmanager
    def _optional_origins(self, enabled):
        # Each loop/filter can skip everything after it. Join that skipped
        # path without losing stores in iterables or already evaluated filters.
        before = self._origin_state() if enabled else None
        try:
            yield
        finally:
            if before is not None:
                self._restore_origins(self._joined_origins([before, self._origin_state()]))

    def _call_references(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.called_functions.add(node.func.id)
            self.instantiated_classes.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            self.called_functions.add(node.func.attr)
        if not self._typing_cast_references(node) and not self._finishing_references and self._unresolved_callee(node.func, include_known=True) and any(
                kind in {"function", "generator"} for kind, _origins, _globals in self._annotation_scopes):
            enclosing = list(self._annotation_scopes)
            first = dotted_name(node.func).partition(".")[0]
            module = enclosing[0]
            if first in enclosing[-1][2] and isinstance(module[1], ChainMap):
                # Its own later global import cannot repair an earlier call;
                # a later module/enclosing-invocation import still can.
                enclosing[0] = (module[0], module[1].parents, module[2])
            self._deferred_casts.append((node, tuple(enclosing)))

    def _unresolved_callee(self, node: ast.AST, include_known=False) -> bool:
        dotted = dotted_name(node)
        if not dotted:
            return False
        first = dotted.partition(".")[0]
        origins = self._annotation_origins()
        deferred = [local for kind, local, _globals in self._annotation_scopes if kind in {"function", "generator"}]
        if deferred and first in deferred[-1]:
            # A later import in this very function is an unbound local at the
            # current call. Only an enclosing namespace may complete later.
            return False
        module = self._annotation_scopes[0][1]
        if (include_known and first in self._annotation_scopes[-1][2]
                and isinstance(module, ChainMap) and first in module.maps[0]):
            # This invocation already overwrote its global. A later enclosing
            # import cannot undo a store that runs before every such call.
            return False
        # Deferred readers can observe an enclosing binding being replaced,
        # even when its origin was already known at definition time.
        return include_known or first not in origins or bool({"", _FALLBACK} & self._origin_options(origins[first]))

    def _typing_cast_references(self, node: ast.Call) -> bool:
        # Only typing's cast gives its first argument type semantics. An
        # unrelated converter.cast("Model", value) receives ordinary data,
        # including when it appears inside quoted Annotated metadata.
        callees = self._canonical_names(node.func, self._annotation_origins())
        if callees & {"typing.cast", "typing_extensions.cast"}:
            typ = node.args[0] if node.args else next((keyword.value for keyword in node.keywords if keyword.arg == "typ"), None)
            self._annotation_references(typ)
            return True
        return False

    def visit_Call(self, node: ast.Call):
        self._call_references(node)
        self.generic_visit(node)

    visit_AsyncFor = visit_For
    visit_TypeVar = visit_ParamSpec = visit_TypeVarTuple = _type_parameter_references
    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _visit_comprehension

    def visit_Import(self, node):
        self._remember_annotation_imports(node)

    visit_ImportFrom = visit_Import

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            kind, origins = self._binding_namespace(node.id)
            if kind == "class" and isinstance(node.ctx, ast.Del):
                origins.pop(node.id, None)
            else:
                origins[node.id] = None
        if isinstance(node.ctx, ast.Load):
            self.used_names.add(node.id)
            self._expression_values[node] = self._canonical_names(node, self._annotation_origins())
            parent = self.parents.get(node)
            if not (isinstance(parent, ast.Call) and parent.func is node):
                self.value_references.add(node.id)

    def _visit_function(self, node):
        # Definition inputs run before the new function's local namespace.
        for expression in (*node.decorator_list, *node.args.defaults,
                           *(default for default in node.args.kw_defaults if default is not None)):
            self.visit(expression)
        for parameter in getattr(node, "type_params", ()):
            self.visit(parameter)
        self._signature_references(node)
        self._store_origins({node.name: None})
        # A function body is deferred. Its global stores are visible within
        # that body (and nested readers), not evidence that it already ran.
        with self._preserve_annotation_bindings(), self._deferred_module_namespace(), self._annotation_scope(node):
            self._visit_statements(node.body)

    visit_FunctionDef = visit_AsyncFunctionDef = _visit_function

    def visit_ClassDef(self, node):
        # Class headers read the old namespace; methods skip the new class one.
        for expression in (*node.decorator_list, *node.bases, *node.keywords,
                           *getattr(node, "type_params", ())):
            self.visit(expression)
        with self._annotation_scope(node):
            for statement in node.body:
                self.visit(statement)
        self._store_origins({node.name: None})
