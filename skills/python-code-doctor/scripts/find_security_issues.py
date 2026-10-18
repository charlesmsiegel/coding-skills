#!/usr/bin/env python3
"""
Detect risky security constructs in Python code via AST analysis.

These are patterns a cleanup pass should never leave in production code.
Conservative — only flags clear, unambiguous cases.

Finds:
  - eval_exec              : calls to builtin eval() or exec()
  - shell_injection        : subprocess.* with shell=True, os.system, os.popen
  - unsafe_yaml            : yaml.load() with no Loader= keyword argument
  - unsafe_deserialization : pickle.load/loads, marshal.load/loads, dynamic __import__
  - literal_import         : a literal __import__ dependency hidden from normal imports
  - weak_hash              : hashlib.md5() or hashlib.sha1()
  - tls_verify_disabled    : call with verify=False, or ssl._create_unverified_context
  - hardcoded_secret       : assignment of a string literal to a name that looks
                             like a credential/secret
  - sql_injection          : DB execute/executemany/etc. called with a dynamically
                             built SQL string (f-string, %-format, +concat, .format)
                             — unless every interpolated piece is provably built
                             from string literals in the source (see
                             _LiteralProof): `",".join("?" * len(ids))` is
                             placeholder plumbing, not injection
  - command_injection      : subprocess/os.system/os.popen called with a dynamically
                             built command string (shell=True makes it worse)
"""

import ast
import json
import argparse
import re
import string
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import (sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files,
                    looks_like_test, warn_detector_error, warn_unparseable)


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""
    # A scored defect by default. Set to "candidate" for a lead the downstream
    # merge must NOT score — a candidate carries no fix and names, in
    # also_caused_by, the ways a healthy codebase produces the same
    # observation. Used for PRAGMA interpolation and quoted constant SQL
    # identifiers: identifiers cannot be passed as bound query parameters,
    # and interpolation alone does not establish injection in these cases.
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


# Names whose assignment to a plain string literal suggests a hardcoded secret
_SECRET_NAMES = {
    "password", "passwd", "pwd", "secret", "api_key", "apikey",
    "access_key", "secret_key", "token", "auth_token", "private_key",
}

# Values that are obviously placeholders — skip these
_PLACEHOLDER_VALUES = {
    "", "changeme", "your_password_here", "xxx", "todo", "none", "null", "example",
}


def _get_line(lines, lineno):
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


def _has_keyword(call_node, kwname):
    """Return True if the call has a keyword argument with the given name."""
    return any(kw.arg == kwname for kw in call_node.keywords)


def _keyword_value(call_node, kwname):
    """Return the AST node for keyword argument kwname, or None."""
    for kw in call_node.keywords:
        if kw.arg == kwname:
            return kw.value
    return None


def _is_false_literal(node):
    return isinstance(node, ast.Constant) and node.value is False


def _is_placeholder_secret(value: str) -> bool:
    """Return True if the string looks like a placeholder rather than a real secret."""
    low = value.strip().lower()
    if low in _PLACEHOLDER_VALUES:
        return True
    # All-uppercase env-var style names like "MY_SECRET_KEY" — likely a reference/default
    stripped = value.strip()
    if stripped.isupper() and "_" in stripped:
        return True
    return False


def _target_looks_like_secret(node) -> bool:
    """Return True if an assignment target Name/Attribute looks like a credential."""
    if isinstance(node, ast.Name):
        return node.id.lower() in _SECRET_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr.lower() in _SECRET_NAMES
    return False


def _is_dynamic_string(node) -> bool:
    """Return True if `node` is a string that is built dynamically at runtime.

    Covers:
      - f-strings (ast.JoinedStr with at least one FormattedValue)
      - %-format BinOp whose left side is a str Constant (or whose operator is Mod)
      - +-concatenation BinOp that involves a str Constant on either side
      - .format() call on a str literal or an arbitrary expression
    """
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in node.values)

    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Mod):
            # "..." % x  or  x % "..."  — treat left-str case as SQL-like interpolation
            return (isinstance(node.left, ast.Constant)
                    and isinstance(node.left.value, str))
        if isinstance(node.op, ast.Add):
            return (
                (isinstance(node.left, ast.Constant) and isinstance(node.left.value, str))
                or (isinstance(node.right, ast.Constant) and isinstance(node.right.value, str))
            )

    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "format":
            # "...".format(x)  or  expr.format(x)
            return True

    return False


