"""Execution graph and held-mutex traversal for detected direct writes.

The effect reader supplies lexical origins and actual visible invocation edges.
This reader follows those edges with the mutex state of the execution site;
deferred definitions contribute inputs, while consumed callbacks contribute bodies.
Finding policy and origin propagation remain in their respective modules.
"""

import ast

import global_state_scopes as scopes


_MUTATING_METHODS = {
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse",
    "add", "discard", "update", "setdefault", "popitem",
    "difference_update", "intersection_update", "symmetric_difference_update",
    "subtract", "move_to_end",
    "appendleft", "popleft", "extendleft", "rotate",
}

def _eager_children(node, contexts=None):
    """Definition inputs execute here; deferred bodies retain their own walk."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        children = scopes.definition_inputs(node)
    elif isinstance(node, ast.Lambda):
        children = list(node.args.defaults) + [value for value in node.args.kw_defaults if value is not None]
    elif isinstance(node, ast.GeneratorExp):
        children = [node.generators[0].iter]
    else:
        children = list(ast.iter_child_nodes(node))
    if isinstance(node, ast.Call):
        callables = {node.func} if isinstance(node.func, ast.Lambda) else {
            value for kind, value in (contexts or {}).get(node.func, ()) if kind == "callable"}
        for value in callables:
            if not scopes.callable_is_deferred(value):
                children.extend([value.body] if isinstance(value, ast.Lambda) else value.body)
    for kind, value in (contexts or {}).get(node, ()):
        if kind == "callback_body":
            children.extend([value.body] if isinstance(value, ast.Lambda) else value.body)
        if kind == "consume":
            children.append(value.elt)
            for index, generator in enumerate(value.generators):
                children.extend(generator.ifs)
                if index:
                    children.append(generator.iter)
    return children

def _eager_nodes(stmts, contexts):
    stack = list(stmts)
    seen = set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        yield node
        stack.extend(_eager_children(node, contexts))

def _module_name(value, names, contexts):
    return (isinstance(value, ast.Name) and value.id in names
            and ("global", value.id) in contexts.get(value, ()))

def mutated_names(node, names, contexts):
    if isinstance(node, ast.AnnAssign) and node.value is None:
        return set()
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr in _MUTATING_METHODS and _module_name(node.func.value, names, contexts):
            return {node.func.value.id}
    targets = (node.targets if isinstance(node, (ast.Assign, ast.Delete)) else
               [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else [])
    return {part.value.id for target in targets for part in ast.walk(target)
            if isinstance(part, ast.Subscript) and isinstance(part.ctx, (ast.Store, ast.Del))
            and _module_name(part.value, names, contexts)}

def _context_mutex(expression, locks, contexts):
    origins = {origin for origin in contexts.get(expression, ()) if origin[0] != "global"}
    if not origins or any(kind != "guard" or name not in locks for kind, name in origins):
        return None
    possible = {locks[name] for _, name in origins}
    return next(iter(possible)) if len(possible) == 1 else None

def blocking_acquire(node, locks, contexts):
    if not isinstance(node, ast.Call):
        return False
    kwargs = {keyword.arg: keyword.value for keyword in node.keywords}
    blocking = node.args[0] if node.args else kwargs.get("blocking")
    timeout = node.args[1] if len(node.args) > 1 else kwargs.get("timeout")
    if isinstance(blocking, ast.Constant) and blocking.value in (False, 0):
        return False
    if isinstance(timeout, ast.Constant) and isinstance(timeout.value, (int, float)) and timeout.value >= 0:
        return False
    held = {locks[name].identity for kind, name in contexts.get(node, ()) if kind == "guard" and name in locks}
    return any(locks[name].factory == "Lock" and locks[name].identity in held
               for kind, name in contexts.get(node.func, ()) if kind == "acquire" and name in locks)

def guarded_nodes(stmts, locks, contexts, invalid_acquires, skipped):
    """Same-scope nodes paired with the known module locks held at each node.

    `locks` names the module-level Lock/RLock objects. A nested function is
    its own scope and its own walk: it runs whenever it is called, which need
    not be while the lock that surrounds its definition is held.
    """
    stack = [(s, frozenset()) for s in stmts]
    seen = set()
    while stack:
        node, held = stack.pop()
        if node in skipped:
            continue
        if (node, held) in seen:
            continue
        seen.add((node, held))
        yield node, held if held is not None else frozenset()
        if isinstance(node, ast.With):
            # Context expressions are evaluated before their own __enter__,
            # in order: the first expression is not protected by a later lock.
            # A thread mutex cannot certify task exclusion across suspension;
            # Lock also cannot be acquired twice by the same thread. Keep an
            # invalid proof invalid through nested contexts, even a different
            # lock: the surrounding operation can already suspend or deadlock.
            entered = set(held or ())
            invalid = held is None
            for item in node.items:
                if entered and _suspends([item.context_expr], skipped):
                    invalid = True
                if any(part in invalid_acquires for part in _eager_nodes([item.context_expr], contexts)):
                    invalid = True
                for kind, name in contexts.get(item.context_expr, ()):
                    if kind == "guard" and name in locks and locks[name].identity in entered and locks[name].factory == "Lock":
                        invalid = True
                entered.update(locks[name].identity for kind, name in contexts.get(item.context_expr, ())
                               if kind == "guard" and name in locks)
            if entered and _suspends(node.body, skipped):
                invalid = True
            if any(part in invalid_acquires for part in _eager_nodes(node.body, contexts)):
                invalid = True
            if invalid:
                held = None
            children = []
            for item in node.items:
                children.append((item.context_expr, held))
                mutex = _context_mutex(item.context_expr, locks, contexts)
                if held is not None and mutex is not None:
                    held = held | {mutex.identity}
                if item.optional_vars:
                    children.append((item.optional_vars, held))
            children.extend((stmt, held) for stmt in node.body)
            stack.extend(reversed(children))
            continue
        for child in _eager_children(node, contexts):
            stack.append((child, held))

def _suspends(stmts, skipped):
    """Visible suspension in this execution scope, including implicit awaits."""
    scope = ast.Module(body=list(stmts), type_ignores=[])
    return any(isinstance(node, (ast.Await, ast.Yield, ast.YieldFrom, ast.AsyncFor, ast.AsyncWith))
               or (isinstance(node, ast.comprehension) and node.is_async)
               for node in scopes.module_scope_nodes(scope, include_class_bodies=True, skipped=skipped))
