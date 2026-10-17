"""Where each import in one file stands: guarded, and after which sys.path addition.

find_dependency_issues.py asks two questions of every import before it charges
a missing dependency. Is an ImportError raised here handled — by a handler
that visibly degrades (an optional import), or by one that does not? And does
the import run after the file puts a directory on sys.path that the source
cannot place inside the project, so the module may be served from there? Both
are read from the one file's AST; the whole-tree reconciliation against the
manifests stays in the detector.
"""

import ast
import os
from dataclasses import dataclass
from pathlib import Path, PurePath

from ast_scopes import walk_same_scope


@dataclass(frozen=True)
class ImportSite:
    """One absolute import of a top-level module, and what surrounds it."""
    file: str
    line: int
    # How an enclosing `except ImportError` bears on it: "" (none), "optional"
    # (a handler visibly degrades), "exits" (no handler visibly degrades: it
    # raises, exits, or does something the reader cannot call a degrade), or
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
_CATCH_ALLS = frozenset({"Exception", "BaseException"})


def _caught(handler: ast.ExceptHandler) -> set[str]:
    """The exception class names an `except` lists, alone or in a tuple."""
    listed = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {t.id if isinstance(t, ast.Name) else t.attr
            for t in listed if isinstance(t, (ast.Name, ast.Attribute))}


def _import_error_handlers(node) -> list:
    """The handlers that name ImportError or ModuleNotFoundError and can run: only
    the first matching handler does, so none after a bare `except:` or an `except
    Exception:` / `except BaseException:` counts."""
    found = []
    for handler in node.handlers:
        caught = _caught(handler)
        if caught & _IMPORT_ERRORS:
            found.append(handler)
        if handler.type is None or caught & _CATCH_ALLS:
            break
    return found


def _canonical(aliases: dict, dotted: str) -> str:
    """`dotted` with its first name replaced by what the file imported it as."""
    head, dot, rest = dotted.partition(".")
    return aliases.get(head, head) + dot + rest


def _rebound(stmt) -> set[str]:
    """The names an assignment or import statement binds."""
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return _bound_names(stmt)
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [getattr(stmt, "target", None)]
    return {n.id for t in targets if t is not None for n in ast.walk(t)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}


# Loggers a degrading handler reports through, by the name of the object called.
_LOGGERS = frozenset({"logging", "logger", "log"})


def _reports(dotted: str) -> bool:
    """A warnings.warn or logging call: `logging.warning`, `logger.info`, `self.log.debug`."""
    return dotted == "warnings.warn" or any(part.lstrip("_").lower() in _LOGGERS
                                            for part in dotted.split(".")[:-1])


def _carries_on(handler: ast.ExceptHandler, guarded: set[str], aliases: dict) -> bool:
    """The handler visibly degrades, so the program runs on without the import.

    Only a last statement that says so counts: `pass`, `continue`, a falsy
    `return` (None, False, 0), a rebinding of a guarded name (`x = None`,
    `x = fallback`, a fallback import of the same name), or a warnings.warn or
    logging call. Anything else — a raise, an exit under any name, a helper
    that may exit, `return 1` from main(), argparse's error() — is not read as
    carrying on, and the import becomes a lead rather than nothing.
    """
    last = handler.body[-1]
    if isinstance(last, (ast.Pass, ast.Continue)):
        return True
    if isinstance(last, ast.Return):
        return last.value is None or (isinstance(last.value, ast.Constant) and not last.value.value)
    if isinstance(last, (ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom)):
        return bool(_rebound(last) & guarded)
    return (isinstance(last, ast.Expr) and isinstance(last.value, ast.Call)
            and _reports(_canonical(aliases, _dotted(last.value.func))))


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
    carries_on: bool        # some handler that catches the error visibly degrades
    fallbacks: frozenset    # names those handlers bind by importing a replacement
    shared: bool            # the body imports more than one module

    @classmethod
    def of(cls, node, handlers: list, aliases: dict) -> "_Guard":
        guarded = {name for n in _imports_in(node.body) for name in _bound_names(n)}
        carrying = [h for h in handlers if _carries_on(h, guarded, aliases)]
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


def _walk(tree: ast.AST, aliases: dict):
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
        handlers = _import_error_handlers(node) if isinstance(node, (ast.Try, ast.TryStar)) else []
        if handlers:
            inner = (*guards, _Guard.of(node, handlers, aliases))
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

    def __init__(self, filepath: Path, module: ast.Module, root: Path, aliases: dict):
        self.filepath, self.module, self.root, self.aliases = filepath, module, root, aliases
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
        if path is None:
            return self.decided_at_run_time(node, scopes, set())
        # A relative entry is relative to the working directory, and the two that
        # occur are the project root and the file's own directory (Sphinx runs
        # docs/conf.py from docs/). It is inside when either lands inside.
        anchors = [path] if path.is_absolute() else [PurePath(self.root, path), PurePath(self.filepath.parent, path)]
        return not any(PurePath(os.path.normpath(a)).is_relative_to(self.root) for a in anchors)

    def decided_at_run_time(self, node, scopes: tuple, seen: set) -> bool:
        """`node` reads a function parameter, the environment, sys.argv, or a name
        bound more than once — directly or through names bound once."""
        for child in ast.walk(node):
            dotted = _canonical(self.aliases, _dotted(child)) if isinstance(child, (ast.Name, ast.Attribute)) else ""
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
        name, args = _canonical(self.aliases, _dotted(node.func)), node.args
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
        """`node`'s parents[index]: .parent and os.path.dirname are index 0. A
        relative path stays relative (the parent of `.` is `..`), so where it
        lands is decided against the working directories it may run from."""
        base = self.read(node, scopes, depth)
        if base is None:
            return None
        if not base.is_absolute():
            return PurePath(os.path.normpath(base.joinpath(*[".."] * (index + 1))))
        return base.parents[index] if index < len(base.parents) else None


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


def import_sites(tree: ast.Module, filepath: Path, root: Path):
    """(top-level module, ImportSite) for every absolute import in one parsed file."""
    aliases = _import_aliases(tree)
    reader = _PathReader(filepath, tree, root, aliases)
    found, additions = [], []
    for node, guards, scopes in _walk(tree, aliases):
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
            yield top, ImportSite(str(filepath), node.lineno, guard, after)
