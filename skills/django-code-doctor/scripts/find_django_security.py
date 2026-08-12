#!/usr/bin/env python3
"""Django-specific security problems, mostly in settings and template escaping.

python-code-doctor's find_security_issues.py covers the language-level hazards
(eval, shell=True, weak hashes, hardcoded secrets in general). This covers the
ones that only exist because it is Django: a production DEBUG, a wildcard
ALLOWED_HOSTS, mark_safe on something that is not a literal, and the security
headers whose absence is the finding.

Two scoping decisions keep this honest. Settings rules only fire in settings
modules — a DEBUG in application code is a local flag. And among settings
modules they prefer the ones that are not obviously local, because DEBUG=True in
dev.py is correct and reporting it trains people to ignore the real one.

Run `python manage.py check --deploy` alongside this. It knows about the
deployed settings module, which a parser cannot.
"""

import ast
import re
import sys

from django_context import call_name, string_value, template_files
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
    "SESSION_COOKIE_HTTPONLY": ("insecure_session_cookie",
                                "SESSION_COOKIE_HTTPONLY=False exposes the session cookie to JavaScript, "
                                "so any XSS becomes a session takeover",
                                "Leave it at its default of True."),
    "SECURE_SSL_REDIRECT": ("missing_ssl_redirect",
                            "SECURE_SSL_REDIRECT=False serves the site over plain HTTP when asked",
                            "Set it True, unless a proxy in front already redirects."),
}
SECRET_NAMES = ("SECRET_KEY", "SECRET_KEY_FALLBACKS", "DB_PASSWORD", "AWS_SECRET_ACCESS_KEY")

# Settings whose *absence* is the finding, and what each one is for.
EXPECTED_SETTINGS = {
    "SECURE_HSTS_SECONDS": (
        "missing_hsts",
        "no SECURE_HSTS_SECONDS, so a browser that has seen this site over HTTPS will still try "
        "HTTP next time and can be stripped down to it",
        "Set it once TLS is stable — start at 3600, then raise. It is hard to undo, because "
        "browsers cache the header for its full duration.",
        "medium"),
    "SECURE_SSL_REDIRECT": (
        "missing_ssl_redirect",
        "no SECURE_SSL_REDIRECT, so plain-HTTP requests are served rather than redirected",
        "Set it True behind TLS (or confirm the proxy in front already does it).",
        "medium"),
}
# Deliberately absent from the list above: CSRF_TRUSTED_ORIGINS and
# SESSION_COOKIE_SAMESITE. Both are correct to omit — a single-origin site needs
# no trusted origins, and SameSite defaults to Lax. Reporting them would fire on
# correctly-written projects, which is the failure mode this whole file is
# arranged to avoid. `manage.py check --deploy` covers the deployment-specific
# half; see references/django-security.md for the ones that need a reader.

# Password hashers weaker than the default, by name.
WEAK_HASHERS = ("MD5PasswordHasher", "UnsaltedMD5PasswordHasher", "SHA1PasswordHasher",
                "UnsaltedSHA1PasswordHasher", "CryptPasswordHasher")

_SAFE_FILTER = re.compile(r"\|\s*safe\b")
_AUTOESCAPE_OFF = re.compile(r"{%-?\s*autoescape\s+off")
_HTML_TEMPLATE_SUFFIXES = {".html", ".htm", ".xhtml"}


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


def _settings_names(ctx, paths):
    """Every name assigned across the given settings modules.

    Split settings mean a value is often set in base.py and overridden in
    production.py. Asking "is this set anywhere" rather than "is it in this
    file" is what stops the absence checks reporting every split-settings
    project.
    """
    names = set()
    for path in paths:
        tree = ctx.parsed(path)
        if tree is None:
            continue
        for name, _value, _line in _assignments(tree):
            names.add(name)
    return names


def _list_elements(node):
    if isinstance(node, (ast.List, ast.Tuple)):
        return node.elts
    return []


