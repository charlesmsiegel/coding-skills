#!/usr/bin/env python3
"""
Identify source modules that have no tests referencing them.

Provides deterministic support for establishing a test safety net by flagging
modules that define logic but are not imported by any test file.

Finds:
  - no_tests_in_repo  : project has source files but zero test files (HIGH)
  - untested_module   : a source module defining functions/classes that no
                        test file imports (MEDIUM)

A test reaches a module in more ways than naming it in an import statement, and
each of these counts:
  - an attribute chain off an imported package — `from app import store` and
    then `store.attempts.forget(...)` reaches `app/store/attempts.py`;
  - `importlib.import_module("pkg.mod")`, `runpy.run_module("pkg.mod")`, and
    loading the file by path with `importlib.util.spec_from_file_location`,
    `runpy.run_path` or `SourceFileLoader` (a script outside any package);
  - for a module of web route handlers (`@router.get("/items/{id}")`), a string
    in a test that requests one of its paths.
A route module none of that reaches is a candidate, not a finding: tests can
drive it over HTTP through the application with URLs built at run time, which
no reading of the test source can rule out.
"""

import ast
import json
import re
import argparse
import contextlib
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Set, List, Tuple, Optional
from collections import defaultdict
from common import cached_parse, project_parts, sort_findings, SEVERITY_ICONS, configure_output, find_python_files, warn_detector_error, warn_unparseable, ROUTE_VERBS


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""
    # "candidate" for a route module no test names: it carries no fix and names,
    # in also_caused_by, how tests can reach it without naming it.
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Shared helpers (verbatim contract from find_global_state.py)
# ---------------------------------------------------------------------------

def _get_line(lines: List[str], lineno: int) -> str:
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


# ---------------------------------------------------------------------------
# File classification helpers
# ---------------------------------------------------------------------------

_EXCLUDE_SEGMENTS: Set[str] = {".venv", "build", "dist", "docs", "migrations"}
_ENTRYPOINT_SEGMENTS: Set[str] = {"scripts", "bin"}


def _is_test_file(p: Path) -> bool:
    """Return True if the file is a test or test-infrastructure file."""
    name = p.name
    if name == "conftest.py":
        return True
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    # Any directory of its project named "tests" or "test" — measured below the
    # project, so a checkout that lives under a `tests/` directory is not all tests.
    for part in project_parts(p)[:-1]:  # exclude the filename itself (already checked)
        if part in ("tests", "test"):
            return True
    return False


def _is_excluded(p: Path, root: Optional[Path] = None) -> bool:
    """Return True if the file lives under an excluded directory segment.

    Checks are performed on the path relative to *root* so that files inside
    (e.g.) a root that happens to be named '.venv' are not incorrectly excluded.
    Falls back to absolute parts when root is not supplied.
    """
    if root is not None:
        try:
            parts = p.relative_to(root).parts[:-1]  # directory parts only
        except ValueError:
            parts = p.parts
    else:
        parts = p.parts
    for part in parts:
        if part in _EXCLUDE_SEGMENTS:
            return True
    return False


def _is_source_file(p: Path, root: Optional[Path] = None) -> bool:
    """Return True if the file should be treated as a source module."""
    name = p.name
    # Explicit exclusions
    if name in ("__init__.py", "setup.py", "conftest.py"):
        return False
    if _is_excluded(p, root):
        return False
    if _is_test_file(p):
        return False
    # Entrypoint scripts (in scripts/ or bin/) are excluded only when those
    # directories appear *below* the package root — i.e., they are not the
    # sole directory level between the analyzed root and the file.  This lets
    # a project whose entire code lives directly inside scripts/ (where scripts/
    # IS the library) still be analyzed, while still excluding helpers tucked
    # into a scripts/ subdirectory of a larger package tree.
    if root is not None:
        try:
            rel_parts = p.relative_to(root).parts[:-1]  # directory parts only
        except ValueError:
            rel_parts = ()
    else:
        rel_parts = p.parts
    # Only apply entrypoint exclusion when there is more than one directory
    # level in the relative path (e.g. "src/scripts/helper.py" is excluded,
    # but "scripts/tool.py" directly under root is kept as a source file).
    if len(rel_parts) > 1:
        for part in rel_parts:
            if part in _ENTRYPOINT_SEGMENTS:
                return False
    return True


