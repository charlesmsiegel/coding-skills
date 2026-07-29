#!/usr/bin/env python3
"""Run every Django detector over a project and merge the results into one report.

The project context is built ONCE and handed to all five Python detectors, which
is the whole reason they share it: parsing a large Django project six times to ask
six questions is most of the runtime.

Usage:
  python analyze_django.py /path/to/project
  python analyze_django.py . --format json > report.json
  python analyze_django.py . --skip templates,overengineering
  python analyze_django.py . --ignore no_default_ordering,missing_related_name

Output is a flat findings list in the same shape as python-simplifier's detectors,
so it pipes straight into that skill's format_findings.py.
"""

import sys
import json
import argparse
from collections import defaultdict

from common import SEVERITY_ICONS, configure_output, warn_detector_error
from django_context import build_context, template_files
from django_report import sort_findings

import find_django_overengineering
import find_django_security
import find_model_issues
import find_query_issues
import find_template_issues
import find_view_issues

CATEGORIES = {
    "query": find_query_issues.collect,
    "model": find_model_issues.collect,
    "view": find_view_issues.collect,
    "security": find_django_security.collect,
    "overengineering": find_django_overengineering.collect,
    "templates": find_template_issues.collect,
}


def analyze(path, skip=(), ignore=()):
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

    stats = {
        "python_files": len(ctx.files),
        "models": len(ctx.models),
        "abstract_models": sum(1 for n in ctx.models if ctx.is_abstract(n)),
        "managers": len(ctx.managers),
        # Function-based views have no base class, so this counts CBVs only.
        "class_based_views": len(ctx.views),
        "forms_and_serializers": len(ctx.forms),
        "templates": len(list(template_files(ctx))),
        "categories_run": sorted(set(CATEGORIES) - skipped),
    }
    return sort_findings([f for f in findings if f["smell_type"] not in ignored]), stats


def render(findings, stats):
    lines = ["\n🎸 DJANGO ANALYSIS", "=" * 66]
    if stats:
        lines.append(f"{stats['python_files']} python file(s), {stats['models']} model(s) "
                     f"({stats['abstract_models']} abstract), "
                     f"{stats['class_based_views']} class-based view(s), "
                     f"{stats['templates']} template(s)")
        lines.append(f"ran: {', '.join(stats['categories_run'])}")
    lines.append("")

    if not findings:
        lines.append("✅ No findings.")
        return "\n".join(lines)

    by_severity = defaultdict(int)
    by_type = defaultdict(int)
    for f in findings:
        by_severity[f["severity"]] += 1
        by_type[f["smell_type"]] += 1

    lines.append(f"{len(findings)} finding(s)  "
                 f"({SEVERITY_ICONS['high']} {by_severity['high']}  "
                 f"{SEVERITY_ICONS['medium']} {by_severity['medium']}  "
                 f"{SEVERITY_ICONS['low']} {by_severity['low']})\n")
    for smell_type, count in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {smell_type}: {count}")
    lines.append("")

    for f in findings[:300]:
        lines.append(f"{SEVERITY_ICONS[f['severity']]} [{f['severity'].upper()}] "
                     f"{f['file']}:{f['line']}  {f['smell_type']}")
        lines.append(f"   {f['description']}")
        lines.append(f"   → {f['suggestion']}")
    if len(findings) > 300:
        lines.append(f"\n... and {len(findings) - 300} more")
    return "\n".join(lines)


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Run every Django detector")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--skip", default="",
                        help=f"Comma-separated categories to skip ({', '.join(CATEGORIES)})")
    parser.add_argument("--ignore", default="", help="Comma-separated smell types to drop")
    args = parser.parse_args()

    findings, stats = analyze(args.path, args.skip.split(","), args.ignore.split(","))
    print(json.dumps(findings, indent=2) if args.format == "json" else render(findings, stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
