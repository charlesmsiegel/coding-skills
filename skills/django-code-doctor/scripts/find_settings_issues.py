#!/usr/bin/env python3
"""Settings and project-layout problems, including the one that is unfixable later.

`AUTH_USER_MODEL` is the finding that justifies this file. Django's documentation
is unusually blunt about it — set a custom user model at the start of a project,
even if the default looks sufficient — because changing it after the first
migration means rewriting every foreign key to `auth.User` in every historical
migration. This is the one Django decision with no cheap escape hatch, and the
detector reports it whether or not anything is currently wrong, because "nothing
is wrong yet" is precisely the window in which it is fixable.

Everything else here is ordinary hygiene: middleware that is missing or in the
wrong order, a development backend left in a production settings module, and
settings that import models (which runs the app registry before Django is ready).

Scoped to settings modules, and among those preferring the ones that are not
obviously local — a SQLite database in dev.py is correct.
"""

import ast
import sys

from django_context import call_name, string_value
from django_report import finding, run

# Middleware Django's own project template ships, and what each one does.
REQUIRED_MIDDLEWARE = {
    "SecurityMiddleware": "the security headers (HSTS, SSL redirect, content-type nosniff)",
    "CsrfViewMiddleware": "CSRF protection on every POST",
    "AuthenticationMiddleware": "request.user — without it, every auth check reads an attribute "
                               "that is not there",
    "XFrameOptionsMiddleware": "clickjacking protection",
    "CommonMiddleware": "APPEND_SLASH and the Host-header check",
}
# Middleware whose order relative to another is load-bearing.
ORDERING_RULES = [
    ("SecurityMiddleware", "SessionMiddleware",
     "SecurityMiddleware sets the headers and does the SSL redirect; anything above it runs on "
     "requests that were about to be redirected anyway"),
    ("SessionMiddleware", "AuthenticationMiddleware",
     "AuthenticationMiddleware reads the session, so the session has to be loaded first"),
    ("AuthenticationMiddleware", "MessageMiddleware",
     "the messages framework attaches to the user, so authentication has to have run"),
]
DEV_ONLY_DATABASE_BACKENDS = ("sqlite3",)
DEV_ONLY_CACHE_BACKENDS = ("locmem", "dummy")


