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

The tree is parsed once and the ASTs cached (``ctx.parsed(path)``). With fifteen
detectors that matters: parsing a large project fifteen times is most of the
runtime, and it was the whole reason this module exists.

The other job here is the gate. ``build_context`` returns None for a tree that is
not a Django project, so the detectors report nothing instead of firing Django
rules at arbitrary Python.
"""

import ast
import sys
from pathlib import Path

from common import EXCLUDE_DIRS, find_python_files, warn_unparseable
from django_detect_version import detect_django_version

# Base names that mean "this class is a Django model". Matched against the last
# component, so `models.Model` and a bare `Model` both count.
MODEL_BASES = frozenset({"Model"})
MANAGER_BASES = frozenset({"Manager", "BaseManager", "QuerySet"})
FORM_BASES = frozenset({"Form", "ModelForm", "BaseForm", "BaseModelForm"})
SERIALIZER_BASES = frozenset({"Serializer", "ModelSerializer", "BaseSerializer",
                              "HyperlinkedModelSerializer", "ListSerializer"})
VIEWSET_BASES = frozenset({"ViewSet", "ModelViewSet", "GenericViewSet",
                           "ReadOnlyModelViewSet"})
API_VIEW_BASES = frozenset({"APIView", "GenericAPIView", "ListAPIView", "CreateAPIView",
                            "RetrieveAPIView", "UpdateAPIView", "DestroyAPIView",
                            "ListCreateAPIView", "RetrieveUpdateAPIView",
                            "RetrieveDestroyAPIView", "RetrieveUpdateDestroyAPIView"})
VIEW_BASES = frozenset({"View", "TemplateView", "ListView", "DetailView", "CreateView",
                        "UpdateView", "DeleteView", "FormView", "RedirectView"}) \
    | VIEWSET_BASES | API_VIEW_BASES
ADMIN_BASES = frozenset({"ModelAdmin", "TabularInline", "StackedInline", "InlineModelAdmin"})
CONFIG_BASES = frozenset({"AppConfig"})

# Relation fields, split by which prefetch strategy applies to them.
FORWARD_RELATIONS = frozenset({"ForeignKey", "OneToOneField"})
MULTI_RELATIONS = frozenset({"ManyToManyField"})
RELATION_FIELDS = FORWARD_RELATIONS | MULTI_RELATIONS

TEXT_FIELDS = frozenset({"CharField", "TextField", "SlugField", "EmailField",
                         "URLField", "FilePathField"})

# Settings module names that are obviously not the deployed configuration. A
# DEBUG=True in dev.py is correct; the same line in production.py is the finding.
DEV_SETTINGS_NAMES = frozenset({"dev", "development", "local", "test", "testing", "ci"})


class ClassInfo:
    """One class definition, with the facts detectors ask about."""

    __slots__ = ("name", "file", "line", "bases", "methods", "node", "meta", "fields",
                 "assignments", "decorators")

    def __init__(self, name, file, line, bases, methods, node, meta, fields,
                 assignments, decorators):
        self.name = name
        self.file = file
        self.line = line
        self.bases = bases            # [str] — last component of each base expression
        self.methods = methods        # {name: ast.FunctionDef}
        self.node = node              # the ClassDef itself
        self.meta = meta              # {assigned name: ast node} from an inner Meta
        self.fields = fields          # {attr: field type} for Model field assignments
        self.assignments = assignments  # {attr: ast node} for every class-level assign
        self.decorators = decorators  # [str] — last component of each decorator

    def __repr__(self):
        return "<ClassInfo " + self.name + " " + str(self.file) + ":" + str(self.line) + ">"


class DjangoContext:
    """The project's class graph, plus the roots detectors need."""

    def __init__(self, root):
        self.root = root
        self.classes = {}          # name -> ClassInfo (first definition wins)
        self.duplicates = set()    # names defined more than once: resolution is unsafe
        self.subclasses = {}       # base name -> [subclass names]
        self.settings_files = []
        self.template_dirs = []
        self.migration_files = []
        self.urls_files = []
        self.test_files = []
        self.files = []
        self.version = None
        self.version_source = ""
        self.imports = {}          # module path -> set of names imported from it
        self._trees = {}           # path -> parsed AST, so each file is parsed once

    # ---- parsing ----------------------------------------------------------- #

    def parsed(self, path):
        """The AST for ``path``, parsed at most once per run."""
        return self._trees.get(path)

    def python_trees(self):
        """(path, tree) for every parseable Python file in the project."""
        for path in self.files:
            tree = self._trees.get(path)
            if tree is not None:
                yield path, tree

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

    # ---- version ----------------------------------------------------------- #

    def at_least(self, major, minor):
        """Whether the project is on ``major.minor`` or newer.

        False when the version is unknown. Every version-gated finding is
        therefore silent rather than speculative — the report says the version
        could not be determined instead of assuming the newest.
        """
        if self.version is None:
            return False
        return self.version >= (major, minor)

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

    @property
    def admins(self):
        return {n: c for n, c in self.classes.items() if self.derives_from(n, ADMIN_BASES)}

    @property
    def serializers(self):
        return {n: c for n, c in self.classes.items() if self.derives_from(n, SERIALIZER_BASES)}

    @property
    def viewsets(self):
        return {n: c for n, c in self.classes.items()
                if self.derives_from(n, VIEWSET_BASES | API_VIEW_BASES)}

    @property
    def app_configs(self):
        return {n: c for n, c in self.classes.items() if self.derives_from(n, CONFIG_BASES)}

    @property
    def uses_drf(self):
        """Whether Django REST Framework is in play at all.

        The DRF detector reports nothing without this: firing DRF rules at a
        project that does not use it is exactly the noise the Django gate exists
        to prevent, one level down.
        """
        return any(module.startswith("rest_framework") for module in self.imports)

    @property
    def production_settings(self):
        """Settings modules that are plausibly the deployed configuration.

        A DEBUG=True in dev.py is correct. Narrowing the settings rules to the
        modules that are not obviously local is what keeps them from reporting
        every project's development file as a vulnerability.
        """
        production = [p for p in self.settings_files if p.stem.lower() not in DEV_SETTINGS_NAMES]
        # A project with only dev-named settings modules still has to be checked
        # against something, so fall back to all of them rather than none.
        return production or list(self.settings_files)

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


