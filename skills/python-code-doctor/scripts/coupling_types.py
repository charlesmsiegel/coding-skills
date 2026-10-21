"""Conservative external-type evidence for coupling analysis.

Lexical inventories prove whether a value belongs outside the repository;
cohesion and feature-envy aggregation consume that evidence in the detector.
This reader reuses the shared binding inventory rather than executing flow.
Import paths identify the same object across lexical aliases; actual member
writes and visible local subclasses can contradict an external pattern proof.
"""

import ast
import sys
from collections import defaultdict
from pathlib import Path

from common import cached_parse, first_party_modules, project_root
from coupling_bindings import _Derivation, _FOREIGN, _SCOPES, _TRANSPARENT, _target_names
from find_import_cycles import _module_to_path, _resolve_relative_import


_BUILTIN_TYPES = frozenset({
    "str", "bytes", "bytearray", "int", "float", "complex", "bool", "list", "dict",
    "set", "frozenset", "tuple", "range", "slice", "memoryview", "object", "type",
})
# Generic containers by what a subscript or an iteration of them yields.
_MAPPINGS = frozenset({"dict", "Mapping", "MutableMapping", "defaultdict", "OrderedDict", "Counter"})
_SEQUENCES = frozenset({"list", "tuple", "set", "frozenset", "Sequence", "MutableSequence",
                        "Iterable", "Iterator", "Collection", "AbstractSet", "MutableSet", "deque"})
_UNWRAP = frozenset({"Optional", "Annotated", "Final", "ClassVar"})


