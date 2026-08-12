#!/usr/bin/env python3
"""The shared shape of a Django detector: one CLI, one findings format, one report.

Every detector here is the same program with a different `collect` function —
build the project context, gather findings, drop the ignored types, sort, print.
Writing that six times would mean six chances for the flags to drift apart, and
the whole point of a shared findings shape is that `format_findings.py` can read
any of them.

A detector is:

    def collect(ctx): -> [finding, ...]
    if __name__ == "__main__":
        sys.exit(run("find_x", "what it looks for", collect))

Records match python-code-doctor's shape exactly: file, line, smell_type,
description, suggestion, severity. Unproven leads additionally carry
``kind: candidate`` so code-overview can show them without grading them.
"""

import json
import argparse
from collections import defaultdict

from common import SEVERITY_ICONS, configure_output
from django_context import build_context

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def finding(file, line, smell_type, description, suggestion, severity):
    return {
        "file": str(file),
        "line": line or 1,
        "smell_type": smell_type,
        "description": description,
        "suggestion": suggestion,
        "severity": severity,
    }


def candidate(file, line, smell_type, description, suggestion, severity, also_caused_by=()):
    record = finding(file, line, smell_type, description, suggestion, severity)
    record["kind"] = "candidate"
    record["also_caused_by"] = list(also_caused_by)
    return record


def render(title, findings, limit=200):
    lines = [f"\n🎸 {title.upper()}", "=" * 66]
    if not findings:
        lines.append("✅ Nothing found.")
        return "\n".join(lines)

    proven = [record for record in findings if record.get("kind") != "candidate"]
    candidates = [record for record in findings if record.get("kind") == "candidate"]
    by_severity = defaultdict(int)
    by_type = defaultdict(int)
    for f in proven:
        by_severity[f["severity"]] += 1
    for f in findings:
        by_type[f["smell_type"]] += 1

    lines.append(f"{len(proven)} finding(s), {len(candidates)} candidate(s)  "
                 f"({SEVERITY_ICONS['high']} {by_severity['high']}  "
                 f"{SEVERITY_ICONS['medium']} {by_severity['medium']}  "
                 f"{SEVERITY_ICONS['low']} {by_severity['low']})\n")
    for smell_type, count in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {smell_type}: {count}")
    lines.append("")

    for f in findings[:limit]:
        marker = ("? [CANDIDATE]" if f.get("kind") == "candidate"
                  else f"{SEVERITY_ICONS[f['severity']]} [{f['severity'].upper()}]")
        lines.append(f"{marker} "
                     f"{f['file']}:{f['line']}  {f['smell_type']}")
        lines.append(f"   {f['description']}")
        lines.append(f"   → {f['suggestion']}")
    if len(findings) > limit:
        lines.append(f"\n... and {len(findings) - limit} more")
    return "\n".join(lines)


def sort_findings(findings):
    return sorted(findings, key=lambda f: (SEVERITY_RANK.get(f["severity"], 1),
                                           str(f["file"]), f["line"], f["smell_type"]))


def collect_findings(collect, path, ignore=(), quiet=False):
    """Run one detector's collect over ``path``. Returns a sorted findings list.

    An empty list here means one of two things — a clean project, or not a Django
    project at all. build_context says which on stderr; it must never be reported
    as a clean bill of health silently.
    """
    ctx = build_context(path, quiet=quiet)
    if ctx is None:
        return []
    ignored = {t.strip() for t in ignore if t.strip()}
    return sort_findings([f for f in collect(ctx) if f["smell_type"] not in ignored])


def run(name, description, collect):
    configure_output()
    parser = argparse.ArgumentParser(prog=name, description=description)
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", default="", help="Comma-separated smell types to drop")
    args = parser.parse_args()

    findings = collect_findings(collect, args.path, args.ignore.split(","))
    print(json.dumps(findings, indent=2) if args.format == "json"
          else render(description, findings))
    return 0