def decorator_name(node):
    """The name of a decorator, whether or not it is called."""
    return base_name(node.func) if isinstance(node, ast.Call) else base_name(node)


def keyword(call, name):
    """The value node of a keyword argument, or None."""
    if not isinstance(call, ast.Call):
        return None
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def is_true(node):
    return isinstance(node, ast.Constant) and node.value is True


def string_value(node):
    """The value of a string constant, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def attribute_chain(node):
    """The attribute/method names in an expression, outermost first."""
    names = []
    current = node
    while True:
        if isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Attribute):
            names.append(current.attr)
            current = current.value
        elif isinstance(current, ast.Name):
            names.append(current.id)
            break
        else:
            break
    return names


def source_of(ctx, path, node):
    """The source text of a node, for the checks that read more cheaply than they walk."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    start = node.lineno - 1
    end = getattr(node, "end_lineno", node.lineno) or node.lineno
    return "\n".join(lines[start:end])


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


def _class_assignments(class_node):
    """Every class-level `name = value`, whatever the value is."""
    assignments = {}
    for item in class_node.body:
        if isinstance(item, ast.Assign):
            for target in item.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = item.value
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            assignments[item.target.id] = item.value
    return assignments


def _field_assignments(class_node):
    """Class-level ``name = models.SomeField(...)`` assignments."""
    fields = {}
    for item in class_node.body:
        if isinstance(item, ast.Assign) and isinstance(item.value, ast.Call):
            kind = call_name(item.value)
            if kind and (kind.endswith("Field") or kind in RELATION_FIELDS):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        fields[target.id] = kind
    return fields


def field_calls(info):
    """(attr, kind, call node) for every model field declared on this class."""
    for stmt in info.node.body:
        if not (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)):
            continue
        kind = call_name(stmt.value)
        targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
        if kind and targets:
            yield targets[0], kind, stmt.value


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


def _record_imports(ctx, tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            ctx.imports.setdefault(node.module, set()).update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                ctx.imports.setdefault(alias.name, set())


def _classify(ctx, path, root):
    """File it under every role it plays. A file can be more than one."""
    parts = path.relative_to(root).parts if path.is_relative_to(root) else path.parts
    name = path.name

    if name == "settings.py" or path.parent.name == "settings" or name.startswith("settings"):
        ctx.settings_files.append(path)
    if "migrations" in parts and name != "__init__.py":
        ctx.migration_files.append(path)
    if name == "urls.py" or name.startswith("urls"):
        ctx.urls_files.append(path)
    if name.startswith("test_") or name == "tests.py" or "tests" in parts:
        ctx.test_files.append(path)


def build_context(root, quiet=False):
    """Build the project graph, or return None when this is not a Django project."""
    root = Path(root)
    if not looks_like_django(root):
        if not quiet:
            print("⚠️  " + str(root) + ": no Django project found (no manage.py, no django imports) — "
                  "reporting nothing rather than applying Django rules to arbitrary Python.",
                  file=sys.stderr)
        return None

    ctx = DjangoContext(root)
    ctx.version, ctx.version_source = detect_django_version(root)

    for path in find_python_files(root):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, ValueError, OSError) as exc:
            warn_unparseable(path, exc)
            continue

        ctx.files.append(path)
        ctx._trees[path] = tree
        _classify(ctx, path, root)
        _record_imports(ctx, tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [b for b in (base_name(b) for b in node.bases) if b]
            info = ClassInfo(
                name=node.name, file=path, line=node.lineno, bases=bases,
                methods={n.name: n for n in node.body
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))},
                node=node, meta=_meta_assignments(node), fields=_field_assignments(node),
                assignments=_class_assignments(node),
                decorators=[d for d in (decorator_name(d) for d in node.decorator_list) if d],
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
    """Every template under a templates/ directory.

    .txt and .eml are included because email bodies are templates too, and an
    unescaped variable in a text template is the same bug in a quieter place.
    """
    seen = set()
    for directory in ctx.template_dirs:
        for pattern in ("*.html", "*.htm", "*.xhtml", "*.txt", "*.eml"):
            for path in directory.rglob(pattern):
                if path not in seen:
                    seen.add(path)
                    yield path