def _leading_sql_literal(node) -> str | None:
    """The static leading text of a dynamically built SQL string, if any.

    For each shape ``_is_dynamic_string`` recognizes, return the literal prefix
    the interpolation is appended to. The leading SQL keyword is fixed even when
    a later value is interpolated, which is how a PRAGMA is told from a SELECT.
    """
    if isinstance(node, ast.JoinedStr):
        first = node.values[0] if node.values else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return None
    if isinstance(node, ast.BinOp):
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            return node.left.value
        if isinstance(node.left, ast.BinOp):
            # 'CREATE TABLE "' + name + '" (...)' nests to the left.
            return _leading_sql_literal(node.left)
        return None
    if isinstance(node, ast.Call):
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "format"
                and isinstance(func.value, ast.Constant)
                and isinstance(func.value.value, str)):
            return func.value.value
    return None


# Statements that take no bound parameters where the interpolation goes: a
# table, view, trigger or column name in DDL, a PRAGMA's argument, a DEFAULT.
_UNBINDABLE_KEYWORDS = frozenset({"create", "drop", "alter", "pragma"})
_LEADING_WORD = re.compile(r"\s*([A-Za-z]+)")


def _unbindable_keyword(node) -> str | None:
    """The leading DDL/PRAGMA keyword of the executed SQL text, upper-cased.

    SQLite (like most engines) accepts no bound parameter for an identifier —
    `DROP TABLE ?` is not a statement it will run — so interpolation is the only
    way to write DDL against a dynamic name. Flagging that as injection is a
    false positive when the name is not attacker-controlled, so the caller
    downgrades it to an unscored lead rather than a scored finding.
    """
    prefix = _leading_sql_literal(node)
    match = _LEADING_WORD.match(prefix or "")
    if match and match.group(1).lower() in _UNBINDABLE_KEYWORDS:
        if match.group(1).lower() == "create":
            parts = _sql_parts(node)
            text = "".join(piece if kind == "text" else "\0" for kind, piece in parts or [])
            # CREATE data queries can bind values, including AS WITH, commented
            # SELECT and AS VALUES forms. Keep these visible without trying to
            # parse a complete SQL grammar from the leading keyword.
            if re.search(r"\b(?:SELECT|VALUES)\b", text, re.IGNORECASE):
                return None
        return match.group(1).upper()
    return None


def _sql_parts(node):
    """The SQL as an ordered list of ("text", str) and ("value", expr) parts.

    None for a shape whose positions cannot be read (a template that is not a
    literal, a %-format whose specifiers do not line up with its values).
    """
    if isinstance(node, ast.JoinedStr):
        parts = []
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append(("text", piece.value))
            elif isinstance(piece, ast.FormattedValue):
                parts.append(("value", piece.value))
            else:
                return None
        return parts
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _sql_parts(node.left), _sql_parts(node.right)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        if not (isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)):
            return None
        values = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
        texts = re.split(r"%[-#0 +]*\d*(?:\.\d+)?[sdirfgx]", node.left.value.replace("%%", "\0"))
        if len(texts) != len(values) + 1:
            return None
        parts = [("text", texts[0])]
        for value, text in zip(values, texts[1:]):
            parts += [("value", value), ("text", text)]
        return parts
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        template = node.func.value
        if not (isinstance(template, ast.Constant) and isinstance(template.value, str)):
            return None
        try:
            fields = list(string.Formatter().parse(template.value))
        except ValueError:
            return None
        # Which argument fills which field is not tracked; every field stands
        # for "some argument", and every argument must be an identifier.
        arguments = list(node.args) + [kw.value for kw in node.keywords]
        if not arguments:
            return None
        parts = []
        for text, field, _spec, _conversion in fields:
            parts.append(("text", text))
            if field is not None:
                parts.append(("value", arguments[0] if len(arguments) == 1 else ast.Tuple(elts=arguments)))
        return parts
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [("text", node.value)]
    return [("value", node)]


# --------------------------------------------------------------------------- #
# Literal-derived strings: SQL whose every piece the source spells out
# --------------------------------------------------------------------------- #

_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_TYPE_ALIAS = getattr(ast, "TypeAlias", None)  # `type X = ...`, Python 3.12+

# A binding whose value this check cannot see: a parameter, an import, a
# `with ... as`, a `global` — anything a caller or another module controls.
_OPAQUE = object()


def _is_str_constant(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))


