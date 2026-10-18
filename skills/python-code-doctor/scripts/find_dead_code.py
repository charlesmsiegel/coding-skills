#!/usr/bin/env python3
"""
Detect dead code and unused code in Python.
Finds: unused imports, unused variables, unreachable code, unused functions/classes.

Three readings that look dead and are not:
  - an import in a package's `__init__.py` binds the name as a package
    attribute for other modules (`from . import sub` is what makes `pkg.sub`
    resolve), so a relative import there is the package's surface, never
    unused; any other import there is a candidate. Anywhere, a name used only
    in a string annotation (`def f(x: "Model")`) is used.
  - a bare `yield` after the last `return` or `raise` of a function with no
    other `yield` is what makes the function a generator: deleting it changes
    the function's kind.
  - a free function's unused parameter is a candidate, not a finding, when the
    function is handed out as a value (`register(mod.handler)`, through the
    module's name or an import alias of it) anywhere in the importable tree it
    lives in — whoever calls it dictates the signature.
"""

import ast
import io
import json
import argparse
import re
import symtable
import tokenize
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Set
from collections import defaultdict
from contextlib import contextmanager
from functools import lru_cache
from common import cached_parse, configure_output, find_python_files, warn_detector_error, warn_unparseable


_NOQA_CODES = re.compile(
    r"#\s*noqa:\s*([A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)",
    re.IGNORECASE,
)
_DJANGO_REGISTRATION_MODULES = {"checks", "signals"}

# `<receiver>.<name>` not followed by a call: a module's function handed out as a
# value. Found at every start so `pkg.mod.fn` yields (mod, fn) as well.
_ATTRIBUTE_VALUE = re.compile(r"(?<!\w)(?=([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b(?!\s*\())")
# `import x as y` / `from p import x as y`: the receiver a module goes by.
_ALIAS = re.compile(r"\b([A-Za-z_]\w*)\s+as\s+([A-Za-z_]\w*)\b")


def _import_root(filepath: Path) -> Path:
    """The directory a module is imported from: above its top-most package."""
    directory = filepath.parent
    while (directory / "__init__.py").exists() and directory.parent != directory:
        directory = directory.parent
    return directory


def _handed_out_in_tree(filepath: Path, function: str) -> bool:
    """True when some module under the file's import root loads
    `<this module>.<function>` without calling it — `register(art.handler)`.

    The receiver must be the module's own name or an alias one of those files
    gives it (`from . import art as art_ctx`), so an unrelated `lease.source`
    does not make every function called `source` look handed out. The tree is
    indexed as text, once per root per process and only when a finding needs
    it; a file whose text matches is then parsed to confirm the reference is
    code, not a mention in a docstring or comment.
    """
    pairs, aliases = _value_index(_import_root(filepath))
    receivers = {filepath.stem, *aliases.get(filepath.stem, ())}
    paths = set().union(*(pairs.get((receiver, function), set()) for receiver in receivers))
    return any(_loads_attribute(path, receivers, function) for path in sorted(paths))


@lru_cache(maxsize=None)
def _value_index(root: Path) -> tuple[dict, dict]:
    """Every `<receiver>.<name>` value reference under ``root``, by pair, and
    every alias a module is imported under, by original name."""
    pairs: dict[tuple[str, str], set[Path]] = {}
    aliases: dict[str, set[str]] = {}
    for path in find_python_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pair in _ATTRIBUTE_VALUE.findall(text):
            pairs.setdefault(pair, set()).add(path)
        for original, alias in _ALIAS.findall(text):
            aliases.setdefault(original, set()).add(alias)
    return pairs, aliases


