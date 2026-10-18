"""Pure lexical inventories and definition-time AST boundaries.

Function closures retain enclosing function names and exclude class namespaces.
Postponed annotations and type aliases stay deferred; defaults/decorators execute.
The effect reader consumes these inventories without duplicating Python scope
rules, while finding policy stays in the detector.
"""

import ast
from collections import Counter

_TYPE_ALIAS = getattr(ast, "TypeAlias", ())


def deferred_expressions(tree):
    """Roots stored for later evaluation rather than run by their statement."""
    roots = {value for node in ast.walk(tree) if isinstance(node, _TYPE_ALIAS)
             for value in [node.value] + node.type_params}
    if not any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
               and any(alias.name == "annotations" for alias in node.names) for node in tree.body):
        return roots
    for node in ast.walk(tree):
        if isinstance(node, (ast.arg, ast.AnnAssign)) and node.annotation is not None:
            roots.add(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            roots.add(node.returns)
    return roots


def definition_inputs(node):
    if isinstance(node, ast.ClassDef):
        return node.decorator_list + node.bases + [keyword.value for keyword in node.keywords]
    args = node.args
    params = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    params += [arg for arg in (args.vararg, args.kwarg) if arg]
    return (getattr(node, "decorator_list", []) + list(args.defaults) + [value for value in args.kw_defaults if value is not None]
            + [arg.annotation for arg in params if arg.annotation is not None]
            + ([node.returns] if getattr(node, "returns", None) is not None else []))


def callable_bound_names(node):
    if not isinstance(node, ast.Lambda):
        return bound_names(node)
    args = node.args
    params = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    params += [arg for arg in (args.vararg, args.kwarg) if arg]
    names = {arg.arg for arg in params}
    names.update(item.id for item in module_scope_nodes(ast.Module(body=[node.body], type_ignores=[]))
                 if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store))
    return names


def callable_is_deferred(node):
    body = [node.body] if isinstance(node, ast.Lambda) else node.body
    return isinstance(node, ast.AsyncFunctionDef) or any(isinstance(item, (ast.Yield, ast.YieldFrom))
               for item in module_scope_nodes(ast.Module(body=body, type_ignores=[])))


def literal_index_values(value, index):
    """Selected literal elements, or all possible elements plus uncertainty.

    This inspects an expression's fresh literal, never a mutable container alias.
    Unknown keys/unpackings cannot erase any visible guard-bearing alternative.
    """
    if isinstance(value, ast.Subscript):
        selected = literal_index_values(value.value, value.slice)
        if selected is None:
            return None
        values, exact = [], selected[1]
        for member in selected[0]:
            nested = literal_index_values(member, index)
            if nested is None:
                values.append(member)
                exact = False
            else:
                values.extend(nested[0])
                exact &= nested[1]
        return values, exact
    if not isinstance(value, (ast.Tuple, ast.List, ast.Dict)):
        return None
    values = value.values if isinstance(value, ast.Dict) else value.elts
    try:
        key = ast.literal_eval(index)
        if isinstance(value, ast.Dict):
            pairs = [(ast.literal_eval(item), result) for item, result in zip(value.keys, values)]
            return [next(result for item, result in reversed(pairs) if item == key)], True
        if any(isinstance(item, ast.Starred) for item in values):
            return values, False
        return [values[key]], True
    except (ValueError, TypeError, IndexError, StopIteration):
        return values, False