def _binds(target, name: str) -> bool:
    """True when assigning to ``target`` binds ``name`` (not merely reads it)."""
    return any(isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store)
               for n in ast.walk(target))


def _alternatives(value) -> list:
    """The expressions ``value`` may evaluate to, one per branch of a conditional."""
    if isinstance(value, ast.IfExp):
        return _alternatives(value.body) + _alternatives(value.orelse)
    return [value]


def _has_starred(elts) -> bool:
    return any(isinstance(e, ast.Starred) for e in elts)


def _own_scope_nodes(body):
    """Every node of one scope: nested defs, classes and lambdas contribute their
    names (and decorators, defaults) but not their bodies, which are scopes of
    their own. Comprehensions are entered, since a walrus there binds here."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.Lambda):
            stack.extend(c for c in ast.iter_child_nodes(node) if c is not node.body)
        elif isinstance(node, _NESTED_SCOPES):
            stack.extend(c for c in ast.iter_child_nodes(node) if c not in node.body)
        else:
            stack.extend(ast.iter_child_nodes(node))


class _LiteralProof:
    """Decides whether a string expression is assembled only from literals.

    Conservative and local. An expression is literal-derived when it is a
    constant; an f-string, ``+`` concatenation, ``%`` or ``.format()`` of
    literal-derived parts; a conditional whose branches both are;
    ``<literal>.join(...)`` over a literal-derived string or over elements that
    are; ``<str literal> * <anything>``; or a name. A name counts only when the
    scope it resolves to binds it exclusively to literal-derived values — every
    assignment, tuple-unpack position (from a literal tuple, or a conditional of
    literal tuples), ``+=``, walrus, and ``for``/comprehension target over a
    literal tuple or list. A module-level name must be bound exactly once and
    never declared ``global``. Anything else — a parameter, a call, an
    attribute, a subscript — is unproven, and the statement stays a finding.
    """

    def __init__(self, tree) -> None:
        self.tree = tree
        self._parents = None
        self._globals = None

    def _parent(self, node):
        if self._parents is None:
            self._parents = {id(child): parent for parent in ast.walk(self.tree)
                             for child in ast.iter_child_nodes(parent)}
        return self._parents.get(id(node))

    def _declared_global(self, name: str) -> bool:
        if self._globals is None:
            self._globals = {n for node in ast.walk(self.tree) if isinstance(node, ast.Global)
                             for n in node.names}
        return name in self._globals

    # -- what a binding construct assigns -------------------------------------
    def _target_values(self, target, value, name):
        """Values ``name`` takes when ``value`` is assigned to ``target``, or None
        when the target does not bind it."""
        if not _binds(target, name):
            return None
        if isinstance(target, ast.Name):
            return [value]
        if isinstance(target, (ast.Tuple, ast.List)) and not _has_starred(target.elts):
            values = []
            for option in _alternatives(value):
                if (not isinstance(option, (ast.Tuple, ast.List)) or _has_starred(option.elts)
                        or len(option.elts) != len(target.elts)):
                    return [_OPAQUE]
                for sub_target, sub_value in zip(target.elts, option.elts):
                    values.extend(self._target_values(sub_target, sub_value, name) or ())
            return values
        return [_OPAQUE]

    def _iteration_values(self, target, iterable, name):
        """Values ``name`` takes as ``target`` walks ``iterable``, or None."""
        if not _binds(target, name):
            return None
        if isinstance(iterable, (ast.Tuple, ast.List, ast.Set)) and not _has_starred(iterable.elts):
            values = []
            for element in iterable.elts:
                values.extend(self._target_values(target, element, name))
            return values
        return [_OPAQUE]

    def _binding_values(self, node, name):
        """Values one node binds ``name`` to (None when it does not bind it)."""
        if isinstance(node, ast.Assign):
            found = [self._target_values(t, node.value, name) for t in node.targets]
            if all(f is None for f in found):
                return None
            return [v for f in found if f for v in f]
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return [node.value] if node.value is not None else []
            return None
        if isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return [node]
            return None
        if isinstance(node, (ast.For, ast.AsyncFor)):
            return self._iteration_values(node.target, node.iter, name)
        if isinstance(node, ast.NamedExpr):
            return [node.value] if node.target.id == name else None
        if isinstance(node, (ast.With, ast.AsyncWith)):
            bound = any(item.optional_vars is not None and _binds(item.optional_vars, name)
                        for item in node.items)
            return [_OPAQUE] if bound else None
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound = any(alias.name == "*" or (alias.asname or alias.name.split(".")[0]) == name
                        for alias in node.names)
            return [_OPAQUE] if bound else None
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return [_OPAQUE] if name in node.names else None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            return [_OPAQUE] if node.name == name else None
        if isinstance(node, ast.MatchMapping):
            return [_OPAQUE] if node.rest == name else None
        if _TYPE_ALIAS is not None and isinstance(node, _TYPE_ALIAS):
            return [_OPAQUE] if node.name.id == name else None
        return None

    def _scope_values(self, scope, name):
        """Every value ``scope`` binds ``name`` to, or None when it never binds it."""
        bound, values = False, []
        if isinstance(scope, _COMPREHENSIONS):
            for generator in scope.generators:
                found = self._iteration_values(generator.target, generator.iter, name)
                if found is not None:
                    bound = True
                    values.extend(found)
            return values if bound else None
        if isinstance(scope, _FUNCTION_SCOPES):
            arguments = scope.args
            params = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                      arguments.vararg, arguments.kwarg]
            if any(p is not None and p.arg == name for p in params):
                bound = True
                values.append(_OPAQUE)
            # A nested function's `nonlocal name` can rebind this scope's variable.
            if any(isinstance(n, ast.Nonlocal) and name in n.names for n in ast.walk(scope)):
                bound = True
                values.append(_OPAQUE)
        body = [scope.body] if isinstance(scope, ast.Lambda) else scope.body
        for node in _own_scope_nodes(body):
            found = self._binding_values(node, name)
            if found is not None:
                bound = True
                values.extend(found)
        return values if bound else None

    def _resolve(self, name_node):
        """``(scope, values)`` for the scope ``name_node`` resolves in, or None
        when nothing in this file binds it (a builtin, or a star import's)."""
        name = name_node.id
        child, node = name_node, self._parent(name_node)
        from_iterable, in_function = False, False
        while node is not None:
            if isinstance(node, ast.comprehension):
                from_iterable = child is node.iter
            elif isinstance(node, _COMPREHENSIONS):
                # The first iterable is evaluated in the enclosing scope.
                if not (from_iterable and child is node.generators[0]):
                    values = self._scope_values(node, name)
                    if values is not None:
                        return node, values
                from_iterable = False
            elif isinstance(node, _FUNCTION_SCOPES):
                in_body = (child is node.body if isinstance(node, ast.Lambda)
                           else any(child is stmt for stmt in node.body))
                if in_body:  # decorators and defaults run in the enclosing scope
                    in_function = True
                    values = self._scope_values(node, name)
                    if values is not None:
                        return node, values
            elif isinstance(node, ast.ClassDef):
                # Class-body names are not modelled; a method never sees them.
                if not in_function and self._scope_values(node, name) is not None:
                    return node, [_OPAQUE]
            elif isinstance(node, ast.Module):
                values = self._scope_values(node, name)
                if values is None:
                    return None
                if len(values) != 1 or self._declared_global(name):
                    return node, [_OPAQUE]
                return node, values
            child, node = node, self._parent(node)
        return None

    # -- the judgement --------------------------------------------------------
    def proves(self, node) -> bool:
        """``literal(node)``, with a chain too deep to follow counted as unproven."""
        try:
            return self.literal(node)
        except RecursionError:
            return False

    def literal(self, node, assuming: frozenset = frozenset()) -> bool:
        """True when ``node`` can only evaluate to text spelled out in the source."""
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.JoinedStr):
            return all(self.literal(v, assuming) for v in node.values)
        if isinstance(node, ast.FormattedValue):
            return (self.literal(node.value, assuming)
                    and (node.format_spec is None or self.literal(node.format_spec, assuming)))
        if isinstance(node, ast.IfExp):
            return self.literal(node.body, assuming) and self.literal(node.orelse, assuming)
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Add):
                return self.literal(node.left, assuming) and self.literal(node.right, assuming)
            if isinstance(node.op, ast.Mult):  # "?" * n repeats the literal
                return _is_str_constant(node.left) or _is_str_constant(node.right)
            if isinstance(node.op, ast.Mod):
                args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
                return self.literal(node.left, assuming) and all(self.literal(a, assuming) for a in args)
            return False
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if func.attr == "join" and len(node.args) == 1 and not node.keywords:
                return self.literal(func.value, assuming) and self._literal_items(node.args[0], assuming)
            if func.attr == "format":
                return (self.literal(func.value, assuming)
                        and all(self.literal(a, assuming) for a in node.args)
                        and all(kw.arg is not None and self.literal(kw.value, assuming)
                                for kw in node.keywords))
            return False
        if isinstance(node, ast.Name):
            resolved = self._resolve(node)
            if resolved is None:
                return False
            scope, values = resolved
            key = (id(scope), node.id)
            if key in assuming:  # a self-reference adds no new source of text
                return True
            assuming = assuming | {key}
            return all(self._literal_binding(v, assuming) for v in values)
        return False

    def _literal_items(self, node, assuming) -> bool:
        """What ``<sep>.join(node)`` joins is literal-derived."""
        if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            return self.literal(node.elt, assuming)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return all(self.literal(e, assuming) for e in node.elts)
        return self.literal(node, assuming)  # the characters of a literal-derived string

    def _literal_binding(self, value, assuming) -> bool:
        if value is _OPAQUE:
            return False
        if isinstance(value, ast.AugAssign):  # name += <literal-derived>
            return isinstance(value.op, ast.Add) and self.literal(value.value, assuming)
        return self.literal(value, assuming)
