#!/usr/bin/env python3
"""Django-specific security problems, mostly in settings and template escaping.

python-simplifier's find_security_issues.py covers the language-level hazards
(eval, shell=True, weak hashes, hardcoded secrets in general). This covers the
ones that only exist because it is Django: a production DEBUG, a wildcard
ALLOWED_HOSTS, and mark_safe on something that is not a literal.
"""

import ast
import sys

from django_context import call_name
from django_report import finding, run

# Settings whose insecure value is a specific constant.
INSECURE_TRUE = {
    "DEBUG": ("debug_true",
              "DEBUG=True serves a full traceback with local variables and settings to "
              "anyone who triggers an error",
              "Read it from the environment and default to False."),
    "DEBUG_PROPAGATE_EXCEPTIONS": ("debug_propagate",
                                   "DEBUG_PROPAGATE_EXCEPTIONS=True leaks tracebacks to clients",
                                   "Leave it False outside local debugging."),
}
INSECURE_FALSE = {
    "SESSION_COOKIE_SECURE": ("insecure_session_cookie",
                              "SESSION_COOKIE_SECURE=False lets the session cookie travel over HTTP",
                              "Set it True wherever the site is served over HTTPS."),
    "CSRF_COOKIE_SECURE": ("insecure_csrf_cookie",
                           "CSRF_COOKIE_SECURE=False lets the CSRF cookie travel over HTTP",
                           "Set it True wherever the site is served over HTTPS."),
}
SECRET_NAMES = ("SECRET_KEY", "SECRET_KEY_FALLBACKS", "DB_PASSWORD", "AWS_SECRET_ACCESS_KEY")


def _is_literal(node):
    """A constant, or a concatenation/join of constants — safe to mark_safe."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.JoinedStr):     # an f-string always interpolates something
        return not any(isinstance(v, ast.FormattedValue) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_literal(node.left) and _is_literal(node.right)
    return False


def _assignments(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    yield target.id, node.value, node.lineno


def collect(ctx):
    findings = []
    settings_paths = {p for p in ctx.settings_files}

    for path in ctx.files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and call_name(node) in ("mark_safe", "SafeString"):
                if node.args and not _is_literal(node.args[0]):
                    findings.append(finding(
                        path, node.lineno, "mark_safe_on_dynamic_value",
                        "mark_safe() on a value that is not a literal turns off escaping for "
                        "whatever that expression produces — if any part of it reaches user "
                        "input, this is stored XSS",
                        "Use format_html('<p>{}</p>', value), which escapes the arguments and "
                        "trusts only the template string.",
                        "high"))

        if path not in settings_paths:
            continue

        for name, value, line in _assignments(tree):
            if name in INSECURE_TRUE and isinstance(value, ast.Constant) and value.value is True:
                smell, description, suggestion = INSECURE_TRUE[name]
                findings.append(finding(path, line, smell, description, suggestion, "high"))

            if name in INSECURE_FALSE and isinstance(value, ast.Constant) and value.value is False:
                smell, description, suggestion = INSECURE_FALSE[name]
                findings.append(finding(path, line, smell, description, suggestion, "medium"))

            if name in SECRET_NAMES and isinstance(value, ast.Constant) \
                    and isinstance(value.value, str) and value.value.strip():
                findings.append(finding(
                    path, line, "hardcoded_secret",
                    f"{name} is a literal in the settings file, so it is in version control "
                    f"and in every clone of this repository",
                    "Read it from the environment; rotate the value that was committed.",
                    "high"))

            if name == "ALLOWED_HOSTS" and isinstance(value, (ast.List, ast.Tuple)):
                for element in value.elts:
                    if isinstance(element, ast.Constant) and element.value in ("*", ".*"):
                        findings.append(finding(
                            path, line, "wildcard_allowed_hosts",
                            "ALLOWED_HOSTS=['*'] accepts any Host header, which enables "
                            "cache poisoning and password-reset links pointing at an "
                            "attacker's domain",
                            "List the hostnames this deployment actually serves.",
                            "medium"))

            if name == "CORS_ALLOW_ALL_ORIGINS" and isinstance(value, ast.Constant) \
                    and value.value is True:
                findings.append(finding(
                    path, line, "cors_allow_all",
                    "CORS_ALLOW_ALL_ORIGINS=True lets any site read authenticated responses",
                    "List the origins that need access.",
                    "medium"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_django_security", "Django security issues", collect))
