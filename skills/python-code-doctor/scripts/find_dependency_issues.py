#!/usr/bin/env python3
"""
Detect dependency management issues in Python projects.

Cross-file analysis that compares declared third-party dependencies against
what the code actually imports.

Finds:
  - missing_dependency    : third-party module imported but not declared
  - unused_dependency     : declared dependency never imported anywhere
  - unpinned_dependency   : requirements.txt entry with no version specifier
  - no_dependency_manifest: third-party imports exist but no manifest at all

Deliberately NOT charged as missing:
  - an optional import: one inside the body of a `try` whose `except` names
    ImportError or ModuleNotFoundError and carries on — its last statement does
    not raise or exit (raise, sys.exit, exit, quit, os._exit). The code runs
    without the package, so it is optional by construction. When the try body
    imports more than one module, only an import the handler replaces with
    another import of the same name is optional. One such site anywhere makes
    the module optional, unless an unguarded import of it elsewhere makes it
    required. It still counts as a use of a declared dependency (an extra). A
    bare `except:` or `except Exception:` also catches the error but does not
    say the import was meant to be optional, so it does not count.

Reported as a candidate, not a finding:
  - an undeclared module imported only under such a guard, where the guard
    does not make it optional: the handler raises or exits (how a script says
    a package is required, and also how an optional feature's entry point
    reports a missing extra), or the try body imports other modules and the
    handler replaces none of this one's names.
  - an undeclared module every import of which follows, in its own file, a
    sys.path addition (sys.path.insert/append/extend, `sys.path += [...]`,
    site.addsitedir) whose directory the source cannot place inside the project:
    one that resolves outside the repository, or one that reads a function
    parameter, os.environ/os.getenv, sys.argv, or a name bound more than once.
    A harness run under another program's interpreter imports that program's
    modules this way, and no manifest here could declare them. A directory the
    source does resolve inside the project explains nothing (every module there
    is already first-party), nor does one spelled in a way the path reader cannot
    follow but with none of those run-time sources in it, and a module-level
    import is not served by an addition inside a function, which cannot have
    run yet when the module loads.
    Scope, stated plainly: one such addition demotes EVERY undeclared module
    whose imports all come after it in that file, not only the modules the added
    directory provides. That covers a production module that loads plugins from
    $PLUGIN_DIR and then imports `requests`, and an ordinary lazy import in any
    function below a `def add_dir(d): sys.path.append(d)` helper. The source
    cannot say which names the directory serves, so all of them are leads.
"""

import ast
import os
import sys
import json
import argparse
import functools
import re
from pathlib import Path, PurePath
from dataclasses import dataclass, asdict
from collections import defaultdict
from ast_scopes import walk_same_scope
from common import (sort_findings, SEVERITY_ICONS, configure_output, find_python_files,
                    first_party_modules, in_nested_repository, warn_detector_error, warn_unparseable)

# tomllib is stdlib on 3.11+. On older interpreters we simply skip pyproject.toml
# parsing rather than depend on a third-party backport (this skill is stdlib-only).
try:
    import tomllib
except ImportError:
    tomllib = None  # type: ignore


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""
    # A scored defect by default; "candidate" for a lead the downstream merge
    # must not score. A candidate carries no fix and names, in also_caused_by,
    # the ways a healthy codebase produces the same observation.
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


# Why a module imported only after its file puts an unreadable directory on
# sys.path need not be a dependency this project should declare.
_RUNTIME_PATH_REASONS = (
    "the file runs inside another program's checkout or interpreter (a test harness, a plugin "
    "host, a script driving a vendored tree) and imports that program's own modules from the "
    "directory it puts on sys.path, which no manifest of this project could declare",
    "the directory comes from a function argument, the environment, sys.argv or a name rebound as "
    "the program runs, or resolves outside this repository, so only the machine the code runs on "
    "shows what it provides",
)


# Why a module imported only under an ImportError guard that does not carry on
# need not be a dependency this project should declare.
_GUARD_REASONS = {
    "exits": (
        "the handler turns a missing optional extra into a clear error at the entry point that "
        "needs it, and every other path through the program runs without the package",
        "the package is installed by a step the project documents rather than declares (a plugin "
        "host, a GPU stack, a system package), and the handler is that step's error message",
    ),
    "shared": (
        "the imports in the try are one optional feature that the handler switches off together "
        "(placeholders such as `x = None`, or a flag such as `HAVE_X = False`)",
    ),
}


# ---------------------------------------------------------------------------
# Known import-name -> distribution-name mappings (safe, well-known only)
# ---------------------------------------------------------------------------
_IMPORT_TO_DIST = {
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "pil": "pillow",
    "sklearn": "scikit_learn",
    "cv2": "opencv_python",
    "dateutil": "python_dateutil",
    "dotenv": "python_dotenv",
    "jwt": "pyjwt",
    "attr": "attrs",
}

