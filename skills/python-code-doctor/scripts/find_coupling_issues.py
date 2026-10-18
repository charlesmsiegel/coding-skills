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
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]


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
        self.module_keys: set = set()
        self.iterated: set = set()  # id() of the iterables a loop or comprehension binds from
        receiver_arg = ([*func.args.posonlyargs, *func.args.args] or [None])[0] if receiver else None
        if self.receiver_key:
            self.bindings[self.receiver_key] = []
        for node in _method_nodes([func]):
            self._collect(node, receiver_arg)
        self.state = {key: all(v is not _FOREIGN for v in values) for key, values in self.bindings.items()}
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

    def _bind(self, scope, name, value):
        if scope is not None:
            self.bindings[(id(scope), name)].append(value)

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

    def _collect(self, node, receiver_arg) -> None:
        if isinstance(node, ast.arg):  # the node itself, so its annotation can be read
            if node is not receiver_arg:
                self._bind(self._scope_of(node), node.arg, node)
            return
        if isinstance(node, ast.comprehension):
            scope = self.parent.get(id(node))
            self.iterated.add(id(node.iter))
            for name in _target_names(node.target):
                self._bind(scope, name, node.iter)
            return
        scope = self._scope_of(node)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                self._assign(scope, target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            self._assign(scope, node.target, node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self.iterated.add(id(node.iter))
            for name in _target_names(node.target):
                self._bind(scope, name, node.iter)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                key = (id(scope), alias.asname or alias.name.split(".")[0])
                self.bindings[key].append(_FOREIGN)
                self.module_keys.add(key)
        elif isinstance(node, ast.AugAssign):
            for name in _target_names(node.target):
                self._bind(scope, name, _FOREIGN)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            for name in _target_names(node.optional_vars):
                self._bind(scope, name, _FOREIGN)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            self._bind(scope, node.name, _FOREIGN)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                self._bind(scope, name, _FOREIGN)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
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
                if not skip and key in self.bindings:
                    return key
                from_first_iterable = False
            child, current = current, self.parent.get(id(current))
        return None

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
            elif isinstance(expr, ast.Name):
                key = self.resolve(expr)
                return key is not None and self.state.get(key, False)
            else:
                return False


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
        self.scope_bindings = {tree: _Derivation(tree, None).bindings}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.imports[alias.asname or alias.name.split(".")[0]] = alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                top = None if node.level else (node.module or "").split(".")[0]
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = top
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

    def representation_builtin(self, expression, derivation):
        """A bare builtin dispatch, with the same lexical binding inventory.

        Local/enclosing/module converters and unknown star imports cannot
        manufacture representation edges. Class attributes do not participate
        in an ordinary method's free-name lookup.
        """
        if not isinstance(expression, ast.Name) or expression.id not in {"str", "repr"}:
            return None
        if derivation.resolve(expression) is not None:
            return None
        current = self.parents.get(expression)
        while current is not None:
            if isinstance(current, _SCOPES):
                if current not in self.scope_bindings:
                    self.scope_bindings[current] = _Derivation(current, None).bindings
                bindings = self.scope_bindings[current]
                if (id(current), expression.id) in bindings or (id(current), "*") in bindings:
                    return None
            current = self.parents.get(current)
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

    def _item_annotation(self, ann) -> bool:
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
                return self._item_annotation(args[0])
            if head in _MAPPINGS:
                return self.annotation(args[-1])
            if head in _SEQUENCES:
                return self.annotation(args[0])
            return False
        # Indexing a library object (a Tensor) gives the library's objects back.
        return self.annotation(ann) and getattr(ann, "id", None) not in _BUILTIN_TYPES

    # -- values ---------------------------------------------------------------
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
            key = derivation.resolve(expr.value)
            values = derivation.bindings.get(key, []) if key else []
            return bool(values) and all(isinstance(v, ast.arg) and v.annotation is not None
                                        and self._item_annotation(v.annotation) for v in values)
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
            elif key is not None and key in derivation.module_keys:
                pass  # a module imported in the method: no class to move to
            elif key is not None and derivation.state.get(key):
                method.derived_reads.add(f"{base.id}.{node.attr}")
            elif key is None and base.id in module_names:
                pass  # a module has no class to move the method to
            elif types is not None and types.name(base, derivation):
                pass  # a library or builtin object: nowhere to move the method to
            elif key is not None and all(id(v) in derivation.iterated for v in derivation.bindings[key]):
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
        if touched == "__str__" and touched not in methods and "__repr__" in methods:
            touched = "__repr__"  # object.__str__ delegates to the repr protocol.
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
    groups = [] if in_tests or interface else _groups(methods, _declared_fields(node, classes))
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
