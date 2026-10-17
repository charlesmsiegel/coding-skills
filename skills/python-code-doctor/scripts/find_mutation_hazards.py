#!/usr/bin/env python3
"""
Detect mutation hazards in Python code via AST analysis.

Findings identify mutation hazards; declarations without mutation evidence are
candidates with benign explanations. None overlap with the existing
detectors: find_code_smells flags the *declaration* of a mutable default
argument; this script flags the *bug* of actually mutating shared or aliased
state.

Finds:
  - mutable_class_attribute : demonstrated instance mutation of a shared class
                              value, or a candidate when only its declaration is known
  - modify_during_iteration : mutating a list/dict/set while iterating over it
  - mutated_default_arg     : a mutable default argument that is mutated in the
                              body, so its contents leak across calls
"""

import ast
import functools
import json
import argparse
from pathlib import Path
from dataclasses import asdict
from collections import defaultdict
from common import Finding as CodeSmell, sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files, warn_detector_error, warn_unparseable


_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

_MUTABLE_BUILTINS = {"list", "dict", "set", "bytearray", "defaultdict",
                     "OrderedDict", "Counter", "deque"}

_MUTATING_METHODS = {
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse",
    "add", "discard", "update", "setdefault", "popitem",
    "appendleft", "popleft", "extendleft", "rotate",
}

# Iterating over any of these produces a fresh snapshot, so mutating the
# original collection inside the loop is safe.
_SNAPSHOT_FUNCS = {"list", "tuple", "set", "frozenset", "sorted", "reversed", "dict"}


def _same_scope_nodes(stmts, stop_types=()):
    """Yield descendants of `stmts` that live in the same lexical scope: descend
    through compound statements but never into nested functions/lambdas (and
    never into `stop_types`, e.g. nested loops)."""
    stack = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARIES) or (stop_types and isinstance(child, stop_types)):
                continue
            stack.append(child)


def _mutable_type(value) -> str | None:
    for nodes, name in (((ast.List, ast.ListComp), "list"),
                        ((ast.Dict, ast.DictComp), "dict"),
                        ((ast.Set, ast.SetComp), "set")):
        if isinstance(value, nodes):
            return name
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in _MUTABLE_BUILTINS:
        return value.func.id
    return None


def _is_mutable_value(value) -> bool:
    return _mutable_type(value) is not None


def _ignored_model_types(node: ast.ClassDef) -> set[str] | None:
    """A local field-discovery override; None inherits, an empty set resets it."""
    values = []
    for item in node.body:
        if isinstance(item, ast.ClassDef) and item.name == "Config":
            for option in item.body:
                if isinstance(option, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "keep_untouched" for t in option.targets):
                    values.append(option.value)
        if not isinstance(item, ast.Assign) or not any(
                isinstance(t, ast.Name) and t.id == "model_config" for t in item.targets):
            continue
        config = item.value
        if isinstance(config, ast.Dict):
            values.extend(value for key, value in zip(config.keys, config.values)
                          if isinstance(key, ast.Constant) and key.value == "ignored_types")
        elif isinstance(config, ast.Call):
            values.extend(kw.value for kw in config.keywords if kw.arg == "ignored_types")
    if not values:
        return None
    value = values[-1]
    names = ({elt.id for elt in value.elts if isinstance(elt, ast.Name)}
             if isinstance(value, (ast.Tuple, ast.List, ast.Set)) else set())
    return _MUTABLE_BUILTINS if "object" in names else names & _MUTABLE_BUILTINS