# Tooling/plugin deps that are often unimported by name — never flag as unused
_TOOLING_DEPS = {
    "pytest", "pytest-cov", "pytest_cov", "coverage", "tox", "mypy",
    "ruff", "black", "isort", "flake8", "pre-commit", "pre_commit",
    "setuptools", "wheel", "pip", "build", "twine", "bandit", "pylint",
}

# Version specifier characters
_VERSION_CHARS = re.compile(r'[><=!~@]')

# Requirement line pattern: split on version/extras separators
_NAME_SPLIT = re.compile(r'[\s><=!~\[;@]')


def _normalize(name: str) -> str:
    """Lowercase and replace - and . with _ for comparison."""
    return name.lower().replace("-", "_").replace(".", "_")


def _parse_dist_name(raw: str) -> str:
    """Extract the bare distribution name from a requirement specifier."""
    # Strip extras like pkg[extra]
    raw = raw.strip()
    raw = re.sub(r'\[.*?\]', '', raw)
    m = _NAME_SPLIT.search(raw)
    if m:
        raw = raw[:m.start()]
    return raw.strip()


def _has_version_specifier(line: str) -> bool:
    """Return True if the line contains a version constraint."""
    # Strip the name portion first
    name = _parse_dist_name(line)
    rest = line[len(name):]
    return bool(_VERSION_CHARS.search(rest)) or (' @ ' in line)


