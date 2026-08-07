#!/usr/bin/env python3
"""Migration problems — the ones that hurt at deploy time, not at review time.

A migration is the only code in a Django project that runs exactly once against
production data, usually unattended, usually while the previous release is still
serving traffic. That makes its failure modes specific:

- A data migration with no reverse cannot be rolled back, so a bad deploy has to
  be fixed forward under pressure.
- `RunPython` that imports models directly gets *today's* model, not the model
  as it was at that point in history. The migration works when written and
  breaks the next time someone runs it from zero — which is every CI run that
  builds a fresh database.
- A non-nullable column added without a default fails on any table with rows.

One thing this cannot check by parsing: whether the migrations match the models.
That needs `manage.py makemigrations --check --dry-run`, which
run_external_tools.py drives.
"""

import ast
import sys
from collections import defaultdict

from django_context import call_name, keyword, string_value
from django_report import finding, run

SCHEMA_OPERATIONS = frozenset({
    "CreateModel", "DeleteModel", "RenameModel", "AlterModelTable", "AddField",
    "RemoveField", "AlterField", "RenameField", "AddIndex", "RemoveIndex",
    "AddConstraint", "RemoveConstraint", "AlterUniqueTogether", "AlterIndexTogether",
    "AlterModelOptions", "AlterOrderWithRespectTo", "AlterModelManagers",
})
DATA_OPERATIONS = frozenset({"RunPython", "RunSQL"})
# Fields that supply their own value, so "no default" is not a problem.
SELF_POPULATING = frozenset({"AutoField", "BigAutoField", "SmallAutoField", "UUIDField"})


def _operations(tree):
    """The elements of the `operations = [...]` list in a Migration class."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Migration":
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            if "operations" in names and isinstance(stmt.value, (ast.List, ast.Tuple)):
                return [e for e in stmt.value.elts if isinstance(e, ast.Call)]
    return []


def _dependencies(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "Migration":
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            if "dependencies" in names and isinstance(stmt.value, (ast.List, ast.Tuple)):
                found = []
                for element in stmt.value.elts:
                    if isinstance(element, ast.Tuple) and len(element.elts) == 2:
                        app = string_value(element.elts[0])
                        name = string_value(element.elts[1])
                        if app and name:
                            found.append((app, name))
                return found
    return []


def _named_functions(tree):
    """Module-level functions, so a RunPython callable can be looked up."""
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _referenced_name(node):
    """`forwards` -> 'forwards'; `migrations.RunPython.noop` -> 'noop'."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _function_arg_name(node, index, keyword_name):
    """The name of a positional-or-keyword argument passed to a call.

    Both forms turn up in real migrations: `RunPython(forwards, backwards)` and
    `RunPython(code=forwards, reverse_code=migrations.RunPython.noop)` — and the
    noop is an attribute, not a bare name.
    """
    if len(node.args) > index:
        found = _referenced_name(node.args[index])
        if found:
            return found
    return _referenced_name(keyword(node, keyword_name))


def _uses_apps_get_model(func):
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and call_name(node) == "get_model":
            return True
    return False


def _touches_a_manager(func):
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and node.attr == "objects":
            return True
    return False