def _annotation_is_classvar_or_final(annotation) -> bool:
    node = annotation
    if isinstance(node, ast.Subscript):
        node = node.value
    name = node.id if isinstance(node, ast.Name) else (node.attr if isinstance(node, ast.Attribute) else None)
    return name in {"ClassVar", "Final"}


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _dotted_name(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


_MIGRATION_CONFIGURATION = {"dependencies", "operations", "replaces", "run_before"}


# Model bases whose metaclass copies every field default per instance. On
# these `tags: list[str] = []` is the documented way to declare a field, not a
# list every instance shares.
_COPYING_MODEL_BASES = frozenset({
    "pydantic.BaseModel", "pydantic.main.BaseModel",
    "pydantic.RootModel", "pydantic.root_model.RootModel",
    "pydantic_settings.BaseSettings", "sqlmodel.SQLModel",
})

# How far a base class is followed into other first-party modules. Deep enough
# for `Model(FrozenModel)` → `FrozenModel(BaseModel)` through a re-export or
# two; bounded because each hop parses a file.
_MAX_RESOLUTION_DEPTH = 8

def _resolver_for(path: Path) -> "ClassFamilyResolver | None":
    """One resolver per first-party module, parsed at most once per process."""
    return _resolver_at(path.resolve())


@functools.lru_cache(maxsize=None)
def _resolver_at(path: Path) -> "ClassFamilyResolver | None":
    try:
        _source, tree = cached_parse(path)
    except (OSError, SyntaxError, ValueError):
        return None
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}
    return ClassFamilyResolver(tree, parents, path)


def _module_file(directory: Path, parts: list[str]) -> Path | None:
    target = directory.joinpath(*parts)
    for candidate in ((target.with_suffix(".py") if parts else None), target / "__init__.py"):
        if candidate is not None and candidate.is_file():
            return candidate
    return None