def _quoted_constant_sql_checker(tree):
    """Recognize constant identifier quoting, without treating arbitrary calls as safe.

    This is deliberately bounded: unique lexical bindings, including tuple
    unpacking, are followed; additional binding writes remain findings. Quoting is
    evidence for a candidate, never proof that a query is safe.
    """
    scopes = {}
    bindings = defaultdict(lambda: defaultdict(list))
    assigned_targets = set()

    def bind(target, value, scope):
        if isinstance(target, ast.Name):
            assigned_targets.add(target)
            bindings[scope][target.id].append(value)
        elif (isinstance(target, (ast.Tuple, ast.List))
              and isinstance(value, (ast.Tuple, ast.List))
              and len(target.elts) == len(value.elts)):
            for left, right in zip(target.elts, value.elts):
                bind(left, right, scope)

    def visit(node, scope):
        scopes[node] = scope
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            if hasattr(node, "name"):
                bindings[scope][node.name].append(None)
            scope = node
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if node not in assigned_targets:
                bindings[scope][node.id].append(None)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or (alias.name.split(".")[0]
                                        if isinstance(node, ast.Import) else alias.name)
                bindings[scope][name].append(None)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bindings[scope][node.name].append(None)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value, scope)
        elif isinstance(node, ast.AnnAssign):
            bind(node.target, node.value, scope)
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, tree)

    def resolve(node, seen=frozenset()):
        if not isinstance(node, ast.Name) or node in seen:
            return node
        scope = scopes[node]
        values = bindings[scope].get(node.id)
        if values is None and scope is not tree:
            # A function parameter shadows module constants.
            args = getattr(scope, "args", None)
            if args and any(arg.arg == node.id for arg in ast.walk(args)
                            if isinstance(arg, ast.arg)):
                return node
            values = bindings[tree].get(node.id)
        if not values or len(values) != 1 or values[0] is None:
            return node
        return resolve(values[0], seen | {node})

    def quoted_constant(node):
        node = resolve(node)
        if not isinstance(node, ast.Call) or len(node.args) != 1 or node.keywords:
            return False
        func = resolve(node.func)
        value = resolve(node.args[0])
        return (isinstance(func, ast.Attribute) and func.attr == "quote_name"
                and isinstance(func.value, ast.Attribute) and func.value.attr == "ops"
                and isinstance(value, ast.Constant) and isinstance(value.value, str))

    def check(node):
        if isinstance(node, ast.JoinedStr):
            values = [v.value for v in node.values if isinstance(v, ast.FormattedValue)]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            values = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            values = [v for v in (node.left, node.right)
                      if not (isinstance(v, ast.Constant) and isinstance(v.value, str))]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            values = [*node.args, *(kw.value for kw in node.keywords)]
        else:
            return False
        return bool(values) and all(quoted_constant(value) for value in values)

    return check


