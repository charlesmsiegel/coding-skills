#!/usr/bin/env python3
"""
Analyze class coupling and cohesion metrics.

Finds:
  - feature_envy  : a method that reads another object's fields more than its
                    own. Reads of a module (`torch.cat`) are not envy, and a
                    local taken from self (`settings = self._settings()`,
                    `for job in self._jobs.values()`) is the object's own data.
  - low_cohesion  : LCOM4 — the class's methods fall into two or more groups
                    that share no instance attribute and never call each other.
  - message_chain : a long `a.b.c.d` navigation inside a method.
  - middle_man    : most public methods do nothing but forward to one delegate.

A method is a def directly in the class body; a function or lambda nested in a
method is part of that method, so its reads count as the method's.
"""

import ast
import sys
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict, field
from collections import Counter, defaultdict
from common import first_party_modules, is_test_file, project_root, sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files, warn_detector_error, warn_unparseable
from find_mutation_hazards import ClassFamilyResolver
from find_import_cycles import _module_to_path, _resolve_relative_import
from coupling_bindings import _Derivation, _FOREIGN, _SCOPES, _TRANSPARENT, _method_nodes, _target_names


@dataclass
class CouplingIssue:
    file: str
    line: int
    issue_type: str
    name: str
    description: str
    metric_value: float
    suggestion: str
    severity: str
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


_INTERFACE_BASES = frozenset({"Protocol", "ABC"})
_VALUE_DECORATORS = frozenset({"dataclass", "define", "frozen", "attrs"})
# Bases that dictate no methods: inheriting from them adds no interface.
_NEUTRAL_BASES = frozenset({"object", "Generic"})

_ENVY_REASONS = (
    "the receiver is a value object, a configuration or a strategy's context that "
    "the method is meant to read — moving the method would make the data depend "
    "on its reader",
    "the method coordinates this class with the receiver, and either home would "
    "read the other's attributes",
)
_INHERITED_COHESION_REASONS = (
    "the base class or interface dictates the methods, so they share the "
    "interface's purpose rather than the subclass's attributes",
)


# A declared record — a dataclass, attrs class, NamedTuple, TypedDict or pydantic
# model — is one value: its generated __init__/__repr__/__eq__ (or validation)
# use every declared field together, so views over different fields are not a
# split. Only state the record does not declare can form a separate group.
_RECORD_DECORATORS = frozenset({"dataclass", "define", "frozen", "mutable", "attrs"})
_RECORD_BASES = frozenset({"NamedTuple", "TypedDict", "BaseModel", "BaseSettings"})
_FIELDS = "<declared fields>"


def _name_of(node: ast.AST) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Subscript):
        target = target.value
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return None


def _declared_fields(node: ast.ClassDef, classes: ClassFamilyResolver | None = None) -> set:
    """The fields a record declares as class-level annotations, or nothing if it is not one."""
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        names.add(getattr(target, "id", getattr(target, "attr", None)))
        if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == "attr" and target.attr == "s"):
            names.add("attrs")
    for base in node.bases:
        target = base.value if isinstance(base, ast.Subscript) else base
        names.add(getattr(target, "id", getattr(target, "attr", None)))
    inherited_model = classes is not None and classes.is_family(node, 'copying_model')
    if not (names & _RECORD_DECORATORS or names & _RECORD_BASES or inherited_model):
        return set()
    return {item.target.id for item in node.body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            # Pydantic private attributes are not validated model fields.
            and (not inherited_model or not item.target.id.startswith('_'))
            and "ClassVar" not in ast.unparse(item.annotation)}


# Initializers assign every attribute the class has, so as LCOM4 nodes they
# would join every group into one and hide exactly the split being measured.
_INITIALIZERS = frozenset({"__init__", "__new__", "__post_init__", "__init_subclass__"})
# A representation can show any slice of an object without adding a behavioral
# responsibility. Reading every field to print it cannot join real responsibilities
# either, so these methods are neither vertices nor bridges in the cohesion graph.
# Other protocols (__len__, __iter__, __call__, ...) still express behavior.
_REPRESENTATIONS = frozenset({"__repr__", "__str__"})
_METHOD_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
_VALIDATOR_DECORATORS = frozenset({
    "model_validator", "field_validator", "validator", "root_validator",
    "validates", "validates_schema",
})