class ClassFamilyResolver:
    """Resolve classes whose ancestry reaches a recognised framework base.

    Django families (views, model forms, migrations) are resolved within the
    file. Model families are followed into first-party modules too: hardy's
    `class Baseline(FrozenModel)` is a pydantic model only because
    `FrozenModel(BaseModel)` says so in another file, and a per-file answer
    charged every one of its fields as shared mutable state.
    """

    def __init__(self, tree: ast.Module, parents: dict[ast.AST, ast.AST], path: Path | None = None):
        self.parents = parents
        self.path = path
        self.bindings: dict[str, list[tuple[tuple[int, int], str, object]]] = defaultdict(list)
        self._families: dict[ast.ClassDef, set[str]] = {}
        self._ignored_types: dict[ast.ClassDef, set[str] | None] = {}
        for node in ast.walk(tree):
            if not self._at_module_scope(node):
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    target = alias.name if alias.asname else alias.name.split(".")[0]
                    self._bind(local, node, "import", target)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        if node.level == 0 and node.module:
                            self._bind(
                                alias.asname or alias.name,
                                node,
                                "import",
                                f"{node.module}.{alias.name}",
                            )
                        else:
                            self._bind(alias.asname or alias.name, node, "relimport",
                                       (node.level, node.module or "", alias.name))
            elif isinstance(node, ast.ClassDef):
                self._bind(node.name, node, "class", node, use_end=True)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._bind(node.name, node, "other", None, use_end=True)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self._bind(node.id, node, "other", None)
        for events in self.bindings.values():
            events.sort(key=lambda event: event[0])

    def _bind(self, name: str, node: ast.AST, kind: str, value, use_end=False) -> None:
        if use_end:
            position = (
                getattr(node, "end_lineno", node.lineno),
                getattr(node, "end_col_offset", node.col_offset),
            )
        else:
            position = (node.lineno, node.col_offset)
        self.bindings[name].append((position, kind, value))

    def _at_module_scope(self, node: ast.AST) -> bool:
        parent = self.parents.get(node)
        while parent is not None and not isinstance(parent, ast.Module):
            if isinstance(parent, (
                ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
            )):
                return False
            parent = self.parents.get(parent)
        return isinstance(parent, ast.Module)

    def _binding_at(self, node: ast.AST) -> tuple[str, object] | None:
        dotted = _dotted_name(node)
        if not dotted:
            return None
        first, separator, rest = dotted.partition(".")
        position = (node.lineno, node.col_offset)
        active = None
        for event in self.bindings.get(first, ()):
            if event[0] >= position:
                break
            active = event
        if active is None:
            return None
        _event_position, kind, value = active
        if kind == "import":
            return kind, value + (separator + rest if separator else "")
        if kind == "relimport":
            return kind, self._relative_binding(value, rest if separator else "")
        if kind == "class" and not separator:
            return kind, value
        return kind, None

    @staticmethod
    def _relative_binding(value, rest=""):
        """Normalize a stored relative import for both bases and re-exports."""
        level, module, name = value
        return (level, [*module.split("."), name] if module else [name],
                rest.split(".") if rest else [])

    @staticmethod
    def _imported_family(canonical: str | None) -> str | None:
        if not canonical:
            return None
        leaf = canonical.rsplit(".", 1)[-1]
        if canonical.startswith("django.views.") and leaf.endswith("View"):
            return "view"
        if canonical.startswith("django.forms.") and leaf == "ModelForm":
            return "modelform"
        if canonical.startswith("django.db.migrations.") and leaf == "Migration":
            return "migration"
        if canonical in _COPYING_MODEL_BASES:
            return "copying_model"
        return None

    def _external_class(self, directories: list[Path], parts: list[str], depth: int):
        """Resolver and name of the class `parts` names, found as a module path plus one
        class name under the first of ``directories`` that holds the module."""
        if depth > _MAX_RESOLUTION_DEPTH or len(parts) < 2:
            return None
        for directory in directories:
            for split in range(len(parts) - 1, 0, -1):
                module = _module_file(directory, parts[:split])
                if module is None or len(parts) - split != 1:
                    continue
                resolver = _resolver_for(module)
                return (resolver, parts[-1]) if resolver else None
        return None

    def named_families(self, name: str, depth: int) -> set[str]:
        """Families of the module-level class ``name`` — defined here or re-exported."""
        events = self.bindings.get(name)
        if not events:
            return set()
        _position, kind, value = events[-1]
        if kind == "class" and isinstance(value, ast.ClassDef):
            return self.families(value, depth=depth)
        if kind == "relimport":
            value = self._relative_binding(value)
        return self._binding_families(kind, value, depth)

    def _binding_families(self, kind: str, value, depth: int) -> set[str]:
        if kind == "import":
            imported = self._imported_family(value)
            if imported:
                return {imported}
        imported_class = self._imported_class(kind, value, depth)
        if imported_class is None:
            return set()
        resolver, name = imported_class
        return resolver.named_families(name, depth + 1)

    def _imported_class(self, kind: str, value, depth: int):
        """Use the same import graph for ancestry and inherited configuration."""
        if self.path is None:
            return None
        if kind == "import":
            if self._imported_family(value):
                return None  # framework defaults are not defined in first-party source
            # An absolute first-party import is found the way the interpreter
            # would find it from a source root: under some ancestor directory.
            return self._external_class([self.path.parent, *self.path.parent.parents],
                                        value.split("."), depth)
        if kind == "relimport":
            level, parts, rest = value
            anchor = self.path.parent
            for _ in range(level - 1):
                anchor = anchor.parent
            return self._external_class([anchor], [*parts, *rest], depth)
        return None

    def named_ignored_types(self, name: str, depth: int) -> set[str] | None:
        """Ignored mutable types of a class defined here or re-exported."""
        events = self.bindings.get(name)
        if not events:
            return None
        _position, kind, value = events[-1]
        if kind == "class" and isinstance(value, ast.ClassDef):
            return self.ignored_model_types(value, depth=depth)
        if kind == "relimport":
            value = self._relative_binding(value)
        imported_class = self._imported_class(kind, value, depth)
        if imported_class is None:
            return None
        resolver, name = imported_class
        return resolver.named_ignored_types(name, depth + 1)

    def ignored_model_types(self, node: ast.ClassDef, visiting=None, depth: int = 0) -> set[str] | None:
        """Static inherited field-discovery config, including explicit resets.

        Pydantic merges configuration keys from bases, then the subclass: a
        frozen=True override keeps ignored_types, while ignored_types=() clears
        it. Annotated fields are still copied, even when their type is ignored.
        """
        if node in self._ignored_types:
            return self._ignored_types[node]
        visiting = set() if visiting is None else visiting
        if node in visiting or depth > _MAX_RESOLUTION_DEPTH:
            return None
        visiting.add(node)
        ignored = None
        for base in node.bases:
            binding = self._binding_at(base)
            if binding is None:
                continue
            kind, value = binding
            if kind == "class" and isinstance(value, ast.ClassDef):
                inherited = self.ignored_model_types(value, visiting, depth)
            else:
                imported_class = self._imported_class(kind, value, depth)
                inherited = (imported_class[0].named_ignored_types(imported_class[1], depth + 1)
                             if imported_class is not None else None)
            if inherited is not None:
                ignored = inherited
        visiting.remove(node)
        local = _ignored_model_types(node)
        if local is not None:
            ignored = local
        self._ignored_types[node] = ignored
        return ignored

    def families(self, node: ast.ClassDef, visiting=None, depth: int = 0) -> set[str]:
        if node in self._families:
            return self._families[node]
        visiting = set() if visiting is None else visiting
        if node in visiting or depth > _MAX_RESOLUTION_DEPTH:
            return set()
        visiting.add(node)
        families = set()
        for base in node.bases:
            binding = self._binding_at(base)
            if binding is None:
                continue
            kind, value = binding
            if kind == "class" and isinstance(value, ast.ClassDef):
                families.update(self.families(value, visiting, depth))
            else:
                families.update(self._binding_families(kind, value, depth))
        visiting.remove(node)
        self._families[node] = families
        return families

    def is_family(self, node: ast.ClassDef, family: str) -> bool:
        return family in self.families(node)