_SQL_EXECUTE_ATTRS = {"execute", "executemany", "executescript", "raw", "mogrify"}

_SUBPROCESS_FUNCS = {"run", "call", "check_call", "check_output", "Popen"}


def _literal_import_arguments(call: ast.Call) -> bool:
    """Only source literals justify changing an import's security classification."""
    if not call.args and not any(keyword.arg == 'name' for keyword in call.keywords):
        return False
    if any(keyword.arg is None for keyword in call.keywords):
        return False
    try:
        for argument in [*call.args, *(keyword.value for keyword in call.keywords)]:
            ast.literal_eval(argument)
    except (ValueError, TypeError):
        return False
    return True


def detect(tree, filename, lines, ignore):
    issues = []
    proof = _LiteralProof(tree)
    quoted_constant_sql = _quoted_constant_sql_checker(tree)

    def add(line, st, desc, sug, sev, kind="finding", also_caused_by=()):
        if st in ignore:
            return
        issues.append(CodeSmell(filename, line, st, desc, sug, sev, _get_line(lines, line),
                                kind, tuple(also_caused_by)))

    in_test = looks_like_test(filename)

    for node in ast.walk(tree):

        # ------------------------------------------------------------------ #
        # eval_exec — calls to builtin eval() or exec()
        # ------------------------------------------------------------------ #
        if isinstance(node, ast.Call):
            func = node.func

            if isinstance(func, ast.Name) and func.id in ("eval", "exec"):
                add(node.lineno, "eval_exec",
                    f"Call to builtin '{func.id}()' executes arbitrary Python code",
                    "Avoid dynamic code execution. Use ast.literal_eval() for safe literal "
                    "parsing, or replace with explicit dispatch logic.",
                    "high")

            # ---------------------------------------------------------------- #
            # sql_injection — execute/executemany/etc. with dynamic SQL string
            # ---------------------------------------------------------------- #
            elif (isinstance(func, ast.Attribute)
                  and func.attr in _SQL_EXECUTE_ATTRS
                  and node.args
                  and _is_dynamic_string(node.args[0])):
                if proof.proves(node.args[0]):
                    continue
                keyword = _unbindable_keyword(node.args[0])
                identifiers = not keyword and quoted_constant_sql(node.args[0])
                reasons = []
                if keyword:
                    # DDL and PRAGMA take no bound parameters, so a dynamic
                    # name there must be interpolated.
                    reasons += [
                        f"a schema identifier in {keyword} cannot be supplied as a bound "
                        "parameter; confirm that the interpolated value occupies that position",
                        "the interpolated value is validated against a known set of table "
                        "names before it reaches the statement",
                    ]
                elif identifiers:
                    reasons.append(
                        "every interpolated value is an identifier — passed through a quoting "
                        "function or written inside a double-quoted name — a position no bound "
                        "parameter can fill; confirm the quoting doubles an embedded `\"`")
                if in_test:
                    reasons.append(
                        "the SQL is built by a test from values the test itself wrote; no "
                        "untrusted input reaches it")
                if reasons:
                    # Kept as an unscored lead rather than a scored
                    # SQL-injection finding: what would make it one (an
                    # attacker reaching the value) is not visible here.
                    what = (f"interpolates a value into a {keyword} statement" if keyword
                            else "interpolates only quoted identifiers into SQL" if identifiers
                            else "passes a dynamically built SQL string in a test file")
                    add(node.lineno, "sql_injection",
                        f"Call to .{func.attr}() {what}",
                        "", "high", kind="candidate", also_caused_by=reasons)
                else:
                    add(node.lineno, "sql_injection",
                        f"Call to .{func.attr}() passes a dynamically built SQL string",
                        "Use parameterized queries — pass parameters as the second argument "
                        "(e.g. execute(sql, params)); never interpolate values into SQL.",
                        "high")

            # ---------------------------------------------------------------- #
            # command_injection — subprocess/os.* with a dynamic command string
            # ---------------------------------------------------------------- #
            elif (isinstance(func, ast.Attribute)
                  and func.attr in ("system", "popen")
                  and isinstance(func.value, ast.Name)
                  and func.value.id == "os"
                  and node.args
                  and _is_dynamic_string(node.args[0])):
                add(node.lineno, "command_injection",
                    f"os.{func.attr}() is called with a dynamically built command string",
                    "Pass an argument list (e.g. [\"ls\", d]) and avoid shell=True; "
                    "never interpolate values into a shell string.",
                    "high")

            elif (isinstance(func, ast.Attribute)
                  and isinstance(func.value, ast.Name)
                  and func.value.id == "subprocess"
                  and func.attr in _SUBPROCESS_FUNCS
                  and node.args
                  and _is_dynamic_string(node.args[0])
                  # Without shell=True the string is an executable path, not a
                  # shell command — metacharacters aren't interpreted, so this
                  # is only injection when a shell is actually invoked.
                  and _keyword_value(node, "shell") is not None
                  and not _is_false_literal(_keyword_value(node, "shell"))):
                add(node.lineno, "command_injection",
                    f"subprocess.{func.attr}() runs a dynamically built command string through the shell (shell=True)",
                    "Pass an argument list (e.g. [\"ls\", d]) with shell=False; "
                    "never interpolate values into a shell string.",
                    "high")

            # ---------------------------------------------------------------- #
            # shell_injection — subprocess.* shell=True, os.system, os.popen
            # ---------------------------------------------------------------- #
            elif (isinstance(func, ast.Attribute)
                  and func.attr in ("system", "popen")
                  and isinstance(func.value, ast.Name)
                  and func.value.id == "os"):
                add(node.lineno, "shell_injection",
                    f"os.{func.attr}() passes a command string to the shell",
                    "Use subprocess.run() with an argument list and shell=False to avoid "
                    "shell injection vulnerabilities.",
                    "high")

            elif (isinstance(func, ast.Attribute)
                  and isinstance(func.value, ast.Name)
                  and func.value.id == "subprocess"):
                shell_val = _keyword_value(node, "shell")
                if shell_val is not None and not _is_false_literal(shell_val):
                    add(node.lineno, "shell_injection",
                        f"subprocess.{func.attr}() called with shell=True",
                        "Pass the command as a list (e.g., ['ls', '-l']) and use shell=False "
                        "to prevent shell injection.",
                        "high")

            # ---------------------------------------------------------------- #
            # unsafe_yaml — yaml.load() with no explicit loader
            # Safe when: Loader= keyword is present OR a second positional arg
            # is supplied (the caller has deliberately chosen a loader).
            # ---------------------------------------------------------------- #
            elif (isinstance(func, ast.Attribute)
                  and func.attr == "load"
                  and isinstance(func.value, ast.Name)
                  and func.value.id == "yaml"
                  and not _has_keyword(node, "Loader")
                  and len(node.args) < 2):
                add(node.lineno, "unsafe_yaml",
                    "yaml.load() called without a Loader= keyword can execute arbitrary code",
                    "Use yaml.safe_load() instead, or pass Loader=yaml.SafeLoader explicitly.",
                    "high")

            # ---------------------------------------------------------------- #
            # unsafe_deserialization — pickle/marshal load/loads, __import__
            # ---------------------------------------------------------------- #
            elif (isinstance(func, ast.Attribute)
                  and func.attr in ("load", "loads")
                  and isinstance(func.value, ast.Name)
                  and func.value.id in ("pickle", "marshal")):
                add(node.lineno, "unsafe_deserialization",
                    f"{func.value.id}.{func.attr}() deserializes untrusted data unsafely",
                    "Avoid deserializing data from untrusted sources with pickle/marshal. "
                    "Use JSON, msgpack, or another safe format instead.",
                    "medium")

            elif isinstance(func, ast.Name) and func.id == "__import__":
                if _literal_import_arguments(node):
                    add(node.lineno, "literal_import",
                        "__import__() loads a literal dependency outside normal import declarations",
                        "Prefer an ordinary import so the dependency is visible to readers and import tooling.",
                        "low")
                else:
                    add(node.lineno, "unsafe_deserialization",
                        "__import__() with dynamic arguments can load arbitrary modules",
                        "Use importlib.import_module() with a validated module name instead.",
                        "medium")

            # ---------------------------------------------------------------- #
            # weak_hash — hashlib.md5() or hashlib.sha1()
            # Skip when usedforsecurity=False is passed explicitly (Constant False).
            # ---------------------------------------------------------------- #
            elif (isinstance(func, ast.Attribute)
                  and func.attr in ("md5", "sha1")
                  and isinstance(func.value, ast.Name)
                  and func.value.id == "hashlib"):
                ufs = _keyword_value(node, "usedforsecurity")
                if ufs is None or not _is_false_literal(ufs):
                    add(node.lineno, "weak_hash",
                        f"hashlib.{func.attr}() is a cryptographically weak hash algorithm",
                        "Use hashlib.sha256() or stronger for security-sensitive uses. "
                        "If only used for checksums, add usedforsecurity=False (Python 3.9+) "
                        "to suppress this warning.",
                        "medium")

            # ---------------------------------------------------------------- #
            # tls_verify_disabled — call with verify=False or ssl._create_unverified_context
            # Only flag verify=False when the callee is a known HTTP API method.
            # ---------------------------------------------------------------- #
            else:
                verify_val = _keyword_value(node, "verify")
                if verify_val is not None and _is_false_literal(verify_val):
                    _HTTP_ATTR_NAMES = {
                        "get", "post", "put", "delete", "patch",
                        "head", "options", "request", "send",
                    }
                    _HTTP_NAME_NAMES = {
                        "get", "post", "put", "delete", "patch",
                        "head", "options", "request",
                    }
                    _is_http_call = (
                        (isinstance(func, ast.Attribute) and func.attr in _HTTP_ATTR_NAMES)
                        or (isinstance(func, ast.Name) and func.id in _HTTP_NAME_NAMES)
                    )
                    if _is_http_call:
                        add(node.lineno, "tls_verify_disabled",
                            "TLS certificate verification disabled via verify=False",
                            "Keep certificate verification enabled. If testing against a local "
                            "server use a proper CA bundle or a self-signed cert trusted by the "
                            "test environment.",
                            "medium")
                elif (isinstance(func, ast.Attribute)
                      and func.attr == "_create_unverified_context"
                      and isinstance(func.value, ast.Name)
                      and func.value.id == "ssl"):
                    add(node.lineno, "tls_verify_disabled",
                        "ssl._create_unverified_context() disables TLS certificate verification",
                        "Use ssl.create_default_context() to keep certificate verification on.",
                        "medium")

        # ------------------------------------------------------------------ #
        # hardcoded_secret — assignment of a string literal to a secret-named var
        # ------------------------------------------------------------------ #
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if isinstance(node, ast.Assign):
                targets = node.targets
                value_node = node.value
            else:
                targets = [node.target]
                value_node = node.value

            if value_node is None:
                continue

            if not (isinstance(value_node, ast.Constant)
                    and isinstance(value_node.value, str)):
                continue

            secret_value = value_node.value
            if len(secret_value) < 6:
                continue
            if _is_placeholder_secret(secret_value):
                continue

            for target in targets:
                if _target_looks_like_secret(target):
                    tname = target.id if isinstance(target, ast.Name) else target.attr
                    add(node.lineno, "hardcoded_secret",
                        f"Hardcoded string literal assigned to '{tname}' looks like a secret",
                        "Load credentials from environment variables (os.environ) or a secrets "
                        "manager (e.g., AWS Secrets Manager, HashiCorp Vault) rather than "
                        "embedding them in source code.",
                        "medium")

    return issues


