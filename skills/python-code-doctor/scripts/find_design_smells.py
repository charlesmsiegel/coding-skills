#!/usr/bin/env python3
"""
Detect classic refactoring-catalog design smells via AST analysis.

Covers the mechanically-detectable slice of the Fowler/refactoring.guru smell
catalog that the other detectors don't already handle:
  type_switch                     - if/elif ladder dispatching on one type tag (Switch Statements)
  duplicate_conditional_fragment  - identical statement in every branch of a conditional
  control_flag                    - boolean loop flag standing in for break/return
  inappropriate_intimacy          - reaching into another object's _private attributes
  temporary_field                 - field that is None except inside a single method
  refused_bequest                 - subclass no-ops or raises on inherited concrete behavior
  lazy_class                      - subclass that adds nothing over its base

Each finding names the catalog refactoring to apply; the judgment call (and the
subtler instances no parser can see) belongs to references/refactoring-catalog.md
and references/refactoring-techniques.md.

type_switch is a finding when the ladder is long (six or more branches) or its
subject is switched on in more than one ladder in the module — the catalog's
smell is complexity and repetition. One short ladder in one place "is often the
clearest form", so it is a candidate.

inappropriate_intimacy is cross-module reaching. A private name some class in
this module defines (`self._x = ...`, `def _x`) may be same-module access,
which the catalog calls conventional: an untyped receiver remains a candidate,
while a receiver annotated with an imported type is still foreign. A private name of a module this file
imports relatively (`from . import overlay; overlay._ledger`) is a candidate:
it is a sibling module's internals unless the package spells package-internal
with an underscore, which one file cannot tell. Absolute imports stay exempt:
`sys._getframe` and `os._exit` are documented API spelled with an underscore.
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from ast_scopes import (body_kind, decorator_names, is_dotted_name, is_dunder, local_bindings,
                        walk_loop_level, walk_same_scope)
from common import (is_test_file, sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files,
                    warn_detector_error, warn_unparseable)


@dataclass
class DesignSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


_SHORT_SWITCH_REASONS = (
    "one short switch in one place is often the clearest form — a factory "
    "choosing what to build, or a test dispatching on a parametrize value",
    "the cases are fixed by an external format or protocol and will not grow",
)
_SIBLING_PRIVATE_REASONS = (
    "the package uses a leading underscore for package-internal rather than "
    "module-internal names, and sibling modules share these helpers by design",
)
_LOCAL_PRIVATE_REASONS = (
    "a class in this module defines the same private member, and an untyped receiver "
    "may be one of its instances; confirm the receiver's owner before changing it",
)
# A ladder this long is a finding even when it is the only one on its subject.
LONG_SWITCH_BRANCHES = 6


# Public-by-convention namedtuple machinery; touching these is not intimacy.
NAMEDTUPLE_API = frozenset({"_replace", "_asdict", "_fields", "_make", "_field_defaults"})

# Documented Options operations provide Django context, not proof of the
# receiver's type. Keep these observations as candidates when identity is unknown.
DJANGO_META_API = frozenset({
    "get_field", "get_fields", "db_table", "local_fields", "related_objects",
    "fields", "concrete_fields", "many_to_many", "model", "model_name",
    "app_label", "label", "label_lower", "verbose_name", "verbose_name_plural",
    "abstract", "proxy", "managed", "ordering", "indexes", "constraints", "pk",
})

# Receivers that mean "my own class", not someone else's internals.
OWN_RECEIVERS = frozenset({"self", "cls", "mcs", "mcls"})

# Base names whose empty subclasses are idiomatic, not lazy.
MARKER_BASES = frozenset({
    "object", "Exception", "BaseException", "Protocol", "ABC", "Generic",
    "NamedTuple", "TypedDict", "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag",
})

PROPERTY_DECORATORS = frozenset({"property", "cached_property"})

# Ladders below this many branches don't count as a switch: short chains are
# often the clearest form, and conservatism keeps the output trustworthy.
MIN_EQ_BRANCHES = 4
MIN_ISINSTANCE_BRANCHES = 4


class DesignSmellDetector(ast.NodeVisitor):
    def __init__(self, filename: str, source_lines: list[str], tree: ast.Module,
                 ignore: set[str] = None, is_test_file: bool = False):
        self.filename = filename
        self.source_lines = source_lines
        self.issues: list[DesignSmell] = []
        self.ignore = ignore or set()
        self.is_test_file = is_test_file
        self._django_context = any(
            (isinstance(n, ast.ImportFrom) and n.level == 0 and n.module is not None
             and (n.module == "django" or n.module.startswith("django.")))
            or (isinstance(n, ast.Import) and any(
                a.name == "django" or a.name.startswith("django.") for a in n.names))
            for n in ast.walk(tree)
        )
        self._metadata_operations = {
            id(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute)
            and n.value.attr == "_meta" and n.attr in DJANGO_META_API
        }
        self._func_stack: list[tuple[str, frozenset[str], frozenset[str]]] = []
        self._class_depth = 0
        self._ladder_members: set[int] = set()
        # Qualifying type switches, held until the whole module is read: whether
        # one is a finding depends on whether its subject is switched elsewhere.
        self._switches: list[tuple[ast.If, str, str, int, tuple[str, ...]]] = []
        # A matching private name is evidence of a possible local owner, not
        # proof of ownership for every receiver with that spelling.
        self.module_privates: set[str] = self._collect_module_privates(tree)
        self._parents = {id(child): parent for parent in ast.walk(tree)
                         for child in ast.iter_child_nodes(parent)}
        self._import_names = {alias.asname or alias.name.split(".")[0]
                              for stmt in tree.body if isinstance(stmt, (ast.Import, ast.ImportFrom))
                              for alias in stmt.names}
        # Names a relative import binds: sibling modules of this package.
        self.relative_imports: set[str] = {
            alias.asname or alias.name.split(".")[0]
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level
            for alias in node.names}
        self._reported: set[tuple[int, str]] = set()
        # Names that are module-scope imports or module-scope classes: touching
        # their _private members is module-internal by convention. Function-local
        # imports exempt only their own scope (tracked on the function stack).
        self.module_names: set[str] = set()
        self._collect_module_names(tree.body)
        # Lexical environment for base-class resolution, built in definition
        # order the way Python binds names: a class's environment holds exactly
        # the classes already bound when its statement executes, so a later
        # nested definition can't shadow a name an earlier subclass resolved.
        self._class_env: dict[int, dict[str, ast.ClassDef]] = {}
        self._all_classes: list[ast.ClassDef] = []
        self._index_classes(tree.body, {})

    @staticmethod
    def _collect_module_privates(tree: ast.Module) -> set[str]:
        """`_x` names a class here defines: `self._x = ...`, `def _x`, `_x = ...`.

        Double-underscore names are left out: a class's `self.__x` is mangled to
        `_Class__x`, so the same spelling elsewhere does not reach it.
        """
        names: set[str] = set()
        for klass in ast.walk(tree):
            if not isinstance(klass, ast.ClassDef):
                continue
            for item in klass.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(item.name)
                elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                    targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                    names.update(t.id for t in targets if isinstance(t, ast.Name))
            for inner in ast.walk(klass):
                targets = []
                if isinstance(inner, ast.Assign):
                    targets = inner.targets
                elif isinstance(inner, (ast.AnnAssign, ast.AugAssign)):
                    targets = [inner.target]
                for target in targets:
                    for sub in ast.walk(target):
                        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                                and sub.value.id == "self":
                            names.add(sub.attr)
        return {n for n in names if n.startswith("_") and not n.startswith("__")}

    def _collect_module_names(self, body: list[ast.stmt]):
        """Imports and class names bound at module scope, in execution order
        (descending into module-level if/try/with blocks, but not into functions
        or classes). A later rebinding revokes the exemption: after
        `import helper; helper = make_object()` the name holds an object."""
        for stmt in body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                for alias in stmt.names:
                    self.module_names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(stmt, ast.ClassDef):
                self.module_names.add(stmt.name)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.module_names.discard(stmt.name)
            else:
                self.module_names -= self._rebound_names(stmt)
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(stmt, field, None)
                    if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                        self._collect_module_names(sub)
                for handler in getattr(stmt, "handlers", []):
                    self._collect_module_names(handler.body)

    def _index_classes(self, body: list[ast.stmt], enclosing: dict[str, ast.ClassDef]):
        self._index_into(body, dict(enclosing))

    @staticmethod
    def _rebound_names(stmt: ast.stmt) -> set[str]:
        """Names a non-class statement rebinds — a cached class binding for any
        of them is obsolete from this statement on."""
        match stmt:
            case ast.Import() | ast.ImportFrom():
                return {alias.asname or alias.name.split(".")[0] for alias in stmt.names}
            case ast.Assign(targets=targets) | ast.Delete(targets=targets):
                pass
            case ast.AnnAssign(target=t) | ast.AugAssign(target=t) | ast.For(target=t) | ast.AsyncFor(target=t):
                targets = [t]
            case ast.With(items=items) | ast.AsyncWith(items=items):
                targets = [item.optional_vars for item in items if item.optional_vars]
            case _:
                return set()
        return {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)}

    def _index_into(self, body: list[ast.stmt], local: dict[str, ast.ClassDef]):
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                # Bases are evaluated before the class name binds: the
                # environment is what's bound *so far*, not all siblings.
                self._class_env[id(stmt)] = dict(local)
                self._all_classes.append(stmt)
                self._index_classes(stmt.body, local)
                local[stmt.name] = stmt
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local.pop(stmt.name, None)
                self._index_classes(stmt.body, local)
            else:
                for name in self._rebound_names(stmt):
                    local.pop(name, None)
                # Branches may or may not execute: inside a branch, sequential
                # resolution is right; after the statement, any name a branch
                # (re)bound is ambiguous and resolves to nothing.
                branch_envs = []
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(stmt, field, None)
                    if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                        branch_env = dict(local)
                        self._index_into(sub, branch_env)
                        branch_envs.append(branch_env)
                for handler in getattr(stmt, "handlers", []):
                    branch_env = dict(local)
                    self._index_into(handler.body, branch_env)
                    branch_envs.append(branch_env)
                for branch_env in branch_envs:
                    for name in set(local) | set(branch_env):
                        if local.get(name) is not branch_env.get(name):
                            local.pop(name, None)

    def _get_line(self, lineno: int) -> str:
        if 0 < lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].strip()[:60]
        return ""

    def _add(self, line: int, smell_type: str, desc: str, suggestion: str, severity: str = "medium"):
        if smell_type in self.ignore:
            return
        self.issues.append(DesignSmell(
            file=self.filename, line=line, smell_type=smell_type,
            description=desc, suggestion=suggestion, severity=severity,
            code_snippet=self._get_line(line)
        ))

    # ----------------------------------------------------------------- #
    # Function context (used to skip dunders for intimacy)
    # ----------------------------------------------------------------- #

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef):
        # Decorators, defaults and annotations execute in the ENCLOSING scope —
        # visit them before entering the function's context so e.g. a private
        # access in a dunder's decorator isn't covered by the dunder exemption.
        for dec in node.decorator_list:
            self.visit(dec)
        a = node.args
        for default in (*a.defaults, *(d for d in a.kw_defaults if d is not None)):
            self.visit(default)
        for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs,
                    *([a.vararg] if a.vararg else []), *([a.kwarg] if a.kwarg else [])):
            if arg.annotation is not None:
                self.visit(arg.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        shadows, local_imports = local_bindings(node)
        self._func_stack.append((node.name, shadows, local_imports))
        for stmt in node.body:
            self.visit(stmt)
        self._func_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda):
        a = node.args
        # Defaults evaluate in the enclosing scope, before the parameters bind.
        for default in (*a.defaults, *(d for d in a.kw_defaults if d is not None)):
            self.visit(default)
        params = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
        if a.vararg:
            params.add(a.vararg.arg)
        if a.kwarg:
            params.add(a.kwarg.arg)
        self._func_stack.append(("<lambda>", frozenset(params), frozenset()))
        self.visit(node.body)
        self._func_stack.pop()

    def _visit_comprehension(self, node):
        # Binding order matters: the first iterable evaluates in the enclosing
        # scope; each later iterable and filter sees only preceding targets.
        self.visit(node.generators[0].iter)
        names: set[str] = set()
        pushed = False
        for i, gen in enumerate(node.generators):
            if i > 0:
                self.visit(gen.iter)
            names |= {n.id for n in ast.walk(gen.target) if isinstance(n, ast.Name)}
            if pushed:
                self._func_stack.pop()
            self._func_stack.append(("<comprehension>", frozenset(names), frozenset()))
            pushed = True
            for condition in gen.ifs:
                self.visit(condition)
        for field in ("elt", "key", "value"):
            sub = getattr(node, field, None)
            if sub is not None:
                self.visit(sub)
        self._func_stack.pop()

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _visit_comprehension

    # ----------------------------------------------------------------- #
    # Switch Statements + Consolidate Duplicate Conditional Fragments
    # ----------------------------------------------------------------- #

    def visit_If(self, node: ast.If):
        if id(node) not in self._ladder_members:
            tests, bodies, final_else = self._collect_ladder(node)
            self._check_type_switch(node, tests)
            self._check_duplicate_fragments(node, bodies, final_else)
        self.generic_visit(node)

    def _collect_ladder(self, node: ast.If):
        """Walk an if/elif/else chain; returns (tests, branch bodies, has-final-else)."""
        tests, bodies = [], []
        current = node
        while True:
            tests.append(current.test)
            bodies.append(current.body)
            if (len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If)
                    and current.orelse[0].col_offset == current.col_offset):
                current = current.orelse[0]
                self._ladder_members.add(id(current))
            else:
                break
        if current.orelse:
            bodies.append(current.orelse)
            return tests, bodies, True
        return tests, bodies, False

    @staticmethod
    def _switch_candidates(test: ast.expr) -> list[tuple[ast.expr, str]]:
        """Candidate (subject, kind) pairs for one ladder test. An eq-case value
        may be a literal or a dotted name (enum/sentinel member); a bare name is
        not a case value — `x == y` is a comparison, not a dispatch."""
        out = []
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
            for subject, case in ((test.left, test.comparators[0]), (test.comparators[0], test.left)):
                if isinstance(subject, (ast.Name, ast.Attribute)) and (
                        isinstance(case, ast.Constant)
                        or (isinstance(case, ast.Attribute) and is_dotted_name(case))):
                    out.append((subject, "eq"))
        elif (isinstance(test, ast.Call) and isinstance(test.func, ast.Name)
                and test.func.id == "isinstance" and len(test.args) == 2
                and isinstance(test.args[0], (ast.Name, ast.Attribute))):
            out.append((test.args[0], "isinstance"))
        return out

    def _check_type_switch(self, node: ast.If, tests: list[ast.expr]):
        candidates = [self._switch_candidates(t) for t in tests]
        if any(not c for c in candidates):
            return
        # One (subject, kind) must hold across every branch: a ladder mixing
        # value cases with isinstance checks is not a uniform dispatch.
        common = set.intersection(*({(ast.dump(s), k) for s, k in c} for c in candidates))
        if not common:
            return
        subject_dump, kind = sorted(common)[0]
        threshold = MIN_ISINSTANCE_BRANCHES if kind == "isinstance" else MIN_EQ_BRANCHES
        if len(tests) < threshold:
            return

        # Every branch must dispatch on a DISTINCT case: repeated conditions are
        # an unreachable-duplicate bug, not a dispatch table.
        def case_of(test: ast.expr) -> str:
            if kind == "isinstance":
                return ast.dump(test.args[1])
            left, right = test.left, test.comparators[0]
            return ast.dump(right if ast.dump(left) == subject_dump else left)

        cases = tuple(sorted(case_of(t) for t in tests))
        if len(set(cases)) != len(tests):
            return
        self._switches.append((node, subject_dump, kind, len(tests), cases))

    def visit_Module(self, node: ast.Module):
        self.generic_visit(node)
        self._report_type_switches()

    def _report_type_switches(self):
        """A long ladder, or a subject switched in several ladders, is the smell;
        one short ladder alone is a lead."""
        ladders_per_subject = defaultdict(int)
        for _, subject_dump, kind, _, cases in self._switches:
            ladders_per_subject[(subject_dump, kind, cases)] += 1
        for node, subject_dump, kind, branches, cases in self._switches:
            subject_src = ast.unparse(next(
                s for t in [node.test] for s, k in self._switch_candidates(t)
                if ast.dump(s) == subject_dump and k == kind))
            via = "isinstance checks on" if kind == "isinstance" else "comparisons against"
            description = f"if/elif ladder with {branches} branches dispatching via {via} '{subject_src}'"
            repeated = ladders_per_subject[(subject_dump, kind, cases)]
            # The description depends on the ladder alone, never on its siblings:
            # analyze_diff matches a pre-existing finding by its description, and
            # a new ladder elsewhere must not make an unchanged one look new.
            if branches >= LONG_SWITCH_BRANCHES or repeated >= 2:
                self._add(node.lineno, "type_switch", description,
                    "Replace Conditional with Polymorphism, a dispatch dict, or match — "
                    "adding a case should mean adding an entry, not editing a ladder",
                    "high" if branches >= LONG_SWITCH_BRANCHES else "medium")
            elif "type_switch" not in self.ignore:
                self.issues.append(DesignSmell(
                    file=self.filename, line=node.lineno, smell_type="type_switch",
                    description=description, suggestion="", severity="low",
                    code_snippet=self._get_line(node.lineno),
                    kind="candidate", also_caused_by=_SHORT_SWITCH_REASONS,
                ))

    def _check_duplicate_fragments(self, node: ast.If, bodies: list[list[ast.stmt]], final_else: bool):
        if not final_else or len(bodies) < 2 or any(not b for b in bodies):
            return
        last_dumps = {ast.dump(b[-1]) for b in bodies}
        if len(last_dumps) == 1:
            self._add(node.lineno, "duplicate_conditional_fragment",
                f"All {len(bodies)} branches end with the same statement",
                "Move the shared statement after the conditional "
                "(Consolidate Duplicate Conditional Fragments)", "medium")
            return
        first_dumps = {ast.dump(b[0]) for b in bodies}
        if len(first_dumps) == 1:
            self._add(node.lineno, "duplicate_conditional_fragment",
                f"All {len(bodies)} branches start with the same statement",
                "Move the shared statement before the conditional — but only if the "
                "condition does not read anything that statement writes AND the condition "
                "itself is side-effect-free: today the statement runs only after the "
                "condition has been evaluated without raising", "low")

    # ----------------------------------------------------------------- #
    # Remove Control Flag
    # ----------------------------------------------------------------- #

    def visit_While(self, node: ast.While):
        # A while-else runs its else only on condition-based exit; replacing the
        # flag assignment with break would skip it, so such loops are exempt.
        if node.orelse:
            self.generic_visit(node)
            return
        flag = terminating = None
        if isinstance(node.test, ast.Name):
            flag, terminating = node.test.id, False  # `while flag:` exits when flag becomes False
        elif (isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not)
                and isinstance(node.test.operand, ast.Name)):
            flag, terminating = node.test.operand.id, True
        # Only an assignment that moves the loop TOWARD termination is a
        # removable control flag (replaceable by break at that point) — and
        # only when no assignment also resets the flag away from termination,
        # since then the terminating value may never reach the loop boundary.
        assigned_values = [
            inner.value.value
            for inner in walk_loop_level(node.body)
            if any(isinstance(t, ast.Name) and t.id == flag for t in
                   (inner.targets if isinstance(inner, ast.Assign)
                    else [inner.target] if isinstance(inner, ast.AnnAssign) and inner.value is not None
                    else []))
            and isinstance(inner.value, ast.Constant)
            and isinstance(inner.value.value, bool)
        ] if flag else []
        if terminating in assigned_values and (not terminating) not in assigned_values:
            self._add(node.lineno, "control_flag",
                f"Loop is steered by boolean flag '{flag}' reassigned in the body",
                "Restructure to exit with break/return once the iteration's remaining "
                "work is done, or make the loop condition the real condition "
                "(Remove Control Flag)", "low")
        self.generic_visit(node)

    # ----------------------------------------------------------------- #
    # Inappropriate Intimacy
    # ----------------------------------------------------------------- #

    def _is_exempt_name(self, name: str) -> bool:
        """Module-scope imports and classes are module-internal by convention.
        Scopes are checked innermost-out: a local import exempts its own scope;
        a shadowing binding (parameter, assignment, loop target) means the
        receiver is whatever object that binding holds, not the module-level
        name — and stops the search."""
        for _, shadows, local_imports in reversed(self._func_stack):
            if name in local_imports:
                return True
            if name in shadows:
                return False
        return name in self.module_names

    def _foreign_receiver(self, value: ast.expr) -> str | None:
        """Source of the receiver when `receiver._attr` reaches into another
        object's internals; None when the access is own/module-internal."""
        if isinstance(value, ast.Name):
            if value.id in OWN_RECEIVERS or self._is_exempt_name(value.id):
                return None
            return value.id
        # Chained receivers: self.account._token, request.user._state,
        # items[0]._cache, get_user()._token.
        root = value
        saw_call = False
        while isinstance(root, (ast.Attribute, ast.Subscript, ast.Call)):
            if isinstance(root, ast.Attribute):
                if root.attr.startswith("_") and not is_dunder(root.attr):
                    return None  # already navigating private structure; the inner access is the finding
                root = root.value
            elif isinstance(root, ast.Subscript):
                root = root.value
            else:
                if isinstance(root.func, ast.Name) and root.func.id == "super":
                    return None  # super() is own-object access
                saw_call = True
                root = root.func
        if not isinstance(root, ast.Name):
            return None
        if saw_call:
            return ast.unparse(value)  # a call result is always another object
        if root.id not in OWN_RECEIVERS and self._is_exempt_name(root.id):
            return None
        return ast.unparse(value)

    def _innermost_function_name(self) -> str | None:
        """Innermost real function, skipping lambda/comprehension expression
        scopes — those belong to the function whose body contains them."""
        for name, _, _ in reversed(self._func_stack):
            if name not in ("<lambda>", "<comprehension>"):
                return name
        return None

    def _has_imported_receiver_type(self, value: ast.expr) -> bool:
        """Whether an un-rebound parameter is annotated with an imported type."""
        if not isinstance(value, ast.Name):
            return False
        current = self._parents.get(id(value))
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = current.args
                parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
                parameter = next((arg for arg in parameters if arg.arg == value.id), None)
                shadowed = any(isinstance(n, ast.Name) and n.id == value.id
                               and isinstance(n.ctx, (ast.Store, ast.Del))
                               for n in walk_same_scope(current.body))
                if shadowed:
                    return False
                if parameter is not None:
                    annotation = parameter.annotation
                    while isinstance(annotation, (ast.Attribute, ast.Subscript)):
                        annotation = annotation.value
                    local_classes = {klass.name for klass in self._all_classes}
                    return (isinstance(annotation, ast.Name) and annotation.id in self._import_names
                            and annotation.id not in local_classes)
            current = self._parents.get(id(current))
        return False

    def visit_Attribute(self, node: ast.Attribute):
        enclosing = self._innermost_function_name()
        in_dunder = bool(enclosing) and is_dunder(enclosing)
        attr = node.attr
        private = attr.startswith("_") and not is_dunder(attr)
        # Inside a class body, `other.__secret` is name-mangled to this class —
        # by construction a same-class access, not intimacy. At module level it
        # reaches into another class's deliberately private state.
        if attr.startswith("__") and self._class_depth > 0:
            private = False
        if (not self.is_test_file and not in_dunder and private
                and attr not in NAMEDTUPLE_API):
            receiver = self._foreign_receiver(node.value)
            key = (node.lineno, f"{receiver or ast.unparse(node.value)}.{attr}")
            if (receiver is not None and attr == "_meta" and self._django_context
                    and id(node) in self._metadata_operations):
                if "inappropriate_intimacy" not in self.ignore:
                    self.issues.append(DesignSmell(
                        file=self.filename, line=node.lineno,
                        smell_type="inappropriate_intimacy",
                        description=f"'{receiver}._meta' uses a documented Django metadata operation; "
                                    "receiver identity is unresolved",
                        suggestion="", severity="medium", code_snippet=self._get_line(node.lineno),
                        kind="candidate", also_caused_by=(
                            "Django Model._meta is a public metadata API despite its leading underscore; "
                            "this module imports Django, but static analysis cannot prove the receiver "
                            "is a Django model. Verify its type before treating this as private coupling.",),
                    ))
            elif receiver is not None:
                self._reported.add(key)
                if attr in self.module_privates and not self._has_imported_receiver_type(node.value):
                    if "inappropriate_intimacy" not in self.ignore:
                        self.issues.append(DesignSmell(
                            self.filename, node.lineno, "inappropriate_intimacy",
                            f"Reaching into '{receiver}.{attr}' — its owner is not established",
                            "", "low", self._get_line(node.lineno),
                            kind="candidate", also_caused_by=_LOCAL_PRIVATE_REASONS))
                else:
                    self._add(node.lineno, "inappropriate_intimacy",
                        f"Reaching into '{receiver}.{node.attr}' — another object's private internals",
                        "Move Method to where the data lives, or expose an intentional API "
                        "(Hide Delegate); classes should know as little about each other as possible",
                        "medium")
            elif self._reaches_sibling_module(node.value) and "inappropriate_intimacy" not in self.ignore:
                self._reported.add(key)
                self.issues.append(DesignSmell(
                    file=self.filename, line=node.lineno, smell_type="inappropriate_intimacy",
                    description=f"Reaching into '{ast.unparse(node.value)}.{attr}' — a sibling "
                                "module's private name",
                    suggestion="", severity="low", code_snippet=self._get_line(node.lineno),
                    kind="candidate", also_caused_by=_SIBLING_PRIVATE_REASONS,
                ))
        self.generic_visit(node)

    def _reaches_sibling_module(self, value: ast.expr) -> bool:
        """True for `mod._x` / `mod.sub._x` where `mod` is a relative import
        that resolves here as the imported module."""
        while isinstance(value, ast.Attribute):
            value = value.value
        return (isinstance(value, ast.Name) and value.id in self.relative_imports
                and self._is_exempt_name(value.id))

    # ----------------------------------------------------------------- #
    # Temporary Field, Refused Bequest, Lazy Class
    # ----------------------------------------------------------------- #

    def visit_ClassDef(self, node: ast.ClassDef):
        self._check_temporary_fields(node)
        self._check_refused_bequest(node)
        self._check_lazy_class(node)
        # Decorators, bases and keywords evaluate in the enclosing scope, before
        # the class exists — name mangling does not apply to them.
        for dec in node.decorator_list:
            self.visit(dec)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._class_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self._class_depth -= 1

    def _methods(self, node: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
        return [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def _check_temporary_fields(self, node: ast.ClassDef):
        init = next((m for m in self._methods(node) if m.name == "__init__"), None)
        if init is None:
            return
        none_fields: dict[str, int] = {}
        disqualified: set[str] = set()
        # Discovery stays in __init__'s own scope: an assignment inside a nested
        # callback does not run during construction. Disqualification stays
        # broad (any read, any non-None write) — broader means quieter.
        for inner in walk_same_scope(init.body):
            targets, value = [], None
            if isinstance(inner, ast.Assign):
                targets, value = inner.targets, inner.value
            elif isinstance(inner, ast.AnnAssign) and inner.value is not None:
                targets, value = [inner.target], inner.value
            for target in targets:
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                        and target.value.id == "self":
                    if isinstance(value, ast.Constant) and value.value is None:
                        none_fields.setdefault(target.attr, inner.lineno)
                    else:
                        disqualified.add(target.attr)
        for inner in ast.walk(init):
            if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name) \
                    and inner.value.id == "self" and isinstance(inner.ctx, ast.Load):
                disqualified.add(inner.attr)
        if not none_fields:
            return
        # Usage must be visible across the whole hierarchy: an inherited method
        # (or a subclass) may also consume the field. Resolvable relatives are
        # included; an unresolvable base means unknown inherited usage → quiet.
        mro = self._mro(node)
        if mro is None or any(self._has_unresolved_base(klass) for klass in mro):
            return
        relatives = list(mro) + [klass for klass in self._all_classes
                                 if klass is not node and node in (self._mro(klass) or [])]
        usage: dict[str, set[str]] = defaultdict(set)
        populated: dict[str, set[str]] = defaultdict(set)
        loaded_in: dict[str, set[str]] = defaultdict(set)
        returned_in: dict[str, set[str]] = defaultdict(set)
        reset_in: dict[str, set[str]] = defaultdict(set)
        accessor_like: set[str] = set()
        for klass, method in ((k, m) for k in relatives for m in self._methods(k)):
            if klass is node and method.name == "__init__":
                continue
            # Identity-keyed: same-named classes in different containers must
            # not collapse into one method.
            qualname = (id(klass), f"{klass.name}.{method.name}")
            decorators = decorator_names(method)
            if decorators & PROPERTY_DECORATORS or {"setter", "getter", "deleter"} & decorators:
                accessor_like.add(qualname)
            # Same-scope only: a deferred callback's accesses are not the
            # method's own — it may run long after the method returns.
            for inner in walk_same_scope(method.body):
                if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name) \
                        and inner.value.id == "self" and inner.attr in none_fields:
                    usage[inner.attr].add(qualname)
                    if isinstance(inner.ctx, ast.Load):
                        loaded_in[inner.attr].add(qualname)
                if isinstance(inner, ast.Return) and inner.value is not None:
                    for sub in ast.walk(inner.value):
                        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                                and sub.value.id == "self" and sub.attr in none_fields:
                            returned_in[sub.attr].add(qualname)
                targets = []
                if isinstance(inner, ast.Assign):
                    targets = inner.targets
                elif isinstance(inner, (ast.AnnAssign, ast.AugAssign)) and inner.value is not None:
                    targets = [inner.target]
                value = getattr(inner, "value", None)
                if value is not None:
                    # Walk nested targets too: destructuring writes like
                    # `self.scratch, status = make_pair()` populate the field.
                    is_none_const = isinstance(value, ast.Constant) and value.value is None
                    for target in targets:
                        for sub in ast.walk(target):
                            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                                    and sub.value.id == "self" and sub.attr in none_fields:
                                (reset_in if is_none_const else populated)[sub.attr].add(qualname)
        for attr, line in none_fields.items():
            if attr in disqualified:
                continue
            methods = usage.get(attr, set())
            if len(methods) != 1 or methods & accessor_like:
                continue
            only_key = next(iter(methods))
            # Lazy-init caches behind a property are idiomatic; a field never
            # populated is dead weight; a reset back to None proves the "None
            # except during one operation" lifecycle. Without a reset, a field
            # the method *returns* is an output for callers (part of the state
            # model), and a field only written is an output too — only a field
            # populated and read back without being exposed is method-local
            # scratch.
            is_temporary = only_key in populated.get(attr, set()) and (
                only_key in reset_in.get(attr, set())
                or (only_key in loaded_in.get(attr, set())
                    and only_key not in returned_in.get(attr, set())))
            if is_temporary:
                only = only_key[1]
                self._add(line, "temporary_field",
                    f"Field 'self.{attr}' is None except inside '{only}' — a temporary field",
                    f"Pass the value through parameters or extract '{only}' and its data into "
                    "its own class (Extract Class / Replace Method with Method Object)", "low")

    def _base_classes(self, node: ast.ClassDef) -> list[ast.ClassDef]:
        """Bases resolved in the class's lexical environment."""
        env = self._class_env.get(id(node), {})
        return [env[base.id] for base in node.bases
                if isinstance(base, ast.Name) and base.id in env and env[base.id] is not node]

    @staticmethod
    def _c3_merge(sequences: list[list[ast.ClassDef]]) -> list[ast.ClassDef] | None:
        """C3 linearization merge; None when the hierarchy is inconsistent."""
        result: list[ast.ClassDef] = []
        seqs = [list(s) for s in sequences if s]
        while seqs:
            for seq in seqs:
                head = seq[0]
                if not any(head in s[1:] for s in seqs):
                    break
            else:
                return None
            result.append(head)
            seqs = [[c for c in s if c is not head] for s in seqs]
            seqs = [s for s in seqs if s]
        return result

    def _mro(self, node: ast.ClassDef, active: frozenset[int] = frozenset()) -> list[ast.ClassDef] | None:
        """Python's real lookup order (C3) over the same-file-resolvable classes;
        None on cycles or inconsistent hierarchies (then we stay quiet)."""
        if id(node) in active:
            return None
        bases = self._base_classes(node)
        sequences = []
        for base in bases:
            sub = self._mro(base, active | {id(node)})
            if sub is None:
                return None
            sequences.append(sub)
        sequences.append(bases)
        merged = self._c3_merge(sequences)
        if merged is None:
            return None
        return [node, *merged]

    def _has_unresolved_base(self, node: ast.ClassDef) -> bool:
        env = self._class_env.get(id(node), {})
        return any(not (isinstance(b, ast.Name) and (b.id in env or b.id == "object"))
                   for b in node.bases)

    def _check_refused_bequest(self, node: ast.ClassDef):
        mro = self._mro(node)
        if mro is None or len(mro) == 1:
            return
        # An imported/unresolvable base anywhere in the hierarchy means this is
        # not Python's actual MRO — the real lookup might reach that base first,
        # so any refusal verdict would be a guess. Stay quiet.
        if any(self._has_unresolved_base(klass) for klass in mro):
            return
        # First definition along the real lookup order wins, exactly as at runtime.
        inherited: dict[str, bool] = {}
        for klass in mro[1:]:
            for method in self._methods(klass):
                if method.name not in inherited and "overload" not in decorator_names(method):
                    inherited[method.name] = ("abstractmethod" not in decorator_names(method)
                                              and body_kind(method.body) is None)
        if not inherited:
            return
        for method in self._methods(node):
            decorators = decorator_names(method)
            # @overload declarations are intentionally `...`-bodied signatures
            # preceding the real implementation, not refusals.
            if is_dunder(method.name) or {"abstractmethod", "overload"} & decorators:
                continue
            kind = body_kind(method.body)
            if kind and inherited.get(method.name) is True:
                action = "raises NotImplementedError on" if kind == "raise_nie" else "no-ops"
                self._add(method.lineno, "refused_bequest",
                    f"'{node.name}.{method.name}' {action} behavior inherited from a concrete base "
                    "— the subclass refuses its bequest",
                    "The hierarchy is off: Replace Inheritance with Delegation, or push the truly "
                    "shared part into a new superclass (Extract Superclass)", "medium")

    def _hierarchy_defines(self, node: ast.ClassDef, base: ast.Name, method_name: str) -> bool:
        """True if the base (resolved in node's lexical environment) or any of
        its resolvable ancestors defines method_name."""
        env = self._class_env.get(id(node), {})
        base_cls = env.get(base.id)
        if base_cls is None:
            return False
        for klass in self._mro(base_cls) or [base_cls]:
            if any(m.name == method_name for m in self._methods(klass)):
                return True
        return False

    def _check_lazy_class(self, node: ast.ClassDef):
        if node.decorator_list or node.keywords or len(node.bases) != 1:
            return
        base = node.bases[0]
        if not isinstance(base, ast.Name):
            return
        if base.id in MARKER_BASES or base.id.endswith(("Error", "Exception", "Warning")):
            return
        if node.name.endswith(("Error", "Exception", "Warning")):
            return
        # A docstring is the documented opt-out: a deliberate marker type that
        # says what it marks has earned its keep.
        if ast.get_docstring(node):
            return
        # A base hierarchy defining __init_subclass__ gives even an empty
        # subclass observable class-creation behavior (plugin registration) —
        # deleting it would unregister the plugin.
        if self._hierarchy_defines(node, base, "__init_subclass__"):
            return
        if body_kind(node.body) == "noop":
            self._add(node.lineno, "lazy_class",
                f"Class '{node.name}' adds nothing over '{base.id}'",
                f"Use '{base.id}' directly and delete this class (Inline Class / Collapse "
                "Hierarchy); if it is a deliberate marker type, say so in a docstring", "low")


