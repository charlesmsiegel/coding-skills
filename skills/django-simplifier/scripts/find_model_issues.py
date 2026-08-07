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

from django_context import (MODEL_BASES, RELATION_FIELDS, TEXT_FIELDS, field_calls,
                            is_true, keyword, string_value)
from django_report import finding, run

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


def collect(ctx):
    findings = []

    for name, info in sorted(ctx.models.items(), key=lambda kv: (str(kv[1].file), kv[1].line)):
        path, abstract = info.file, ctx.is_abstract(name)

        if "__str__" not in info.methods and not abstract:
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

        if not abstract and not info.meta.get("ordering") and not info.meta.get("order_with_respect_to"):
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