# ---------------------------------------------------------------------------
# Module name helpers
# ---------------------------------------------------------------------------

def _module_names_for_file(p: Path, root: Path) -> Tuple[str, str]:
    """Return (dotted_name, bare_stem) for a source file relative to root."""
    try:
        rel = p.relative_to(root)
    except ValueError:
        return (p.stem, p.stem)
    parts = list(rel.parts)
    parts[-1] = parts[-1][:-3]  # strip .py
    dotted = ".".join(parts)
    stem = parts[-1]
    return (dotted, stem)


# ---------------------------------------------------------------------------
# Import name extraction
# ---------------------------------------------------------------------------

def _collect_imported_names(tree: ast.AST, nodes: Optional[list] = None) -> tuple[Set[str], Set[str]]:
    """
    Collect module/name references from import statements.

    Returns a pair (dotted_names, bare_names):
      - dotted_names: every dotted import string with at least one dot
        (e.g. "pkg.util", "from pkg import util" → "pkg.util" or just "pkg").
        For ``from pkg import name`` we record "pkg.name" when there's a module
        part, or the bare name when there isn't.
      - bare_names: every single-component (no dot) name seen in any import.

    The separation lets the caller apply different matching rules: dotted
    imports require a suffix match against a module's full dotted path, while
    bare imports fall back to the generous stem-based match.

    ``nodes`` is ``list(ast.walk(tree))`` when the caller already has it: a
    large suite is walked once, not once per question asked of it.
    """
    dotted_names: Set[str] = set()
    bare_names: Set[str] = set()

    def _add(name: str) -> None:
        if "." in name:
            dotted_names.add(name)
        else:
            bare_names.add(name)

    for node in (nodes if nodes is not None else ast.walk(tree)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dotted = alias.name
                _add(dotted)
                # top-level package (bare)
                bare_names.add(dotted.split(".")[0])
                # leaf name (bare)
                bare_names.add(dotted.split(".")[-1])

        elif isinstance(node, ast.ImportFrom):
            module = node.module
            if module:
                _add(module)
                bare_names.add(module.split(".")[0])
                bare_names.add(module.split(".")[-1])
                # `from pkg import name` → record "pkg.name" as dotted
                for alias in node.names:
                    if alias.name != "*":
                        dotted_names.add(module + "." + alias.name)
            else:
                # `from . import name` (relative, no module) — only bare
                for alias in node.names:
                    if alias.name != "*":
                        bare_names.add(alias.name)

            # Relative: `from .x import y` — base name is "x" (bare)
            if node.level and node.level > 0 and module:
                bare_names.add(module.split(".")[-1])

    return dotted_names, bare_names


def _import_bindings(nodes: list) -> dict[str, str]:
    """Local name -> the absolute dotted path it is bound to by an import statement.

    `import a.b` binds `a` to `a`; `import a.b as c` binds `c` to `a.b`;
    `from a import b` binds `b` to `a.b` (a submodule or a name in `a`, which
    the matcher treats alike). Relative imports have no absolute path here.
    """
    bound: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    bound[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                if alias.name != "*":
                    bound[alias.asname or alias.name] = node.module + "." + alias.name
    return bound


def _attribute_chain(node: ast.Attribute) -> tuple[str, list[str]] | None:
    """`a.b.c` -> ("a", ["b", "c"]); None when the chain is not rooted at a name."""
    attrs: list[str] = []
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return node.id, attrs[::-1]


def _collect_attribute_reaches(nodes: list) -> Set[str]:
    """Dotted paths a test reaches through attributes of a name it imported.

    `from app import store` then `store.attempts.forget` gives
    `app.store.attempts` and `app.store.attempts.forget` — the module is the
    longest prefix that exists, which the matcher finds by suffix.
    """
    bound = _import_bindings(nodes)
    reached: Set[str] = set()
    for node in nodes:
        if not isinstance(node, ast.Attribute):
            continue
        chain = _attribute_chain(node)
        if chain is None or chain[0] not in bound:
            continue
        base, attrs = bound[chain[0]], chain[1]
        for depth in range(1, len(attrs) + 1):
            reached.add(base + "." + ".".join(attrs[:depth]))
    return reached


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


# Calls that import a module named by a dotted string, and calls that load a
# file by its path. The path is the last positional argument they take a path in.
_IMPORT_BY_NAME = frozenset({"import_module", "run_module", "__import__"})
_LOAD_BY_PATH = frozenset({"spec_from_file_location", "run_path", "SourceFileLoader"})


def _path_segments(node: ast.expr, assigned: dict) -> tuple[str, ...]:
    """The literal tail of a path expression, as segments.

    `ROOT / "android" / "entry.py"` -> ("android", "entry.py");
    `os.path.join(HERE, "tools/run.py")` -> ("tools", "run.py"); a name bound
    once in the module to such an expression is followed. What precedes the
    literal tail (a computed root) is dropped: only the tail can be matched.
    """
    if isinstance(node, ast.Name) and node.id in assigned:
        return _path_segments(assigned[node.id], {})
    if isinstance(node, ast.Call) and _call_name(node.func) in ("str", "Path", "PurePath") and len(node.args) == 1:
        return _path_segments(node.args[0], assigned)
    parts: list[ast.expr] = []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts.append(node.right)
            node = node.left
        parts.append(node)
        parts.reverse()
    elif isinstance(node, ast.Call) and _call_name(node.func) == "join":
        parts = list(node.args)
    else:
        parts = [node]
    tail: list[str] = []
    for part in reversed(parts):
        if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
            break
        tail[:0] = [seg for seg in re.split(r"[\\/]", part.value) if seg and seg != "."]
    return tuple(tail)


def _collect_dynamic_imports(tree: ast.AST, nodes: list) -> tuple[Set[str], List[Tuple[str, ...]]]:
    """(dotted names imported by string, path tails of files loaded by path)."""
    assigned = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assigned[node.targets[0].id] = node.value
    dotted: Set[str] = set()
    paths: List[Tuple[str, ...]] = []
    for node in nodes:
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = _call_name(node.func)
        if name in _IMPORT_BY_NAME:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                dotted.add(first.value)
        elif name in _LOAD_BY_PATH:
            # spec_from_file_location(name, path), SourceFileLoader(name, path),
            # run_path(path): the path is the last positional argument.
            tail = _path_segments(node.args[-1], assigned)
            if tail and tail[-1].endswith(".py"):
                paths.append(tail)
    return dotted, paths


# ---------------------------------------------------------------------------
# Web routes
# ---------------------------------------------------------------------------

def _route_templates(tree: ast.AST) -> List[str]:
    """The path templates of every route handler the module registers.

    Empty for a module with no route-decorated functions — the signal that it
    is a route module at all.
    """
    templates: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in ROUTE_VERBS):
                continue
            path = dec.args[0] if dec.args else next(
                (kw.value for kw in dec.keywords if kw.arg in ("path", "rule")), None)
            if isinstance(path, ast.Constant) and isinstance(path.value, str):
                templates.append(path.value)
            else:
                templates.append("")   # a route, path unknown: still a route module
    return templates


def _template_pattern(template: str):
    """A regex for the requests a template serves, or None when it names nothing.

    `{id}` / `<id>` match one segment, `{p:path}` / `<path:p>` any tail. A
    template must keep at least one literal segment: `/{id}` alone would match
    every request in the suite and prove nothing. It is matched against the
    end of a request path, since a router prefix the module does not spell
    sits in front of it.
    """
    pieces = re.split(r"(\{[^}]*\}|<[^>]*>)", template)
    if not any(re.search(r"[A-Za-z0-9]", piece) for piece in pieces[0::2]):
        return None
    out = []
    for index, piece in enumerate(pieces):
        if index % 2 == 0:
            out.append(re.escape(piece))
        elif piece.endswith(":path}") or piece.startswith("<path:"):
            out.append(r".+")
        else:
            out.append(r"[^/?#]+")
    return re.compile("".join(out).rstrip("/") + r"/?(?:[?#]|$)")


# Stands in for an f-string's {expression} when a test builds a URL.
_HOLE = "_x_"


def _string_value(node: ast.expr) -> str | None:
    """A string literal, an f-string or a `+` of them, with each hole filled."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else _HOLE for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string_value(node.left), _string_value(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else _HOLE) + (right if right is not None else _HOLE)
    return None


def _collect_request_strings(nodes: list) -> Set[str]:
    """Literal paths passed to HTTP-shaped calls, rather than incidental prose."""
    found: Set[str] = set()
    for node in nodes:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "post", "put", "patch", "delete", "head",
                                       "options", "request", "open"}):
            continue
        argument = next((kw.value for kw in node.keywords if kw.arg in {"url", "path"}), None)
        if argument is None:
            index = 1 if node.func.attr == "request" else 0
            argument = node.args[index] if len(node.args) > index else None
        value = _string_value(argument)
        if value and "/" in value:
            found.add(value.split("://", 1)[-1])
    return found


# ---------------------------------------------------------------------------
# Re-exports: `from pkg import build_server` tests pkg/server.py
# ---------------------------------------------------------------------------

def _top_level(body: list):
    """A module's statements, including those under a top-level if/try/with."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, (ast.If, ast.Try, ast.With)):
            for field in ("body", "orelse", "finalbody"):
                yield from _top_level(getattr(stmt, field, []))
            for handler in getattr(stmt, "handlers", []):
                yield from _top_level(handler.body)


def _literal_names(value) -> list | None:
    """The strings of a list/tuple literal such as `__all__ = ["a", "b"]`; None if it is not one."""
    if not isinstance(value, (ast.List, ast.Tuple)):
        return None
    names = [elt.value for elt in value.elts if isinstance(elt, ast.Constant)]
    return names if all(isinstance(name, str) for name in names) and len(names) == len(value.elts) else None


@dataclass
class _Bindings:
    """What a module's top level binds: its own definitions, the names it imports
    from other modules (name -> (module, level, original name)), its star imports,
    and its `__all__` when that is a literal."""
    defined: set
    imported: dict
    stars: list
    exported: list | None = None


def _record_import(found: _Bindings, stmt: ast.ImportFrom) -> None:
    for alias in stmt.names:
        if alias.name == "*":
            found.stars.append((stmt.module, stmt.level))
        else:
            name = alias.asname or alias.name
            found.defined.discard(name)
            found.imported[name] = (stmt.module, stmt.level, alias.name)


def _record_assignment(found: _Bindings, stmt: ast.Assign | ast.AnnAssign | ast.AugAssign) -> None:
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    names = {node.id for target in targets for node in ast.walk(target)
             if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    found.defined |= names
    for name in names:
        found.imported.pop(name, None)
    if "__all__" not in names or stmt.value is None:
        return
    listed = _literal_names(stmt.value)
    if listed is not None and isinstance(stmt, ast.AugAssign) and found.exported is not None:
        listed = found.exported + listed
    found.exported = listed


def _bindings(tree: ast.Module) -> _Bindings:
    found = _Bindings(set(), {}, [])
    for stmt in _top_level(tree.body):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.defined.add(stmt.name)
            found.imported.pop(stmt.name, None)
        elif isinstance(stmt, ast.ImportFrom):
            _record_import(found, stmt)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            _record_assignment(found, stmt)
    return found


class _ReExports:
    """Follows a name imported from a project module to the module that defines it.

    `pkg/__init__.py` doing `from .server import build_server` makes
    `from pkg import build_server` hand a test the function server.py defines, so
    that test exercises server.py. The same holds for a plain module that imports
    a name and is imported from (`from pkg.facade import read_header`, where
    facade got it from pkg.layout), and for star imports, which honour `__all__`.
    Only the imported name is followed: a module that is merely imported by the
    module a test imports is not tested by it.
    """

    def __init__(self, files: list, root: Path):
        self._dotted: dict = {}                       # file -> its package-or-module dotted name
        self._by_suffix: dict = defaultdict(list)    # dotted suffix -> files
        for path in files:
            dotted = _module_names_for_file(path, root)[0]
            if path.name == "__init__.py":
                dotted = dotted.rpartition(".")[0]
            self._dotted[path] = dotted
            suffix = dotted
            while suffix:                             # src.pkg.server, pkg.server, server
                self._by_suffix[suffix].append(path)
                suffix = suffix.partition(".")[2]
        self._parsed: dict = {}

    def resolve(self, importer: Path, module: str | None, level: int) -> list:
        """The project files `from <module> import ...` in `importer` can name."""
        if not level:
            return self._by_suffix.get(module, []) if module else []
        package = self._dotted.get(importer, "").split(".")
        if importer.name != "__init__.py":
            package = package[:-1]
        if level - 1 > len(package):
            return []
        base = package[:len(package) - (level - 1)]
        target = ".".join(base + (module.split(".") if module else []))
        return [path for path in self._by_suffix.get(target, []) if self._dotted[path] == target]

    def _bindings_of(self, path: Path) -> _Bindings:
        if path not in self._parsed:
            try:
                self._parsed[path] = _bindings(cached_parse(path)[1])
            except (SyntaxError, ValueError, OSError):
                self._parsed[path] = _Bindings(set(), {}, [])
        return self._parsed[path]

    def exports(self, path: Path, seen: frozenset = frozenset()) -> set:
        """The names `from <path> import *` binds: `__all__`, else every public name."""
        bound = self._bindings_of(path)
        if bound.exported is not None:
            return set(bound.exported)
        names = {name for name in bound.defined | set(bound.imported) if not name.startswith("_")}
        for module, level in bound.stars:
            for target in self.resolve(path, module, level):
                if target not in seen:
                    names |= self.exports(target, seen | {path})
        return names

    def defining(self, importer: Path, node: ast.ImportFrom) -> set:
        """The project files that define what `node`, an import in `importer`, binds."""
        found: set = set()
        names = [alias.name for alias in node.names]
        for target in self.resolve(importer, node.module, node.level):
            for name in self.exports(target) if names == ["*"] else names:
                found |= self.definers(target, name)
        return found

    def definers(self, path: Path, name: str, seen: frozenset = frozenset()) -> set:
        """The project files that define `name` as `path` exposes it."""
        if (path, name) in seen:
            return set()
        seen = seen | {(path, name)}
        bound = self._bindings_of(path)
        if name in bound.defined:
            return {path}
        found: set = set()
        if name in bound.imported:
            module, level, original = bound.imported[name]
            for target in self.resolve(path, module, level):
                found |= self.definers(target, original, seen)
            return found
        for module, level in bound.stars:
            for target in self.resolve(path, module, level):
                if name in self.exports(target):
                    found |= self.definers(target, name, seen)
        return found


def _tested_through_reexports(test_files: list, files: list, root: Path) -> set:
    """Files that define a name some test imports, wherever the test imports it from."""
    reexports = _ReExports(files, root)
    tested: set = set()
    for test in test_files:
        try:
            tree = cached_parse(test)[1]
        except (SyntaxError, ValueError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                tested |= reexports.defining(test, node)
    return tested


def _has_definitions(tree: ast.AST) -> bool:
    """Return True if the module defines at least one function or class."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return True
    return False


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(root: Path, ignore: Set[str]) -> List[CodeSmell]:
    py_files = list(find_python_files(root))
    if not py_files:
        return []

    test_files: List[Path] = []
    source_files: List[Path] = []

    for p in py_files:
        if _is_test_file(p):
            test_files.append(p)
        elif _is_source_file(p, root):
            source_files.append(p)

    issues: List[CodeSmell] = []

    # no_tests_in_repo
    if source_files and not test_files:
        if "no_tests_in_repo" not in ignore:
            issues.append(CodeSmell(
                file=str(root),
                line=1,
                smell_type="no_tests_in_repo",
                description="Project has source modules but no test files were found",
                suggestion=(
                    "Establish a safety net before refactoring — add pytest and "
                    "characterization tests for the highest-churn modules first."
                ),
                severity="high",
            ))
        # Still report untested_module findings even when no tests exist,
        # so callers can see what needs coverage.

    # Collect all names referenced by test files, split into dotted vs bare.
    test_dotted: Set[str] = set()   # dotted imports (at least one dot component)
    test_bare: Set[str] = set()     # bare single-name imports
    test_attrs: Set[str] = set()    # attribute chains off imported names
    test_paths: List[Tuple[str, ...]] = []   # files loaded by path
    test_strings: Set[str] = set()  # literals that could be request paths
    for tp in test_files:
        # Unreadable/unparseable test files are skipped by design.
        with contextlib.suppress(Exception):
            source = tp.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(tp))
            nodes = list(ast.walk(tree))
            d, b = _collect_imported_names(tree, nodes)
            test_dotted.update(d)
            test_bare.update(b)
            test_attrs.update(_collect_attribute_reaches(nodes))
            by_name, by_path = _collect_dynamic_imports(tree, nodes)
            test_dotted.update(n for n in by_name if "." in n)
            test_bare.update(n for n in by_name if "." not in n)
            test_paths.extend(by_path)
            test_strings.update(_collect_request_strings(nodes))

    def _is_tested(dotted: str, stem: str) -> bool:
        """Return True if any test import covers this module.

        Matching rules
        --------------
        1. Full dotted name exact match (dotted imports).
        2. Suffix match aligned on dot boundaries — e.g. test imports "pkg.util"
           and module is "src.pkg.util" (dotted imports only).
        3. Bare stem match — if a test does a bare ``import util`` or uses "util"
           as a leaf name, it matches any module with that stem (generous, kept
           for backward-compat with existing bare-import style tests).
           Bare stem matching uses *test_bare* only, so that a dotted import
           ``from pkg1 import util`` (recorded as "pkg1.util" in dotted) does
           NOT bless an unrelated ``pkg2/util.py`` via stem alone.
        """
        # Rule 1 & 2: dotted imports require identity or suffix alignment
        for test_imp in test_dotted:
            if test_imp == dotted:
                return True
            # suffix check: module dotted must end with "."+test_imp or be test_imp
            if dotted == test_imp or dotted.endswith("." + test_imp):
                return True
            # also check if test_imp ends with "."+dotted (test imports a parent)
            if test_imp.endswith("." + dotted) or test_imp == dotted:
                return True

        # Rule 3: bare stem match (generous — keeps existing behaviour for bare imports)
        if stem in test_bare:
            return True

        # Rule 4: an attribute chain off an imported package. Suffix only: the
        # reverse rule above would let `os.path` bless a top-level `path.py`.
        # Looked up by the module's own suffixes — a large suite reaches tens of
        # thousands of chains, and scanning them per module is quadratic.
        parts = dotted.split(".")
        if any(".".join(parts[i:]) in test_attrs for i in range(len(parts) - 1)):
            return True

        return False

    def _is_loaded_by_path(sp: Path) -> bool:
        """Rule 5: a test loads this file by path (its literal tail ends the path)."""
        parts = sp.relative_to(root).parts if sp.is_relative_to(root) else sp.parts
        return any(len(tail) <= len(parts) and parts[-len(tail):] == tail for tail in test_paths)

    def _route_requested(templates: List[str]) -> bool:
        """Rule 6: a test string requests one of this route module's paths."""
        for template in templates:
            pattern = _template_pattern(template)
            if pattern is not None and any(pattern.search(text) for text in test_strings):
                return True
        return False

    # A test that imports a name re-exported through another module (a
    # package's __init__, or any module that imports it) tests where it is defined.
    reexported = _tested_through_reexports(test_files, py_files, root)

    # Check each source file
    if "untested_module" not in ignore:
        for sp in sorted(source_files, key=str):
            try:
                source = sp.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=str(sp))
            except (SyntaxError, ValueError) as exc:
                warn_unparseable(sp, exc)
                continue
            except Exception as exc:
                warn_detector_error(sp, exc)
                continue

            if not _has_definitions(tree):
                continue

            dotted, stem = _module_names_for_file(sp, root)
            if _is_tested(dotted, stem) or _is_loaded_by_path(sp) or sp in reexported:
                continue

            lines = source.splitlines()
            templates = _route_templates(tree)
            if templates:
                if _route_requested(templates):
                    continue
                issues.append(CodeSmell(
                    file=str(sp),
                    line=1,
                    smell_type="untested_module",
                    description=(f"Module '{dotted}' registers web routes, and no test imports it "
                                 "or names one of its paths"),
                    suggestion="",
                    severity="medium",
                    code_snippet=_get_line(lines, 1),
                    kind="candidate",
                    also_caused_by=(
                        "tests drive its handlers over HTTP through the application (a test "
                        "client over the app factory, which imports this module), with request "
                        "paths built at run time rather than written out",
                        "its router is mounted under a prefix and the tests request the full "
                        "path through a helper that assembles it",
                    ),
                ))
                continue
            issues.append(CodeSmell(
                file=str(sp),
                line=1,
                smell_type="untested_module",
                description=f"Module '{dotted}' defines functions/classes but is not imported by any test file",
                suggestion="Add at least a characterization test pinning current behavior before refactoring this module.",
                severity="medium",
                code_snippet=_get_line(lines, 1),
            ))

    return issues


def to_record(issue: "CodeSmell") -> dict:
    """The JSON shape this detector emits, shared with the runner.

    `kind` is always present, so a consumer never has to read an absent key as
    a classification; the reasons tuple serialises as a list.
    """
    record = asdict(issue)
    record["also_caused_by"] = list(record["also_caused_by"])
    return record


def analyze_tree(path: Path, ignore: set) -> list:
    """Findings for the whole tree, ordered as main() orders them."""
    root = Path(path)
    if root.is_file():
        root = root.parent
    findings = analyze(root.resolve(), ignore)
    sort_findings(findings)
    return findings


def main():
    configure_output()
    parser = argparse.ArgumentParser(
        description="Identify source modules with no test coverage references"
    )
    parser.add_argument("path", nargs="?", default=".", help="File or directory to analyze")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    root = Path(args.path)
    if root.is_file():
        root = root.parent
    root = root.resolve()

    all_issues = analyze(root, ignore)
    sort_findings(all_issues)

    if args.format == "json":
        print(json.dumps([to_record(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No test-coverage issues found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        leads = sum(1 for i in all_issues if i.kind == "candidate")
        print(f"Found {len(all_issues) - leads} test-coverage issue(s), {leads} candidate(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = SEVERITY_ICONS
        for i in all_issues:
            marker = ("? [CANDIDATE]" if i.kind == "candidate"
                      else f"{icons[i.severity]} [{i.severity.upper()}]")
            print(f"{marker} {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            # A candidate has no fix to print; what it has is the readings to rule out.
            if i.suggestion:
                print(f"   → {i.suggestion}")
            for reason in i.also_caused_by:
                print(f"   ? also caused by: {reason}")
            print()


if __name__ == "__main__":
    main()
