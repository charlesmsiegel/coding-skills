#!/usr/bin/env python3
"""Model definition problems: missing __str__, null on text fields, unnamed relations.

These are cheap to fix and expensive to leave, because a model definition is the
one thing in a Django project that everything else is built on top of — and
several of them (a missing related_name, a nullable CharField) get harder to
change once there is data.
"""

import ast
import sys

from django_context import RELATION_FIELDS, TEXT_FIELDS, call_name
from django_report import finding, run

FAT_MODEL_METHODS = 20
TOO_MANY_FIELDS = 25


def _keyword(call, name):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_true(node):
    return isinstance(node, ast.Constant) and node.value is True


def collect(ctx):
    findings = []

    for name, info in sorted(ctx.models.items(), key=lambda kv: (str(kv[1].file), kv[1].line)):
        path, abstract = info.file, ctx.is_abstract(name)

        if "__str__" not in info.methods and not abstract:
            findings.append(finding(
                path, info.line, "missing_str_method",
                f"model `{name}` has no __str__, so it renders as "
                f"'{name} object (1)' in the admin, in logs, and in every error message",
                "Add a __str__ returning the field a human would use to identify the row.",
                "medium"))

        field_count = len(info.fields)
        if field_count > TOO_MANY_FIELDS:
            findings.append(finding(
                path, info.line, "too_many_fields",
                f"model `{name}` declares {field_count} fields",
                "Look for a group that is really a separate entity, or a set of columns "
                "that are only ever populated together, and split it out.",
                "low"))

        public_methods = [m for m in info.methods if not m.startswith("_")]
        if len(public_methods) > FAT_MODEL_METHODS:
            findings.append(finding(
                path, info.line, "fat_model",
                f"model `{name}` has {len(public_methods)} public methods",
                "Move the behaviour that is about a *process* rather than about this row "
                "into a service function; keep what genuinely belongs to the record.",
                "low"))

        if not abstract and not info.meta.get("ordering") and not info.meta.get("order_with_respect_to"):
            # Only worth saying when something actually paginates or slices it.
            findings.append(finding(
                path, info.line, "no_default_ordering",
                f"model `{name}` has no Meta.ordering, so queries come back in whatever "
                f"order the database chooses — pagination can repeat or skip rows",
                "Set Meta.ordering to a unique-enough key (usually '-created_at' plus 'pk').",
                "low"))

        for stmt in info.node.body:
            if not (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)):
                continue
            kind = call_name(stmt.value)
            targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            if not targets or not kind:
                continue
            attr = targets[0]

            if kind in TEXT_FIELDS and _is_true(_keyword(stmt.value, "null")):
                findings.append(finding(
                    path, stmt.lineno, "null_on_text_field",
                    f"`{name}.{attr}` is a {kind} with null=True, so 'no value' is now two "
                    f"distinct states — NULL and '' — and every query has to handle both",
                    "Drop null=True and use blank=True with default='' instead.",
                    "medium"))

            if kind in RELATION_FIELDS and _keyword(stmt.value, "related_name") is None:
                findings.append(finding(
                    path, stmt.lineno, "missing_related_name",
                    f"`{name}.{attr}` has no related_name, so the reverse accessor is the "
                    f"default `{name.lower()}_set` — renaming the model silently changes it",
                    f"Set related_name explicitly (e.g. related_name='{name.lower()}s').",
                    "low"))

            if kind in ("ForeignKey", "OneToOneField") and _keyword(stmt.value, "on_delete") is None:
                findings.append(finding(
                    path, stmt.lineno, "missing_on_delete",
                    f"`{name}.{attr}` declares no on_delete",
                    "on_delete is required; choose CASCADE/PROTECT/SET_NULL deliberately — "
                    "the wrong one deletes or orphans rows.",
                    "high"))

            choices = _keyword(stmt.value, "choices")
            if isinstance(choices, (ast.List, ast.Tuple)) and choices.elts:
                findings.append(finding(
                    path, stmt.lineno, "inline_choices",
                    f"`{name}.{attr}` defines choices as a literal, so the valid values have "
                    f"no names and every comparison is a bare string",
                    "Use a TextChoices/IntegerChoices class and reference its members.",
                    "low"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_model_issues", "Django model issues", collect))