def _is_validator(method) -> bool:
    for decorator in method.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if getattr(target, "id", getattr(target, "attr", None)) in _VALIDATOR_DECORATORS:
            return True
    return False


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _receiver(func) -> str | None:
    """The name a method's body calls its instance (or class) by; None for a staticmethod."""
    for dec in func.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if getattr(target, "id", getattr(target, "attr", None)) == "staticmethod":
            return None
    params = [*func.args.posonlyargs, *func.args.args]
    return params[0].arg if params else None


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
        for node in ast.walk(tree):
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
                values.extend(writer.nonlocal_writes.get((scope_id, name), []))
        return values

    def _lexical_binding(self, expression, name):
        """Nearest possible values, or None for an unbound/builtin name.

        Global selects the module, not an enclosing local and not a foreign
        value. Both representation dispatch and pattern typing use this same
        ownership lookup over the existing inventories.
        """
        current = self.parents.get(expression)
        module_only = False
        module_writes = []
        nonlocal_writes = []
        first_scope = True
        while current is not None:
            if isinstance(current, ast.ClassDef) and first_scope:
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
            current = self.parents.get(current)
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

    def _pattern_class_root(self, expression) -> bool:
        """Every identity alias must ground in an actual external import.

        Resolve each RHS in its own source scope, not the pattern's scope.
        Iteration keeps flat, long alias chains off the Python call stack;
        cycles, extracted data and any possible foreign replacement decline.
        """
        pending = [(expression, False)]
        active, proven = set(), set()
        while pending:
            current, leaving = pending.pop()
            root = self._root_name(current)
            if root is None:
                return False
            key = (current, root)
            if key in proven:
                continue
            if leaving:
                active.remove(key)
                proven.add(key)
                continue
            if key in active:
                return False
            values = self._lexical_binding(current, root)
            if values is None:
                if isinstance(current, ast.Name) and root in _BUILTIN_TYPES:
                    proven.add(key)
                    continue
                return False
            if not values:
                return False
            active.add(key)
            pending.append((current, True))
            for value in values:
                if isinstance(value, ast.Import):
                    modules = [alias.name.split(".")[0] for alias in value.names
                               if (alias.asname or alias.name.split(".")[0]) == root]
                elif isinstance(value, ast.ImportFrom) and not value.level:
                    modules = [(value.module or "").split(".")[0]]
                elif isinstance(value, (ast.Name, ast.Attribute)) and self._identity_assignment(value, root):
                    pending.append((value, False))
                    continue
                else:
                    return False
                if not modules or not all(self._external_module(top) for top in modules):
                    return False
        return True

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
            # The lexical root proof already checks the actual external import.
            # The general callee reader treats method-local imports as unknown,
            # which would discard that proof for both bare and qualified classes.
            return self._pattern_class_root(pattern.cls)
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


def _chain_length(node: ast.Attribute) -> int:
    length = 1
    current = node.value
    while isinstance(current, ast.Attribute):
        length += 1
        current = current.value
    return length


def _forwarded_to(func, receiver: str | None, method_names: set) -> str | None:
    """The attribute a method only forwards to, or None.

    Forwarding is a body (docstring aside) that is a single `return` or
    expression statement calling `self.<attr>.<name>(...)` or `self.<attr>(...)`
    — and `self.<attr>` is not a sibling method, which would be an alias.
    """
    if receiver is None:
        return None
    body = func.body
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], (ast.Return, ast.Expr)):
        return None
    value = body[0].value
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
        return None
    callee = value.func
    if (isinstance(callee.value, ast.Attribute) and isinstance(callee.value.value, ast.Name)
            and callee.value.value.id == receiver):
        return callee.value.attr
    if (isinstance(callee.value, ast.Name) and callee.value.id == receiver
            and callee.attr not in method_names):
        return callee.attr
    return None


@dataclass
class _Method:
    """What one method reads: via self, via self-derived locals, and elsewhere."""

    name: str
    line: int
    receiver: str | None
    self_names: list = field(default_factory=list)   # `self.<name>`, in source order
    derived_reads: set = field(default_factory=set)   # `<self-derived local>.<name>`
    other: dict = field(default_factory=dict)        # foreign name -> distinct data attributes
    chains: list = field(default_factory=list)
    params: set = field(default_factory=set)          # the method's own parameter names
    constructs: bool = False                          # builds the object (see _constructs)
    validates: bool = False                           # one field invariant, not a responsibility