def analyze_file(filepath: Path, ignore: set) -> list:
    try:
        source, tree = cached_parse(filepath)
        return detect(tree, str(filepath), source.splitlines(), ignore)
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def to_record(issue: "CodeSmell") -> dict:
    """The JSON shape this detector emits, shared with the runner.

    `kind` is always present, so a consumer never has to read an absent key as
    a classification; the reasons tuple serialises as a list.
    """
    record = asdict(issue)
    record["also_caused_by"] = list(record["also_caused_by"])
    return record


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect security issues in Python code")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    all_issues = []
    for filepath in find_python_files(Path(args.path)):
        all_issues.extend(analyze_file(filepath, ignore))
    sort_findings(all_issues)

    if args.format == "json":
        # `kind` is always present — "finding" for a scored defect, "candidate"
        # for a lead the downstream merge must not score.
        print(json.dumps([to_record(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No security issues found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        # A candidate is a lead, not a defect: counting one as a "security issue
        # found" is a verdict the evidence does not support, so it is named apart.
        proven = [i for i in all_issues if i.kind != "candidate"]
        leads = [i for i in all_issues if i.kind == "candidate"]
        print(f"Found {len(proven)} finding(s), {len(leads)} candidate(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = SEVERITY_ICONS
        for i in all_issues:
            # Marked as django_report.render marks it: a severity icon on a lead
            # reads as a verdict the evidence does not support.
            marker = ("? [CANDIDATE]" if i.kind == "candidate"
                      else f"{icons[i.severity]} [{i.severity.upper()}]")
            print(f"{marker} {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            # A candidate has no fix to print — an empty arrow reads as one that
            # was forgotten. What it has is the readings the reader must rule out.
            if i.suggestion:
                print(f"   → {i.suggestion}")
            if i.kind == "candidate":
                for reason in i.also_caused_by:
                    print(f"   ? also caused by: {reason}")
            print()


if __name__ == "__main__":
    main()
