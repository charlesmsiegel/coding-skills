#!/usr/bin/env python3
"""
Detect code smells in Python code via AST analysis.
Finds: magic numbers, bare excepts, mutable defaults, type comparisons,
       god classes, data classes.

A long parameter list is analyze_complexity's `parameter_count`; it is not
reported here too, because one signature is one defect.

A magic number is a bare literal that encodes meaning a name should carry. A
test file is not checked: the literal in `assert total == 3` is the expected
value, the specification itself, and naming it would only restate it or import
the production constant the test exists to check. In production code an HTTP
status code where a status is expected (`HTTPException(404)`,
`resp.status_code == 409`) and a mode written in octal (`0o755`) already say
what they are.
"""

import ast
import json
import argparse
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from common import count_parameters, framework_hook_overrides, is_test_file, parameter_severity, signature_is_a_contract, sort_findings, cached_parse, SEVERITY_ICONS, configure_output, find_python_files, warn_detector_error, warn_unparseable, looks_like_test


# Callables whose first positional argument is an HTTP status code.
_STATUS_CALLEES = frozenset({"HTTPException", "HTTPError", "abort"})
_HTTP_STATUS = range(100, 600)


def _callee_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _names_a_status(node: ast.AST) -> bool:
    """`status`, `status_code`, `resp.status_code`, `http_status`, ..."""
    name = node.id if isinstance(node, ast.Name) else (
        node.attr if isinstance(node, ast.Attribute) else None)
    return bool(name) and (name.lower().endswith("status") or name.lower().endswith("status_code"))


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


# Classes with these decorators/bases are data containers on purpose.
DATA_CLASS_MARKERS = frozenset({
    "dataclass", "attrs", "attr", "define", "frozen",
    "NamedTuple", "TypedDict", "Protocol",
    "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag",
    "BaseModel",  # pydantic
})

# Public concrete generic views; an arbitrary *View name proves no contract.
DJANGO_GENERIC_VIEWS = {
    "django.views.generic": frozenset({
        "View", "TemplateView", "RedirectView", "DetailView", "ListView",
        "FormView", "CreateView", "UpdateView", "DeleteView", "ArchiveIndexView",
        "YearArchiveView", "MonthArchiveView", "WeekArchiveView", "DayArchiveView",
        "TodayArchiveView", "DateDetailView",
    }),
    "django.views.generic.base": frozenset({"View", "TemplateView", "RedirectView"}),
    "django.views.generic.detail": frozenset({"DetailView"}),
    "django.views.generic.list": frozenset({"ListView"}),
    "django.views.generic.edit": frozenset({"FormView", "CreateView", "UpdateView", "DeleteView"}),
    "django.views.generic.dates": frozenset({
        "ArchiveIndexView", "YearArchiveView", "MonthArchiveView", "WeekArchiveView",
        "DayArchiveView", "TodayArchiveView", "DateDetailView",
    }),
}


# These bases supply persistence, validation, or declarative configuration APIs;
# replacing their subclasses with dataclasses would discard inherited behavior.
DJANGO_DECLARATIVE_BASES = {
    **DJANGO_GENERIC_VIEWS,
    "django.db.models": frozenset({"Model", "Choices", "TextChoices", "IntegerChoices"}),
    "django.db.models.base": frozenset({"Model"}),
    "django.db.models.enums": frozenset({"Choices", "TextChoices", "IntegerChoices"}),
    "django.forms": frozenset({"Form", "BaseForm", "ModelForm", "BaseModelForm",
                               "BaseFormSet", "BaseModelFormSet", "BaseInlineFormSet"}),
    "django.forms.forms": frozenset({"Form", "BaseForm"}),
    "django.forms.models": frozenset({"ModelForm", "BaseModelForm", "BaseModelFormSet", "BaseInlineFormSet"}),
    "django.forms.formsets": frozenset({"BaseFormSet"}),
    "django.contrib.admin": frozenset({"ModelAdmin", "TabularInline", "StackedInline"}),
    "django.contrib.admin.options": frozenset({"ModelAdmin", "InlineModelAdmin", "TabularInline", "StackedInline"}),
}


# ModelBase and ModelFormMetaclass consume these immediate inner Meta classes.
# BaseModelForm alone does not install ModelFormMetaclass.
DJANGO_META_CONSUMERS = frozenset({
    "django.db.models.Model", "django.db.models.base.Model",
    "django.forms.ModelForm", "django.forms.models.ModelForm",
})