def collect(ctx):
    findings = []
    # (app directory) -> {migration name: [dependency names within the app]}
    by_app = defaultdict(list)

    for path in ctx.migration_files:
        tree = ctx.parsed(path)
        if tree is None:
            continue

        by_app[path.parent].append(path)
        operations = _operations(tree)
        functions = _named_functions(tree)

        kinds = {call_name(op) for op in operations}
        schema = kinds & SCHEMA_OPERATIONS
        data = kinds & DATA_OPERATIONS

        if schema and data:
            findings.append(finding(
                path, operations[0].lineno if operations else 1,
                "schema_and_data_in_one_migration",
                "this migration mixes schema changes (" + ", ".join(sorted(schema)[:3]) + ") with "
                "data changes (" + ", ".join(sorted(data)) + ") — on most backends the schema "
                "change takes a lock the data pass then holds for its whole duration, and a "
                "failure halfway leaves the two half-applied",
                "Split them: one migration alters the schema, the next backfills. That also lets "
                "the backfill be re-run or throttled without re-running the DDL.",
                "medium"))

        for operation in operations:
            name = call_name(operation)

            if name == "RunPython":
                forward = _function_arg_name(operation, 0, "code")
                reverse = _function_arg_name(operation, 1, "reverse_code")

                if reverse is None:
                    findings.append(finding(
                        path, operation.lineno, "run_python_without_reverse",
                        "RunPython with no reverse_code — this migration cannot be rolled back, so "
                        "a bad deploy has to be fixed forward while it is failing",
                        "Pass a reverse function, or RunPython.noop when the change genuinely "
                        "cannot be undone. noop is a decision on the record; the absence of an "
                        "argument is not.",
                        "medium"))

                func = functions.get(forward)
                if func is not None and _touches_a_manager(func) and not _uses_apps_get_model(func):
                    findings.append(finding(
                        path, func.lineno, "run_python_imports_model",
                        "`" + str(forward) + "` uses a model imported at the top of the file, which "
                        "is the model as it is *today* — not as it was at this point in history. "
                        "This works now and breaks the next time the migration runs from zero, "
                        "which is every CI run that builds a fresh database",
                        "Take the historical model from the registry: "
                        "Model = apps.get_model('app', 'Model') inside the function.",
                        "high"))

            if name == "RunSQL":
                reverse = keyword(operation, "reverse_sql")
                if reverse is None and len(operation.args) < 2:
                    findings.append(finding(
                        path, operation.lineno, "run_sql_without_reverse",
                        "RunSQL with no reverse_sql — irreversible, and raw SQL is exactly the "
                        "kind of change that most needs to be undoable",
                        "Pass reverse_sql, or RunSQL.noop to say deliberately that it cannot be "
                        "undone.",
                        "medium"))

            if name == "AddField":
                field = keyword(operation, "field")
                if isinstance(field, ast.Call):
                    kind = call_name(field)
                    nullable = keyword(field, "null")
                    has_default = (keyword(field, "default") is not None
                                   or keyword(field, "db_default") is not None)
                    is_nullable = isinstance(nullable, ast.Constant) and nullable.value is True
                    if (not is_nullable and not has_default and kind not in SELF_POPULATING):
                        findings.append(finding(
                            path, operation.lineno, "non_nullable_without_default",
                            "AddField adds a NOT NULL " + str(kind) + " with no default, which "
                            "fails on any table that already has rows",
                            "Give it a default, or add it nullable, backfill in a separate "
                            "migration, and only then make it NOT NULL. On a large table the "
                            "three-step version is also the one that does not hold a lock.",
                            "high"))

    # ---- two leaf migrations in one app cannot both be applied --------------- #
    for app_dir, paths in by_app.items():
        if len(paths) < 2:
            continue
        depended_on = set()
        names = {}
        for path in paths:
            tree = ctx.parsed(path)
            if tree is None:
                continue
            names[path.stem] = path
            for _app, dependency in _dependencies(tree):
                depended_on.add(dependency)
        leaves = sorted(set(names) - depended_on)
        if len(leaves) > 1:
            anchor = names[leaves[0]]
            findings.append(finding(
                anchor, 1, "conflicting_leaf_migrations",
                "app '" + app_dir.parent.name + "' has " + str(len(leaves)) + " leaf migrations (" +
                ", ".join(leaves) + ") — two branches added a migration and neither depends on "
                "the other, so migrate refuses to run",
                "Run makemigrations --merge, or rewrite the later migration's dependencies to "
                "point at the other leaf. Read the result: a merge is a claim that the two "
                "branches commute, and sometimes they do not.",
                "high"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_migration_issues", "Django migration issues", collect))
