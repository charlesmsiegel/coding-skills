#!/usr/bin/env python3
"""Model definition problems: missing __str__, null on text fields, unnamed relations.

These are cheap to fix and expensive to leave, because a model definition is the
one thing in a Django project that everything else is built on top of — and
several of them (a missing related_name, a nullable CharField) get harder to
change once there is data.

Which is also the standing caveat on this whole file: a model field is a database
column. Nothing here is a pure refactor. Generate the migration, read it, and
check it says what you expected before applying any of these.
"""

import ast
import sys

from django_context import (ClassInfo, MODEL_BASES, RELATION_FIELDS, TEXT_FIELDS,
                            _class_assignments, _meta_assignments, base_name, field_calls,
                            is_true, keyword, string_value)
from django_report import candidate, finding, run

FAT_MODEL_METHODS = 20
TOO_MANY_FIELDS = 25

# Fields whose stored value is a file path, not the file.
FILE_FIELDS = frozenset({"FileField", "ImageField"})


def _save_extends_update_fields(method):
    """Whether a save() override cooperates with update_fields.

    Since 4.2, update_or_create() passes update_fields down to save(). An
    override that computes a field and does not add it to update_fields has its
    write silently dropped — the object in memory is right and the row is not.
    """
    for node in ast.walk(method):
        # Either mentioning update_fields at all, or refusing kwargs entirely.
        if isinstance(node, ast.Constant) and node.value == "update_fields":
            return True
        if isinstance(node, ast.Name) and node.id == "update_fields":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "update_fields":
            return True
    return False


def _save_assigns_fields(method):
    """Whether save() computes a field before delegating (`self.x = ...`)."""
    for node in ast.walk(method):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                        and target.value.id == "self"):
                    return True
    return False


def _concrete_model_parents(ctx, info):
    """Project-defined model bases that are NOT abstract — multi-table inheritance."""
    parents = []
    for base in info.bases:
        if base in MODEL_BASES:
            continue
        if base in ctx.models and not ctx.is_abstract(base) and not ctx.is_ambiguous(base):
            parents.append(base)
    return parents


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return prefix + "." + node.attr if prefix else None
    return None


def _base_import(ctx, info, expression):
    """Resolve a base expression through imports visible before this class."""
    dotted = _dotted(expression)
    if not dotted:
        return False
    first, separator, rest = dotted.partition(".")
    tree = ctx.parsed(info.file)
    canonical = None
    for statement in tree.body if tree else ():
        if statement.lineno >= info.line:
            break
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if (alias.asname or alias.name.split(".")[0]) == first:
                    canonical = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                if (alias.asname or alias.name) == first:
                    canonical = ("." * statement.level + (statement.module + "." if statement.module else "")
                                 + alias.name)
        elif isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if statement.name == first:
                canonical = None
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if any(isinstance(target, ast.Name) and target.id == first for target in targets):
                canonical = None
    canonical = canonical + (separator + rest if separator else "") if canonical else None
    return canonical


def _parent_class(ctx, info, expression, canonical):
    """Resolve exact top-level definitions; unrelated duplicate names are harmless.

    Re-exports, dynamic bases and star imports remain unresolved rather than
    selecting an arbitrary project class with the same final name.
    """
    if canonical:
        module, _, name = canonical.rpartition(".")
        if canonical.startswith("."):
            level = len(canonical) - len(canonical.lstrip("."))
            relative_module, _, name = canonical[level:].rpartition(".")
            directory = info.file.parent
            for _ in range(level - 1):
                directory = directory.parent
            target = directory.joinpath(*relative_module.split(".")) if relative_module else directory
        elif module:
            target = ctx.root.joinpath(*module.split("."))
        else:
            return None
        paths = (target.with_suffix(".py"), target / "__init__.py")
    elif isinstance(expression, ast.Name):
        name, paths = expression.id, (info.file,)
    else:
        return None
    for path in paths:
        tree = ctx.parsed(path)
        if tree is None:
            continue
        nodes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
        if len(nodes) != 1:
            continue
        node = nodes[0]
        return ClassInfo(
            name=name, file=path, line=node.lineno,
            bases=[base_name(base) for base in node.bases],
            methods={child.name: child for child in node.body
                     if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))},
            node=node, meta=_meta_assignments(node), fields={},
            assignments=_class_assignments(node), decorators=list(node.decorator_list),
        )
    return None


