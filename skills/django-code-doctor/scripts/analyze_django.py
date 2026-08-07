#!/usr/bin/env python3
"""Run every Django detector over a project and merge the results into one report.

The project context is built ONCE and handed to all fifteen detectors, which is
the whole reason they share it: parsing a large Django project fifteen times to
ask fifteen questions is most of the runtime.

Usage:
  python analyze_django.py /path/to/project
  python analyze_django.py . --format json > report.json
  python analyze_django.py . --skip templates,overengineering
  python analyze_django.py . --ignore no_default_ordering,missing_related_name
  python analyze_django.py . --target-version 6.1

Output is a flat findings list in the same shape as python-code-doctor's detectors,
so it pipes straight into that skill's format_findings.py.
"""

import sys
import json
import argparse
from collections import defaultdict

from common import SEVERITY_ICONS, configure_output, warn_detector_error
from django_context import build_context, template_files
from django_report import sort_findings
import django_versions as V

import find_admin_issues
import find_async_issues
import find_django_overengineering
import find_django_security
import find_drf_issues
import find_form_issues
import find_migration_issues
import find_model_issues
import find_query_issues
import find_settings_issues
import find_template_issues
import find_test_issues
import find_transaction_issues
import find_version_issues
import find_view_issues

CATEGORIES = {
    "query": find_query_issues.collect,
    "model": find_model_issues.collect,
    "view": find_view_issues.collect,
    "security": find_django_security.collect,
    "overengineering": find_django_overengineering.collect,
    "templates": find_template_issues.collect,
    "forms": find_form_issues.collect,
    "admin": find_admin_issues.collect,
    "drf": find_drf_issues.collect,
    "migrations": find_migration_issues.collect,
    "settings": find_settings_issues.collect,
    "async": find_async_issues.collect,
    "transactions": find_transaction_issues.collect,
    "tests": find_test_issues.collect,
    # "version" is handled separately: it is the one detector that takes a target.
}


def analyze(path, skip=(), ignore=(), target=None):
    """Returns (findings, stats). Findings is empty when this is not a Django project."""
    ctx = build_context(path)
    if ctx is None:
        return [], None

    skipped = {s.strip() for s in skip if s.strip()}
    ignored = {i.strip() for i in ignore if i.strip()}

    findings = []
    for name, collect in CATEGORIES.items():
        if name in skipped:
            continue
        try:
            findings.extend(collect(ctx))
        except Exception as exc:  # one detector failing must not sink the report
            # Says "incomplete, not clean" — the distinction that matters when a
            # category is missing from a report someone is about to trust.
            warn_detector_error(name, exc)

    if "version" not in skipped:
        try:
            findings.extend(find_version_issues.collect(ctx, target=target or V.CURRENT_RELEASE))
        except Exception as exc:
            warn_detector_error("version", exc)

    stats = {
        "python_files": len(ctx.files),
        "django_version": V.describe(ctx.version),
        "django_version_source": ctx.version_source,
        "models": len(ctx.models),
        "abstract_models": sum(1 for n in ctx.models if ctx.is_abstract(n)),
        "managers": len(ctx.managers),
        # Function-based views have no base class, so this counts CBVs only.
        "class_based_views": len(ctx.views),
        "forms_and_serializers": len(ctx.forms),
        "admins": len(ctx.admins),
        "migrations": len(ctx.migration_files),
        "templates": len(list(template_files(ctx))),
        "uses_drf": ctx.uses_drf,
        "categories_run": sorted((set(CATEGORIES) | {"version"}) - skipped),
    }
    return sort_findings([f for f in findings if f["smell_type"] not in ignored]), stats


def render(findings, stats, target=None):
    lines = ["\n🎸 DJANGO ANALYSIS", "=" * 66]
    if stats:
        lines.append(str(stats["python_files"]) + " python file(s), " +
                     str(stats["models"]) + " model(s) (" +
                     str(stats["abstract_models"]) + " abstract), " +
                     str(stats["class_based_views"]) + " class-based view(s), " +
                     str(stats["migrations"]) + " migration(s), " +
                     str(stats["templates"]) + " template(s)")
        version_line = "Django " + stats["django_version"] + "  (from " + stats["django_version_source"] + ")"
        if target:
            version_line += "  → target " + str(target[0]) + "." + str(target[1])
        lines.append(version_line)
        if stats["uses_drf"]:
            lines.append("Django REST Framework detected")
        lines.append("ran: " + ", ".join(stats["categories_run"]))
    lines.append("")

    if not findings:
        lines.append("✅ No findings.")
        return "\n".join(lines)

    by_severity = defaultdict(int)
    by_type = defaultdict(int)
    for f in findings:
        by_severity[f["severity"]] += 1
        by_type[f["smell_type"]] += 1

    lines.append(str(len(findings)) + " finding(s)  (" +
                 SEVERITY_ICONS["high"] + " " + str(by_severity["high"]) + "  " +
                 SEVERITY_ICONS["medium"] + " " + str(by_severity["medium"]) + "  " +
                 SEVERITY_ICONS["low"] + " " + str(by_severity["low"]) + ")\n")
    for smell_type, count in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append("  " + smell_type + ": " + str(count))
    lines.append("")

    for f in findings[:300]:
        lines.append(SEVERITY_ICONS[f["severity"]] + " [" + f["severity"].upper() + "] " +
                     str(f["file"]) + ":" + str(f["line"]) + "  " + f["smell_type"])
        lines.append("   " + f["description"])
        lines.append("   → " + f["suggestion"])
    if len(findings) > 300:
        lines.append("\n... and " + str(len(findings) - 300) + " more")
    return "\n".join(lines)


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Run every Django detector")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--skip", default="",
                        help="Comma-separated categories to skip (" +
                             ", ".join(sorted(set(CATEGORIES) | {"version"})) + ")")
    parser.add_argument("--ignore", default="", help="Comma-separated smell types to drop")
    parser.add_argument("--target-version", default=None,
                        help="Django version to check upgrade readiness against "
                             "(default: " + str(V.CURRENT_RELEASE[0]) + "." +
                             str(V.CURRENT_RELEASE[1]) + ")")
    args = parser.parse_args()

    target = V.parse_version(args.target_version) if args.target_version else None
    if target and target > V.LATEST_KNOWN:
        print("⚠️  this table does not know Django " + str(target[0]) + "." + str(target[1]) +
              "; version findings cover changes up to " + str(V.LATEST_KNOWN[0]) + "." +
              str(V.LATEST_KNOWN[1]) + " only.", file=sys.stderr)

    findings, stats = analyze(args.path, args.skip.split(","), args.ignore.split(","), target)
    print(json.dumps(findings, indent=2) if args.format == "json"
          else render(findings, stats, target or V.CURRENT_RELEASE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