def _collect_requirements_files(root: Path):
    """
    Find all requirements*.txt files at or under root.
    Returns list of (path, [(line_number, raw_line)]) entries of package lines.
    """
    results = []
    for req_file in sorted(root.rglob("requirements*.txt")):
        if ".venv" in req_file.parts or "node_modules" in req_file.parts:
            continue
        if in_nested_repository(root, req_file):
            continue  # another repository's manifest, not this project's
        entries = []
        try:
            text = req_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, raw in enumerate(text.splitlines(), 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(("-r ", "-e ", "-c ", "--")):
                continue
            entries.append((lineno, stripped))
        results.append((req_file, entries))
    return results


def _collect_pyproject_deps(root: Path):
    """
    Parse pyproject.toml at root.
    Returns list of (dist_name, line_number_approx, has_version).
    Line numbers are approximate (we scan the raw text for the entry).
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.exists() or tomllib is None:
        return []

    try:
        raw_text = pyproject.read_text(encoding="utf-8", errors="replace")
        data = tomllib.loads(raw_text)
    except Exception as exc:
        warn_unparseable(pyproject, exc)
        return []

    raw_lines = raw_text.splitlines()

    def find_line(name: str) -> int:
        """Best-effort: find the line containing this dep name."""
        pat = re.compile(r'\b' + re.escape(name) + r'\b', re.IGNORECASE)
        for i, line in enumerate(raw_lines, 1):
            if pat.search(line):
                return i
        return 1

    deps = []

    def add_dep(spec: str):
        name = _parse_dist_name(spec)
        if not name or _normalize(name) == "python":
            return
        lineno = find_line(name)
        has_ver = _has_version_specifier(spec)
        deps.append((name, lineno, has_ver))

    # [project].dependencies
    project = data.get("project", {})
    for spec in project.get("dependencies", []):
        add_dep(spec)

    # [project].optional-dependencies.*
    for group_deps in project.get("optional-dependencies", {}).values():
        for spec in group_deps:
            add_dep(spec)

    # [tool.poetry.dependencies] and [tool.poetry.group.*.dependencies]
    poetry = data.get("tool", {}).get("poetry", {})
    for name in poetry.get("dependencies", {}):
        if _normalize(name) != "python":
            lineno = find_line(name)
            deps.append((name, lineno, True))  # Poetry always has version constraint

    for group_info in poetry.get("group", {}).values():
        for name in group_info.get("dependencies", {}):
            if _normalize(name) != "python":
                lineno = find_line(name)
                deps.append((name, lineno, True))

    return deps


@dataclass(frozen=True)
class _ImportSite:
    """One absolute import of a top-level module, and what surrounds it."""
    file: str
    line: int
    # How an enclosing `except ImportError` bears on it: "" (none), "optional"
    # (the handler carries on), "exits" (every such handler raises or exits), or
    # "shared" (the handler replaces other imports of the same try, not this one).
    guard: str = ""
    # The line of a sys.path addition that runs before this import and that the
    # source cannot place inside the project, so the import may be served from
    # it; 0 when there is none.
    after_path: int = 0

    @property
    def plain(self) -> bool:
        """Nothing around the import explains its absence: a missing package fails here."""
        return not self.guard and not self.after_path


_IMPORT_ERRORS = frozenset({"ImportError", "ModuleNotFoundError"})


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    """An `except` that names ImportError or ModuleNotFoundError, alone or in a tuple."""
    caught = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any((isinstance(t, ast.Name) and t.id in _IMPORT_ERRORS)
               or (isinstance(t, ast.Attribute) and t.attr in _IMPORT_ERRORS) for t in caught)


# A handler whose last statement is one of these does not carry on.
_EXIT_CALLS = frozenset({"sys.exit", "exit", "quit", "os._exit"})


def _stops(handler: ast.ExceptHandler) -> bool:
    """The handler ends by raising or exiting, so the program does not run on without the import."""
    last = handler.body[-1]
    return isinstance(last, ast.Raise) or (
        isinstance(last, ast.Expr) and isinstance(last.value, ast.Call)
        and _dotted(last.value.func) in _EXIT_CALLS)


def _bound_names(node) -> set[str]:
    """The names an import statement binds."""
    if isinstance(node, ast.Import):
        return {a.asname or a.name.split(".")[0] for a in node.names}
    return {a.asname or a.name for a in node.names if a.name != "*"}


def _module_keys(node) -> set[str]:
    """The top-level modules an import statement loads, relative ones by their dots."""
    if isinstance(node, ast.Import):
        return {a.name.split(".")[0] for a in node.names}
    return {"." * node.level + (node.module or "").split(".")[0]}


def _imports_in(stmts: list) -> list:
    return [n for n in walk_same_scope(stmts) if isinstance(n, (ast.Import, ast.ImportFrom))]


@dataclass(frozen=True)
class _Guard:
    """A `try` whose ImportError handlers bear on the imports in its body."""
    carries_on: bool        # some handler that catches the error finishes normally
    fallbacks: frozenset    # names those handlers bind by importing a replacement
    shared: bool            # the body imports more than one module

    @classmethod
    def of(cls, node, handlers: list) -> "_Guard":
        carrying = [h for h in handlers if not _stops(h)]
        fallbacks = frozenset(name for h in carrying for n in _imports_in(h.body) for name in _bound_names(n))
        modules = {key for n in _imports_in(node.body) for key in _module_keys(n)}
        return cls(bool(carrying), fallbacks, len(modules) > 1)

    def verdict(self, node) -> str:
        if not self.carries_on:
            return "exits"
        if self.shared and not _bound_names(node) & self.fallbacks:
            return "shared"
        return "optional"


def _guard_of(node, guards: tuple) -> str:
    """An import's guard status: optional if any enclosing guard makes it so,
    otherwise what the innermost one says, "" when nothing guards it."""
    verdicts = [g.verdict(node) for g in guards]
    return "optional" if "optional" in verdicts else (verdicts[-1] if verdicts else "")


def _walk(tree: ast.AST):
    """Every node, with the ImportError guards around it and the functions it
    sits in (both innermost last).

    Only a `try` body is guarded: its handlers, `else` and `finally` run outside
    it. A function defined in the body imports when it is called, after the try
    has finished, so a def starts unguarded again.
    """
    stack = [(tree, (), ())]
    while stack:
        node, guards, scopes = stack.pop()
        yield node, guards, scopes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            guards, scopes = (), (*scopes, node)
        handlers = ([h for h in node.handlers if _catches_import_error(h)]
                    if isinstance(node, (ast.Try, ast.TryStar)) else [])
        if handlers:
            inner = (*guards, _Guard.of(node, handlers))
            stack.extend((stmt, inner, scopes) for stmt in node.body)
            stack.extend((part, guards, scopes) for part in (*node.handlers, *node.orelse, *node.finalbody))
        else:
            stack.extend((child, guards, scopes) for child in ast.iter_child_nodes(node))


def _top_level_names(node) -> list[str]:
    """The top-level module each name of an absolute import loads."""
    if isinstance(node, ast.Import):
        names = [alias.name.split(".")[0] for alias in node.names]
    elif node.level or not node.module:
        return []  # relative import
    else:
        names = [node.module.split(".")[0]]
    return [n for n in names if n and n != "__future__"]


# ---------------------------------------------------------------------------
# Directories a module puts on sys.path
# ---------------------------------------------------------------------------

def _is_sys_path(node) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "path"
            and isinstance(node.value, ast.Name) and node.value.id == "sys")


def _entries(node) -> list:
    """The items of a list or tuple display; anything else is one opaque entry."""
    return list(node.elts) if isinstance(node, (ast.List, ast.Tuple)) else [node]


def _added_paths(node) -> list | None:
    """What a statement puts on sys.path, or None if it is not such a statement:
    sys.path.insert/append/extend, `sys.path += [...]`, or site.addsitedir."""
    if isinstance(node, ast.AugAssign):
        adds = isinstance(node.op, ast.Add) and _is_sys_path(node.target)
        return _entries(node.value) if adds else None
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    owner, method, args = node.func.value, node.func.attr, node.args
    if isinstance(owner, ast.Name) and owner.id == "site":
        return args[:1] if method == "addsitedir" else None
    if not _is_sys_path(owner):
        return None
    if method == "extend":
        return _entries(args[0]) if len(args) == 1 else None
    arity = {"insert": 2, "append": 1}.get(method)
    return args[-1:] if arity is not None and len(args) == arity else None


def _dotted(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def _parameters(function) -> set[str]:
    args = function.args
    return {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg)
            if a is not None}


def _import_aliases(module: ast.Module) -> dict[str, str]:
    """Each name an import binds to something else -> the dotted name it imports:
    `osp` -> `os.path`, `join` -> `os.path.join`, `P` -> `pathlib.Path`."""
    aliases = {}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            aliases.update({a.asname: a.name for a in node.names if a.asname})
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            aliases.update({a.asname or a.name: f"{node.module}.{a.name}" for a in node.names if a.name != "*"})
    return aliases


def _sole_assignment(stmts: list, name: str) -> tuple[int, ast.expr | None]:
    """How many times one scope binds `name`, and the value when its only binding
    is a plain `name = value` (not a loop target, an import, a def...)."""
    binds, values = 0, []
    for node in walk_same_scope(stmts):
        if isinstance(node, ast.Name):
            binds += node.id == name and isinstance(node.ctx, ast.Store)
        elif isinstance(node, ast.alias):
            binds += (node.asname or node.name.split(".")[0]) == name
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler)):
            binds += node.name == name
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            binds += name in node.names
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name) and targets[0].id == name:
                values.append(node.value)
    return binds, (values[0] if binds == 1 and len(values) == 1 else None)


# Calls that hand back where their one argument points.
_SAME_PATH = frozenset({"Path", "PurePath", "pathlib.Path", "pathlib.PurePath", "str", "os.fspath",
                        "os.path.abspath", "os.path.realpath", "os.path.normpath"})
_RENAMES = frozenset({"with_name", "with_stem", "with_suffix"})
# What only the run decides: a directory read from one of these is opaque.
_RUN_TIME_SOURCES = ("os.environ", "os.getenv", "sys.argv")
_MAX_PATH_DEPTH = 12


class _PathReader:
    """Reads where a path expression in one file points, when the source alone says.

    It follows string literals, `__file__`, Path()/str()/os.fspath(), .parent,
    .parents[n], .resolve(), .absolute(), .joinpath(), .with_name/.with_stem/
    .with_suffix, `/`, os.path's dirname, abspath, realpath, normpath and join —
    under whatever names the file imports them as — and a name bound once, by a
    plain assignment, in the scope that reads it.
    """

    def __init__(self, filepath: Path, module: ast.Module, root: Path):
        self.filepath, self.module, self.root = filepath, module, root
        self.aliases = _import_aliases(module)
        # Each name binding is read once. A binding met again while it is still
        # being read is defined in terms of itself (`ROOT = join(ROOT, ...)`) and
        # names no directory; without both, such a name fans out exponentially.
        self._read: dict[tuple, PurePath | None] = {}
        self._reading: set[tuple] = set()

    def reaches_outside(self, node, scopes: tuple) -> bool:
        """`node` names a directory the source cannot place inside the project:
        one it resolves outside, or one only the run decides. An expression the
        reader merely cannot follow, with no run-time source in it, is neither."""
        path = self.read(node, scopes)
        if path is not None:
            return not self._anchored(path).is_relative_to(self.root)
        return self.decided_at_run_time(node, scopes, set())

    def decided_at_run_time(self, node, scopes: tuple, seen: set) -> bool:
        """`node` reads a function parameter, the environment, sys.argv, or a name
        bound more than once — directly or through names bound once."""
        for child in ast.walk(node):
            dotted = self._canonical(_dotted(child)) if isinstance(child, (ast.Name, ast.Attribute)) else ""
            if any(dotted == source or dotted.startswith(source + ".") for source in _RUN_TIME_SOURCES):
                return True
            if isinstance(child, ast.Name) and self._name_decided_at_run_time(child.id, scopes, seen):
                return True
        return False

    def _name_decided_at_run_time(self, name: str, scopes: tuple, seen: set) -> bool:
        for depth in range(len(scopes), 0, -1):
            scope = scopes[depth - 1]
            if name in _parameters(scope):
                return True
            binds, value = _sole_assignment(scope.body, name) if not isinstance(scope, ast.Lambda) else (0, None)
            if binds:
                return self._binding_decided(binds, value, (id(scope), name), scopes[:depth], seen)
        binds, value = _sole_assignment(self.module.body, name)
        return self._binding_decided(binds, value, (id(self.module), name), (), seen)

    def _binding_decided(self, binds: int, value, key: tuple, scopes: tuple, seen: set) -> bool:
        if binds > 1:
            return True
        if value is None or key in seen:
            return False
        seen.add(key)
        return self.decided_at_run_time(value, scopes, seen)

    def _canonical(self, dotted: str) -> str:
        """`dotted` with its first name replaced by what the file imported it as."""
        head, dot, rest = dotted.partition(".")
        return self.aliases.get(head, head) + dot + rest

    def _anchored(self, path: PurePath) -> PurePath:
        # A relative sys.path entry is relative to the working directory, taken
        # to be the project root.
        return PurePath(os.path.normpath(path if path.is_absolute() else PurePath(self.root, path)))

    def _binding(self, name: str, scopes: tuple):
        """The expression `name` holds where it is read, the scopes to read it in,
        and the binding's identity; None for a parameter or a name bound any way
        but once, plainly."""
        for depth in range(len(scopes), 0, -1):
            scope = scopes[depth - 1]
            if name in _parameters(scope):
                return None
            if isinstance(scope, ast.Lambda):
                continue
            bound, value = _sole_assignment(scope.body, name)
            if bound:
                return (value, scopes[:depth], (id(scope), name)) if value is not None else None
        value = _sole_assignment(self.module.body, name)[1]
        return (value, (), (id(self.module), name)) if value is not None else None

    def _name(self, name: str, scopes: tuple, depth: int) -> PurePath | None:
        bound = self._binding(name, scopes)
        if bound is None:
            return None
        value, where, key = bound
        if key not in self._read:
            if key in self._reading:
                return None
            self._reading.add(key)
            try:
                self._read[key] = self.read(value, where, depth)
            finally:
                self._reading.discard(key)
        return self._read[key]

    def read(self, node, scopes: tuple, depth: int = 0) -> PurePath | None:
        if depth > _MAX_PATH_DEPTH:
            return None
        found = self._step(node, scopes, depth + 1)
        return PurePath(os.path.normpath(found)) if found is not None else None

    def _step(self, node, scopes: tuple, depth: int) -> PurePath | None:
        if isinstance(node, ast.Constant):
            return PurePath(node.value) if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            return PurePath(self.filepath) if node.id == "__file__" else self._name(node.id, scopes, depth)
        if isinstance(node, ast.BinOp):
            return self._joined([node.left, node.right], scopes, depth) if isinstance(node.op, ast.Div) else None
        if isinstance(node, ast.Call):
            return self._call(node, scopes, depth)
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            return self._ancestor(node.value, 0, scopes, depth)
        index = _parents_index(node)
        return self._ancestor(node.value.value, index, scopes, depth) if index is not None else None

    def _call(self, node: ast.Call, scopes: tuple, depth: int) -> PurePath | None:
        name, args = self._canonical(_dotted(node.func)), node.args
        method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if node.keywords:
            return None
        if method in _RENAMES:
            return self._renamed(node, scopes, depth)
        if name in _SAME_PATH and len(args) == 1:
            return self.read(args[0], scopes, depth)
        if name == "os.path.dirname" and len(args) == 1:
            return self._ancestor(args[0], 0, scopes, depth)
        if name == "os.path.join" and args:
            return self._joined(args, scopes, depth)
        if method in {"resolve", "absolute"} and not args:
            return self.read(node.func.value, scopes, depth)
        return self._joined([node.func.value, *args], scopes, depth) if method == "joinpath" else None

    def _renamed(self, node: ast.Call, scopes: tuple, depth: int) -> PurePath | None:
        """`base.with_name("x")` and its siblings, for a literal argument."""
        args = node.args
        if len(args) != 1 or not isinstance(args[0], ast.Constant) or not isinstance(args[0].value, str):
            return None
        base = self.read(node.func.value, scopes, depth)
        try:
            return getattr(base, node.func.attr)(args[0].value) if base is not None else None
        except ValueError:  # an empty name, a suffix without a dot
            return None

    def _joined(self, parts: list, scopes: tuple, depth: int) -> PurePath | None:
        paths = [self.read(part, scopes, depth) for part in parts]
        return None if None in paths else PurePath(*paths)

    def _ancestor(self, node, index: int, scopes: tuple, depth: int) -> PurePath | None:
        """`node`'s parents[index]: .parent and os.path.dirname are index 0."""
        base = self.read(node, scopes, depth)
        above = self._anchored(base).parents if base is not None else ()
        return above[index] if index < len(above) else None


def _parents_index(node) -> int | None:
    """n, for a `<path>.parents[n]` subscript with a literal, non-negative n."""
    if not (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
            and node.value.attr == "parents" and isinstance(node.slice, ast.Constant)):
        return None
    index = node.slice.value
    return index if type(index) is int and index >= 0 else None


def _after_path(position: tuple, in_function: bool, additions: list) -> int:
    """The line of the first opaque sys.path addition that runs before an import at
    `position`, or 0. Module-level code runs top to bottom when the module loads,
    before any function can be called, so an addition inside a function precedes
    only imports that are themselves inside a function."""
    for where, adds_in_function in additions:
        if where < position and (in_function or not adds_in_function):
            return where[0]
    return 0


def _collect_imports(root: Path) -> dict[str, list[_ImportSite]]:
    """
    Walk all Python files and collect top-level module imports.
    Returns dict: module_name -> [_ImportSite, ...]
    """
    imports: dict[str, list[_ImportSite]] = defaultdict(list)
    for filepath in find_python_files(root):
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(filepath))
        except (SyntaxError, ValueError) as exc:
            warn_unparseable(filepath, exc)
            continue
        except Exception as exc:
            warn_detector_error(filepath, exc)
            continue
        reader = _PathReader(filepath, tree, root)
        found, additions = [], []
        for node, guards, scopes in _walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                found.append((node, _guard_of(node, guards), bool(scopes)))
                continue
            added = _added_paths(node)
            if added and any(reader.reaches_outside(entry, scopes) for entry in added):
                additions.append(((node.lineno, node.col_offset), bool(scopes)))
        additions.sort()
        for node, guard, in_function in found:
            after = _after_path((node.lineno, node.col_offset), in_function, additions)
            for top in _top_level_names(node):
                imports[top].append(_ImportSite(str(filepath), node.lineno, guard, after))
    return imports


