#!/usr/bin/env python3
"""A whole-project model of a Django codebase, built once and shared by every detector.

Almost every interesting Django question is cross-file. Is this abstract model
extended anywhere? Is this mixin used twice or once? Does this manager earn its
keep? A per-file detector cannot answer any of them, so it falls back to guessing
from names — flagging every class with "Mixin" in it and every class with
"Service" in it — and a detector that guesses gets ignored.

So this walks the tree once and builds a class graph:

    ctx = build_context(Path("."))
    ctx.models                 # classes that really derive from models.Model
    ctx.subclasses["BaseUser"] # who actually extends it
    ctx.is_abstract("BaseUser")

Inheritance is resolved by class *name* rather than by import, which is the
pragmatic trade: resolving imports properly means resolving Django's own package,
and a name collision across two apps is rarer than the false positives that
name-substring matching produces. Where a name is ambiguous the context says so
and the detectors stay quiet.

The other job here is the gate. `build_context` returns None for a tree that is
not a Django project, so the detectors report nothing instead of firing Django
rules at arbitrary Python.
"""

import ast
import sys
from pathlib import Path

from common import EXCLUDE_DIRS, find_python_files, warn_unparseable

# Base names that mean "this class is a Django model". Matched against the last
# component, so `models.Model` and a bare `Model` both count.
MODEL_BASES = frozenset({"Model"})
MANAGER_BASES = frozenset({"Manager", "BaseManager", "QuerySet"})
FORM_BASES = frozenset({"Form", "ModelForm", "BaseForm", "BaseModelForm"})
SERIALIZER_BASES = frozenset({"Serializer", "ModelSerializer", "BaseSerializer",
                              "HyperlinkedModelSerializer"})
VIEW_BASES = frozenset({"View", "TemplateView", "ListView", "DetailView", "CreateView",
                        "UpdateView", "DeleteView", "FormView", "RedirectView",
                        "APIView", "GenericAPIView", "ViewSet", "ModelViewSet",
                        "GenericViewSet", "ReadOnlyModelViewSet"})

# Relation fields, split by which prefetch strategy applies to them.
FORWARD_RELATIONS = frozenset({"ForeignKey", "OneToOneField"})
MULTI_RELATIONS = frozenset({"ManyToManyField"})
RELATION_FIELDS = FORWARD_RELATIONS | MULTI_RELATIONS

TEXT_FIELDS = frozenset({"CharField", "TextField", "SlugField", "EmailField",
                         "URLField", "FilePathField"})


class ClassInfo:
    """One class definition, with the facts detectors ask about."""

    __slots__ = ("name", "file", "line", "bases", "methods", "node", "meta", "fields")

    def __init__(self, name, file, line, bases, methods, node, meta, fields):
        self.name = name
        self.file = file
        self.line = line
        self.bases = bases          # [str] — last component of each base expression
        self.methods = methods      # {name: ast.FunctionDef}
        self.node = node            # the ClassDef itself
        self.meta = meta            # {assigned name: ast node} from an inner Meta
        self.fields = fields        # {attr: field type} for Model field assignments

    def __repr__(self):
        return f"<ClassInfo {self.name} {self.file}:{self.line}>"


class DjangoContext:
    """The project's class graph, plus the roots detectors need."""

    def __init__(self, root):
        self.root = root
        self.classes = {}          # name -> ClassInfo (first definition wins)
        self.duplicates = set()    # names defined more than once: resolution is unsafe
        self.subclasses = {}       # base name -> [subclass names]
        self.settings_files = []
        self.template_dirs = []
        self.files = []

    # ---- graph queries ---------------------------------------------------- #

    def _walk_bases(self, name, seen):
        info = self.classes.get(name)
        if info is None:
            return
        for base in info.bases:
            if base in seen:
                continue
            seen.add(base)
            yield base
            yield from self._walk_bases(base, seen)

    def ancestors(self, name):
        """Every base name reachable from ``name``, transitively."""
        return set(self._walk_bases(name, {name}))

    def derives_from(self, name, base_names):
        """True when ``name`` inherits (directly or not) from any of ``base_names``."""
        info = self.classes.get(name)
        if info is None:
            return False
        if base_names & set(info.bases):
            return True
        return bool(base_names & self.ancestors(name))

    def is_ambiguous(self, name):
        """A name defined in two places cannot be resolved safely by name alone."""
        return name in self.duplicates

    # ---- derived collections ---------------------------------------------- #

    @property
    def models(self):
        return {n: c for n, c in self.classes.items() if self.derives_from(n, MODEL_BASES)}

    @property
    def managers(self):
        return {n: c for n, c in self.classes.items() if self.derives_from(n, MANAGER_BASES)}

    @property
    def views(self):
        return {n: c for n, c in self.classes.items() if self.derives_from(n, VIEW_BASES)}

    @property
    def forms(self):
        return {n: c for n, c in self.classes.items()
                if self.derives_from(n, FORM_BASES | SERIALIZER_BASES)}

    def is_abstract(self, name):
        info = self.classes.get(name)
        if info is None:
            return False
        value = info.meta.get("abstract")
        return isinstance(value, ast.Constant) and value.value is True

    def concrete_subclasses(self, name):
        """Direct subclasses that are not themselves abstract."""
        return [s for s in self.subclasses.get(name, []) if not self.is_abstract(s)]


