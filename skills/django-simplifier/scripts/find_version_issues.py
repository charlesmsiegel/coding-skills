#!/usr/bin/env python3
"""What breaks between the Django this project is on and the one you want.

This is the upgrade detector. It answers one question — *what in this project
does not survive the target version?* — and derives the severity from the
answer rather than assigning it by hand:

    removed at or before the target  -> 🔴  the project will not run
    deprecated already               -> 🟡  works today, on a clock
    removal scheduled after target   -> 🟢  fix it while you are here

Everything it knows lives in django_versions.CHANGES. This file is only the
matcher: it walks the tree once, asks each change whether it appears, and turns
the hits into findings. Adding Django 6.2 or 7.0 is therefore a data edit next
door, not a change here.

Two refusals worth knowing about. When the project's Django version cannot be
determined, nothing version-conditional is reported and the report says why —
a deprecation warning built on a guessed version is worse than silence. And
when the target is newer than the table knows, it says so on stderr instead of
implying the check was complete.

Usage:
  python find_version_issues.py .
  python find_version_issues.py . --target 5.2
  python find_version_issues.py . --from 4.2 --target 6.0
  python find_version_issues.py . --list-known
"""

import argparse
import ast
import json
import re
import sys

from common import configure_output
from django_context import build_context, call_name, template_files
from django_report import finding, render, sort_findings
import django_versions as V

# `{{ value|length_is:"4" }}` and `{% if x|length_is:'4' %}` alike.
_FILTER_RE = re.compile(r"\|\s*([a-zA-Z_][\w]*)")


def _version_text(version):
    return str(version[0]) + "." + str(version[1])


def _smell_and_severity(change, current, target):
    """The finding type and severity both fall out of the version arithmetic.

    Order matters. "Already deprecated" is checked before "will be removed",
    because a construct that is emitting a DeprecationWarning on the version
    the project runs today is a different (and more urgent) thing from one
    whose clock has not started.
    """
    if change.removed_in is not None and target is not None and change.removed_in <= target:
        return "removed_in_target", "high"
    already_warning = (change.deprecated_in is not None and current is not None
                       and change.deprecated_in <= current)
    if already_warning:
        return "deprecated_api", "medium"
    return "future_removal", "low"


def _describe(change, current, target):
    """One sentence that names the construct and the release that takes it away."""
    parts = ["`" + change.name + "`"]
    if change.removed_in is not None and target is not None and change.removed_in <= target:
        parts.append("was removed in Django " + _version_text(change.removed_in) +
                     ", so this project will not run on " + _version_text(target))
    elif change.removed_in is not None:
        parts.append("is deprecated" +
                     (" since Django " + _version_text(change.deprecated_in) if change.deprecated_in else "") +
                     " and is removed in Django " + _version_text(change.removed_in))
    else:
        parts.append("is deprecated since Django " + _version_text(change.deprecated_in))
    return " ".join(parts)


def _suggest(change):
    text = "Use " + change.replacement + "."
    if change.note:
        text += " " + change.note
    return text


# --------------------------------------------------------------------------- #
# Matchers — one per match["kind"]. Each yields the line numbers where the
# construct appears in one file.
# --------------------------------------------------------------------------- #

def _match_setting(change, tree, is_settings_module):
    # Scoped to settings modules on purpose: USE_L10N in application code is a
    # local variable, and flagging it is how a version report loses its reader.
    if not is_settings_module:
        return
    names = set(V.all_names(change))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    yield node.lineno


def _match_import(change, tree):
    module = change.match.get("module")
    names = set(V.all_names(change))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if module and node.module != module:
                continue
            for alias in node.names:
                if alias.name in names:
                    yield node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if module and alias.name == module + "." + next(iter(names)):
                    yield node.lineno
    # `timezone.utc` after `from django.utils import timezone` — the import line
    # is innocent, the use site is the finding.
    if module:
        tail = module.rsplit(".", 1)[-1]
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr in names
                    and isinstance(node.value, ast.Name) and node.value.id == tail):
                yield node.lineno


def _match_call(change, tree):
    names = set(V.all_names(change))
    wants_no_args = change.match.get("no_args", False)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if call_name(node) not in names:
            continue
        if wants_no_args and (node.args or node.keywords):
            continue
        yield node.lineno


def _match_kwarg(change, tree):
    call_names = {change.match["call"]} | set(change.aliases)
    keyword_name = change.match["name"]
    requires_no_positional = change.match.get("requires_no_positional", False)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or call_name(node) not in call_names:
            continue
        if requires_no_positional and node.args:
            continue
        for kw in node.keywords:
            if kw.arg == keyword_name:
                yield node.lineno


def _match_meta_option(change, tree):
    names = set(V.all_names(change))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "Meta"):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id in names:
                        yield stmt.lineno


def _match_attribute(change, tree):
    names = set(V.all_names(change))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in names:
            yield node.lineno
        elif isinstance(node, ast.Name) and node.id in names:
            yield node.lineno


def _match_template_filter(change, text):
    names = set(V.all_names(change))
    for number, line in enumerate(text.splitlines(), start=1):
        for match in _FILTER_RE.finditer(line):
            if match.group(1) in names:
                yield number