def _module_assignments(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    yield target.id, node.value, node.lineno


def _string_elements(node):
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    return [s for s in (string_value(e) for e in node.elts) if s is not None]


def _short_names(entries):
    """['a.b.CsrfViewMiddleware'] -> ['CsrfViewMiddleware'], order preserved."""
    return [entry.rsplit(".", 1)[-1] for entry in entries]


def _dict_string_values(node):
    """Every string appearing anywhere inside a (possibly nested) dict literal."""
    found = []
    for sub in ast.walk(node):
        text = string_value(sub)
        if text is not None:
            found.append(text)
    return found


def _assigned_across(ctx, paths):
    """{name: (path, line, value)} for every module-level assignment in the settings."""
    assigned = {}
    for path in paths:
        tree = ctx.parsed(path)
        if tree is None:
            continue
        for name, value, line in _module_assignments(tree):
            assigned[name] = (path, line, value)
    return assigned


def collect(ctx):
    findings = []
    settings_paths = list(ctx.settings_files)
    if not settings_paths:
        return findings

    assigned = _assigned_across(ctx, settings_paths)
    production = ctx.production_settings
    anchor = production[0] if production else settings_paths[0]

    # ---- the one that cannot be fixed later --------------------------------- #
    if "AUTH_USER_MODEL" not in assigned:
        findings.append(finding(
            anchor, 1, "default_user_model",
            "no AUTH_USER_MODEL, so this project uses django.contrib.auth.User — a decision that "
            "is free to change today and very expensive to change once there is data, because "
            "every historical migration holds a foreign key to auth.User",
            "Even if the default looks sufficient, define a custom user model now: "
            "class User(AbstractUser): pass, then AUTH_USER_MODEL = 'accounts.User'. "
            "Django's own docs recommend this for every new project. If the project is already "
            "live on the default, this is a much larger job — see references/django-migrations.md "
            "before starting it.",
            "medium" if ctx.migration_files else "high"))

    if "DEFAULT_AUTO_FIELD" not in assigned:
        findings.append(finding(
            anchor, 1, "missing_default_auto_field",
            "no DEFAULT_AUTO_FIELD — before Django 6.0 that means new models get a 32-bit "
            "AutoField, which runs out at 2.1 billion rows",
            "Set DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'. (From Django 6.0 this is "
            "the default, so on 6.0+ the setting is only needed to opt back out.)",
            "low" if ctx.at_least(6, 0) else "medium"))

    if "USE_TZ" not in assigned and not ctx.at_least(5, 0):
        findings.append(finding(
            anchor, 1, "missing_use_tz",
            "no USE_TZ — before Django 5.0 it defaults to False, so datetimes are stored naive "
            "and every comparison across a DST boundary is wrong by an hour",
            "Set USE_TZ = True and store aware datetimes. (Django 5.0 made True the default.)",
            "medium"))

    # ---- middleware ---------------------------------------------------------- #
    if "MIDDLEWARE" in assigned:
        path, line, value = assigned["MIDDLEWARE"]
        entries = _short_names(_string_elements(value))
        position = {name: index for index, name in enumerate(entries)}

        missing = [n for n in REQUIRED_MIDDLEWARE if n not in position]
        if missing:
            findings.append(finding(
                path, line, "missing_security_middleware",
                "MIDDLEWARE is missing " + ", ".join(missing) + " — " +
                REQUIRED_MIDDLEWARE[missing[0]],
                "Restore it from Django's project template. Middleware removed to 'simplify' the "
                "list is the most common way a project loses CSRF protection.",
                "high"))

        for first, second, why in ORDERING_RULES:
            if first in position and second in position and position[first] > position[second]:
                findings.append(finding(
                    path, line, "middleware_order",
                    second + " is listed before " + first + " — " + why,
                    "Reorder them. MIDDLEWARE is applied top-down on the request and bottom-up on "
                    "the response, so the order is behaviour, not style.",
                    "high"))

    # ---- development backends in a production module -------------------------- #
    for path in production:
        tree = ctx.parsed(path)
        if tree is None:
            continue
        for name, value, line in _module_assignments(tree):
            if name == "DATABASES" and isinstance(value, ast.Dict):
                for text in _dict_string_values(value):
                    if any(backend in text for backend in DEV_ONLY_DATABASE_BACKENDS):
                        findings.append(finding(
                            path, line, "sqlite_in_production",
                            "the production settings module configures SQLite, which has no "
                            "concurrent writer and no network access — every write serialises "
                            "and the data lives on one container's disk",
                            "Point this at the real database, read from the environment. If this "
                            "module is not actually the deployed one, name it so.",
                            "high"))
                        break
            if name == "CACHES" and isinstance(value, ast.Dict):
                for text in _dict_string_values(value):
                    if any(backend in text for backend in DEV_ONLY_CACHE_BACKENDS):
                        findings.append(finding(
                            path, line, "locmem_cache_in_production",
                            "the production cache is per-process, so each worker has its own "
                            "copy — invalidation in one does not reach the others, and the hit "
                            "rate falls with every worker added",
                            "Use a shared backend (Redis or Memcached) anywhere there is more "
                            "than one process.",
                            "medium"))
                        break

    # ---- settings that reach into the app ------------------------------------- #
    for path in settings_paths:
        tree = ctx.parsed(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.endswith("models") or ".models" in node.module:
                    findings.append(finding(
                        path, node.lineno, "settings_imports_models",
                        "the settings module imports models, which touches the app registry "
                        "before Django has populated it — the error it raises "
                        "(AppRegistryNotReady) names the symptom rather than this line",
                        "Import lazily inside the function that needs it, or refer to the model "
                        "by its 'app.Model' string, which every Django setting accepts.",
                        "high"))

            # BASE_DIR is a Path; joining it with os.path is a leftover from 3.0.
            if isinstance(node, ast.Call) and call_name(node) == "join":
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                if "BASE_DIR" in names:
                    findings.append(finding(
                        path, node.lineno, "ospath_join_basedir",
                        "os.path.join(BASE_DIR, ...) — BASE_DIR has been a pathlib.Path since the "
                        "Django 3.1 project template, so this converts it to a string to do what "
                        "the / operator already does",
                        "Write BASE_DIR / 'subdir' / 'file'.",
                        "low"))

    # ---- deprecated storage settings ------------------------------------------ #
    for name in ("DEFAULT_FILE_STORAGE", "STATICFILES_STORAGE"):
        if name in assigned:
            path, line, _value = assigned[name]
            findings.append(finding(
                path, line, "deprecated_storage_setting",
                name + " was removed in Django 5.1 — on 5.1 or newer it is silently ignored, so "
                "the storage backend it names is simply not used",
                "Move it into STORAGES: STORAGES = {'default': {'BACKEND': ...}, "
                "'staticfiles': {'BACKEND': ...}}.",
                "high" if ctx.at_least(5, 1) else "medium"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_settings_issues", "Django settings and project layout", collect))