def base_name(node):
    """The last component of a base expression: models.Model -> 'Model'."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):       # Generic[T] in typed codebases
        return base_name(node.value)
    if isinstance(node, ast.Call):            # a factory used as a base
        return base_name(node.func)
    return None


def call_name(node):
    """The last component of a call's target: models.CharField(...) -> 'CharField'."""
    return base_name(node.func) if isinstance(node, ast.Call) else None


def _meta_assignments(class_node):
    meta = {}
    for item in class_node.body:
        if isinstance(item, ast.ClassDef) and item.name == "Meta":
            for stmt in item.body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name):
                            meta[target.id] = stmt.value
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    meta[stmt.target.id] = stmt.value
    return meta


def _field_assignments(class_node):
    """Class-level ``name = models.SomeField(...)`` assignments."""
    fields = {}
    for item in class_node.body:
        if isinstance(item, ast.Assign) and isinstance(item.value, ast.Call):
            kind = call_name(item.value)
            if kind and kind.endswith("Field") or kind in RELATION_FIELDS:
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        fields[target.id] = kind
    return fields


def looks_like_django(root):
    """Whether this tree is a Django project at all."""
    root = Path(root)
    if root.is_file():
        return "django" in root.read_text(encoding="utf-8", errors="replace").lower()
    if (root / "manage.py").exists():
        return True
    for path in root.rglob("manage.py"):
        if EXCLUDE_DIRS.isdisjoint(path.relative_to(root).parts):
            return True
    # A library or an app installed into a larger project has no manage.py.
    for path in find_python_files(root):
        head = ""
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                head = "".join(next(handle, "") for _ in range(40))
        except OSError:
            continue
        if "from django" in head or "import django" in head:
            return True
    return False


def build_context(root, quiet=False):
    """Build the project graph, or return None when this is not a Django project."""
    root = Path(root)
    if not looks_like_django(root):
        if not quiet:
            print(f"⚠️  {root}: no Django project found (no manage.py, no django imports) — "
                  f"reporting nothing rather than applying Django rules to arbitrary Python.",
                  file=sys.stderr)
        return None

    ctx = DjangoContext(root)
    for path in find_python_files(root):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, ValueError, OSError) as exc:
            warn_unparseable(path, exc)
            continue

        ctx.files.append(path)
        if path.name == "settings.py" or path.parent.name == "settings":
            ctx.settings_files.append(path)

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [b for b in (base_name(b) for b in node.bases) if b]
            info = ClassInfo(
                name=node.name, file=path, line=node.lineno, bases=bases,
                methods={n.name: n for n in node.body
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))},
                node=node, meta=_meta_assignments(node), fields=_field_assignments(node),
            )
            if node.name in ctx.classes:
                # Two classes with one name: any base resolved to it could be either.
                ctx.duplicates.add(node.name)
            else:
                ctx.classes[node.name] = info
            for base in bases:
                ctx.subclasses.setdefault(base, []).append(node.name)

    for template in root.rglob("templates"):
        if template.is_dir() and EXCLUDE_DIRS.isdisjoint(template.relative_to(root).parts):
            ctx.template_dirs.append(template)
    return ctx


def template_files(ctx):
    """Every .html under a templates/ directory."""
    seen = set()
    for directory in ctx.template_dirs:
        for path in directory.rglob("*.html"):
            if path not in seen:
                seen.add(path)
                yield path