def _is_stdlib(name: str) -> bool:
    return name in sys.stdlib_module_names


@functools.lru_cache(maxsize=None)
def _compute_local_packages(root: Path) -> frozenset:
    """
    Compute the set of local top-level package/module names for the project
    rooted at `root`.  Cached per root (lru_cache) — no shared mutable state.

    Includes:
    - root/<name>.py  or root/<name>/__init__.py  (directly under root)
    - root/src/<name>.py  or root/src/<name>/__init__.py  (src layout)
    - The top-level package name of any __init__.py found anywhere under root
      (skipping .venv / node_modules / __pycache__): walk up from the package
      directory while the parent also contains __init__.py; the topmost such
      directory's name is a local top-level package.
    - Stems of all .py files sitting directly inside any "src" directory under
      root.
    - The stem of every <name>.py and the name of every <name>/__init__.py
      anywhere in the tree (outside venvs, node_modules and the rest of
      common.EXCLUDE_DIRS). A script that puts `tests/models` on sys.path and
      imports `sampler_case` is importing a file of this repository; nothing
      can install it, so no manifest should declare it.
    """
    names: set[str] = set()

    # Direct children: root/<name>.py or root/<name>/__init__.py
    for p in root.iterdir():
        if p.suffix == ".py" and p.stem != "__init__":
            names.add(p.stem)
        elif p.is_dir() and (p / "__init__.py").exists():
            names.add(p.name)

    # src/ children: root/src/<name>.py or root/src/<name>/__init__.py
    src_dir = root / "src"
    if src_dir.is_dir():
        for p in src_dir.iterdir():
            if p.suffix == ".py" and p.stem != "__init__":
                names.add(p.stem)
            elif p.is_dir() and (p / "__init__.py").exists():
                names.add(p.name)

    # Walk all __init__.py files under root to find top-level packages
    _skip = {".venv", "node_modules", "__pycache__"}
    for init_file in root.rglob("__init__.py"):
        # Skip ignored directories anywhere in the path
        if any(part in _skip for part in init_file.parts) or in_nested_repository(root, init_file):
            continue
        pkg_dir = init_file.parent
        # Walk up while the parent also has __init__.py (i.e. is still a pkg)
        while (pkg_dir.parent / "__init__.py").exists():
            pkg_dir = pkg_dir.parent
        # pkg_dir is now the top-level package directory
        names.add(pkg_dir.name)

    # Also add stems of all .py files directly inside any "src" directory
    for src_candidate in root.rglob("src"):
        if not src_candidate.is_dir():
            continue
        if any(part in _skip for part in src_candidate.parts):
            continue
        for p in src_candidate.iterdir():
            if (p.is_file() and p.suffix == ".py" and p.stem != "__init__"
                    and not in_nested_repository(root, p)):
                names.add(p.stem)

    # Any module or package that exists as a file in the repository is first-party.
    names |= first_party_modules(root)

    return frozenset(names)