def _constructs(func, receiver) -> bool:
    """`__init__` / `__new__` / `__post_init__`, or a `from_*` classmethod that
    returns `cls(...)`: a method whose job is to build the object from what it
    was handed."""
    if func.name in _INITIALIZERS - {"__init_subclass__"}:
        return True
    is_classmethod = any(getattr(dec, "id", getattr(dec, "attr", None)) == "classmethod"
                         for dec in func.decorator_list)
    return (is_classmethod and func.name.startswith("from_") and receiver is not None
            and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == receiver
                    for n in _method_nodes(func.body)))


def _read_method(func, module_names: set, types: "_ExternalTypes | None" = None) -> _Method:
    receiver = _receiver(func)
    derivation = _Derivation(func, receiver)
    method = _Method(func.name, func.lineno, receiver)
    method.params = {a.arg for a in [*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs,
                                     func.args.vararg, func.args.kwarg] if a is not None}
    method.constructs = _constructs(func, receiver)
    method.validates = _is_validator(func)
    called = {id(node.func) for node in _method_nodes(func.body) if isinstance(node, ast.Call)}
    for node in _method_nodes(func.body):
        if (types is not None and isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords
                and isinstance(node.args[0], ast.Name) and derivation.receiver_key is not None
                and derivation.resolve(node.args[0]) == derivation.receiver_key
                # Self-derived replacements can be a child with a different protocol.
                and derivation.receiver_key not in derivation.unpacked
                and all(derivation.binding_identity(value, derivation.receiver_key,
                            lambda _inventory, _expression, _key: False)
                        for value in (derivation.bindings[derivation.receiver_key]
                                      + types._nonlocal_write_values(func, receiver)))
                and not any(_name_of(dec) == "classmethod" for dec in func.decorator_list)):
            representation = types.representation_builtin(node.func, derivation)
            if representation is not None:
                method.self_names.append(representation)
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        if isinstance(base, ast.Name):
            key = derivation.resolve(base)
            if key is not None and key == derivation.receiver_key:
                method.self_names.append(node.attr)
            elif key is not None and derivation.module_namespace(
                    base, types.imported_namespace if types else None, types.global_namespace if types else None):
                pass  # module/owned-data reads are safe only when every write proves it
            elif key is not None and derivation.state.get(key):
                method.derived_reads.add(f"{base.id}.{node.attr}")
            elif key is None and base.id in module_names:
                pass  # a module has no class to move the method to
            elif key is not None and derivation.bindings[key] and all(
                    isinstance(value, ast.ClassDef) and not (value.bases or value.keywords or value.decorator_list)
                    for value in derivation.bindings[key]):
                pass  # a proven class namespace is not another instance's data
            elif types is not None and types.name(base, derivation):
                pass  # a library or builtin object: nowhere to move the method to
            elif key is not None and derivation.bindings[key] and all(
                    id(v) in derivation.iterated for v in derivation.bindings[key]):
                pass  # iteration elements are consumed here; the loop is not their owner
            elif key is not None and id(node) not in called and isinstance(node.ctx, ast.Load):
                # Only local objects' distinct data reads count. Calls delegate work;
                # free names can be modules, classes or constants rather than instances.
                method.other.setdefault(base.id, set()).add(node.attr)
        length = _chain_length(node)
        if length >= 3:
            method.chains.append(length)
    return method


def _behavior_names(methods: dict, name: str, seen: frozenset = frozenset()):
    """A behavior keeps the state it reaches through a presentation protocol.

    The protocol itself is not a responsibility, but delegating a save's state
    access to it does not make that save stateless. A normal caller can also
    coordinate real behaviors through the representation that calls them.
    """
    for touched in methods[name].self_names:
        if touched in _REPRESENTATIONS and touched in methods:
            if touched not in seen:
                yield from _behavior_names(methods, touched, seen | {touched})
        else:
            yield touched