def _loads_attribute(path: Path, receivers: set[str], attr: str) -> bool:
    """True when ``path`` loads `<receiver>.<attr>` other than to call it."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return False
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    return any(isinstance(node, ast.Attribute) and node.attr == attr and id(node) not in called
               and isinstance(node.value, ast.Name) and node.value.id in receivers
               for node in ast.walk(tree))


def _dotted_name(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


# Issues below this confidence are noise by default. Shared with the runner so
# both entry points apply the same floor.
DEFAULT_MIN_CONFIDENCE = 60


@dataclass
class DeadCodeIssue:
    file: str
    line: int
    issue_type: str
    name: str
    description: str
    confidence: int
    # A scored defect by default. Set to "candidate" for a lead the downstream
    # merge must NOT score: cross-module use is invisible to this file-local
    # analysis, so a public module-level name cannot be *proven* dead here. A
    # finding carries a fix; a candidate carries its benign explanations and no
    # fix, because recommending a deletion on file-local evidence is how live
    # code gets deleted.
    kind: str = "finding"
    suggestion: str = ""
    also_caused_by: tuple[str, ...] = ()


# What to do about a proven dead-code finding, by issue type. A finding must
# carry a fix; these are the fixes the description already implies.
_SUGGESTIONS = {
    "unused_import": "Remove the import, or re-export it through __all__ if it is public API.",
    "unused_variable": "Remove the assignment, or use the value.",
    "unused_parameter": "Remove the parameter, or prefix it with an underscore if the "
                        "signature is dictated by a caller.",
    "unreachable_code": "Delete the code after the return/raise, or fix the control flow "
                        "so it can run.",
    "constant_condition": "Replace the always-true/always-false condition with the branch "
                          "that runs, or fix the condition.",
    "dead_loop": "Remove the loop body that never executes, or fix the loop bounds.",
    "empty_if": "Remove the empty branch, or fill it with the intended action.",
}
_DEFAULT_SUGGESTION = "Remove it, or reference it where it is meant to be used."

_CANDIDATE_REASONS = {
    "unused_function": ("another module imports and calls it — cross-module use is "
                        "invisible to a single-file scan",
                        "it is looked up by name: a plugin registry, getattr, or a template"),
    "unused_class": ("another module imports and instantiates it",
                     "it is registered by name: an ORM model, a plugin, a serializer"),
    "unused_parameter": ("the signature is dictated by the receiver — a callback, a "
                         "registry entry, a dispatch-table row",),
    "unused_import": ("a package's __init__.py binds what it imports as package "
                      "attributes, and another module may read it as `package.name`",),
}


class ScopeTracker(ast.NodeVisitor):
    """Record uses in lexical namespaces, distinguishing immediate expressions
    from deferred bodies. Defaults and definition headers read the enclosing
    namespace; lambda/comprehension bindings stay local, and bodies do not
    close over class namespaces. Unresolved body casts get a second reading
    after enclosing bindings are known, without retracting earlier proven
    uses or pretending to simulate arbitrary call order.
    """

    def __init__(
        self,
        filename: str,
        parents: dict[ast.AST, ast.AST],
        comments: dict[int, list[str]],
        function_bindings: dict[tuple[int, str], set[str]],
    ):
        self.filename = filename
        self.issues: list[DeadCodeIssue] = []
        self.imports: dict[str, int] = {}
        self.from_imports: dict[str, int] = {}
        self.used_names: Set[str] = set()
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        self.dunder_all: Set[str] = set()
        self.called_functions: Set[str] = set()
        self.instantiated_classes: Set[str] = set()
        self.parents = parents
        self.comments = comments
        self.function_bindings = function_bindings
        self.import_origins: dict[str, str] = {}
        self._annotation_scopes = [("module", {})]
        # Unresolved calls in deferred bodies may use an import below their
        # definition. Keep their lexical namespaces alive until the module has
        # been read; an earlier proven use still stands if a name is rebound.
        self._deferred_casts: list[tuple[ast.Call, tuple]] = []
        self.current_app_config_names: set[str] = set()
        self.django_app_config_classes: set[ast.ClassDef] = set()
        self.function_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        # Names loaded as values rather than called: `Command("help", handle_help)`
        # hands the function to a registry, and whoever calls it later dictates
        # its parameters. Decided in finalize, once the whole file has been read.
        self.value_references: Set[str] = set()
        self.pending_params: list[tuple[DeadCodeIssue, ast.FunctionDef | ast.AsyncFunctionDef]] = []
        # Names bound by a relative import (or an absolute import of this
        # file's own package). In a package `__init__.py` that is a re-export.
        self.own_package_imports: Set[str] = set()
        self.is_package_init = Path(filename).name == "__init__.py"
        self.package_top = self._package_top(Path(filename))

    @staticmethod
    def _package_top(filepath: Path) -> str | None:
        root = _import_root(filepath)
        try:
            relative = filepath.parent.relative_to(root)
        except ValueError:
            return None
        return relative.parts[0] if relative.parts else None

    def _at_module_scope(self, node: ast.AST) -> bool:
        parent = self.parents.get(node)
        while parent is not None and not isinstance(parent, ast.Module):
            if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return False
            parent = self.parents.get(parent)
        return isinstance(parent, ast.Module)

    def _is_test_file(self) -> bool:
        base = Path(self.filename).name
        return base.startswith("test_") or base.endswith("_test.py") or base == "conftest.py"

    @staticmethod
    def _decorator_name(decorator: ast.AST) -> str | None:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute):
            return target.attr
        if isinstance(target, ast.Name):
            return target.id
        return None

    def _is_decorated(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> bool:
        return bool(node.decorator_list)

    def _is_fixture(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        return any(self._decorator_name(d) == "fixture" for d in node.decorator_list)

    @staticmethod
    def _is_test_function_name(name: str) -> bool:
        # pytest collects functions named test* by default.
        return name.startswith("test")

    def _collect_dunder_all(self, node: ast.Assign | ast.AugAssign) -> None:
        if not self._at_module_scope(node):
            return
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            return
        value = node.value
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            for elt in value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    self.dunder_all.add(elt.value)

    def visit_Assign(self, node: ast.Assign):
        self._collect_dunder_all(node)
        # Python evaluates the value with the old binding, then stores each
        # target. In `cast = cast("models.Model", value)` that order decides
        # whether the first argument is a type or ordinary string data.
        self.visit(node.value)
        for target in node.targets:
            self.visit(target)

    def visit_NamedExpr(self, node: ast.NamedExpr):
        self.visit(node.value)
        # Unlike a comprehension's iteration target, a walrus binds in its
        # containing non-comprehension scope (PEP 572).
        previous = self._annotation_scopes
        end = len(previous)
        while previous[end - 1][0] == "comprehension":
            end -= 1
        self._annotation_scopes = previous[:end]
        try:
            self.visit(node.target)
        finally:
            self._annotation_scopes = previous

    def visit_For(self, node: ast.For | ast.AsyncFor):
        self.visit(node.iter)
        self.visit(node.target)
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_If(self, node: ast.If):
        # The symbol table already establishes function-local names, including
        # unreachable declarations. Only the executed branch can replace an
        # import's progressive origin, in any scope.
        if isinstance(node.test, ast.Constant):
            self.visit(node.test)
            for statement in node.body if node.test.value else node.orelse:
                self.visit(statement)
        else:
            self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign):
        self._collect_dunder_all(node)
        self.generic_visit(node)

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
                out.update(origins)
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
        for call, enclosing in tuple(self._deferred_casts):
            previous = self._annotation_scopes
            self._annotation_scopes = list(enclosing)
            try:
                self._typing_cast_references(call)
            finally:
                self._annotation_scopes = previous

    def _remember_imports(self, node: ast.Import | ast.ImportFrom) -> None:
        self._remember_annotation_imports(node)
        if not self._at_module_scope(node):
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                target = alias.name if alias.asname else alias.name.split(".")[0]
                self.import_origins[local] = target
                self.current_app_config_names.discard(local)
        elif node.module or node.level:
            for alias in node.names:
                if alias.name != "*":
                    local = alias.asname or alias.name
                    self.current_app_config_names.discard(local)
                    if node.level == 0 and node.module:
                        self.import_origins[local] = f"{node.module}.{alias.name}"
                    else:
                        self.import_origins.pop(local, None)

    def _canonical_name(self, node: ast.AST, origins=None) -> str | None:
        dotted = _dotted_name(node)
        if not dotted:
            return None
        first, separator, rest = dotted.partition(".")
        imported = (self.import_origins if origins is None else origins).get(first)
        if not imported:
            return None
        return imported + (separator + rest if separator else "")

    def _line_suppresses_f401(self, line: int) -> bool:
        for comment in self.comments.get(line, ()):
            match = _NOQA_CODES.search(comment)
            if match is not None:
                codes = {code.strip().upper() for code in match.group(1).split(",")}
                if "F401" in codes:
                    return True
        return False

    def _suppresses_f401(self, node: ast.Import | ast.ImportFrom, alias: ast.alias) -> bool:
        end_line = getattr(node, "end_lineno", node.lineno)
        closing_line_has_alias = any(
            candidate.lineno <= end_line
            <= getattr(candidate, "end_lineno", candidate.lineno)
            for candidate in node.names
        )
        if self._line_suppresses_f401(node.lineno) or (
            not closing_line_has_alias and self._line_suppresses_f401(end_line)
        ):
            return True
        alias_end = getattr(alias, "end_lineno", alias.lineno)
        return any(
            self._line_suppresses_f401(line)
            for line in range(alias.lineno, alias_end + 1)
        )

    def _in_appconfig_ready(self) -> bool:
        if not self.function_stack:
            return False
        method = self.function_stack[-1]
        parent = self.parents.get(method)
        return method.name == "ready" and isinstance(parent, ast.ClassDef) \
            and parent in self.django_app_config_classes

    @staticmethod
    def _is_registration_import(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> bool:
        if isinstance(node, ast.Import):
            module = alias.name
        else:
            module = node.module or alias.name
            if module.rsplit(".", 1)[-1] not in _DJANGO_REGISTRATION_MODULES:
                module = alias.name
        return module.rsplit(".", 1)[-1] in _DJANGO_REGISTRATION_MODULES

    @staticmethod
    def _is_explicit_reexport(alias: ast.alias) -> bool:
        # `from m import x as x` / `import m as m`: the redundant alias is the
        # typed-Python convention (PEP 484) for "this name is part of my public
        # API". A facade module is nothing but these, and none of them is used
        # by the facade itself.
        return alias.asname is not None and alias.asname == alias.name

    def visit_Import(self, node: ast.Import):
        self._remember_imports(node)
        for alias in node.names:
            if self._suppresses_f401(node, alias) or self._is_explicit_reexport(alias) or \
                    (self._in_appconfig_ready() and self._is_registration_import(node, alias)):
                continue
            name = alias.asname if alias.asname else alias.name.split('.')[0]
            self.imports[name] = node.lineno
            if alias.name.split('.')[0] == self.package_top:
                self.own_package_imports.add(name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        self._remember_imports(node)
        if node.module == "__future__":
            # A compiler directive. It binds a name, but nothing is meant to read it.
            return
        for alias in node.names:
            if alias.name == '*':
                continue
            if self._suppresses_f401(node, alias) or self._is_explicit_reexport(alias) or \
                    (self._in_appconfig_ready() and self._is_registration_import(node, alias)):
                continue
            name = alias.asname if alias.asname else alias.name
            self.from_imports[name] = node.lineno
            if node.level or (node.module or "").split(".")[0] == self.package_top:
                self.own_package_imports.add(name)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self._annotation_scopes[-1][1][node.id] = None
        if isinstance(node.ctx, ast.Load):
            self.used_names.add(node.id)
            parent = self.parents.get(node)
            if not (isinstance(parent, ast.Call) and parent.func is node):
                self.value_references.add(node.id)
        elif isinstance(node.ctx, ast.Store) and self._at_module_scope(node):
            self.import_origins.pop(node.id, None)
            self.current_app_config_names.discard(node.id)
        self.generic_visit(node)

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
                self._annotation_references(expression)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            # Match the existing file-local, conservative treatment of names.
            self.used_names.add(node.id)
            self.value_references.add(node.id)
        if isinstance(node, ast.Subscript):
            name = self._canonical_name(node.value, self._annotation_origins())
            self._annotation_references(node.value)
            if name in {"typing.Literal", "typing_extensions.Literal"}:
                self._expression_references(node.slice)
                return  # String values are data, not forward references.
            if name in {"typing.Annotated", "typing_extensions.Annotated"}:
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
        if node.value is not None:
            self.visit(node.value)
        if node.value is not None or not isinstance(node.target, ast.Name):
            self.visit(node.target)
        self._annotation_references(node.annotation)
        self.visit(node.annotation)

    def _type_parameter_references(self, node):
        # PEP 695 bounds/constraints and PEP 696 defaults are type positions,
        # including strings. The visitor names are harmless on Python 3.11,
        # whose parser cannot produce these newer nodes.
        self._annotation_references(getattr(node, "bound", None))
        self._annotation_references(getattr(node, "default_value", None))
        self.generic_visit(node)

    visit_TypeVar = visit_ParamSpec = visit_TypeVarTuple = _type_parameter_references

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        # These expressions execute before the function's parameters bind.
        for expression in (*node.decorator_list, *node.args.defaults,
                           *(default for default in node.args.kw_defaults if default is not None)):
            self.visit(expression)
        for parameter in getattr(node, "type_params", ()):
            self.visit(parameter)
        self._signature_references(node)
        if node.col_offset == 0:
            self.functions[node.name] = node
        self._check_unreachable(node)
        self._check_unused_params(node)
        self._annotation_scopes[-1][1][node.name] = None
        if self._at_module_scope(node):
            self.import_origins.pop(node.name, None)
            self.current_app_config_names.discard(node.name)
        self.function_stack.append(node)
        try:
            with self._annotation_scope(node):
                for statement in node.body:
                    self.visit(statement)
        finally:
            self.function_stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _visit_function

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

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _visit_comprehension

    def visit_ClassDef(self, node: ast.ClassDef):
        if node.col_offset == 0:
            self.classes[node.name] = node
        is_app_config = any(
            self._canonical_name(base) == "django.apps.AppConfig"
            or (isinstance(base, ast.Name) and base.id in self.current_app_config_names)
            for base in node.bases
        )
        if is_app_config:
            self.django_app_config_classes.add(node)
        # A nested class's header still reads the enclosing class namespace;
        # only its body starts a new namespace, and its methods skip that one.
        for expression in (*node.decorator_list, *node.bases, *node.keywords,
                           *getattr(node, "type_params", ())):
            self.visit(expression)
        with self._annotation_scope(node):
            for statement in node.body:
                self.visit(statement)
        self._annotation_scopes[-1][1][node.name] = None
        if self._at_module_scope(node):
            self.import_origins.pop(node.name, None)
            if is_app_config:
                self.current_app_config_names.add(node.name)
            else:
                self.current_app_config_names.discard(node.name)

    def _call_references(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.called_functions.add(node.func.id)
            self.instantiated_classes.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            self.called_functions.add(node.func.attr)
        if not self._typing_cast_references(node) and self._unresolved_callee(node.func) and any(
                kind == "function" for kind, _origins in self._annotation_scopes):
            self._deferred_casts.append((node, tuple(self._annotation_scopes)))

    def _unresolved_callee(self, node: ast.AST) -> bool:
        dotted = _dotted_name(node)
        if not dotted:
            return False
        first = dotted.partition(".")[0]
        origins = self._annotation_origins()
        return first not in origins or origins[first] == ""

    def _typing_cast_references(self, node: ast.Call) -> bool:
        # Only typing's cast gives its first argument type semantics. An
        # unrelated converter.cast("Model", value) receives ordinary data,
        # including when it appears inside quoted Annotated metadata.
        callee = self._canonical_name(node.func, self._annotation_origins())
        if callee in {"typing.cast", "typing_extensions.cast"} and node.args:
            self._annotation_references(node.args[0])
            return True
        return False

    def visit_Call(self, node: ast.Call):
        self._call_references(node)
        self.generic_visit(node)

    @staticmethod
    def _is_generator_marker(tail: list[ast.stmt], node) -> bool:
        """True when the unreachable tail is a bare yield that makes ``node`` a generator.

        `raise Unavailable(...)` then `yield` is the idiom for a generator that
        raises when first advanced (a fake stream in a test, an iterator that is
        never valid). The yield never runs and is still the reason the function
        is a generator; with another yield anywhere else it is only dead.
        """
        if not tail or not all(isinstance(s, ast.Expr) and isinstance(s.value, (ast.Yield, ast.YieldFrom))
                               for s in tail):
            return False
        markers = {id(s.value) for s in tail}
        stack = list(ast.iter_child_nodes(node))
        while stack:
            child = stack.pop()
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, (ast.Yield, ast.YieldFrom)) and id(child) not in markers:
                return False
            stack.extend(ast.iter_child_nodes(child))
        return True

    def _check_unreachable(self, node):
        for i, stmt in enumerate(node.body):
            if isinstance(stmt, (ast.Return, ast.Raise)):
                if i < len(node.body) - 1 and not self._is_generator_marker(node.body[i + 1:], node):
                    next_stmt = node.body[i + 1]
                    self.issues.append(DeadCodeIssue(
                        file=self.filename, line=next_stmt.lineno,
                        issue_type="unreachable_code", name="statement",
                        description=f"Code after {'return' if isinstance(stmt, ast.Return) else 'raise'} is unreachable",
                        confidence=100
                    ))

    @staticmethod
    def _is_stub_body(node) -> bool:
        """A Protocol method, an abstract stub, `raise NotImplementedError`: the
        parameters are the contract and the body is empty on purpose."""
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]
        for stmt in body:
            if isinstance(stmt, ast.Pass):
                continue
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
                    and stmt.value.value is Ellipsis:
                continue
            if isinstance(stmt, ast.Raise) and stmt.exc is not None:
                raised = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
                if isinstance(raised, ast.Name) and raised.id == "NotImplementedError":
                    continue
            return False
        return True

    def _check_unused_params(self, node):
        if node.name.startswith('_'):
            return
        # pytest collects test functions and injects fixtures by name; their
        # parameters are the framework's business, not dead code.
        if self._is_fixture(node) or (self._is_test_file() and self._is_test_function_name(node.name)):
            return
        if self._is_stub_body(node):
            return

        # Only a free, module-level function chooses its own signature, so an
        # unused parameter there is a provable defect. A method (overrides,
        # ABC/Protocol conformance) or a nested callback/stub (its signature is
        # dictated by the caller it is passed to, e.g. monkeypatch targets) must
        # accept parameters it need not use — those are unscored leads. So is a
        # free function the file hands out as a value or decorates: finalize
        # downgrades those once it has seen every reference.
        kind = ("finding" if self._at_module_scope(node) and not node.decorator_list
                else "candidate")

        # A leading underscore is the conventional mark of a deliberately unused
        # parameter; honoring it keeps the detector aligned with every linter.
        params = set()
        for arg in node.args.args:
            if arg.arg not in ('self', 'cls') and not arg.arg.startswith('_'):
                params.add(arg.arg)
        for arg in node.args.kwonlyargs:
            if not arg.arg.startswith('_'):
                params.add(arg.arg)

        used = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                used.add(child.id)

        for param in params - used:
            self.pending_params.append((DeadCodeIssue(
                file=self.filename, line=node.lineno,
                issue_type="unused_parameter", name=param,
                description=f"Parameter '{param}' in {node.name}() is never used",
                confidence=80, kind=kind,
                also_caused_by=(() if kind == "finding"
                                else _CANDIDATE_REASONS["unused_parameter"])
            ), node))

    def finalize(self):
        # A free function this file passes around as a value (a registry entry,
        # a callback, a dispatch-table row) has its signature dictated by the
        # receiver, exactly like a nested callback. Only now, with the whole
        # file read, is that known.
        for issue, node in self.pending_params:
            # Handed out as a value here, or (checked last, because it reads the
            # tree) as `module.function` by any module importable beside it.
            if issue.kind == "finding" and (
                    node.name in self.value_references
                    or _handed_out_in_tree(Path(self.filename), node.name)):
                issue.kind = "candidate"
                issue.also_caused_by = _CANDIDATE_REASONS["unused_parameter"]
            self.issues.append(issue)
        self.pending_params.clear()

        # An unused import IS provable inside one file — unless the name is
        # re-exported via __all__, in which case it is part of the public API,
        # or the file is a package's __init__.py, whose imports are the
        # package's attributes: `from . import sub` is what lets `pkg.sub`
        # resolve for every other module. A relative (own-package) import there
        # is that surface; any other import there may be too, so it is a lead.
        for name, line in [*self.imports.items(), *self.from_imports.items()]:
            if name in self.used_names or name in self.dunder_all:
                continue
            if self.is_package_init and name in self.own_package_imports:
                continue
            if self.is_package_init:
                self.issues.append(DeadCodeIssue(
                    file=self.filename, line=line,
                    issue_type="unused_import", name=name,
                    description=f"Import '{name}' is never used in this package's __init__.py",
                    confidence=70, kind="candidate",
                    also_caused_by=_CANDIDATE_REASONS["unused_import"],
                ))
                continue
            self.issues.append(DeadCodeIssue(
                file=self.filename, line=line,
                issue_type="unused_import", name=name,
                description=f"Import '{name}' is never used",
                confidence=90
            ))

        # A public module-level function or class cannot be *proven* dead by
        # reading one file: any other module may import and use it. So these are
        # never scored findings.
        for name, node in self.functions.items():
            if name in self.called_functions or name.startswith('_'):
                continue
            if name in self.dunder_all:
                continue  # re-exported: part of the public API, definitely used
            if self._is_test_file() and (self._is_test_function_name(name) or self._is_fixture(node)):
                continue  # collected/injected by pytest — not dead, not a lead
            if self._is_decorated(node):
                continue  # registered or dispatched by its decorator (route, hook, ...)
            self.issues.append(DeadCodeIssue(
                file=self.filename, line=node.lineno,
                issue_type="unused_function", name=name,
                description=f"Function '{name}' appears unused in this file",
                confidence=60, kind="candidate",
                also_caused_by=_CANDIDATE_REASONS["unused_function"]
            ))

        for name, node in self.classes.items():
            if name in self.instantiated_classes or name in self.used_names:
                continue
            if name.startswith('_') or name in self.dunder_all:
                continue
            if self._is_test_file() and name.startswith("Test"):
                continue  # pytest test class — collected, not dead
            if self._is_decorated(node):
                continue  # registered/dispatched by its decorator
            self.issues.append(DeadCodeIssue(
                file=self.filename, line=node.lineno,
                issue_type="unused_class", name=name,
                description=f"Class '{name}' appears unused in this file",
                confidence=60, kind="candidate",
                also_caused_by=_CANDIDATE_REASONS["unused_class"]
            ))


class RedundantCodeDetector(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.issues: list[DeadCodeIssue] = []

    def visit_If(self, node: ast.If):
        if isinstance(node.test, ast.Constant):
            if node.test.value is True:
                self.issues.append(DeadCodeIssue(
                    file=self.filename, line=node.lineno,
                    issue_type="constant_condition", name="if True",
                    description="Condition is always True",
                    confidence=100
                ))
            elif node.test.value is False:
                self.issues.append(DeadCodeIssue(
                    file=self.filename, line=node.lineno,
                    issue_type="constant_condition", name="if False",
                    description="Condition is always False, code is unreachable",
                    confidence=100
                ))
        
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass) and not node.orelse:
            self.issues.append(DeadCodeIssue(
                file=self.filename, line=node.lineno,
                issue_type="empty_if", name="if ... pass",
                description="Empty if block does nothing",
                confidence=90
            ))
        self.generic_visit(node)

    def visit_While(self, node: ast.While):
        if isinstance(node.test, ast.Constant) and node.test.value is False:
            self.issues.append(DeadCodeIssue(
                file=self.filename, line=node.lineno,
                issue_type="dead_loop", name="while False",
                description="Loop will never execute",
                confidence=100
            ))
        self.generic_visit(node)


def _function_bindings(source: str, filename: str) -> dict[tuple[int, str], set[str]]:
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


def analyze_file(filepath: Path, ignore: set[str] = frozenset()) -> list[DeadCodeIssue]:
    try:
        source, tree = cached_parse(filepath)
        comments = defaultdict(list)
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                comments[token.start[0]].append(token.string)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        
        tracker = ScopeTracker(str(filepath), parents, comments, _function_bindings(source, str(filepath)))
        tracker.visit(tree)
        tracker.finalize()
        
        redundant = RedundantCodeDetector(str(filepath))
        redundant.visit(tree)
        
        return [i for i in tracker.issues + redundant.issues
                if i.issue_type not in ignore]
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def sort_key(record: dict):
    """This detector ranks by confidence, not severity — main() and the runner
    both order through this, so neither can drift from the other."""
    return (-record.get("confidence", 0), record.get("file", ""), record.get("line", 0))


def default_filter(records: list[dict]) -> list[dict]:
    """The confidence floor `find_dead_code.py <path>` applies unless told otherwise.

    The runner applies it too; without that, a pooled report would carry
    low-confidence leads this detector's own CLI never shows.
    """
    return [r for r in records if r.get("confidence", 0) >= DEFAULT_MIN_CONFIDENCE]


def to_record(issue: "DeadCodeIssue") -> dict:
    """The JSON shape this detector emits. Shared with the runner so a pooled
    run and a `find_dead_code.py <path>` run produce the same records.

    A finding carries a fix and a candidate carries its benign explanations;
    `kind` is always present. Severity is derived from confidence, which is
    what this detector ranks by.
    """
    record = asdict(issue)
    record['also_caused_by'] = list(issue.also_caused_by)
    if issue.kind == "finding" and not issue.suggestion:
        record['suggestion'] = _SUGGESTIONS.get(issue.issue_type, _DEFAULT_SUGGESTION)
    if issue.kind == "candidate":
        record['suggestion'] = ""
    record['severity'] = (
        'high' if issue.confidence >= 90
        else ('medium' if issue.confidence >= 70 else 'low')
    )
    return record


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect dead and unused code")
    parser.add_argument('path', nargs='?', default='.', help='File or directory')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--min-confidence', type=int, default=DEFAULT_MIN_CONFIDENCE)
    parser.add_argument('--ignore', type=str, default='', help='Comma-separated issue types to ignore')

    args = parser.parse_args()
    ignore = set(args.ignore.split(',')) if args.ignore else set()

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))

    all_issues = [i for i in all_issues if i.confidence >= args.min_confidence]
    # Same key the runner orders a pooled report by; it reads it off this module.
    all_issues.sort(key=lambda i: sort_key({"confidence": i.confidence,
                                            "file": i.file, "line": i.line}))

    if args.format == 'json':
        # severity travels with the finding so standalone output renders the
        # same as analyze_all's aggregation (confidence maps onto severity).
        # `kind` is always present — "finding" for a scored defect, "candidate"
        # for a lead the downstream merge must not score.
        print(json.dumps([to_record(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No dead code found!")
            return
        
        by_type = defaultdict(int)
        for issue in all_issues:
            by_type[issue.issue_type] += 1
        
        print(f"Found {len(all_issues)} potential dead code issue(s):\n")
        print("Summary:")
        for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {t}: {c}")
        print()
        
        def conf_icon(c):
            return '🔴' if c == 100 else ('🟡' if c >= 80 else '🟢')
        
        for issue in all_issues:
            icon = conf_icon(issue.confidence)
            print(f"{icon} [{issue.confidence}%] {issue.file}:{issue.line}")
            print(f"   {issue.issue_type}: {issue.name}")
            print(f"   {issue.description}\n")


if __name__ == '__main__':
    main()