def _resolves_beside(name: str, importer: Path) -> bool:
    """True when `name` is a module or directory in the importing file's own directory."""
    beside = importer.parent
    return (beside / f"{name}.py").is_file() or (beside / name).is_dir()


def _is_namespace_dir(directory: Path) -> bool:
    """A directory of .py files with no __init__.py is an importable namespace
    package (PEP 420) — `tests/` is the everyday example."""
    return directory.is_dir() and any(p.suffix == ".py" for p in directory.iterdir())


def _is_local(name: str, root: Path) -> bool:
    """Return True if `name` appears to be a local module under root."""
    # Fast direct checks
    if (
        (root / f"{name}.py").exists()
        or (root / name / "__init__.py").exists()
        or (root / "src" / f"{name}.py").exists()
        or (root / "src" / name / "__init__.py").exists()
        or _is_namespace_dir(root / name)
        or _is_namespace_dir(root / "src" / name)
    ):
        return True
    # Full package scan (cached)
    return name in _compute_local_packages(root)


def _lead(mod: str, sites: list[_ImportSite]) -> CodeSmell:
    """The candidate for an undeclared module no import of which is plain: each
    is guarded without being optional, or follows an opaque sys.path addition."""
    first = min(sites, key=lambda s: (s.file, s.line))
    if first.guard == "exits":
        why = ("it is imported here inside a try whose ImportError handler raises or exits, which is "
               "how a required dependency announces itself and also how an optional extra's entry "
               "point reports that it is missing")
    elif first.guard == "shared":
        why = ("it is imported here in a try with other imports, and the ImportError handler replaces "
               "none of its names, so it may be optional along with them or required and merely caught")
    else:
        why = (f"it is imported here after line {first.after_path} puts a directory on sys.path that "
               f"the source cannot place inside this project, so it may be served from that directory")
    return CodeSmell(
        file=first.file,
        line=first.line,
        smell_type="missing_dependency",
        description=f"Module '{mod}' is not declared in any dependency manifest, but {why}",
        suggestion="",
        severity="high",
        kind="candidate",
        also_caused_by=_GUARD_REASONS.get(first.guard, _RUNTIME_PATH_REASONS),
    )


