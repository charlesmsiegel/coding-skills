"""Stable imported mutex roots and constructor/import capture inventory.

One source inventory preserves canonical object identities and original import
positions through stable aliases. The supplied execution reader reports visible
interference; this inventory validates roots against those effects. Lexical call
binding, execution edges and finding policy remain in their existing readers.
"""

import ast
from dataclasses import dataclass

import global_state_scopes as scopes


LOCK_FACTORIES = {"Lock", "RLock"}


@dataclass(frozen=True)
class _Mutex:
    factory: str
    identity: tuple[int, int]


@dataclass(frozen=True)
class _Factory:
    name: str
    # A direct import captures the attribute's current object. Replacing that
    # module attribute later does not change the saved factory (or its aliases).
    imported_at: tuple[int, int] | None = None
    # sys.modules replacement affects subsequent imports, not a module/factory
    # object captured by an earlier import and copied to another stable alias.
    module_at: tuple[int, int] | None = None


def _module_names(stmt):
    """Direct assignment/import targets understood by the module identity proof."""
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return [(alias.asname or alias.name).split(".")[0] for alias in stmt.names]
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
    return [target.id for target in targets if isinstance(target, ast.Name)]


def _module_aliases(tree, known, stable):
    """Propagate direct, stable module aliases without conflating spellings.

    A chain assignment creates one object, and a later `alias = name` retains
    that identity. Only propagate a binding after its statement executes: a
    wildcard may supply an earlier object with the same spelling. Other
    assignment shapes remain outside this bounded proof.
    """
    visible = {}
    for stmt in tree.body:
        value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
        names = _module_names(stmt)
        if isinstance(value, ast.Name) and value.id in visible:
            visible.update((name, visible[value.id]) for name in names if name in stable)
        visible.update((name, known[name]) for name in names if name in known)
    return visible


def _argument_free_call(node):
    """Only explicit empty unpackings are also provably argument-free."""
    return (all(isinstance(arg, ast.Starred) and isinstance(arg.value, (ast.List, ast.Tuple)) and not arg.value.elts
                for arg in node.args)
            and all(keyword.arg is None and isinstance(keyword.value, ast.Dict) and not keyword.value.keys
                    for keyword in node.keywords))


def module_locks(tree, contexts, skipped, builtin_names, read_interference) -> dict:
    """Stable names bound once to an imported threading Lock/RLock constructor.

    A global declaration anywhere keeps a name out of this bounded proof: it
    permits a later call to replace the object the name originally identified.
    """
    # An unknown export can replace a factory alias, the threading module or
    # an already-created guard. Without resolving that surface none is stable.
    last_wildcard = max(((node.lineno, node.col_offset) for node in scopes.module_scope_nodes(tree)
                         if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)), default=(0, 0))
    bindings = scopes.module_bindings(tree)
    global_names = {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}
    stable = {name for name, count in bindings.items() if count == 1} - global_names
    threading_modules, lock_factories, builtin_origins = {}, {}, {}
    for stmt in tree.body:
        if (stmt.lineno, stmt.col_offset) <= last_wildcard:
            continue
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == "threading":
                    threading_modules[alias.asname or "threading"] = (stmt.lineno, stmt.col_offset)
                elif alias.name in {"builtins", "sys"}:
                    builtin_origins[alias.asname or alias.name] = frozenset({("module", alias.name)})
        elif isinstance(stmt, ast.ImportFrom) and stmt.module == "threading" and stmt.level == 0:
            for alias in stmt.names:
                if alias.name in LOCK_FACTORIES:
                    position = (stmt.lineno, stmt.col_offset)
                    lock_factories[alias.asname or alias.name] = _Factory(alias.name, position, position)
        elif isinstance(stmt, ast.ImportFrom) and stmt.module == "builtins" and stmt.level == 0:
            for alias in stmt.names:
                if alias.name in builtin_names:
                    builtin_origins[alias.asname or alias.name] = frozenset({("builtin", alias.name)})
        elif isinstance(stmt, ast.ImportFrom) and stmt.module == "sys" and stmt.level == 0:
            for alias in stmt.names:
                if alias.name == "modules":
                    builtin_origins[alias.asname or alias.name] = frozenset({("namespace", "imports")})
    threading_modules = {name: position for name, position in threading_modules.items() if name in stable}
    lock_factories = {name: factory for name, factory in lock_factories.items() if name in stable}
    threading_modules = _module_aliases(tree, threading_modules, stable)
    lock_factories = _module_aliases(tree, lock_factories, stable)
    builtin_origins = _module_aliases(tree, {name: origin for name, origin in builtin_origins.items() if name in stable}, stable)
    bound_at = {name: (stmt.lineno, stmt.col_offset) for stmt in tree.body for name in _module_names(stmt)}
    locks = {}
    for stmt in tree.body:
        if (stmt.lineno, stmt.col_offset) <= last_wildcard:
            continue
        value = stmt.value if isinstance(stmt, (ast.Assign, ast.AnnAssign)) else None
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        position = (stmt.lineno, stmt.col_offset)
        is_lock = (
            (isinstance(func, ast.Name) and func.id in lock_factories and bound_at[func.id] < position)
            or (isinstance(func, ast.Attribute) and func.attr in LOCK_FACTORIES
                and isinstance(func.value, ast.Name) and func.value.id in threading_modules
                and bound_at[func.value.id] < position)
        )
        if not is_lock:
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        origin = (_Factory(func.attr, module_at=threading_modules[func.value.id])
                  if isinstance(func, ast.Attribute) else lock_factories[func.id])
        if origin.name == "Lock" and not _argument_free_call(value):
            continue
        mutex = _Mutex(origin.name, (value.lineno, value.col_offset))
        locks.update((t.id, (origin, mutex)) for t in targets if isinstance(t, ast.Name) and t.id in stable)
    locks = _module_aliases(tree, locks, stable)
    replaced, released = read_interference(tree, threading_modules, set(locks), contexts, builtin_origins, skipped)
    relevant = set(locks) | set(threading_modules) | set(lock_factories)
    if any(effect == "@global:*" or effect.removeprefix("@global:") in relevant
           for effect, _position in replaced if effect.startswith("@global:")):
        return {}
    released_identities = {locks[name][1].identity for name in released}
    return {name: mutex for name, (origin, mutex) in locks.items()
            if mutex.identity not in released_identities
            and not any(factory == "@threading-import" and (origin.module_at is None or position < origin.module_at)
                        for factory, position in replaced)
            and not any(factory == origin.name and (origin.imported_at is None or position < origin.imported_at)
                        for factory, position in replaced)}
