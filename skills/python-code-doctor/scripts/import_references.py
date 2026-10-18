"""Lexical import uses in Python expressions and annotation type positions.

This is the one reference reader used by find_dead_code. It owns progressive
namespace origins and deferred enclosing lookups; the caller owns dead-code
policy, confidence, issue formatting and framework registration exemptions.
"""

import ast
import symtable
from contextlib import contextmanager


_FALLBACK = object()

def dotted_name(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def function_bindings(source: str, filename: str) -> dict[tuple[int, str], set[str]]:
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
        if table.get_type() == "function" and table.get_name() != "lambda":
            bindings[table.get_lineno(), table.get_name()] = {
                symbol.get_name() for symbol in table.get_symbols() if symbol.is_local()}
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
        self._annotation_scopes = [("module", {})]
        self._deferred_casts = []
        self._deferred_annotations = []
        self._quoted_depth = 0
        self._finishing_references = False
        self._reference_only = False

    def visit_Assign(self, node: ast.Assign):
        # Python evaluates the value with the old binding, then stores each
        # target. In `cast = cast("models.Model", value)` that order decides
        # whether the first argument is a type or ordinary string data.
        self.visit(node.value)
        assignments = [self._assignment_origins(target, node.value) for target in node.targets]
        for target, origins in zip(node.targets, assignments):
            self.visit(target)
            self._annotation_scopes[-1][1].update(origins)

    def _assignment_origins(self, target, value):
        if isinstance(target, ast.Name):
            names = self._canonical_names(value, self._annotation_origins())
            return {target.id: frozenset(name or None for name in names)} if names else {}
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts):
            return {name: origin for left, right in zip(target.elts, value.elts)
                    for name, origin in self._assignment_origins(left, right).items()}
        return {}

    def visit_NamedExpr(self, node: ast.NamedExpr):
        self.visit(node.value)
        # Unlike a comprehension's iteration target, a walrus binds in its
        # containing non-comprehension scope (PEP 572).
        previous = self._annotation_scopes
        end = len(previous)
        while previous[end - 1][0] in {"comprehension", "generator"}:
            end -= 1
        self._annotation_scopes = previous[:end]
        try:
            self.visit(node.target)
        finally:
            self._annotation_scopes = previous

    def visit_For(self, node: ast.For | ast.AsyncFor):
        self.visit(node.iter)
        # The iterable can be empty. Only an executing iteration binds its
        # target or body, so retain the incoming origin beside that outcome.
        self._join_branches(([], [node.target, *node.body]))
        for statement in node.orelse:
            self.visit(statement)

    def visit_If(self, node: ast.If):
        self._visit_conditional(node.test, node.body, node.orelse)

    def visit_IfExp(self, node: ast.IfExp):
        self._visit_conditional(node.test, [node.body], [node.orelse])

    def _visit_conditional(self, test, body, otherwise):
        # The symbol table already establishes function-local names, including
        # unreachable declarations. Only the executed branch can replace an
        # import's progressive origin, in any scope.
        self.visit(test)
        if isinstance(test, ast.Constant):
            for statement in body if test.value else otherwise:
                self.visit(statement)
        else:
            self._join_branches((body, otherwise))

    def _join_branches(self, alternatives):
        # Neither branch is known to run. Keep each possible origin; the last
        # branch visited is not evidence that the other was skipped. Restore
        # maps in place: queued deferred bodies retain these namespace objects.
        before = self._origin_state()
        branches = []
        for statements in alternatives:
            self._restore_origins(before)
            for statement in statements:
                self.visit(statement)
            branches.append(self._origin_state())
        self._restore_origins(self._joined_origins(branches))

    def _origin_state(self):
        return [origins.copy() for _kind, origins in self._annotation_scopes]

    def _restore_origins(self, state):
        for (_kind, origins), original in zip(self._annotation_scopes, state):
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
        partial = [self._origin_state()]
        for statement in node.body:
            self.visit(statement)
            partial.append(self._origin_state())
        handler_entry = self._joined_origins(partial)
        for statement in node.orelse:
            self.visit(statement)
            partial.append(self._origin_state())
        continuing = [self._origin_state()]
        for handler in node.handlers:
            self._restore_origins(handler_entry)
            self._visit_exception_handler(handler, partial)
            continuing.append(self._origin_state())
            if isinstance(node, ast.TryStar):
                # Several except* handlers can run in order; an ordinary except
                # chooses only one. The next group may also skip earlier ones.
                handler_entry = self._joined_origins([handler_entry, continuing[-1]])
        normal = self._joined_origins(continuing)
        self._restore_origins(normal)
        if not node.finalbody:
            return
        self._visit_cleanup(node.finalbody, partial, normal)

    visit_TryStar = visit_Try

    def _visit_exception_handler(self, handler, partial):
        if handler.type is not None:
            self.visit(handler.type)
        if handler.name:
            self._annotation_scopes[-1][1][handler.name] = None
        for statement in handler.body:
            self.visit(statement)
            partial.append(self._origin_state())
        if handler.name:
            # Python deletes the exception target at handler exit; a class can
            # then fall back to an enclosing binding, unlike a function local.
            kind, origins = self._annotation_scopes[-1]
            if kind in {"module", "class"}:
                origins.pop(handler.name, None)
            else:
                origins[handler.name] = None

    def _visit_cleanup(self, statements, partial, normal):
        if self._reference_only:
            self._restore_origins(self._joined_origins([*partial, normal]))
        for statement in statements:
            self.visit(statement)
        after = self._origin_state()
        if not self._reference_only:
            # Finally also runs while an uncaught exception escapes. Its uses
            # count, but that path cannot supply bindings to later statements.
            # Reference-only nested cleanups are walked once, avoiding an
            # exponential second walk for deeply nested try/finally statements.
            self._reference_only = True
            self._restore_origins(self._joined_origins([*partial, normal]))
            try:
                for statement in statements:
                    self.visit(statement)
            finally:
                self._reference_only = False
                self._restore_origins(after)

    def visit_BoolOp(self, node: ast.BoolOp):
        self.visit(node.values[0])
        conditional = False
        for previous, value in zip(node.values, node.values[1:]):
            known = isinstance(previous, ast.Constant)
            if known and bool(previous.value) != isinstance(node.op, ast.And):
                break
            conditional = conditional or not known
            if conditional:
                self._join_branches(([], [value]))
            else:
                self.visit(value)

    @staticmethod
    def _origin_options(value):
        return value if isinstance(value, frozenset) else frozenset((value,))

    def _remember_annotation_imports(self, node):
        origins = self._annotation_scopes[-1][1]
        for alias in node.names:
            if alias.name == "*":
                origins.clear()
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in {"typing", "typing_extensions"}:
                    origins.update((name, f"{node.module}.{name}") for name in ("cast", "Literal", "Annotated"))
                continue
            local = alias.asname or alias.name.split(".")[0]
            if isinstance(node, ast.Import):
                origins[local] = alias.name if alias.asname else local
            elif node.level == 0 and node.module:
                origins[local] = f"{node.module}.{alias.name}"
            else:
                origins[local] = None

    def _annotation_origins(self) -> dict:
        """Visible import origins, with explicit local shadows. A function or
        comprehension does not close over a class's namespace; a class body
        itself can see the names it has already bound."""
        out = {}
        for index, (kind, origins) in enumerate(self._annotation_scopes):
            if kind != "class" or index == len(self._annotation_scopes) - 1:
                for name, value in origins.items():
                    options = self._origin_options(value)
                    if _FALLBACK in options:
                        # A class branch may leave its local name absent. That
                        # path falls back to the enclosing namespace.
                        options = (options - {_FALLBACK}) | self._origin_options(out.get(name, _FALLBACK))
                    out[name] = options
        return out

    @contextmanager
    def _preserve_annotation_bindings(self, enabled=True):
        saved = [(origins, origins.copy()) for _kind, origins in self._annotation_scopes] if enabled else []
        try:
            yield
        finally:
            for origins, original in saved:
                origins.clear()
                origins.update(original)

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
        origins = {name: "" for name in self.function_bindings.get((node.lineno, getattr(node, "name", "")), ())}
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
        self._annotation_scopes.append((kind, origins))
        try:
            yield
        finally:
            self._annotation_scopes.pop()

    def visit_Module(self, node: ast.Module):
        self.generic_visit(node)
        # This is a file-local use analysis, not a simulation of call order.
        # Revisit only unresolved type-bearing calls, retaining earlier proven
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
                expression = ast.parse(node.value, mode="eval").body
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
            if self._quoted_depth and not self._finishing_references and self._unresolved_callee(node.value):
                # Fully quoted forms can resolve against imports below the
                # definition. Decide whether their strings are types or data
                # once that enclosing namespace is complete.
                self._deferred_annotations.append((node, tuple(self._annotation_scopes)))
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
        self._annotation_references(node.returns)
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                    node.args.vararg, node.args.kwarg):
            if arg is not None:
                self._annotation_references(arg.annotation)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        origins = {}
        if node.value is not None:
            self.visit(node.value)
            origins = self._assignment_origins(node.target, node.value)
        if node.value is not None or not isinstance(node.target, ast.Name):
            self.visit(node.target)
            self._annotation_scopes[-1][1].update(origins)
        self._annotation_references(node.annotation)
        self.visit(node.annotation)

    def _type_parameter_references(self, node):
        # PEP 695 bounds/constraints and PEP 696 defaults are type positions,
        # including strings. The visitor names are harmless on Python 3.11,
        # whose parser cannot produce these newer nodes.
        self._annotation_references(getattr(node, "bound", None))
        self._annotation_references(getattr(node, "default_value", None))
        self.generic_visit(node)

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
        with self._preserve_annotation_bindings(isinstance(node, ast.GeneratorExp)), self._annotation_scope(node):
            for index, generator in enumerate(node.generators):
                if index:
                    self.visit(generator.iter)
                self.visit(generator.target)
                for condition in generator.ifs:
                    self.visit(condition)
            for field in ("elt", "key", "value"):
                expression = getattr(node, field, None)
                if expression is not None:
                    self.visit(expression)

    def _call_references(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.called_functions.add(node.func.id)
            self.instantiated_classes.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            self.called_functions.add(node.func.attr)
        if not self._typing_cast_references(node) and not self._finishing_references and self._unresolved_callee(node.func) and any(
                kind in {"function", "generator"} for kind, _origins in self._annotation_scopes):
            self._deferred_casts.append((node, tuple(self._annotation_scopes)))

    def _unresolved_callee(self, node: ast.AST) -> bool:
        dotted = dotted_name(node)
        if not dotted:
            return False
        first = dotted.partition(".")[0]
        origins = self._annotation_origins()
        deferred = [local for kind, local in self._annotation_scopes if kind in {"function", "generator"}]
        if deferred and first in deferred[-1]:
            # A later import in this very function is an unbound local at the
            # current call. Only an enclosing namespace may complete later.
            return False
        return first not in origins or bool({"", _FALLBACK} & self._origin_options(origins[first]))

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
            kind, origins = self._annotation_scopes[-1]
            if kind == "class" and isinstance(node.ctx, ast.Del):
                origins.pop(node.id, None)
            else:
                origins[node.id] = None
        if isinstance(node.ctx, ast.Load):
            self.used_names.add(node.id)
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
        self._annotation_scopes[-1][1][node.name] = None
        with self._annotation_scope(node):
            for statement in node.body:
                self.visit(statement)

    visit_FunctionDef = visit_AsyncFunctionDef = _visit_function

    def visit_ClassDef(self, node):
        # Class headers read the old namespace; methods skip the new class one.
        for expression in (*node.decorator_list, *node.bases, *node.keywords,
                           *getattr(node, "type_params", ())):
            self.visit(expression)
        with self._annotation_scope(node):
            for statement in node.body:
                self.visit(statement)
        self._annotation_scopes[-1][1][node.name] = None