class MutationHazardDetector(ast.NodeVisitor):
    def __init__(self, filename: str, source_lines: list, parents, classes, ignore=None):
        self.filename = filename
        self.source_lines = source_lines
        self.issues = []
        self.ignore = ignore or set()
        self.parents = parents
        self.classes = classes

    def _get_line(self, lineno: int) -> str:
        if 0 < lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].strip()[:80]
        return ""

    def _add(self, line, smell_type, desc, suggestion, severity="high"):
        if smell_type in self.ignore:
            return
        self.issues.append(CodeSmell(
            file=self.filename, line=line, smell_type=smell_type,
            description=desc, suggestion=suggestion, severity=severity,
            code_snippet=self._get_line(line),
        ))

    # -- mutable class attribute ------------------------------------------
    def _is_django_declaration(self, node: ast.ClassDef, name: str) -> bool:
        if name == "http_method_names" and self.classes.is_family(node, "view"):
            return True
        if name in _MIGRATION_CONFIGURATION and self.classes.is_family(node, "migration"):
            return True
        parent = self.parents.get(node)
        if name == "fields" and node.name == "Meta" and isinstance(parent, ast.ClassDef):
            return self.classes.is_family(parent, "modelform")
        return False

    def _shared_instance_mutation(self, node, name):
        """A deliberately narrow proof: direct mutation via an instance method,
        with no visible instance shadowing or unresolved inherited initializer.
        Aliases, decorators, and interprocedural writes remain candidates.
        """
        if node.bases or node.decorator_list or node.keywords:
            return None
        mutations = []
        for method in node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if method.decorator_list:
                # A decorated initializer could shadow the attribute too.
                return None
            params = list(method.args.posonlyargs) + list(method.args.args)
            if not params:
                continue
            receiver = params[0].arg

            def attribute(expr):
                return (isinstance(expr, ast.Attribute) and expr.attr == name
                        and isinstance(expr.value, ast.Name) and expr.value.id == receiver)

            # The walker skips child scopes; definitions passed as roots must
            # also be excluded before their unrelated receivers can be visited.
            method_body = (statement for statement in method.body
                           if not isinstance(statement, _SCOPE_BOUNDARIES + (ast.ClassDef,)))
            for child in _same_scope_nodes(method_body, stop_types=(ast.ClassDef,)):
                if isinstance(child, ast.Name) and child.id == receiver and isinstance(child.ctx, ast.Store):
                    return None
                # Assignment/deletion of the attribute itself changes lookup.
                if attribute(child) and isinstance(child.ctx, (ast.Store, ast.Del)):
                    return None
                if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                        and child.func.attr in _MUTATING_METHODS and attribute(child.func.value)):
                    mutations.append(child.lineno)
                if (isinstance(child, ast.Subscript) and attribute(child.value)
                        and isinstance(child.ctx, (ast.Store, ast.Del))):
                    mutations.append(child.lineno)
        return min(mutations) if mutations else None

    def _class_attribute(self, node, item, target):
        if _is_dunder(target.id) or self._is_django_declaration(node, target.id):
            return
        if "mutable_class_attribute" in self.ignore:
            return
        mutation = self._shared_instance_mutation(node, target.id)
        if mutation:
            self._add(item.lineno, "mutable_class_attribute",
                f"Instance method mutates shared class attribute '{target.id}' on line {mutation}; "
                f"instances of {node.name} use the same mutable object",
                "Create a fresh collection for each instance in __init__ if this is instance state. "
                "For an intentional shared registry, declare and document it as ClassVar.")
        else:
            self.issues.append(CodeSmell(
                file=self.filename, line=item.lineno, smell_type="mutable_class_attribute",
                description=f"Class attribute '{target.id}' holds a mutable value; "
                            "no mutation of shared instance state is established here",
                severity="low", kind="candidate", code_snippet=self._get_line(item.lineno),
                also_caused_by=(
                    "Declarative framework configuration, including inherited Django ModelForm Meta options.",
                    "A read-only lookup table or an intentionally shared registry.",
                    "Instance initialization or inherited behavior may replace or copy the value before mutation.",
                ),
            ))

    def visit_ClassDef(self, node: ast.ClassDef):
        copying_model = self.classes.is_family(node, "copying_model")
        ignored_types = (self.classes.ignored_model_types(node) or set()) if copying_model else set()
        for item in node.body:
            if isinstance(item, ast.Assign) and _is_mutable_value(item.value):
                # Fields are copied, but ignored_types/keep_untouched leave
                # unannotated attributes as ordinary, shared class state.
                if copying_model and _mutable_type(item.value) not in ignored_types:
                    continue
                for target in item.targets:
                    if copying_model and isinstance(target, ast.Name) and target.id == "model_config":
                        continue
                    if isinstance(target, ast.Name):
                        self._class_attribute(node, item, target)
            elif isinstance(item, ast.AnnAssign) and item.value is not None and _is_mutable_value(item.value):
                if copying_model:
                    continue
                if _annotation_is_classvar_or_final(item.annotation):
                    continue
                if isinstance(item.target, ast.Name):
                    self._class_attribute(node, item, item.target)
        self.generic_visit(node)

    # -- modify during iteration ------------------------------------------
    def _iterated_collection_name(self, it):
        """Name of the collection being iterated if mutating it would be unsafe;
        None when the iterable is a safe snapshot or can't be resolved."""
        if isinstance(it, ast.Name):
            return it.id
        if isinstance(it, ast.Call):
            func = it.func
            if isinstance(func, ast.Name) and func.id in _SNAPSHOT_FUNCS:
                return None                      # list(x), sorted(x), ... -> snapshot
            if isinstance(func, ast.Attribute) and func.attr == "copy":
                return None                      # x.copy() -> snapshot
            if isinstance(func, ast.Attribute) and func.attr in {"keys", "values", "items"}:
                if isinstance(func.value, ast.Name):
                    return func.value.id         # iterating a live dict view
        return None

    def _check_for(self, node):
        name = self._iterated_collection_name(node.iter)
        if not name:
            return
        body_nodes = list(_same_scope_nodes(node.body, stop_types=(ast.For, ast.AsyncFor, ast.While)))
        # "mutate then break" is the one safe in-place pattern; skip if it breaks.
        if any(isinstance(n, ast.Break) for n in body_nodes):
            return
        for n in body_nodes:
            mutates = False
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr in _MUTATING_METHODS and isinstance(n.func.value, ast.Name) and n.func.value.id == name:
                    mutates = True
            elif isinstance(n, ast.Delete):
                for t in n.targets:
                    if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == name:
                        mutates = True
            if mutates:
                self._add(getattr(n, "lineno", node.lineno), "modify_during_iteration",
                    f"'{name}' is mutated while it is being iterated over",
                    "Iterate over a copy (e.g. `for x in list(coll):`) or build a new collection instead of "
                    "mutating in place.")
                return  # one report per loop is enough

    def visit_For(self, node: ast.For):
        self._check_for(node)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor):
        self._check_for(node)
        self.generic_visit(node)

    # -- mutated default argument -----------------------------------------
    def _mutable_default_params(self, args: ast.arguments):
        names = set()
        positional = list(getattr(args, "posonlyargs", [])) + list(args.args)
        defaults = list(args.defaults)
        if defaults:
            for arg, default in zip(positional[-len(defaults):], defaults):
                if _is_mutable_value(default):
                    names.add(arg.arg)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            if default is not None and _is_mutable_value(default):
                names.add(arg.arg)
        return names

    def _check_function(self, node):
        params = self._mutable_default_params(node.args)
        if not params:
            return
        body_nodes = list(_same_scope_nodes(node.body))
        for name in params:
            reassigned = False
            mutated_line = None
            for n in body_nodes:
                # Rebinding the name to a fresh value makes it a local; no longer the shared default.
                if isinstance(n, ast.Assign):
                    if any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
                        reassigned = True
                elif isinstance(n, ast.AnnAssign):
                    if isinstance(n.target, ast.Name) and n.target.id == name and n.value is not None:
                        reassigned = True
                # In-place mutation of the default object.
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                    if n.func.attr in _MUTATING_METHODS and isinstance(n.func.value, ast.Name) and n.func.value.id == name:
                        mutated_line = mutated_line or getattr(n, "lineno", node.lineno)
                elif isinstance(n, ast.Delete):
                    for t in n.targets:
                        if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == name:
                            mutated_line = mutated_line or getattr(n, "lineno", node.lineno)
                elif isinstance(n, (ast.Assign, ast.AugAssign)):
                    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                    for t in targets:
                        if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == name:
                            mutated_line = mutated_line or getattr(n, "lineno", node.lineno)
            if mutated_line and not reassigned:
                self._add(mutated_line, "mutated_default_arg",
                    f"Mutable default argument '{name}' is mutated in {node.name}; its contents persist across calls",
                    "Default the parameter to None and create a fresh list/dict/set inside the function body.")

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._check_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._check_function(node)
        self.generic_visit(node)


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source, tree = cached_parse(filepath)
        lines = source.splitlines()
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        classes = ClassFamilyResolver(tree, parents, Path(filepath))
        detector = MutationHazardDetector(str(filepath), lines, parents, classes, ignore)
        detector.visit(tree)
        return detector.issues
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect mutation hazards in Python")
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
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No mutation hazards found!")
            return

        by_type = defaultdict(int)
        for issue in all_issues:
            by_type[issue.smell_type] += 1

        proven = sum(issue.kind == "finding" for issue in all_issues)
        print(f"Found {proven} mutation hazard(s), {len(all_issues) - proven} candidate(s):\n")
        print("Summary:")
        for smell, count in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {smell}: {count}")
        print()

        severity_icons = SEVERITY_ICONS
        for issue in all_issues:
            icon = severity_icons[issue.severity]
            marker = "CANDIDATE" if issue.kind == "candidate" else issue.severity.upper()
            print(f"{icon} [{marker}] {issue.file}:{issue.line}")
            print(f"   {issue.smell_type}: {issue.description}")
            if issue.code_snippet:
                print(f"   Code: {issue.code_snippet}")
            if issue.suggestion:
                print(f"   → {issue.suggestion}")
            for reason in issue.also_caused_by:
                print(f"   ? also caused by: {reason}")
            print()


if __name__ == "__main__":
    main()