class _ExternalTypes:
    """What one module proves about a name's type being outside this repository.

    Fowler's remedy for envy is to move the method onto the envied class, which
    cannot be done to torch.Tensor, re.Match, os.DirEntry or str. A type is
    *known* external only from evidence in the source:

    - an annotation naming a builtin, or a name imported from a package that is
      not first-party (common.first_party_modules — the dependency detector's
      rule), unwrapping Optional / `X | None`; a subscript of a `dict[K, V]` or
      `list[V]` parameter yields its V;
    - a value from a call whose callee is such an import, a builtin type, a
      method of an external value, or a method of a module-level name bound once
      from such a call (`PATTERN = re.compile(...)` then `PATTERN.match(...)`);
    - a loop target over such a call, through list/sorted/tuple/... wrappers.

    Anything else is unknown, and an unknown type still counts as envy.
    """

    def __init__(self, tree: ast.AST, filename: str) -> None:
        self.filename = filename
        self._first_party = None
        self.imports: dict[str, str | None] = {}  # local name -> top-level module (None: relative)
        self.defined: set = set()
        self.module_values: dict[str, list] = defaultdict(list)
        self.parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        self.module_scope = tree
        self.scope_inventories = {tree: _Derivation(tree, None)}
        self.global_scopes: dict[str, set] = defaultdict(set)
        self.nonlocal_scopes: dict[str, set] = defaultdict(set)
        self.attribute_writes: list[ast.Attribute] = []
        self.import_paths: dict[ast.AST, set[tuple]] = {}
        self.member_writes: dict[int, set] = {}
        self.subclass_paths: dict[int, set] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                parent = self.parents.get(node)
                if isinstance(parent, ast.AnnAssign) and parent.value is None:
                    continue  # An annotation evaluates its target without replacing the member.
                path = self._attribute_path(node)
                if path is not None:
                    self.attribute_writes.append(node)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.imports[alias.asname or alias.name.split(".")[0]] = alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                top = None if node.level else (node.module or "").split(".")[0]
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = top
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                scope = self.parents.get(node)
                while scope is not None and not isinstance(scope, _SCOPES + (ast.ClassDef,)):
                    scope = self.parents.get(scope)
                if scope is not None and not isinstance(scope, ast.Module):
                    for name in node.names:
                        owners = self.global_scopes if isinstance(node, ast.Global) else self.nonlocal_scopes
                        owners[name].add(scope)
        for node in getattr(tree, "body", []):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    for name in _target_names(target):
                        self.module_values[name].append(node.value if isinstance(target, ast.Name) else None)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                self.module_values[node.target.id].append(node.value)

    def _external_module(self, top) -> bool:
        if not top:
            return False  # a relative import is this package's own code
        if top in sys.stdlib_module_names:
            return True
        if self._first_party is None:
            self._first_party = first_party_modules(project_root(Path(self.filename)))
        return top not in self._first_party

    def _external_name(self, name: str) -> bool:
        if name in self.imports:
            return self._external_module(self.imports[name])
        return name not in self.defined and name not in self.module_values and name in _BUILTIN_TYPES

    def _inventory(self, scope):
        if scope not in self.scope_inventories:
            # A class body's explicit globals need write evidence too. Reuse
            # the collector without making its namespace a method's closure.
            tree = ast.Module(body=scope.body, type_ignores=[]) if isinstance(scope, ast.ClassDef) else scope
            self.scope_inventories[scope] = _Derivation(tree, None)
        return self.scope_inventories[scope]

    def _namespace_write(self, value, name, inventory):
        """Ground this writer locally; another global needs its own proof."""
        return (isinstance(value, ast.Import)
                or (isinstance(value, ast.ImportFrom) and self.imported_namespace(value, name))
                or inventory.module_namespace(value, self.imported_namespace,
                    lambda _inventory, _expression, key: key[1] == name))

    @staticmethod
    def _unconditional_write(inventory, expression, key):
        """A direct earlier statement runs whenever this same-scope read does."""
        scope = inventory._scope_of(expression)
        for value in inventory.bindings.get(key, []):
            statement = value if isinstance(value, (ast.Import, ast.ImportFrom)) else inventory.parent.get(id(value))
            if (isinstance(statement, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign))
                    and inventory.parent.get(id(statement)) is scope
                    and (statement.end_lineno, statement.end_col_offset) < (expression.lineno, expression.col_offset)):
                return True
        return False

    def global_namespace(self, inventory, expression, key):
        """A method's global aliases also inherit writes outside that method.

        The module inventory skips classes. Consult the same cached owner
        inventories for visible global writers, including class bodies/methods.
        A straight-line overwrite can supersede initializers, never another
        possible writer; unknown cross-global dependencies stay conservative.
        """
        name = key[1]
        for scope in self.global_scopes.get(name, ()):
            writer = self._inventory(scope)
            if not all(self._namespace_write(value, name, writer)
                       for value in writer.namespace_values((writer.global_scope, name), expression)):
                return False
        module = self._inventory(self.module_scope)
        values = module.bindings.get((id(self.module_scope), name), [])
        if all(self._namespace_write(value, name, module) for value in values):
            return True
        return self._unconditional_write(inventory, expression, key) and all(
            (isinstance(value, ast.AST) and module._scope_of(value) is self.module_scope)
            or self._namespace_write(value, name, module) for value in values)

    def _module_write_values(self, name):
        """Actual canonical writes from the already indexed global owners."""
        values = []
        for scope in self.global_scopes.get(name, ()):
            writer = self._inventory(scope)
            values.extend(writer.bindings.get((writer.global_scope, name), []))
        return values

    def _nonlocal_owner(self, scope, name):
        """Find the actual enclosing cell, skipping classes/declarations."""
        current = self.parents.get(scope)
        while current is not None and not isinstance(current, ast.Module):
            if isinstance(current, _SCOPES):
                inventory = self._inventory(current)
                key = (id(current), name)
                if key in inventory.globals:
                    return None
                if key in inventory.bindings and key not in inventory.nonlocals:
                    return current
            current = self.parents.get(current)
        return None

    def _nonlocal_write_values(self, owner, name):
        values = []
        for scope in self.nonlocal_scopes.get(name, ()):
            if self._nonlocal_owner(scope, name) is owner:
                writer = self._inventory(scope)
                scope_id = writer.global_scope if isinstance(scope, ast.ClassDef) else id(scope)
                # A plain class's exact `nonlocal name; name = name` changes
                # neither cell nor value. Its RHS is absent from the method's
                # parent map, so do not turn this no-op into an unknown/cycle.
                # A custom class namespace can override LOAD_CLASSDEREF.
                plain = not isinstance(scope, ast.ClassDef) or not (scope.bases or scope.keywords or scope.decorator_list)
                values.extend(value for value in writer.nonlocal_writes.get((scope_id, name), [])
                              if not (plain and isinstance(value, ast.Name) and value.id == name
                                      and self._identity_assignment(value, name)))
        return values

    def _lexical_binding(self, expression, name):
        """Nearest possible values, or None for an unbound/builtin name.

        Global selects the module, not an enclosing local and not a foreign
        value. Both representation dispatch and pattern typing use this same
        ownership lookup over the existing inventories.
        """
        child, current = expression, self.parents.get(expression)
        module_only = False
        module_writes = []
        nonlocal_writes = []
        first_scope = True
        while current is not None:
            if isinstance(current, ast.ClassDef) and first_scope and child in current.body:
                # A class body's own aliases see its namespace; an ordinary
                # method (or nested class) never closes over that namespace.
                first_scope = False
                inventory = self._inventory(current)
                key = (inventory.global_scope, name)
                if key in inventory.globals:
                    module_only = True
                elif key in inventory.nonlocals:
                    nonlocal_writes.extend(inventory.nonlocal_writes.get(key, []))
                elif key in inventory.bindings:
                    return inventory.bindings[key]
            if isinstance(current, _SCOPES):
                first_scope = False
                inventory = self._inventory(current)
                key = (id(current), name)
                if isinstance(current, ast.Module):
                    # Class bodies and sibling functions are not ancestors,
                    # but their actual global writes reach this namespace too.
                    module_writes.extend(self._module_write_values(name))
                if not module_only or isinstance(current, ast.Module):
                    if key in inventory.globals:
                        if isinstance(current, ast.Module):
                            values = inventory.bindings.get(inventory.globals[key], [])
                            return (module_writes + values) or None
                        # Ownership alone cannot finish the proof: sibling
                        # helpers' actual module writes live in outer inventories.
                        module_only = True
                    elif key in inventory.nonlocals:
                        nonlocal_writes.extend(inventory.nonlocal_writes.get(key, []))
                    elif (id(current), "*") in inventory.bindings:
                        return [_FOREIGN]  # an unknown star export may shadow it
                    elif key in inventory.bindings:
                        values = inventory.bindings[key]
                        return (module_writes + values if isinstance(current, ast.Module) else
                                nonlocal_writes + values + self._nonlocal_write_values(current, name))
                if not isinstance(current, ast.Module):
                    # A nested helper can write the module without declaring
                    # this read global. Keep that evidence, but a genuine
                    # enclosing cell still owns the read before the module.
                    # An explicit global skips cells, not other module writes.
                    module_writes.extend(inventory.bindings.get((inventory.global_scope, name), []))
            child, current = current, self.parents.get(current)
        return module_writes or None

    def representation_builtin(self, expression, derivation):
        """A bare builtin dispatch, with the same lexical binding inventory.

        Local/enclosing/module converters and unknown star imports cannot
        manufacture representation edges. Class attributes do not participate
        in an ordinary method's free-name lookup.
        """
        if not isinstance(expression, ast.Name) or expression.id not in {"str", "repr"}:
            return None
        if self._lexical_binding(expression, expression.id) is not None:
            return None
        return "__str__" if expression.id == "str" else "__repr__"

    def class_namespace(self, expression):
        """Every lexical writer, including class-body nonlocals, leaves a plain class."""
        values = self._lexical_binding(expression, expression.id)
        return bool(values) and all(isinstance(value, ast.ClassDef)
                                    and not (value.bases or value.keywords or value.decorator_list)
                                    for value in values)

    @staticmethod
    def _root_name(expr):
        while isinstance(expr, ast.Attribute):
            expr = expr.value
        return expr.id if isinstance(expr, ast.Name) else None

    # -- annotations ----------------------------------------------------------
    def annotation(self, ann) -> bool:
        """The annotated value's own type is external (a `dict[...]` is a dict)."""
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                ann = ast.parse(ann.value, mode="eval").body
            except SyntaxError:
                return False
        if isinstance(ann, ast.Name):
            return self._external_name(ann.id)
        if isinstance(ann, ast.Attribute):
            root = self._root_name(ann)
            return root is not None and root in self.imports and self._external_module(self.imports[root])
        if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
            members = [m for m in (ann.left, ann.right)
                       if not (isinstance(m, ast.Constant) and m.value is None)]
            return bool(members) and all(self.annotation(m) for m in members)
        if isinstance(ann, ast.Subscript):
            head = getattr(ann.value, "id", getattr(ann.value, "attr", None))
            args = ann.slice.elts if isinstance(ann.slice, ast.Tuple) else [ann.slice]
            if head in _UNWRAP:
                return self.annotation(args[0])
            if head == "Union":
                members = [m for m in args if not (isinstance(m, ast.Constant) and m.value is None)]
                return bool(members) and all(self.annotation(m) for m in members)
            return self.annotation(ann.value)
        return False

    def _item_annotation(self, ann, index=None) -> bool:
        """What subscripting a value of this annotation yields is external."""
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                ann = ast.parse(ann.value, mode="eval").body
            except SyntaxError:
                return False
        if isinstance(ann, ast.Subscript):
            head = getattr(ann.value, "id", getattr(ann.value, "attr", None))
            args = ann.slice.elts if isinstance(ann.slice, ast.Tuple) else [ann.slice]
            if head in _UNWRAP:
                return self._item_annotation(args[0], index)
            if head in _MAPPINGS:
                return self.annotation(args[-1])
            if head == "tuple" and not (len(args) == 2 and isinstance(args[-1], ast.Constant)
                                        and args[-1].value is Ellipsis):
                # A positional tuple does not have one uniform element type.
                # An unknown index needs every possible position to qualify.
                if index is None:
                    return bool(args) and all(self.annotation(arg) for arg in args)
                return -len(args) <= index < len(args) and self.annotation(args[index])
            if head in _SEQUENCES:
                return self.annotation(args[0])
            return False
        # Indexing a library object (a Tensor) gives the library's objects back.
        return self.annotation(ann) and getattr(ann, "id", None) not in _BUILTIN_TYPES

    # -- values ---------------------------------------------------------------
    def _indexed_value(self, subject, derivation, index=None) -> bool:
        """One element of an annotated parameter, shared by indexing and patterns."""
        key = derivation.resolve(subject) if isinstance(subject, ast.Name) else None
        values = derivation.bindings.get(key, []) if key else []
        return bool(values) and all(isinstance(v, ast.arg) and v.annotation is not None
                                    and self._item_annotation(v.annotation, index) for v in values)

    @staticmethod
    def _attribute_path(expression):
        names = []
        while isinstance(expression, ast.Attribute):
            names.append(expression.attr)
            expression = expression.value
        return (expression.id, *reversed(names)) if isinstance(expression, ast.Name) else None

    @staticmethod
    def _import_origins(declaration, name):
        if isinstance(declaration, ast.Import):
            return {tuple((alias.name if alias.asname else alias.name.split(".")[0]).split("."))
                    for alias in declaration.names if (alias.asname or alias.name.split(".")[0]) == name}
        if isinstance(declaration, ast.ImportFrom) and not declaration.level:
            return {tuple((declaration.module or "").split(".")) + tuple(alias.name.split("."))
                    for alias in declaration.names if (alias.asname or alias.name) == name}
        return None

    def _import_paths(self, expression):
        """Canonical import paths, grounded iteratively in every lexical write.

        A qualified alias retains its suffix at every hop. Unknown writes,
        extraction and cycles cannot certify identity; modules are never loaded.
        """
        pending = [(expression, False)]
        active = set()
        while pending:
            current, leaving = pending.pop()
            if current in self.import_paths:
                continue
            path = self._attribute_path(current)
            if path is None:
                return set()
            root, suffix = path[0], path[1:]
            values = self._lexical_binding(current, root)
            if values is None:
                if isinstance(current, ast.Name) and root in _BUILTIN_TYPES:
                    self.import_paths[current] = {("builtins", root)}
                    continue
                return set()
            if not values:
                return set()
            if leaving:
                paths = set()
                for value in values:
                    origins = self._import_origins(value, root)
                    paths.update(origin + suffix for origin in (
                        origins if origins is not None else self.import_paths[value]))
                self.import_paths[current] = paths
                active.remove(current)
                continue
            if current in active:
                return set()
            active.add(current)
            pending.append((current, True))
            for value in values:
                if isinstance(value, ast.Import) or (isinstance(value, ast.ImportFrom) and not value.level):
                    continue
                if isinstance(value, (ast.Name, ast.Attribute)) and self._identity_assignment(value, root):
                    pending.append((value, False))
                else:
                    return set()
        return self.import_paths.get(expression, set())

    def _possible_import_paths(self, expression, max_depth, *, generic=False):
        """Contradictions need any known origin, even beside unknown writes.

        Only paths no longer than the pattern can contradict its prefixes.
        That bound also terminates aliases whose cycles add member suffixes.
        """
        pending = [(expression, ())]
        seen, paths = set(), set()
        while pending:
            current, suffix = pending.pop()
            if generic and isinstance(current, ast.Subscript):
                current = current.value
            key = (current, suffix)
            if key in seen or len(suffix) >= max_depth:
                continue
            seen.add(key)
            path = self._attribute_path(current)
            if path is None:
                continue
            root, suffix = path[0], path[1:] + suffix
            values = self._lexical_binding(current, root)
            if values is None:
                if isinstance(current, ast.Name) and root in _BUILTIN_TYPES:
                    paths.add(("builtins", root) + suffix)
                continue
            for value in values:
                origins = self._import_origins(value, root)
                if origins is not None:
                    paths.update(origin + suffix for origin in origins if len(origin + suffix) <= max_depth)
                elif isinstance(value, (ast.Name, ast.Attribute)) or (generic and isinstance(value, ast.Subscript)):
                    pending.append((value, suffix))
        return paths

    def _pattern_class_root(self, expression) -> bool:
        """An external import with no visible writes to its canonical members."""
        paths = self._import_paths(expression)
        if not paths or not all(self._external_module(path[0]) for path in paths):
            return False
        depth = max(map(len, paths))
        if depth not in self.member_writes:
            self.member_writes[depth] = {path for write in self.attribute_writes
                                         for path in self._possible_import_paths(write, depth)}
        return not any(path[:end] in self.member_writes[depth] for path in paths for end in range(2, len(path) + 1))

    def _identity_assignment(self, value, name):
        """A whole RHS assigned to this name, never an iterable/extraction."""
        parent = self.parents.get(value)
        if isinstance(parent, ast.Assign) and parent.value is value:
            return any(isinstance(target, ast.Name) and target.id == name for target in parent.targets)
        return (isinstance(parent, (ast.AnnAssign, ast.NamedExpr)) and parent.value is value
                and isinstance(parent.target, ast.Name) and parent.target.id == name)

    def imported_namespace(self, declaration, name) -> bool:
        """An external value or proven first-party submodule, never any member.

        ImportFrom ownership alone says nothing about its exported value. The
        existing module resolver can prove a submodule only when its package
        has no contrary export, unknown star export or dynamic attribute hook.
        """
        if not declaration.level and self._external_module((declaration.module or "").split(".")[0]):
            return True  # retain the existing external-value policy
        alias = next((alias for alias in declaration.names if (alias.asname or alias.name) == name), None)
        if alias is None or alias.name == "*":
            return False
        filename = Path(self.filename).resolve()
        root = project_root(filename)
        module = (_resolve_relative_import(declaration.level, declaration.module, filename, root)
                  if declaration.level else declaration.module)
        if not module:
            return False
        for base in (root, root / "src"):
            package = _module_to_path(module, base)
            child = _module_to_path(module + "." + alias.name, base)
            if package is None or package.name != "__init__.py" or child is None:
                continue
            try:
                _, tree = cached_parse(package)
            except (OSError, SyntaxError, UnicodeError):
                return False
            bindings = _Derivation(tree, None).bindings
            return not any((id(tree), key) in bindings for key in (alias.name, "*", "__getattr__"))
        return False

    def _pattern_type(self, pattern) -> bool:
        """The whole matched instance, not fields taken from that instance."""
        if isinstance(pattern, ast.MatchSingleton):
            return True  # True/False/None match by identity, not overloaded equality.
        if isinstance(pattern, ast.MatchClass):
            if not self._pattern_class_root(pattern.cls):
                return False
            paths = self._import_paths(pattern.cls)
            depth = max(map(len, paths))
            if depth not in self.subclass_paths:
                self.subclass_paths[depth] = {("builtins", "object")} | {
                    path for node in ast.walk(self.module_scope) if isinstance(node, ast.ClassDef)
                    for base in node.bases for path in self._possible_import_paths(base, depth, generic=True)}
            # A class pattern accepts subclasses too. Visible first-party
            # subclasses keep the capture eligible for an ungraded candidate.
            return paths.isdisjoint(self.subclass_paths[depth])
        if isinstance(pattern, ast.MatchAs):
            return self._pattern_type(pattern.pattern)
        if isinstance(pattern, ast.MatchOr):
            return all(self._pattern_type(branch) for branch in pattern.patterns)
        # MatchValue uses equality: a custom first-party object can match a
        # numeric/string literal while the capture still holds that object.
        return False

    def _pattern_value(self, capture, derivation, seen) -> bool:
        """A direct sequence/mapping extraction has the same type as indexing.

        A library object's fields and nested containers need additional type
        evidence. Do not infer their types merely from the outer annotation.
        """
        if self._pattern_type(capture.pattern):
            return True
        subject = derivation._pattern_subject(capture, whole=True)
        if subject is not None:
            return self.value(subject, derivation, seen)
        child = capture
        parent = derivation.parent.get(id(child))
        while isinstance(parent, (ast.MatchAs, ast.MatchOr)):
            child = parent
            parent = derivation.parent.get(id(child))
        if not isinstance(parent, (ast.MatchSequence, ast.MatchMapping)):
            return False
        subject = derivation._pattern_subject(derivation.parent.get(id(parent)), whole=True)
        index = None
        if isinstance(parent, ast.MatchSequence):
            index = parent.patterns.index(child)
            star = next((i for i, pattern in enumerate(parent.patterns) if isinstance(pattern, ast.MatchStar)), None)
            if star is not None and index > star:
                index -= len(parent.patterns)  # suffix positions are known from the end
        return self._indexed_value(subject, derivation, index)

    def _callee(self, func, derivation, seen) -> bool:
        if isinstance(func, ast.Name):
            if derivation.resolve(func) is not None:
                return False
            return self._external_name(func.id)
        if isinstance(func, ast.Attribute):
            base = func.value
            if isinstance(base, ast.Name) and derivation.resolve(base) is None:
                if base.id in self.imports:
                    return self._external_module(self.imports[base.id])
                values = self.module_values.get(base.id, [])
                return len(values) == 1 and values[0] is not None and self.value(values[0], derivation, seen)
            return self.value(base, derivation, seen)  # a method of an external value
        return False

    def value(self, expr, derivation, seen=frozenset()) -> bool:
        """``expr`` evaluates to an object of external type."""
        if isinstance(expr, ast.Call):
            return self._callee(expr.func, derivation, seen)
        if isinstance(expr, ast.IfExp):
            branches = [b for b in (expr.body, expr.orelse)
                        if not (isinstance(b, ast.Constant) and b.value is None)]
            return bool(branches) and all(self.value(b, derivation, seen) for b in branches)
        if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name):
            index = expr.slice.value if isinstance(expr.slice, ast.Constant) and isinstance(expr.slice.value, int) else None
            if (isinstance(expr.slice, ast.UnaryOp) and isinstance(expr.slice.op, ast.USub)
                    and isinstance(expr.slice.operand, ast.Constant) and isinstance(expr.slice.operand.value, int)):
                index = -expr.slice.operand.value
            return self._indexed_value(expr.value, derivation, index)
        if isinstance(expr, ast.MatchAs):
            return self._pattern_value(expr, derivation, seen)
        if isinstance(expr, (ast.MatchStar, ast.MatchMapping)):
            return True  # *rest and **rest are fresh builtin list/dict objects.
        if isinstance(expr, ast.Name):
            return self.name(expr, derivation, seen)
        return False

    def elements(self, expr, derivation, seen=frozenset()) -> bool:
        """Iterating ``expr`` yields objects of external type."""
        if isinstance(expr, ast.Call):
            func = expr.func
            if (isinstance(func, ast.Name) and func.id in _TRANSPARENT and expr.args
                    and derivation.resolve(func) is None):
                return self.elements(expr.args[0], derivation, seen)
            if isinstance(func, ast.Name) and func.id in _BUILTIN_TYPES | {"enumerate", "zip", "map", "filter"}:
                return False  # a builtin wrapper says nothing about what it wraps
            return self._callee(func, derivation, seen)
        if isinstance(expr, ast.Name):
            key = derivation.resolve(expr)
            if key is None or key in seen:
                return False
            values = derivation.bindings.get(key, [])
            return bool(values) and all(
                v is not _FOREIGN and not isinstance(v, ast.arg) and id(v) not in derivation.iterated
                and self.elements(v, derivation, seen | {key}) for v in values)
        return False

    def name(self, name_node, derivation, seen=frozenset()) -> bool:
        """Every value ``name_node`` may hold, in the scope it resolves to, is external."""
        key = derivation.resolve(name_node)
        if key is None:
            values = self.module_values.get(name_node.id, [])
            return len(values) == 1 and values[0] is not None and self.value(values[0], derivation, seen)
        if key in seen or key in derivation.module_keys:
            return False
        values = derivation.bindings.get(key, [])
        seen = seen | {key}

        def external(v):
            if v is _FOREIGN:
                return False
            if isinstance(v, ast.arg):
                return v.annotation is not None and self.annotation(v.annotation)
            if id(v) in derivation.iterated:
                return self.elements(v, derivation, seen)
            return self.value(v, derivation, seen)
        return bool(values) and all(external(v) for v in values)
