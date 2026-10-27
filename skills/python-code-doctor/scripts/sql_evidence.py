#!/usr/bin/env python3
"""Local SQL assembly, lexical binding and identifier-quoting evidence.

Kept separate from security reporting so evidence rules stay inspectable and the
CLI retains the common finding/candidate and scoring contracts.
"""
import ast
import re
import string


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
                if piece.conversion != -1 or piece.format_spec is not None:
                    return None
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
        # Only unmodified string substitution preserves identifier evidence.
        template = node.left.value.replace("%%", "\0")
        texts = re.split(r"%s", template)
        if len(texts) != len(values) + 1 or any("%" in t for t in texts):
            return None
        parts = [("text", texts[0].replace("\0", "%"))]
        for value, text in zip(values, texts[1:]):
            parts += [("value", value), ("text", text.replace("\0", "%"))]
        return parts
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        template = node.func.value
        if not (isinstance(template, ast.Constant) and isinstance(template.value, str)):
            return None
        try:
            fields = list(string.Formatter().parse(template.value))
        except ValueError:
            return None
        parts, automatic = [], 0
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
        for text, field, spec, conversion in fields:
            parts.append(("text", text))
            if field is not None:
                if spec or conversion:
                    return None
                if field == "":
                    field, automatic = str(automatic), automatic + 1
                if field.isdecimal() and int(field) < len(node.args):
                    value = node.args[int(field)]
                elif field in keywords:
                    value = keywords[field]
                else:
                    return None
                parts.append(("value", value))
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


class _IdentifierProof(_LiteralProof):
    """Unique lexical bindings and one inspectable, undecorated quoting body."""

    def _binding_values(self, node, name):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return [node]
        return super()._binding_values(node, name)

    def resolve_unique(self, node, seen=frozenset()):
        if not isinstance(node, ast.Name):
            return node
        resolved = self._resolve(node)
        if resolved is None:
            return node
        scope, values = resolved
        key = (id(scope), node.id)
        if key in seen or len(values) != 1 or values[0] is _OPAQUE:
            return None
        value = values[0]
        # Conditional writes and use-before-binding are not local evidence.
        statement = value
        while statement is not None and not isinstance(statement, ast.stmt):
            statement = self._parent(statement)
        body = getattr(scope, "body", ())
        if not isinstance(body, (list, tuple)):
            return None
        if statement not in body or value.lineno >= node.lineno:
            return None
        return self.resolve_unique(value, seen | {key})

    @staticmethod
    def quoting_body(func):
        if not isinstance(func, ast.FunctionDef) or func.decorator_list:
            return False
        args = func.args
        if (len(args.posonlyargs + args.args) != 1 or args.vararg or args.kwarg
                or args.kwonlyargs or args.defaults):
            return False
        body = func.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            return False
        # AST equality checks the complete expression, including both delimiters,
        # replacement strings and absence of a count argument or transformations.
        parameter = (args.posonlyargs + args.args)[0].arg
        quote = repr(chr(34))
        doubled = repr(chr(34) * 2)
        expected = ast.parse(f"{quote} + {parameter}.replace({quote}, {doubled}) + {quote}", mode="eval").body
        return ast.dump(body[0].value) == ast.dump(expected)

    def quoted(self, node):
        node = self.resolve_unique(node)
        if not isinstance(node, ast.Call) or len(node.args) != 1 or node.keywords:
            return False
        func = self.resolve_unique(node.func)
        if self.quoting_body(func):
            return True
        # Existing framework constant-identifier evidence is also position checked.
        value = self.resolve_unique(node.args[0])
        return (isinstance(func, ast.Attribute) and func.attr == "quote_name"
                and isinstance(func.value, ast.Attribute) and func.value.attr == "ops"
                and isinstance(value, ast.Constant) and isinstance(value.value, str))


def _identifier_positions(parts):
    """A bounded SQL lexer: holes must be whole, unquoted identifier tokens.

    Recognize relation positions and UPDATE SET columns; values, comments,
    literals, expression contexts and unsupported grammar remain unproven.
    """
    marker = "\x00"
    text = "".join(piece if kind == "text" else marker for kind, piece in parts)
    # Dialect-specific escaped/dollar literals and nested comments need a
    # fuller lexer. Lack of evidence must not become an identifier candidate.
    if "$" in text or "\\" in text or text.count("/*") > 1:
        return False
    tokens = re.findall(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|"
                        r"`[^`]*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|\s+|.", text, re.S)
    previous, clause, holes = "", "", 0
    for i, token in enumerate(tokens):
        if token.isspace():
            continue
        if token in {"'", '"', '`', '['} or (token == '/' and i + 1 < len(tokens) and tokens[i + 1] == '*'):
            return False
        if marker in token:
            if token != marker:
                return False
            following = tokens[i + 1] if i + 1 < len(tokens) else ""
            if following and (following[0].isalnum() or following[0] in "_\"'`[" or following == marker):
                return False
            if previous not in {"FROM", "JOIN", "UPDATE", "INTO", "TABLE", "VIEW", "INDEX", "TRIGGER", "SET"}:
                if not (previous == "," and clause == "SET"):
                    return False
            holes += 1
        if token.startswith(("--", "/*")):
            continue
        upper = token.upper()
        if upper in {"SET", "WHERE", "VALUES", "RETURNING", "ORDER", "GROUP"}:
            clause = upper
        previous = upper
    return holes == sum(kind == "value" for kind, _ in parts) and holes > 0


def _quoted_constant_sql_checker(tree):
    proof = _IdentifierProof(tree)

    def check(node):
        parts = _sql_parts(node)
        return (parts is not None and _identifier_positions(parts)
                and all(proof.quoted(value) for kind, value in parts if kind == "value"))

    return check