def analyze(root: Path, ignore: set) -> list[CodeSmell]:
    issues: list[CodeSmell] = []
    root = root.resolve()

    # ------------------------------------------------------------------ #
    # 1. Declared dependencies
    # ------------------------------------------------------------------ #
    req_files = _collect_requirements_files(root)
    pyproject_deps = _collect_pyproject_deps(root)

    # Manifest presence is about the FILE existing (and parsing), not about how
    # many deps it declares — an intentionally-empty dependency list is still a
    # manifest, and undeclared imports against it are missing_dependency, not
    # no_dependency_manifest.
    def _pyproject_parses() -> bool:
        pyproject = root / "pyproject.toml"
        if not pyproject.exists() or tomllib is None:
            return False
        try:
            tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
            return True
        except Exception:
            return False

    has_manifest = (
        bool(req_files)
        or bool(pyproject_deps)
        or any(not in_nested_repository(root, p) for p in root.rglob("requirements*.txt"))
        or _pyproject_parses()
    )

    # Build set of normalized declared names and lookup for location
    # declared_norm -> (source_file, lineno)
    declared: dict[str, tuple[str, int]] = {}

    for req_file, entries in req_files:
        for lineno, raw in entries:
            name = _parse_dist_name(raw)
            if not name:
                continue
            norm = _normalize(name)
            declared[norm] = (str(req_file), lineno)

            # unpinned_dependency
            if "unpinned_dependency" not in ignore and not _has_version_specifier(raw):
                issues.append(CodeSmell(
                    file=str(req_file),
                    line=lineno,
                    smell_type="unpinned_dependency",
                    description=f"Dependency '{name}' has no version specifier",
                    suggestion=(
                        "Pin a version (e.g. 'requests==2.31.0') or add a floor "
                        "(e.g. 'requests>=2.0') for reproducible installs."
                    ),
                    severity="low",
                    code_snippet=raw[:80],
                ))

    for name, lineno, _has_ver in pyproject_deps:
        norm = _normalize(name)
        if norm not in declared:
            pyproject_path = str(root / "pyproject.toml")
            declared[norm] = (pyproject_path, lineno)

    # Also build reverse map: norm -> canonical name (for reporting)
    declared_display: dict[str, str] = {}
    for req_file, entries in req_files:
        for _lineno, raw in entries:
            name = _parse_dist_name(raw)
            if name:
                declared_display[_normalize(name)] = name
    for name, _lineno, _hv in pyproject_deps:
        declared_display[_normalize(name)] = name

    # ------------------------------------------------------------------ #
    # 2. Actual imports
    # ------------------------------------------------------------------ #
    all_imports = _collect_imports(root)

    # ------------------------------------------------------------------ #
    # 3. Classify imports as third-party
    # ------------------------------------------------------------------ #
    third_party_imports: dict[str, list[_ImportSite]] = {}
    for mod, sites in all_imports.items():
        if _is_stdlib(mod):
            continue
        if _is_local(mod, root):
            continue
        # pytest (and any script run from its own directory) puts the importing
        # file's directory on sys.path, so `from test_chat import helper` in
        # tests/ resolves to the file next door. No manifest declares that, and
        # a tests/ directory needs no __init__.py for it to work.
        sites = [s for s in sites if not _resolves_beside(mod, Path(s.file))]
        # An optional import is one the code runs without: the package being
        # absent is handled where it is imported, so no manifest owes it. One
        # such site shows the program is built to run without the module, unless
        # a plain import elsewhere makes it required.
        if not any(s.plain for s in sites) and any(s.guard == "optional" for s in sites):
            continue
        sites = [s for s in sites if s.guard != "optional"]
        if sites:
            third_party_imports[mod] = sites

    # ------------------------------------------------------------------ #
    # 4. No manifest at all
    # ------------------------------------------------------------------ #
    # A module with no plain import is guarded without being optional, or comes
    # after a sys.path addition the source cannot read: a lead, reported below.
    unexplained = {mod for mod, sites in third_party_imports.items() if any(s.plain for s in sites)}
    if not has_manifest and unexplained:
        if "no_dependency_manifest" not in ignore:
            issues.append(CodeSmell(
                file=str(root),
                line=1,
                smell_type="no_dependency_manifest",
                description=(
                    f"No requirements*.txt or pyproject.toml found, but "
                    f"{len(unexplained)} third-party module(s) are imported"
                ),
                suggestion=(
                    "Add a pyproject.toml (or requirements.txt) declaring your "
                    "dependencies so the project can be installed reproducibly."
                ),
                severity="medium",
            ))
        return issues

    # ------------------------------------------------------------------ #
    # 5. missing_dependency: imported but not declared
    # ------------------------------------------------------------------ #
    if "missing_dependency" not in ignore:
        for mod, sites in sorted(third_party_imports.items()):
            norm_mod = _normalize(mod)
            # Check direct match
            if norm_mod in declared:
                continue
            # Check via import->dist map
            mapped = _IMPORT_TO_DIST.get(mod.lower())
            if mapped and _normalize(mapped) in declared:
                continue
            if mod not in unexplained:
                issues.append(_lead(mod, sites))
                continue
            # Not found — report once, pointing at the root
            example_file = sorted({s.file for s in sites if s.plain})[0]
            issues.append(CodeSmell(
                file=str(root),
                line=1,
                smell_type="missing_dependency",
                description=(
                    f"Module '{mod}' is imported (e.g. in {example_file}) "
                    f"but not declared in any dependency manifest"
                ),
                suggestion=(
                    f"Add '{mod}' (or its distribution name) to pyproject.toml "
                    f"or requirements.txt so installs are reproducible."
                ),
                severity="high",
            ))

    # ------------------------------------------------------------------ #
    # 6. unused_dependency: declared but never imported
    # ------------------------------------------------------------------ #
    if "unused_dependency" not in ignore:
        # A declared dep imported anywhere (even via a stdlib-shadowing name)
        # counts as used, so compare against ALL imports, not just third-party.
        all_imported_norms = {_normalize(m) for m in all_imports}

        for norm_dep, (src_file, lineno) in sorted(declared.items()):
            display_name = declared_display.get(norm_dep, norm_dep)
            # Skip tooling deps
            if norm_dep in {_normalize(t) for t in _TOOLING_DEPS}:
                continue
            if display_name.lower() in {t.lower() for t in _TOOLING_DEPS}:
                continue

            # Check if any import matches this dep
            found = False
            # Direct: dep name == module name (normalized)
            if norm_dep in all_imported_norms:
                found = True
            # Reverse map: dist name -> import name
            if not found:
                for imp_name, dist_name in _IMPORT_TO_DIST.items():
                    if _normalize(dist_name) == norm_dep and _normalize(imp_name) in all_imported_norms:
                        found = True
                        break
            if not found:
                issues.append(CodeSmell(
                    file=src_file,
                    line=lineno,
                    smell_type="unused_dependency",
                    description=(
                        f"Dependency '{display_name}' is declared but never imported anywhere"
                    ),
                    suggestion=(
                        "Remove it if it is no longer needed, or add a comment explaining "
                        "why it is required (e.g. a runtime plugin, CLI tool, or implicit dep)."
                    ),
                    severity="medium",
                    code_snippet=display_name[:80],
                ))

    return issues