def analyze_file(filepath: Path, ignore: set[str]) -> list[DesignSmell]:
    try:
        source, tree = cached_parse(filepath)
        lines = source.splitlines()
        detector = DesignSmellDetector(str(filepath), lines, tree, ignore, is_test_file(filepath))
        detector.visit(tree)
        return detector.issues
    except (SyntaxError, ValueError, OSError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect classic refactoring-catalog design smells")
    parser.add_argument('path', nargs='?', default='.', help='File or directory')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--ignore', type=str, default='',
        help='Comma-separated smells to ignore')

    args = parser.parse_args()
    ignore = set(args.ignore.split(',')) if args.ignore else set()

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))

    sort_findings(all_issues)

    if args.format == 'json':
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        proven = [issue for issue in all_issues if issue.kind == "finding"]
        candidates = [issue for issue in all_issues if issue.kind == "candidate"]
        if not all_issues:
            print("✅ No design smells found!")
            return

        by_type = defaultdict(int)
        for issue in proven:
            by_type[issue.smell_type] += 1

        print(f"Found {len(proven)} design smell(s):\n")
        print("Summary:")
        for smell, count in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {smell}: {count}")
        print()

        severity_icons = SEVERITY_ICONS
        for issue in all_issues:
            if issue.kind == "candidate":
                print(f"? [CANDIDATE] {issue.file}:{issue.line}")
            else:
                print(f"{severity_icons[issue.severity]} [{issue.severity.upper()}] {issue.file}:{issue.line}")
            print(f"   {issue.smell_type}: {issue.description}")
            if issue.code_snippet:
                print(f"   Code: {issue.code_snippet}")
            if issue.suggestion:
                print(f"   → {issue.suggestion}")
            for reason in issue.also_caused_by:
                print(f"   ? also caused by: {reason}")
            print()


if __name__ == '__main__':
    main()