def collect(ctx):
    findings = []
    settings_paths = set(ctx.settings_files)
    production = ctx.production_settings

    for path, tree in ctx.python_trees():
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
        is_production = path in production

        for name, value, line in _assignments(tree):
            if name in INSECURE_TRUE and isinstance(value, ast.Constant) and value.value is True:
                smell, description, suggestion = INSECURE_TRUE[name]
                findings.append(finding(path, line, smell, description, suggestion,
                                        "high" if is_production else "low"))

            if name in INSECURE_FALSE and isinstance(value, ast.Constant) and value.value is False:
                smell, description, suggestion = INSECURE_FALSE[name]
                findings.append(finding(path, line, smell, description, suggestion, "medium"))

            if name in SECRET_NAMES and isinstance(value, ast.Constant) \
                    and isinstance(value.value, str) and value.value.strip():
                findings.append(finding(
                    path, line, "hardcoded_secret",
                    name + " is a literal in the settings file, so it is in version control "
                    "and in every clone of this repository",
                    "Read it from the environment; rotate the value that was committed.",
                    "high"))

            if name == "ALLOWED_HOSTS":
                for element in _list_elements(value):
                    if string_value(element) in ("*", ".*"):
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

            if name == "X_FRAME_OPTIONS" and string_value(value) not in (None, "DENY"):
                findings.append(finding(
                    path, line, "weak_frame_options",
                    "X_FRAME_OPTIONS is '" + str(string_value(value)) + "' rather than DENY, so "
                    "this site can be framed — which is what clickjacking needs",
                    "Use DENY unless something genuinely embeds these pages; then SAMEORIGIN, "
                    "or a frame-ancestors CSP directive for finer control.",
                    "medium"))

            if name == "AUTH_PASSWORD_VALIDATORS" and not _list_elements(value):
                findings.append(finding(
                    path, line, "no_password_validators",
                    "AUTH_PASSWORD_VALIDATORS is empty, so 'a' is an acceptable password",
                    "Restore the four defaults from the project template — length, common "
                    "passwords, numeric-only, and similarity to the username.",
                    "high"))

            if name == "PASSWORD_HASHERS":
                for element in _list_elements(value):
                    text = string_value(element) or ""
                    for weak in WEAK_HASHERS:
                        if text.endswith(weak):
                            findings.append(finding(
                                path, line, "weak_password_hasher",
                                weak + " is listed in PASSWORD_HASHERS; if it is first, every new "
                                "password is stored under a hash designed to be fast",
                                "Put PBKDF2 or Argon2 first. Keep a weak hasher in the list only "
                                "to let existing users log in once and be upgraded — removing it "
                                "outright locks them out.",
                                "high"))

            if name == "SECURE_PROXY_SSL_HEADER" and value is not None:
                findings.append(finding(
                    path, line, "spoofable_proxy_ssl_header",
                    "SECURE_PROXY_SSL_HEADER makes Django trust a header to decide whether the "
                    "request was HTTPS — if the proxy does not strip that header from client "
                    "requests, anyone can set it",
                    "Confirm the proxy overwrites (not merely sets) the header on every request. "
                    "If it does, this is correct; if it does not, this is a bypass.",
                    "medium"))

    # ---- settings whose absence is the finding ----------------------------- #
    if production:
        assigned = _settings_names(ctx, ctx.settings_files)
        anchor = production[0]
        for name, (smell, description, suggestion, severity) in EXPECTED_SETTINGS.items():
            if name not in assigned:
                findings.append(finding(anchor, 1, smell, description, suggestion, severity))

        # Content-Security-Policy became a first-class Django setting in 6.0.
        # Reporting its absence on 5.2 would be advice to use something that does
        # not exist, so this one is gated on the detected version.
        if ctx.at_least(6, 0) and "SECURE_CSP" not in assigned:
            findings.append(finding(
                anchor, 1, "missing_csp",
                "no SECURE_CSP, and this project is on Django " +
                str(ctx.version[0]) + "." + str(ctx.version[1]) +
                " where Content-Security-Policy is built in — a CSP is the control that turns a "
                "surviving XSS into a blocked script",
                "Add ContentSecurityPolicyMiddleware and set SECURE_CSP. Start with "
                "SECURE_CSP_REPORT_ONLY to find what breaks before enforcing.",
                "medium"))

    # ---- escaping turned off in templates ---------------------------------- #
    for path in template_files(ctx):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if _SAFE_FILTER.search(line):
                findings.append(finding(
                    path, number, "safe_filter_in_template",
                    "the |safe filter turns off escaping for this value — if any part of it "
                    "reaches user input, this is stored XSS",
                    "Escape at the source instead: format_html() in Python, or sanitise the HTML "
                    "with a library before it is stored. |safe on a value you did not construct "
                    "is a decision to trust whoever did.",
                    "high"))
            if path.suffix.lower() in _HTML_TEMPLATE_SUFFIXES and _AUTOESCAPE_OFF.search(line):
                findings.append(finding(
                    path, number, "autoescape_off",
                    "{% autoescape off %} disables escaping for a whole block, so every variable "
                    "in it is unescaped — including ones added later by someone who did not read "
                    "this line",
                    "Turn escaping off for the single value that needs it, not the block.",
                    "high"))

    return findings


if __name__ == "__main__":
    sys.exit(run("find_django_security", "Django security issues", collect))