def _model_fact(ctx, info, fact, seen=None):
    """Return present/absent/unknown, never treating unresolved ancestry as absence.

    This is an absence check, not a replacement for Python's full MRO: any known
    implementation is enough to avoid asserting that one is missing, while
    duplicate names, cycles and imported bases prevent a definitive absence.
    """
    seen = set() if seen is None else seen
    identity = (info.file, info.line)
    if identity in seen:
        return "unknown"
    seen = seen | {identity}
    meta = next((item for item in info.node.body
                 if isinstance(item, ast.ClassDef) and item.name == "Meta"), None)
    if fact == "__str__":
        if fact in info.methods:
            return "present"
        if fact in info.assignments or info.decorators:
            return "unknown"
    else:
        for key in ("ordering", "order_with_respect_to"):
            if key not in info.meta:
                continue
            try:
                value = ast.literal_eval(info.meta[key])
            except (ValueError, TypeError):
                return "unknown"
            if value:
                return "present"
        # Explicitly empty ordering disables inherited ordering.
        if "ordering" in info.meta:
            return "absent"
        if meta is not None and meta.bases:
            return "unknown"
    states = []
    for expression in info.node.bases:
        canonical = _base_import(ctx, info, expression)
        if canonical in {"django.db.models.Model", "django.db.models.base.Model"}:
            states.append("absent")
            continue
        parent = _parent_class(ctx, info, expression, canonical)
        if parent is None:
            states.append("unknown")
            continue
        # An explicitly declared Meta does not inherit abstract-parent options
        # unless its own bases say so (handled conservatively above).
        if fact == "ordering" and meta is not None and is_true(parent.meta.get("abstract")):
            continue
        states.append(_model_fact(ctx, parent, fact, seen))
    if "present" in states:
        return "present"
    if "unknown" in states:
        return "unknown"
    return "absent"


