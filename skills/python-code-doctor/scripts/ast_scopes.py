"""
Scope-aware AST helpers for find_design_smells.py.

A smell about a function is a smell about its own scope: a name bound in a
nested def, class or lambda is a different variable, and a flag set in an inner
loop cannot be replaced by a `break` of the outer one. These walks and
classifiers keep that boundary in one place.
"""

import ast
from typing import Iterator


def is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def walk_same_scope(stmts: list[ast.stmt]) -> Iterator[ast.AST]:
    """Walk statements without descending into nested scopes (defs, classes,
    lambdas) — a name assigned there is a different variable."""
    stack: list[ast.AST] = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def walk_loop_level(stmts: list[ast.stmt]) -> Iterator[ast.AST]:
    """Walk statements without descending into nested scopes OR nested loops —
    a flag assigned inside an inner loop cannot be replaced by a `break` of
    the outer one."""
    stack: list[ast.AST] = list(stmts)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                 ast.Lambda, ast.While, ast.For, ast.AsyncFor)):
            stack.extend(ast.iter_child_nodes(node))


def strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def body_kind(body: list[ast.stmt]) -> str | None:
    """Classify a trivial body: 'noop' (pass/.../docstring only), 'raise_nie', or None."""
    rest = strip_docstring(body)
    if not rest or all(
        isinstance(s, ast.Pass)
        or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is Ellipsis)
        for s in rest
    ):
        return "noop"
    if len(rest) == 1 and isinstance(rest[0], ast.Raise):
        exc = rest[0].exc
        if isinstance(exc, ast.Call):
            exc = exc.func
        if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
            return "raise_nie"
    return None


def local_bindings(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[frozenset[str], frozenset[str]]:
    """(shadows, local_imports) for the function's own scope. Shadows are names
    bound by parameters/assignments/loop targets — they hide a module-level
    name; local imports are module-internal receivers within this scope only."""
    a = node.args
    shadows = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    if a.vararg:
        shadows.add(a.vararg.arg)
    if a.kwarg:
        shadows.add(a.kwarg.arg)
    imports = set()
    for inner in walk_same_scope(node.body):
        if isinstance(inner, (ast.Import, ast.ImportFrom)):
            for alias in inner.names:
                imports.add(alias.asname or alias.name.split(".")[0])
            continue
        if isinstance(inner, ast.Assign):
            targets = inner.targets
        elif isinstance(inner, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = [inner.target]
        elif isinstance(inner, (ast.For, ast.AsyncFor)):
            targets = [inner.target]
        elif isinstance(inner, ast.withitem) and inner.optional_vars is not None:
            targets = [inner.optional_vars]
        else:
            continue
        for target in targets:
            for name_node in ast.walk(target):
                if isinstance(name_node, ast.Name):
                    shadows.add(name_node.id)
    return frozenset(shadows), frozenset(imports)


def is_dotted_name(expr: ast.expr) -> bool:
    """True for attribute chains rooted at a bare name (Kind.CREATE, mod.Kind.CREATE)."""
    while isinstance(expr, ast.Attribute):
        expr = expr.value
    return isinstance(expr, ast.Name)


def decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> set[str]:
    names = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names