def _dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _module_binding_facts(tree):
    """Only trust unconditional imports with no other bindings or mutations.

    Scope-insensitive invalidation deliberately favors reporting a candidate
    defect over suppressing one when a nested scope shadows an import.
    """
    bindings = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split('.')[0]
                bindings[name].append(node)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bindings[node.id].append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings[node.name].append(node)
        elif isinstance(node, ast.arg):
            bindings[node.arg].append(node)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bindings[node.name].append(node)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            bindings[node.name].append(node)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bindings[node.rest].append(node)
        elif isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            root = node.value
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if isinstance(root, ast.Name):
                bindings[root.id].append(node)
    imports = {}
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.level:
            continue
        for alias in node.names:
            name = alias.asname or alias.name.split('.')[0]
            if bindings[name] != [node]:
                continue
            if isinstance(node, ast.ImportFrom):
                imports[name] = (f"{node.module}.{alias.name}", node.lineno)
            else:
                imports[name] = (alias.name if alias.asname else name, node.lineno)
    # A wildcard can replace any imported identity.
    wildcard = any(isinstance(n, ast.ImportFrom) and any(a.name == '*' for a in n.names)
                   for n in ast.walk(tree))
    if wildcard:
        imports = {}
    return imports, set(bindings) | ({"object"} if wildcard else set())


def _stable_imports(tree):
    return _module_binding_facts(tree)[0]


def _constant_expression(node: ast.AST | None) -> bool:
    """Whether ``node`` is arithmetic over literals only: `4 << 20`, `30 * 60`."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
    if isinstance(node, ast.UnaryOp):
        return _constant_expression(node.operand)
    if isinstance(node, ast.BinOp):
        return _constant_expression(node.left) and _constant_expression(node.right)
    return False


def _unwrap_negative(node: ast.AST) -> ast.AST:
    """-5 parses as UnaryOp(USub, Constant(5)); return the inner constant."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return node.operand
    return node


