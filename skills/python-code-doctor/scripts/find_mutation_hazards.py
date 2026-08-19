#!/usr/bin/env python3
"""
Detect mutation hazards in Python code via AST analysis.

These are correctness bugs, not style nits. None overlap with the existing
detectors: find_code_smells flags the *declaration* of a mutable default
argument; this script flags the *bug* of actually mutating shared or aliased
state.

Finds:
  - mutable_class_attribute : a mutable value bound at class scope and therefore
                              shared by every instance of the class
  - modify_during_iteration : mutating a list/dict/set while iterating over it
  - mutated_default_arg     : a mutable default argument that is mutated in the
                              body, so its contents leak across calls
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import cached_parse, SEVERITY_ICONS, configure_output, find_python_files, warn_detector_error, warn_unparseable


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""


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


def _is_mutable_value(value) -> bool:
    if isinstance(value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in _MUTABLE_BUILTINS:
        return True
    return False


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


class DjangoClassResolver:
    """Resolve only classes whose ancestry reaches an imported Django class."""

    def __init__(self, tree: ast.Module, parents: dict[ast.AST, ast.AST]):
        self.parents = parents
        self.bindings: dict[str, list[tuple[tuple[int, int], str, object]]] = defaultdict(list)
        self._families: dict[ast.ClassDef, set[str]] = {}
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
                            self._bind(alias.asname or alias.name, node, "other", None)
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
        if kind == "class" and not separator:
            return kind, value
        return kind, None

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
        return None

    def families(self, node: ast.ClassDef, visiting=None) -> set[str]:
        if node in self._families:
            return self._families[node]
        visiting = set() if visiting is None else visiting
        if node in visiting:
            return set()
        visiting.add(node)
        families = set()
        for base in node.bases:
            binding = self._binding_at(base)
            if binding is None:
                continue
            kind, value = binding
            if kind == "import":
                imported = self._imported_family(value)
                if imported:
                    families.add(imported)
            elif kind == "class" and isinstance(value, ast.ClassDef):
                families.update(self.families(value, visiting))
        visiting.remove(node)
        self._families[node] = families
        return families

    def is_family(self, node: ast.ClassDef, family: str) -> bool:
        return family in self.families(node)


class MutationHazardDetector(ast.NodeVisitor):
    def __init__(self, filename: str, source_lines: list, parents, django_classes, ignore=None):
        self.filename = filename
        self.source_lines = source_lines
        self.issues = []
        self.ignore = ignore or set()
        self.parents = parents
        self.django_classes = django_classes

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
        if name == "http_method_names" and self.django_classes.is_family(node, "view"):
            return True
        if name in _MIGRATION_CONFIGURATION and self.django_classes.is_family(node, "migration"):
            return True
        parent = self.parents.get(node)
        if name == "fields" and node.name == "Meta" and isinstance(parent, ast.ClassDef):
            return self.django_classes.is_family(parent, "modelform")
        return False

    def visit_ClassDef(self, node: ast.ClassDef):
        for item in node.body:
            if isinstance(item, ast.Assign) and _is_mutable_value(item.value):
                for target in item.targets:
                    if isinstance(target, ast.Name) and not _is_dunder(target.id) \
                            and not self._is_django_declaration(node, target.id):
                        self._add(item.lineno, "mutable_class_attribute",
                            f"Class attribute '{target.id}' is a mutable value shared by every instance of {node.name}",
                            "Assign it in __init__ instead, or use an immutable constant (tuple/frozenset). "
                            "If a shared class variable is truly intended, annotate it ClassVar and document it.")
            elif isinstance(item, ast.AnnAssign) and item.value is not None and _is_mutable_value(item.value):
                if _annotation_is_classvar_or_final(item.annotation):
                    continue
                if isinstance(item.target, ast.Name) and not _is_dunder(item.target.id) \
                        and not self._is_django_declaration(node, item.target.id):
                    self._add(item.lineno, "mutable_class_attribute",
                        f"Class attribute '{item.target.id}' is a mutable default shared by every instance of {node.name}",
                        "Use dataclasses.field(default_factory=...) in dataclasses, assign in __init__, "
                        "or annotate ClassVar if a shared value is genuinely intended.")
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
        django_classes = DjangoClassResolver(tree, parents)
        detector = MutationHazardDetector(str(filepath), lines, parents, django_classes, ignore)
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

    all_issues.sort(key=lambda x: (x.severity != "high", x.severity != "medium", x.file, x.line))

    if args.format == "json":
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No mutation hazards found!")
            return

        by_type = defaultdict(int)
        for issue in all_issues:
            by_type[issue.smell_type] += 1

        print(f"Found {len(all_issues)} mutation hazard(s):\n")
        print("Summary:")
        for smell, count in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {smell}: {count}")
        print()

        severity_icons = SEVERITY_ICONS
        for issue in all_issues:
            icon = severity_icons[issue.severity]
            print(f"{icon} [{issue.severity.upper()}] {issue.file}:{issue.line}")
            print(f"   {issue.smell_type}: {issue.description}")
            if issue.code_snippet:
                print(f"   Code: {issue.code_snippet}")
            print(f"   → {issue.suggestion}\n")


if __name__ == "__main__":
    main()