def _groups(methods: dict, fields: set = frozenset()) -> list[list[str]]:
    """LCOM4: connected components of the methods that touch instance state.

    Two methods are joined when they touch a common `self.<attribute>` (read or
    write; a property read through `self.prop` reaches the property's own node)
    or when one refers to the other through self; a record's declared ``fields``
    count as one attribute (see _declared_fields). Initializers and representations
    are left out, and so is a method with no edge at all: one that uses no instance
    state is a static-method candidate, not evidence that the class splits.
    """
    names = [n for n in methods
             if n not in _INITIALIZERS and n not in _REPRESENTATIONS and not methods[n].validates]
    parent = {n: n for n in names}

    def find(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    def union(a, b):
        parent[find(a)] = find(b)

    linked = set()
    holders = defaultdict(list)
    for name in names:
        for touched in _behavior_names(methods, name):
            if touched in parent and touched != name:
                union(name, touched)
                linked.update((name, touched))
            elif touched not in methods and not _is_dunder(touched):
                holders[_FIELDS if touched in fields else touched].append(name)
                linked.add(name)
    for group in holders.values():
        for other in group[1:]:
            union(group[0], other)

    components: dict[str, list[str]] = {}
    for name in names:
        if name in linked:
            components.setdefault(find(name), []).append(name)
    return list(components.values())


def _describe_groups(groups: list[list[str]]) -> str:
    spelled = ["{" + ", ".join(group) + "}" for group in groups]
    return ", ".join(spelled[:-1]) + " and " + spelled[-1]


def coupling_issues(tree: ast.AST, filename: str, in_tests: bool = False) -> list[CouplingIssue]:
    """Coupling findings for every class in ``tree``.

    ``in_tests`` drops low_cohesion and feature_envy: a test double's methods
    are shaped by the interface it fakes (a recording fake reads the argument
    it records), and a test class's by the cases it holds.
    """
    module_names = {alias.asname or alias.name.split(".")[0]
                    for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
    issues: list[CouplingIssue] = []
    types = _ExternalTypes(tree, filename)
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}
    classes = ClassFamilyResolver(tree, parents, Path(filename))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            issues.extend(_analyze_class(node, filename, module_names, in_tests, types, classes))
    return issues


def _analyze_class(node: ast.ClassDef, filename: str, module_names: set,
                   in_tests: bool = False, types: "_ExternalTypes | None" = None,
                   classes: ClassFamilyResolver | None = None) -> list[CouplingIssue]:
    defs = [item for item in node.body if isinstance(item, _METHOD_TYPES)]
    if len(defs) < 2:
        return []
    methods: dict[str, _Method] = {}
    for func in defs:  # a property's getter and setter share a name: one node
        read = _read_method(func, module_names, types)
        if func.name in methods:
            merged = methods[func.name]
            merged.self_names += read.self_names
            merged.derived_reads |= read.derived_reads
            for name, attributes in read.other.items():
                merged.other.setdefault(name, set()).update(attributes)
            merged.chains += read.chains
            merged.params |= read.params
            merged.constructs = merged.constructs and read.constructs
            merged.validates = merged.validates or read.validates
        else:
            methods[func.name] = read

    issues: list[CouplingIssue] = []
    class_name = node.name

    # Low cohesion (LCOM4)
    interface = any(_name_of(base) in _INTERFACE_BASES for base in node.bases) or any(
        kw.arg == "metaclass" and _name_of(kw.value) == "ABCMeta" for kw in node.keywords)
    cohesion_methods = methods
    # With no explicit base, object.__str__ delegates to this class's __repr__.
    # A custom base can override __str__, so its fallback is not established.
    if not node.bases and "__str__" not in methods and "__repr__" in methods:
        cohesion_methods = {**methods, "__str__": methods["__repr__"]}
    groups = [] if in_tests or interface else _groups(cohesion_methods, _declared_fields(node, classes))
    inherited = any(_name_of(base) not in _NEUTRAL_BASES for base in node.bases)
    if len(groups) >= 2:
        issues.append(CouplingIssue(
            file=filename, line=node.lineno,
            issue_type="low_cohesion", name=class_name,
            description=(f"Class has low cohesion (LCOM4={len(groups)} over {sum(map(len, groups))} stateful methods): methods fall into "
                         f"{len(groups)} groups sharing no state: {_describe_groups(groups)}"),
            metric_value=len(groups),
            suggestion="" if inherited else "Each group is a responsibility of its own: split the class along these "
                       "groups, or move the group that does not belong to the class whose state it uses",
            severity="medium" if len(groups) == 2 else "high",
            kind="candidate" if inherited else "finding",
            also_caused_by=_INHERITED_COHESION_REASONS if inherited else (),
        ))

    # Feature Envy — reported at the envious method, which is what moves. Not in
    # test files: a fake reading the argument it records is doing its job.
    for method in ([] if in_tests else methods.values()):
        self_accesses = len(set(method.self_names) | method.derived_reads)
        # Lanza & Marinescu: concentrated interest in foreign data, measured
        # consistently on distinct attributes while retaining self-derived locals.
        foreign = {name: attributes for name, attributes in method.other.items()
                   if not (method.constructs and name in method.params)}
        atfd = sum(len(attributes) for attributes in foreign.values())
        if atfd >= 3 and self_accesses * 3 < self_accesses + atfd and len(foreign) <= 2:
            other_obj = max(foreign, key=lambda name: len(foreign[name]))
            count = len(foreign[other_obj])
            issues.append(CouplingIssue(
                file=filename, line=method.line,
                issue_type="feature_envy",
                name=f"{class_name}.{method.name}",
                description=f"Method '{method.name}' reads {count} of '{other_obj}'s attributes and {self_accesses} of its own",
                metric_value=atfd,
                suggestion="",
                severity="medium", kind="candidate", also_caused_by=_ENVY_REASONS,
            ))

    # Message Chains
    for method in methods.values():
        if method.chains and max(method.chains) >= 4:
            max_chain = max(method.chains)
            issues.append(CouplingIssue(
                file=filename, line=node.lineno,
                issue_type="message_chain",
                name=f"{class_name}.{method.name}",
                description=f"Long message chain (depth={max_chain})",
                metric_value=max_chain,
                suggestion="Add delegate method or restructure",
                severity="low" if max_chain < 5 else "medium",
            ))

    # Middle Man — most public methods only forward to a delegate.
    method_names = set(methods)
    public = {}
    for func in defs:
        if not func.name.startswith("_") and func.name not in public:
            public[func.name] = _forwarded_to(func, _receiver(func), method_names)
    delegating = Counter(target for target in public.values() if target)
    forwarding = sum(delegating.values())
    if len(public) >= 3 and forwarding * 2 > len(public):
        delegates = ", ".join(f"self.{name}" for name, _ in delegating.most_common())
        issues.append(CouplingIssue(
            file=filename, line=node.lineno,
            issue_type="middle_man", name=class_name,
            description=f"Class is a middle man: {forwarding} of {len(public)} public methods only forward to {delegates}",
            metric_value=forwarding,
            suggestion=f"Remove the middle man and let callers use {delegates} directly, "
                       "or give the class behaviour of its own",
            severity="low",
        ))
    return issues


def analyze_file(filepath: Path, ignore: set[str] = frozenset()) -> list[CouplingIssue]:
    try:
        _source, tree = cached_parse(filepath)
        return [i for i in coupling_issues(tree, str(filepath), is_test_file(filepath))
                if i.issue_type not in ignore]
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Analyze class coupling and cohesion")
    parser.add_argument('path', nargs='?', default='.', help='File or directory')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--ignore', type=str, default='', help='Comma-separated issue types to ignore')

    args = parser.parse_args()
    ignore = set(args.ignore.split(',')) if args.ignore else set()

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))
    
    sort_findings(all_issues)
    
    if args.format == 'json':
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No coupling/cohesion issues found!")
            return
        
        by_type = defaultdict(int)
        for issue in all_issues:
            by_type[issue.issue_type] += 1
        
        print(f"Found {len(all_issues)} coupling/cohesion issue(s):\n")
        print("Summary:")
        for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {t}: {c}")
        print()
        
        severity_icons = SEVERITY_ICONS
        
        for issue in all_issues:
            icon = severity_icons[issue.severity]
            print(f"{icon} [{issue.severity.upper()}] {issue.file}:{issue.line}")
            print(f"   {issue.issue_type}: {issue.name}")
            print(f"   {issue.description}")
            print(f"   → {issue.suggestion}\n")


if __name__ == '__main__':
    main()