def module_scope_nodes(tree, *, include_class_bodies=False, skipped=frozenset()):
    """Nodes evaluated in the module's scope, including definition inputs.

    Bodies of functions and classes have their own namespace. Defaults,
    decorators and bases still run here, where a walrus can replace a lock.
    """
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if node in skipped:
            continue
        yield node
        if isinstance(node, _TYPE_ALIAS):
            stack.append(node.name)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.extend(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                stack.extend(node.bases)
                stack.extend(k.value for k in node.keywords)
                if include_class_bodies:
                    # Class assignments bind a separate namespace, but an
                    # attribute store may still replace threading.Lock itself.
                    stack.extend(node.body)
            else:
                stack.extend(ast.iter_child_nodes(node.args))
                if node.returns:
                    stack.append(node.returns)
            continue
        if isinstance(node, ast.Lambda):
            stack.extend(node.args.defaults)
            stack.extend(v for v in node.args.kw_defaults if v is not None)
            continue
        if isinstance(node, ast.AnnAssign) and node.value is None:
            stack.append(node.annotation)   # a bare annotation assigns no value
            continue
        if isinstance(node, ast.comprehension):
            stack.extend([node.iter] + list(node.ifs))
            continue
        stack.extend(ast.iter_child_nodes(node))


def module_bindings(tree) -> Counter:
    """Count bindings and deletes of module names, excluding same-named locals."""
    bound = Counter()
    for node in module_scope_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound[node.name] += 1
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound[node.id] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names if a.name != "*")
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound[node.name] += 1
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound[node.name] += 1
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound[node.rest] += 1
    return bound


def bound_names(fn) -> set:
    """Names a function binds in its own scope — its locals, as Python decides
    them: parameters, assignment and loop targets, `as` names, imports, and the
    defs and classes it creates. `global` names are the module's and are left
    out; `nonlocal` names are an enclosing function's, so they are kept (they
    are not the module's either)."""
    args = fn.args
    params = list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs)
    params += [a for a in (args.vararg, args.kwarg) if a is not None]
    bound = {a.arg for a in params}
    declared_global = set()
    stack = [s for s in fn.body]
    while stack:
        node = stack.pop()
        if isinstance(node, _TYPE_ALIAS):
            stack.append(node.name)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)     # the name is local; the body is another scope
            stack.extend(definition_inputs(node))
            continue
        if isinstance(node, ast.Lambda):
            stack.extend(definition_inputs(node))
            continue
        if isinstance(node, ast.Global):
            declared_global.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names if a.name != "*")
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        if isinstance(node, ast.comprehension):
            # A comprehension's loop variable lives in the comprehension's own
            # scope; only what it iterates (and a walrus in it) reaches ours.
            stack.extend([node.iter] + list(node.ifs))
            continue
        stack.extend(ast.iter_child_nodes(node))
    return bound - declared_global


def function_scopes(tree):
    """Every function, with the names enclosing *functions* bind (a closure
    sees those, not the module's). Class bodies bind nothing a method sees."""
    def visit(node, enclosing):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child, enclosing
                yield from visit(child, enclosing | bound_names(child))
            else:
                yield from visit(child, enclosing)
    yield from visit(tree, frozenset())


def independent_function_scopes(tree):
    """Entry points whose bodies need a walk independent of visible calls.

    A nested synchronous helper can use only its real invocation edges when
    its single local binding has exclusively direct calls in the owning body.
    Aliases, returned values, decorators and references in another deferred
    body retain the independent-entry walk: their callers are not established.
    """
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for fn, enclosing in function_scopes(tree):
        owner = parents[fn]
        while not isinstance(owner, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents[owner]
        if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) and not fn.decorator_list and not callable_is_deferred(fn):
            body = ast.Module(body=owner.body, type_ignores=[])
            deferred = {node for node in ast.walk(body) if isinstance(node, ast.GeneratorExp)}
            immediate = set(module_scope_nodes(body, skipped=deferred))
            references = [node for node in ast.walk(body) if isinstance(node, ast.Name)
                          and node.id == fn.name and isinstance(node.ctx, ast.Load)]
            external_binding = any(isinstance(node, (ast.Global, ast.Nonlocal)) and fn.name in node.names
                                   for node in immediate)
            if (references and module_bindings(body)[fn.name] == 1 and not external_binding
                    and all(node in immediate and isinstance(parents[node], ast.Call)
                            and parents[node].func is node for node in references)):
                continue
        yield fn, enclosing