def _is_type_call(node) -> bool:
    """`type(x)` asking an object's type. `type(None)` is not one: it names
    NoneType, as when an annotation's arguments are compared against it."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "type"
            and not (len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                     and node.args[0].value is None))


class CodeSmellDetector(ast.NodeVisitor):
    # Values so conventional a name would add nothing.
    MAGIC_NUMBER_WHITELIST = frozenset({
        10, 100, 1000, 24, 60, 365,
        16, 32, 64, 128, 255, 256, 512, 1024,
    })

    def __init__(self, filename: str, source_lines: list[str], ignore: set[str] = None,
                 is_test_file: bool = False):
        self.filename = filename
        self.source_lines = source_lines
        self.issues: list[CodeSmell] = []
        self.ignore = ignore or set()
        # In a test a literal is fixture data or an expected value — the number
        # *is* the specification, and naming it would only move it a line up.
        self.is_test_file = is_test_file
        self.current_class = None
        self.class_info: dict[str, dict] = {}
        self._named_constants: set[int] = set()
        self._hooks: frozenset = frozenset()
        self._in_assert = 0
        self._framework_imports = {}
        self._object_is_builtin = True
        self._framework_meta_nodes = set()
        self.is_test_file = looks_like_test(Path(filename))

    def visit_Module(self, node: ast.Module):
        self._hooks = framework_hook_overrides(node)
        self._framework_imports, bound_names = _module_binding_facts(node)
        self._object_is_builtin = "object" not in bound_names
        # A literal is only "magic" when nothing names it. Collect the node
        # ids of literals that already have a name or a self-explaining
        # position, so visit_Constant can skip them:
        #   NAME = 30              the assignment names it
        #   obj.timeout = 30       an attribute also names the value
        #   def f(timeout=30)      the parameter names it
        #   get(url, timeout=30)   the keyword names it
        #   text[:200]             slice bounds
        #   [1, 30, 5] / {...}     data tables, not control values
        for parent in ast.walk(node):
            if isinstance(parent, (ast.Assign, ast.AnnAssign)):
                targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                value = getattr(parent, "value", None)
                # `MAX_BODY = 4 << 20` names the expression, and with it every
                # literal the expression is built from.
                if _constant_expression(value) and any(isinstance(t, ast.Name) for t in targets):
                    for sub in ast.walk(value):
                        if isinstance(sub, ast.Constant):
                            self._named_constants.add(id(sub))
                elif isinstance(_unwrap_negative(value), ast.Constant) and any(
                        isinstance(t, ast.Attribute) for t in targets):
                    self._named_constants.add(id(_unwrap_negative(value)))
            elif isinstance(parent, ast.keyword):
                value = _unwrap_negative(parent.value)
                if isinstance(value, ast.Constant):
                    self._named_constants.add(id(value))
            elif isinstance(parent, ast.arguments):
                for default in [*parent.defaults, *parent.kw_defaults]:
                    default = _unwrap_negative(default) if default else default
                    if isinstance(default, ast.Constant):
                        self._named_constants.add(id(default))
            elif isinstance(parent, ast.Subscript):
                for sub in ast.walk(parent.slice):
                    if isinstance(sub, ast.Constant):
                        self._named_constants.add(id(sub))
            elif isinstance(parent, (ast.List, ast.Tuple, ast.Set)):
                for elt in parent.elts:
                    elt = _unwrap_negative(elt)
                    if isinstance(elt, ast.Constant):
                        self._named_constants.add(id(elt))
            elif isinstance(parent, ast.Dict):
                for elt in [*parent.keys, *parent.values]:
                    elt = _unwrap_negative(elt) if elt else elt
                    if isinstance(elt, ast.Constant):
                        self._named_constants.add(id(elt))
            # An HTTP status where a status is expected names itself:
            #   HTTPException(404, ...)   raise/abort with a status
            #   JSONResponse(body, 201)   a *Response's positional status
            #   resp.status_code == 409   compared against a status
            elif isinstance(parent, ast.Call) and parent.args:
                callee = _callee_name(parent) or ""
                if callee in _STATUS_CALLEES:
                    self._name_status(parent.args[0])
                elif callee.endswith("Response"):
                    # Frameworks disagree on where the status sits (second for
                    # Starlette and Flask, third for Django), and a response's
                    # body is not an int in the status range.
                    for arg in parent.args:
                        self._name_status(arg)
            elif isinstance(parent, ast.Compare):
                operands = [parent.left, *parent.comparators]
                if any(_names_a_status(o) for o in operands):
                    for operand in operands:
                        self._name_status(operand)
        self.generic_visit(node)

    def _name_status(self, node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, int) \
                and not isinstance(node.value, bool) and node.value in _HTTP_STATUS:
            self._named_constants.add(id(node))

    def _written_in_octal(self, node: ast.Constant) -> bool:
        """`0o755` is a permission mode by its spelling; nobody writes a count in octal."""
        if 0 < node.lineno <= len(self.source_lines):
            text = self.source_lines[node.lineno - 1][node.col_offset:node.col_offset + 2]
            return text.lower() == "0o"
        return False

    def _get_line(self, lineno: int) -> str:
        if 0 < lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].strip()[:60]
        return ""

    def _add(self, line: int, smell_type: str, desc: str, suggestion: str, severity: str = "medium",
             *, kind="finding", also_caused_by=()):
        if smell_type in self.ignore:
            return
        self.issues.append(CodeSmell(
            file=self.filename, line=line, smell_type=smell_type,
            description=desc, suggestion=suggestion, severity=severity,
            code_snippet=self._get_line(line), kind=kind, also_caused_by=tuple(also_caused_by)
        ))

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._check_function(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._check_function(node)
        self.generic_visit(node)

    def _check_function(self, node):
        # Mutable defaults
        for default in node.args.defaults + node.args.kw_defaults:
            if default is None:
                continue
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self._add(node.lineno, "mutable_default",
                    f"Mutable default argument in {node.name}",
                    "Use None and create inside function", "high")
            elif isinstance(default, ast.Call):
                if isinstance(default.func, ast.Name) and default.func.id in ('list', 'dict', 'set'):
                    self._add(node.lineno, "mutable_default",
                        f"Mutable default from {default.func.id}() in {node.name}",
                        "Use None and create inside function", "high")

    def visit_ExceptHandler(self, node: ast.ExceptHandler):
        if node.type is None:
            self._add(node.lineno, "bare_except",
                "Bare except catches all exceptions including KeyboardInterrupt",
                "Use 'except Exception:' or specific types", "high")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant):
        if (not self.is_test_file
                and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
                and id(node) not in self._named_constants
                and node.value not in self.MAGIC_NUMBER_WHITELIST
                and abs(node.value) > 2
                and not self._written_in_octal(node)):
            self._add(node.lineno, "magic_number",
                f"Magic number {node.value}",
                "Extract to named constant", "low")
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert):
        # In a test, `assert type(x) is float` pins the exact type on purpose —
        # isinstance would accept a subclass, which is what the test rules out.
        self._in_assert += 1
        self.generic_visit(node)
        self._in_assert -= 1

    def visit_Compare(self, node: ast.Compare):
        if not (self.is_test_file and self._in_assert):
            # Identity deliberately requests an exact type; only equality
            # suggests an accidental substitute for isinstance.
            operands = [node.left, *node.comparators]
            for operator, left, right in zip(node.ops, operands, operands[1:]):
                if isinstance(operator, (ast.Eq, ast.NotEq)) and (_is_type_call(left) or _is_type_call(right)):
                    self._add(node.lineno, "type_comparison",
                        "Using type() for comparison",
                        "Use isinstance() for subclasses, or `type(x) is T` for an exact type", "medium")
                    break
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        old_class = self.current_class
        self.current_class = node.name
        
        methods = []
        attributes = set()
        
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append(item.name)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        attributes.add(target.id)
        
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == '__init__':
                for stmt in ast.walk(item):
                    if isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, ast.Attribute):
                                if isinstance(target.value, ast.Name) and target.value.id == 'self':
                                    attributes.add(target.attr)
        
        # God class
        non_dunder = [m for m in methods if not m.startswith('_')]
        if len(methods) > 15 and len(attributes) > 10:
            self._add(node.lineno, "god_class",
                f"Class {node.name} has {len(methods)} methods and {len(attributes)} attributes",
                "Split into smaller focused classes", "high")
        
        # Data class — but a class already declared as one (@dataclass,
        # NamedTuple, TypedDict, Enum, pydantic model, ...) is the fix applied,
        # not the smell.
        marker_names = set()
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Name):
                marker_names.add(target.id)
            elif isinstance(target, ast.Attribute):
                marker_names.add(target.attr)
        for base in node.bases:
            if isinstance(base, ast.Name):
                marker_names.add(base.id)
            elif isinstance(base, ast.Attribute):
                marker_names.add(base.attr)
            elif isinstance(base, ast.Subscript) and isinstance(base.value, ast.Name):
                marker_names.add(base.value.id)
        is_declared_data = bool(marker_names & DATA_CLASS_MARKERS)
        has_framework_behavior = False
        consumes_meta = False
        for base in node.bases:
            dotted = _dotted_name(base)
            if not dotted:
                continue
            root, _, suffix = dotted.partition('.')
            imported = self._framework_imports.get(root)
            if imported and imported[1] < node.lineno:
                resolved = f"{imported[0]}.{suffix}" if suffix else imported[0]
                module, _, name = resolved.rpartition('.')
                if name in DJANGO_DECLARATIVE_BASES.get(module, ()):
                    has_framework_behavior = True
                if resolved in DJANGO_META_CONSUMERS:
                    consumes_meta = True
        if consumes_meta:
            # AST identities tie the exemption to immediate class-body members;
            # sibling classes and classes inside methods receive no contract.
            self._framework_meta_nodes.update(child for child in node.body
                                              if isinstance(child, ast.ClassDef) and child.name == "Meta")
        if (len(attributes) > 3 and len(non_dunder) == 0 and not is_declared_data
                and not has_framework_behavior and node not in self._framework_meta_nodes):
            inherited_behavior_unknown = any(not (isinstance(base, ast.Name) and base.id == "object"
                                                  and self._object_is_builtin)
                                             for base in node.bases)
            if inherited_behavior_unknown:
                self._add(node.lineno, "data_class",
                    f"Class {node.name} declares data locally; check the behavior inherited from its bases",
                    "", "low", kind="candidate",
                    also_caused_by=("Base classes may supply validation, persistence, or other behavior.",
                                    "Class attributes may configure an inherited framework API."))
            else:
                self._add(node.lineno, "data_class",
                    f"Class {node.name} has only data, no behavior methods",
                    "Consider using @dataclass or namedtuple", "low")
        
        self.generic_visit(node)
        self.current_class = old_class

    def visit_Call(self, node: ast.Call):
        bool_args = sum(1 for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, bool))
        if bool_args >= 2:
            self._add(node.lineno, "boolean_blindness",
                "Multiple boolean arguments make code hard to read",
                "Use keyword arguments or Enums", "low")
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp):
        if isinstance(node.op, ast.Mod):
            if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
                if '%' in node.left.value:
                    self._add(node.lineno, "old_string_format",
                        "Using %-formatting",
                        "Use f-strings instead", "low")
        self.generic_visit(node)


def analyze_file(filepath: Path, ignore: set[str]) -> list[CodeSmell]:
    try:
        source, tree = cached_parse(filepath)
        lines = source.splitlines()
        detector = CodeSmellDetector(str(filepath), lines, ignore, is_test_file(filepath))
        detector.visit(tree)
        return detector.issues
    except (SyntaxError, ValueError) as exc:
        warn_unparseable(filepath, exc)
        return []
    except Exception as exc:
        warn_detector_error(filepath, exc)
        return []


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect code smells in Python")
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
        if not all_issues:
            print("✅ No code smells found!")
            return
        
        by_type = defaultdict(int)
        for issue in all_issues:
            by_type[issue.smell_type] += 1
        
        candidate_count = sum(issue.kind == "candidate" for issue in all_issues)
        print(f"Found {len(all_issues) - candidate_count} finding(s), {candidate_count} candidate(s):\n")
        print("Summary:")
        for smell, count in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {smell}: {count}")
        print()
        
        severity_icons = SEVERITY_ICONS
        for issue in all_issues:
            icon = severity_icons[issue.severity]
            marker = "? [CANDIDATE]" if issue.kind == "candidate" else f"{icon} [{issue.severity.upper()}]"
            print(f"{marker} {issue.file}:{issue.line}")
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