def collect(ctx):
    findings = []

    for name, info in sorted(ctx.models.items(), key=lambda kv: (str(kv[1].file), kv[1].line)):
        path, abstract = info.file, ctx.is_abstract(name)

        str_status = _model_fact(ctx, info, "__str__")
        if not abstract and str_status == "unknown":
            findings.append(candidate(
                path, info.line, "missing_str_method",
                "model `" + name + "` has no local __str__; its complete inherited behavior "
                "could not be resolved",
                ["An imported or ambiguous ancestor may supply __str__.",
                 "A class decorator or assigned method may provide its display representation."],
                "medium"))
        elif not abstract and str_status == "absent":
            findings.append(finding(
                path, info.line, "missing_str_method",
                "model `" + name + "` has no __str__, so it renders as "
                "'" + name + " object (1)' in the admin, in logs, and in every error message",
                "Add a __str__ returning the field a human would use to identify the row.",
                "medium"))

        field_count = len(info.fields)
        if field_count > TOO_MANY_FIELDS:
            findings.append(finding(
                path, info.line, "too_many_fields",
                "model `" + name + "` declares " + str(field_count) + " fields",
                "Look for a group that is really a separate entity, or a set of columns "
                "that are only ever populated together, and split it out.",
                "low"))

        public_methods = [m for m in info.methods if not m.startswith("_")]
        if len(public_methods) > FAT_MODEL_METHODS:
            findings.append(finding(
                path, info.line, "fat_model",
                "model `" + name + "` has " + str(len(public_methods)) + " public methods",
                "Move the behaviour that is about a *process* rather than about this row "
                "into a service function; keep what genuinely belongs to the record.",
                "low"))

        ordering_status = _model_fact(ctx, info, "ordering")
        if not abstract and ordering_status == "unknown":
            findings.append(candidate(
                path, info.line, "no_default_ordering",
                "model `" + name + "` has no statically resolved default ordering",
                ["Ordering may be supplied by an imported base or inherited Meta options.",
                 "Queries may specify their own order_by() or not require stable ordering."],
                "low"))
        elif not abstract and ordering_status == "absent":
            findings.append(finding(
                path, info.line, "no_default_ordering",
                "model `" + name + "` has no Meta.ordering, so queries come back in whatever "
                "order the database chooses — pagination can repeat or skip rows",
                "Set Meta.ordering to a unique-enough key (usually '-created_at' plus 'pk').",
                "low"))

        # unique_together still works, but constraints express more and are where
        # Django is going: a UniqueConstraint takes a condition.
        if info.meta.get("unique_together") is not None:
            findings.append(finding(
                path, info.line, "unique_together_over_constraints",
                "model `" + name + "` uses Meta.unique_together, which cannot express a "
                "condition — 'unique among non-deleted rows' is not sayable with it",
                "Meta.constraints = [models.UniqueConstraint(fields=[...], name='...')], "
                "which also takes condition= and nulls_distinct=.",
                "low"))

        # Multi-table inheritance adds a hidden join to every query on the child.
        parents = _concrete_model_parents(ctx, info)
        if parents and not abstract:
            findings.append(finding(
                path, info.line, "multi_table_inheritance",
                "model `" + name + "` inherits from the concrete model `" + parents[0] + "`, so "
                "every query on it carries an implicit join and every save writes two tables",
                "If the parent is only there to share fields, make it abstract. If the two are "
                "genuinely separate records, use an explicit OneToOneField so the join is visible.",
                "medium"))

        # A save() override that computes a field but ignores update_fields loses
        # that write whenever the caller passes one — which update_or_create does.
        save = info.methods.get("save")
        if save is not None and _save_assigns_fields(save) and not _save_extends_update_fields(save):
            findings.append(finding(
                path, save.lineno, "save_ignores_update_fields",
                "`" + name + ".save()` computes a field but never mentions update_fields — when a "
                "caller passes update_fields (update_or_create does, since Django 4.2) the "
                "computed value is silently dropped",
                "Add the field to update_fields before delegating: "
                "if 'update_fields' in kwargs and kwargs['update_fields'] is not None: "
                "kwargs['update_fields'] = {*kwargs['update_fields'], 'computed'}.",
                "medium"))

        if not abstract and "get_absolute_url" not in info.methods:
            # Only worth saying when something actually links to this record.
            has_slug_or_detail = any(
                kind in ("SlugField",) or attr in ("slug",) for attr, kind, _ in field_calls(info))
            if has_slug_or_detail:
                findings.append(finding(
                    path, info.line, "missing_get_absolute_url",
                    "model `" + name + "` has a slug but no get_absolute_url, so every template "
                    "and redirect builds its URL by hand",
                    "Add get_absolute_url returning reverse('...', kwargs={'slug': self.slug}); "
                    "the admin and {% url %} both pick it up.",
                    "low"))

        for attr, kind, call in field_calls(info):
            if kind in TEXT_FIELDS and is_true(keyword(call, "null")):
                findings.append(finding(
                    path, call.lineno, "null_on_text_field",
                    "`" + name + "." + attr + "` is a " + kind + " with null=True, so 'no value' "
                    "is now two distinct states — NULL and '' — and every query has to handle both",
                    "Drop null=True and use blank=True with default='' instead.",
                    "medium"))

            if kind in RELATION_FIELDS:
                related = keyword(call, "related_name")
                if related is None:
                    findings.append(finding(
                        path, call.lineno, "missing_related_name",
                        "`" + name + "." + attr + "` has no related_name, so the reverse accessor "
                        "is the default `" + name.lower() + "_set` — renaming the model silently "
                        "changes it",
                        "Set related_name explicitly (e.g. related_name='" + name.lower() + "s').",
                        "low"))
                elif string_value(related) == "+":
                    findings.append(finding(
                        path, call.lineno, "related_name_disabled",
                        "`" + name + "." + attr + "` sets related_name='+', which removes the "
                        "reverse accessor entirely — nothing can get from the other side back here",
                        "Only keep this if the reverse direction is genuinely meaningless; "
                        "otherwise name it. Two FKs to the same model need distinct names, not none.",
                        "low"))

            if kind in ("ForeignKey", "OneToOneField"):
                if keyword(call, "on_delete") is None and not call.args[1:]:
                    findings.append(finding(
                        path, call.lineno, "missing_on_delete",
                        "`" + name + "." + attr + "` declares no on_delete",
                        "on_delete is required; choose CASCADE/PROTECT/SET_NULL deliberately — "
                        "the wrong one deletes or orphans rows.",
                        "high"))
                if is_true(keyword(call, "db_index")):
                    findings.append(finding(
                        path, call.lineno, "redundant_db_index_on_fk",
                        "`" + name + "." + attr + "` sets db_index=True on a relation, which "
                        "Django already indexes — this creates a second identical index",
                        "Drop db_index=True. Use Meta.indexes for a composite index if that is "
                        "what was wanted.",
                        "low"))

            if kind == "DecimalField":
                if keyword(call, "max_digits") is None or keyword(call, "decimal_places") is None:
                    findings.append(finding(
                        path, call.lineno, "decimal_without_precision",
                        "`" + name + "." + attr + "` is a DecimalField without both max_digits and "
                        "decimal_places, which Django requires and which decide how money rounds",
                        "Declare both — max_digits=10, decimal_places=2 for currency.",
                        "high"))

            if kind in FILE_FIELDS and keyword(call, "upload_to") is None:
                findings.append(finding(
                    path, call.lineno, "file_field_without_upload_to",
                    "`" + name + "." + attr + "` has no upload_to, so every upload lands directly "
                    "in MEDIA_ROOT and a collision overwrites",
                    "Set upload_to to a per-model path, or a callable that includes the pk or a "
                    "uuid.",
                    "low"))

            if is_true(keyword(call, "auto_now_add")) and keyword(call, "default") is not None:
                findings.append(finding(
                    path, call.lineno, "auto_now_add_with_default",
                    "`" + name + "." + attr + "` sets both auto_now_add and default — auto_now_add "
                    "always wins, so the default is dead code that reads as if it applies",
                    "Keep auto_now_add and delete the default, or use "
                    "default=timezone.now if the value should be overridable.",
                    "low"))

            choices = keyword(call, "choices")
            if isinstance(choices, (ast.List, ast.Tuple)) and choices.elts:
                findings.append(finding(
                    path, call.lineno, "inline_choices",
                    "`" + name + "." + attr + "` defines choices as a literal, so the valid values "
                    "have no names and every comparison is a bare string",
                    "Use a TextChoices/IntegerChoices class and reference its members. "
                    "Since Django 5.0 you can pass the class itself: choices=Status.",
                    "low"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_model_issues", "Django model issues", collect))