def analyze_tree(path: Path, ignore: set) -> list:
    """Findings for the whole tree, ordered as main() orders them."""
    findings = analyze(Path(path), ignore)
    sort_findings(findings)
    return findings


def to_record(issue: CodeSmell) -> dict:
    """The JSON shape this detector emits, shared with the runner.

    `kind` is always present, so a consumer never reads an absent key as a
    classification; the reasons tuple serialises as a list.
    """
    record = asdict(issue)
    record["also_caused_by"] = list(record["also_caused_by"])
    return record


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect dependency management issues in Python")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    root = Path(args.path)
    all_issues = analyze(root, ignore)
    sort_findings(all_issues)

    if args.format == "json":
        print(json.dumps([to_record(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No dependency issues found!")
            return
        by_type: dict[str, int] = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        # A candidate is a lead, not a defect, so it is counted apart.
        leads = sum(1 for i in all_issues if i.kind == "candidate")
        print(f"Found {len(all_issues) - leads} dependency issue(s), "
              f"{leads} candidate(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = SEVERITY_ICONS
        for i in all_issues:
            # A severity icon on a lead reads as a verdict the evidence does not support.
            marker = ("? [CANDIDATE]" if i.kind == "candidate"
                      else f"{icons[i.severity]} [{i.severity.upper()}]")
            print(f"{marker} {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            if i.suggestion:
                print(f"   → {i.suggestion}")
            for reason in i.also_caused_by:
                print(f"   ? also caused by: {reason}")
            print()


if __name__ == "__main__":
    main()