def _python_hits(change, tree, is_settings_module):
    kind = change.match["kind"]
    if kind == "setting":
        return _match_setting(change, tree, is_settings_module)
    if kind == "import":
        return _match_import(change, tree)
    if kind == "call":
        return _match_call(change, tree)
    if kind == "kwarg":
        return _match_kwarg(change, tree)
    if kind == "meta_option":
        return _match_meta_option(change, tree)
    if kind == "attribute":
        return _match_attribute(change, tree)
    return iter(())


# --------------------------------------------------------------------------- #

def collect(ctx, target=None, declared_from=None):
    """Findings for a move from the project's current Django to ``target``."""
    findings = []
    current = declared_from if declared_from is not None else ctx.version
    target = target or V.CURRENT_RELEASE

    if current is None:
        findings.append(finding(
            ctx.root, 1, "django_version_unknown",
            "the project's Django version could not be determined (" + ctx.version_source + "), so "
            "nothing version-conditional was checked",
            "Pin Django in pyproject.toml or requirements.txt and re-run; "
            "until then treat this report as covering only version-independent findings.",
            "medium"))
    elif V.is_end_of_life(current):
        findings.append(finding(
            ctx.root, 1, "django_end_of_life",
            "Django " + V.describe(current) + " no longer receives security fixes, so this project is "
            "running unpatched against every advisory published since its end of life",
            "Upgrade to " + V.describe(V.CURRENT_LTS) + " (the LTS) or " +
            V.describe(V.CURRENT_RELEASE) + " (current). This finding subsumes every other one in "
            "this report — an unpatched framework is a likelier way in than anything the detectors found.",
            "high"))

    # Everything below is conditional on knowing where we are.
    if current is None:
        return findings

    relevant = V.changes_between(current, target)
    settings_paths = set(ctx.settings_files)

    for path, tree in ctx.python_trees():
        is_settings_module = path in settings_paths
        for change in relevant:
            if change.match["kind"] == "template_filter":
                continue
            if change.scope == "settings" and not is_settings_module:
                continue
            if change.scope == "templates":
                continue
            for line in sorted(set(_python_hits(change, tree, is_settings_module))):
                smell, severity = _smell_and_severity(change, current, target)
                findings.append(finding(
                    path, line, smell, _describe(change, current, target),
                    _suggest(change), severity))

    template_changes = [c for c in relevant
                        if c.match["kind"] == "template_filter" or c.scope == "templates"]
    if template_changes:
        for path in template_files(ctx):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for change in template_changes:
                for line in sorted(set(_match_template_filter(change, text))):
                    smell, severity = _smell_and_severity(change, current, target)
                    findings.append(finding(
                        path, line, smell, _describe(change, current, target),
                        _suggest(change), severity))

    return findings


def _parse_target(text):
    if not text or text.lower() == "unknown":
        return None
    return V.parse_version(text)


def main():
    configure_output()
    parser = argparse.ArgumentParser(
        prog="find_version_issues",
        description="Django constructs that do not survive the target version")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", default="", help="Comma-separated smell types to drop")
    parser.add_argument("--target", default=None,
                        help="Version to upgrade to (default: the newest this table knows)")
    parser.add_argument("--from", dest="from_version", default=None,
                        help="Override the detected current version ('unknown' to force the "
                             "unknown-version path)")
    parser.add_argument("--list-known", action="store_true",
                        help="Print what the change table covers, then exit")
    args = parser.parse_args()

    if args.list_known:
        print("django-simplifier version table")
        print("  newest release known: " + _version_text(V.LATEST_KNOWN))
        print("  current release:      " + V.describe(V.CURRENT_RELEASE))
        print("  current LTS:          " + V.describe(V.CURRENT_LTS))
        print("  changes tracked:      " + str(len(V.CHANGES)))
        print("")
        print("Releases and their security support:")
        for version in sorted(V.SUPPORTED):
            info = V.SUPPORTED[version]
            until = info["security_until"] or "ENDED - no security fixes"
            print("  " + V.describe(version).ljust(10) + " released " + info["released"] +
                  "   security: " + until)
        print("")
        print("For a target newer than " + _version_text(V.LATEST_KNOWN) + ", read")
        print("  https://docs.djangoproject.com/en/dev/releases/")
        print("  https://docs.djangoproject.com/en/dev/internals/deprecation/")
        return 0

    target = _parse_target(args.target) or V.CURRENT_RELEASE
    if target > V.LATEST_KNOWN:
        print("⚠️  this table does not know Django " + _version_text(target) +
              " (newest known: " + _version_text(V.LATEST_KNOWN) + "). Findings cover changes up to "
              "the known release only — read the release notes for the gap.", file=sys.stderr)

    declared_from = "unknown" if (args.from_version or "").lower() == "unknown" else None
    ctx = build_context(args.path)
    if ctx is None:
        findings = []
    else:
        override = None
        if args.from_version and declared_from is None:
            override = V.parse_version(args.from_version)
        if declared_from == "unknown":
            ctx.version = None
            ctx.version_source = "overridden with --from unknown"
        findings = collect(ctx, target=target, declared_from=override)

    ignored = {t.strip() for t in args.ignore.split(",") if t.strip()}
    findings = sort_findings([f for f in findings if f["smell_type"] not in ignored])

    if args.format == "json":
        print(json.dumps(findings, indent=2))
    else:
        if ctx is not None:
            print("Django " + V.describe(ctx.version) + " (from " + ctx.version_source + ")"
                  " → target " + _version_text(target))
        print(render("Django version issues", findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
