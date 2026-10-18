"""Lexical import uses in Python expressions and annotation type positions.

This is the one reference reader used by find_dead_code. It owns progressive
namespace origins and deferred enclosing lookups; the caller owns dead-code
policy, confidence, issue formatting and framework registration exemptions.
"""

import ast
import symtable
from collections import ChainMap
from contextlib import contextmanager


_FALLBACK = object()
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
        self._continuing = True
        self._abrupt = []
        self._prefix_collectors = []

    def _visit_block(self, statements, before=None):
        """Transfer one block; only normal outcomes reach its next statement."""
        previous = self._continuing, self._abrupt
        self._continuing, self._abrupt = True, []
        if before is not None:
            self._restore_origins(before)
        try:
            for statement in statements:
                if not self._continuing:
                    break
                possible_exception = self._may_raise(statement)
                if possible_exception:
                    self._capture_prefix()
                self.visit(statement)
                if possible_exception:
                    self._capture_prefix()
            return self._abrupt + ([("normal", self._origin_state())] if self._continuing else [])
        finally:
            self._continuing, self._abrupt = previous

    @staticmethod
    def _may_raise(node):
        # Pure control transfers do not create a suppressed-exception path.
        # Dynamic expressions may fail; nested bodies record their own prefixes.
        if isinstance(node, (ast.If, ast.While)):
            node = node.test
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            node = node.iter
        elif isinstance(node, (ast.Try, ast.TryStar)):
            return False
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return True  # Definition inputs/class execution can fail.
        return any(isinstance(part, (ast.Call, ast.Attribute, ast.Subscript, ast.BinOp, ast.UnaryOp,
                                     ast.Compare, ast.Import, ast.ImportFrom, ast.Raise, ast.Await))
                   or isinstance(part, ast.Name) and isinstance(part.ctx, (ast.Load, ast.Del))
                   for part in ast.walk(node))

    def _capture_prefix(self):
        for depth, partial in self._prefix_collectors:
            partial.append(self._origin_state()[:depth])

    def _block_prefixes(self, statements, before=None):
        partial = []
        self._prefix_collectors.append((len(self._annotation_scopes), partial))
        try:
            return self._visit_block(statements, before), partial
        finally:
            self._prefix_collectors.pop()

    def _accept_paths(self, paths):
        normal = [state for kind, state in paths if kind == "normal"]
        self._abrupt.extend((kind, state) for kind, state in paths if kind != "normal")
        self._continuing = bool(normal)
        if normal:
            self._restore_origins(self._joined_origins(normal))

    def _exit(self, kind, node):
        self.generic_visit(node)
        self._abrupt.append((kind, self._origin_state()))
        self._continuing = False

    def visit_Return(self, node):
        self._exit("return", node)

    def visit_Raise(self, node):
        self._exit("raise", node)

    def visit_Continue(self, node):
        self._exit("continue", node)

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
            names = self._canonical_names(value, self._annotation_origins())
            return {target.id: frozenset(name or None for name in names)} if names else {}
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts):
            return {name: origin for left, right in zip(target.elts, value.elts)
                    for name, origin in self._assignment_origins(left, right).items()}
        return {}

    def visit_NamedExpr(self, node: ast.NamedExpr):
        self.visit(node.value)
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
        self._visit_loop(node, [node.target, *node.body])

    def visit_While(self, node: ast.While):
        self._visit_loop(node, [node.test, *node.body])

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

    def _visit_loop(self, node, body):
        # Origins are finite helper/nonhelper alternatives. Iteration joins
        # loop-carried paths to a fixed point; break alone skips the loop else.
        enters = self._loop_entry(node)
        before = self._origin_state()
        exhausted = [before] if enters is not True else []
        exits, entry = [], before
        if enters is not False:
            while True:
                paths = self._visit_block(body, entry)
                carried = [state for kind, state in paths if kind in {"normal", "continue"}]
                exits.extend(("normal" if kind == "break" else kind, state)
                             for kind, state in paths if kind not in {"normal", "continue"})
                exhausted.extend(carried)
                joined = self._joined_origins([before, *carried])
                if not carried or joined == entry:
                    break
                entry = joined
        if isinstance(node, ast.While) and enters is True:
            exhausted = []
        if exhausted:
            exits.extend(self._visit_block(node.orelse, self._joined_origins(exhausted)))
        self._accept_paths(exits)

    def visit_Break(self, node):
        self._exit("break", node)

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
            catches_all = self._exhaustive_pattern(case.pattern)
            guard = (bool(case.guard.value) if isinstance(case.guard, ast.Constant)
                     else True if case.guard is None else None)
            unmatched = [] if catches_all else [incoming, captured]
            if guard is not True:
                unmatched.append(captured)
            if guard is not False:
                completed.extend(self._visit_block(case.body))
        self._accept_paths([*completed, *(("normal", state) for state in unmatched)])

    @staticmethod
    def _exhaustive_pattern(pattern):
        if isinstance(pattern, ast.MatchAs):
            return pattern.pattern is None or ImportReferences._exhaustive_pattern(pattern.pattern)
        return isinstance(pattern, ast.MatchOr) and any(ImportReferences._exhaustive_pattern(item) for item in pattern.patterns)

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
        self._visit_conditional(node.test, node.body, node.orelse)

    def visit_IfExp(self, node: ast.IfExp):
        self._visit_conditional(node.test, [node.body], [node.orelse])

    def _visit_conditional(self, test, body, otherwise):
        # The symbol table already establishes function-local names, including
        # unreachable declarations. Only the executed branch can replace an
        # import's progressive origin, in any scope.
        self.visit(test)
        if isinstance(test, ast.Constant):
            self._accept_paths(self._visit_block(body if test.value else otherwise))
        else:
            self._join_branches((body, otherwise))

    def _join_branches(self, alternatives):
        # Neither branch is known to run. Keep each possible origin; the last
        # branch visited is not evidence that the other was skipped. Restore
        # maps in place: queued deferred bodies retain these namespace objects.
        before = self._origin_state()
        paths = []
        for statements in alternatives:
            paths.extend(self._visit_block(statements, before))
        self._accept_paths(paths)

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
        before = self._origin_state()
        body, partial = self._block_prefixes(node.body)
        normal = [state for kind, state in body if kind == "normal"]
        paths = [(kind, state) for kind, state in body if kind != "normal"]
        if normal:
            otherwise, prefixes = self._block_prefixes(node.orelse, self._joined_origins(normal))
            paths.extend(otherwise)
            partial.extend(prefixes)
        handler_entry = self._joined_origins(partial or [before])
        for handler in node.handlers:
            self._restore_origins(handler_entry)
            handled, prefixes = self._visit_exception_handler(handler)
            paths.extend(handled)
            partial.extend(prefixes)
            if isinstance(node, ast.TryStar):
                continuing = [state for kind, state in handled if kind == "normal"]
                handler_entry = self._joined_origins([handler_entry, *continuing])
        # Unknown exceptions may escape. Their finally references count, while
        # these paths cannot supply origins to statements after the try.
        paths.extend(("raise", state) for state in partial)
        self._accept_paths(self._cleanup_paths(node.finalbody, paths) if node.finalbody else paths)

    visit_TryStar = visit_Try

    def _visit_exception_handler(self, handler):
        if handler.type is not None:
            self.visit(handler.type)
        if handler.name:
            self._store_origins({handler.name: None})
        paths, partial = self._block_prefixes(handler.body)
        if handler.name:
            # Python deletes the exception target at handler exit; a class can
            # then fall back to an enclosing binding, unlike a function local.
            cleaned = []
            for outcome, state in paths:
                self._restore_origins(state)
                kind, origins = self._binding_namespace(handler.name)
                if kind in {"module", "class"}:
                    origins.pop(handler.name, None)
                else:
                    origins[handler.name] = None
                cleaned.append((outcome, self._origin_state()))
            paths = cleaned
        return paths, partial

    def _cleanup_paths(self, statements, paths):
        if not paths:
            return []
        if self._reference_only:
            cleanup = self._visit_block(statements, self._joined_origins([state for _kind, state in paths]))
            return [(incoming if outcome == "normal" else outcome, state)
                    for incoming in {kind for kind, _state in paths} for outcome, state in cleanup]
        normal = [state for kind, state in paths if kind == "normal"]
        result = self._visit_block(statements, self._joined_origins(normal)) if normal else []
        abrupt = [(kind, state) for kind, state in paths if kind != "normal"]
        if abrupt:
            # A second, reference-only walk covers escaping paths. Nested
            # cleanups stay single-pass here, bounding deeply nested finally.
            self._reference_only = True
            try:
                cleanup = self._visit_block(statements, self._joined_origins([state for _kind, state in abrupt]))
                result.extend((incoming if outcome == "normal" else outcome, state)
                              for incoming in {kind for kind, _state in abrupt} for outcome, state in cleanup)
            finally:
                self._reference_only = False
        return result

    def visit_With(self, node):
        partial = []
        for index, item in enumerate(node.items):
            if index:
                partial.append(self._origin_state())
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
        paths, prefixes = self._block_prefixes(node.body)
        partial.extend(prefixes)
        if len(node.items) > 1:
            partial.extend(state for _kind, state in paths)
        # An unknown __exit__ may suppress an exception after any dynamic body
        # prefix. Return/break/continue themselves remain abrupt outcomes.
        self._accept_paths([*paths, *(("normal", state) for state in partial)])

    visit_AsyncWith = visit_With

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
        self._accept_paths(self._visit_block(node.body))
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
        names = {imported + (separator + rest if separator else "")
                 if isinstance(imported, str) and imported else ""
                 for imported in self._origin_options(origins.get(first, _FALLBACK))
                 if imported is not _FALLBACK and imported != ""}
        # Only typing helper identity affects string semantics. Collapsing
        # other origins prevents attribute reassignment loops growing strings
        # forever while preserving their ordinary/custom-generic distinction.
        return {name if name in _TYPING_ORIGINS else "" for name in names}

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
            self._store_origins(origins)
        self._annotation_references(node.annotation)
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
                kind in {"function", "generator"} for kind, _origins, _globals in self._annotation_scopes):
            enclosing = list(self._annotation_scopes)
            first = dotted_name(node.func).partition(".")[0]
            module = enclosing[0]
            if first in enclosing[-1][2] and isinstance(module[1], ChainMap):
                # Its own later global import cannot repair an earlier call;
                # a later module/enclosing-invocation import still can.
                enclosing[0] = (module[0], module[1].parents, module[2])
            self._deferred_casts.append((node, tuple(enclosing)))

    def _unresolved_callee(self, node: ast.AST) -> bool:
        dotted = dotted_name(node)
        if not dotted:
            return False
        first = dotted.partition(".")[0]
        origins = self._annotation_origins()
        module = self._annotation_scopes[0][1]
        if first in self._annotation_scopes[-1][2] and isinstance(module, ChainMap) and first in module.maps[0]:
            return False  # This invocation already replaced its own global.
        deferred = [local for kind, local, _globals in self._annotation_scopes if kind in {"function", "generator"}]
        if deferred and first in deferred[-1]:
            # A later import in this very function is an unbound local at the
            # current call. Only an enclosing namespace may complete later.
            return False
        if deferred:
            return True  # A known enclosing origin may also be rebound later.
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
            kind, origins = self._binding_namespace(node.id)
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
        self._store_origins({node.name: None})
        # A function body is deferred. Its global stores are visible within
        # that body (and nested readers), not evidence that it already ran.
        with self._preserve_annotation_bindings(), self._deferred_module_namespace(), self._annotation_scope(node):
            previous = self._prefix_collectors
            self._prefix_collectors = []  # Defining this body does not run it.
            try:
                paths = self._visit_block(node.body)
                if paths:
                    self._restore_origins(self._joined_origins([state for _kind, state in paths]))
            finally:
                self._prefix_collectors = previous

    visit_FunctionDef = visit_AsyncFunctionDef = _visit_function

    def visit_ClassDef(self, node):
        # Class headers read the old namespace; methods skip the new class one.
        for expression in (*node.decorator_list, *node.bases, *node.keywords,
                           *getattr(node, "type_params", ())):
            self.visit(expression)
        with self._annotation_scope(node):
            paths = self._visit_block(node.body)
        self._accept_paths(paths)
        if self._continuing:
            self._store_origins({node.name: None})
