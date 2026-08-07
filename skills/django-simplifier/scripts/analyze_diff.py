#!/usr/bin/env python3
"""Review what a change did, not the legacy around it.

For an AI-written feature or a CR, running the whole-project analyzer buries the
twenty lines that changed under four hundred findings about code nobody touched.
This runs the file-level detectors and keeps only the findings that land on
added or modified lines.

The context is still built from the **whole project**, because that is what makes
the findings true: whether a loop is an N+1 depends on model definitions in
another file, and whether a mixin is used once depends on the rest of the tree.
Only the *reporting* is narrowed.

Some detectors cannot be narrowed at all — over-engineering ("used once" is a
whole-tree fact), settings, migrations-graph conflicts, the test-suite-wide query
count, and the version sweep. Those are listed as skipped rather than silently
dropped, so the report does not read as if they came back clean.

Usage:
  python analyze_diff.py                  # working tree vs. merge-base with the default branch
  python analyze_diff.py origin/main      # vs. an explicit base ref
  python analyze_diff.py --format json
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from common import SEVERITY_ICONS, configure_output, warn_detector_error
from django_context import build_context
from django_report import sort_findings

import find_admin_issues
import find_async_issues
import find_django_security
import find_drf_issues
import find_form_issues
import find_model_issues
import find_query_issues
import find_template_issues
import find_transaction_issues
import find_view_issues

# Detectors whose findings are anchored to a line in a file, so they can be
# filtered to the diff.
LINE_ANCHORED = {
    "query": find_query_issues.collect,
    "model": find_model_issues.collect,
    "view": find_view_issues.collect,
    "security": find_django_security.collect,
    "templates": find_template_issues.collect,
    "forms": find_form_issues.collect,
    "admin": find_admin_issues.collect,
    "drf": find_drf_issues.collect,
    "async": find_async_issues.collect,
    "transactions": find_transaction_issues.collect,
}
# Whole-tree questions a diff cannot answer. Named in the output, never hidden.
WHOLE_TREE_ONLY = [
    ("overengineering", "'used once' and 'extended by nobody' are whole-tree facts"),
    ("settings", "a settings gap is about the project, not about a changed line"),
    ("migrations", "leaf-migration conflicts are a property of the whole graph"),
    ("tests", "the query-count check is about the suite, not one test"),
    ("version", "the upgrade sweep is about the project's Django, not the diff"),
]


def _git(args, cwd="."):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120)
    return result.stdout if result.returncode == 0 else ""


def _default_branch(cwd="."):
    head = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd).strip()
    if head:
        return head.rsplit("/", 1)[-1]
    for candidate in ("main", "master"):
        if _git(["rev-parse", "--verify", candidate], cwd).strip():
            return candidate
    return "main"


def _base_ref(explicit, cwd="."):
    if explicit:
        return explicit
    branch = _default_branch(cwd)
    merge_base = _git(["merge-base", "HEAD", branch], cwd).strip()
    return merge_base or branch


def changed_lines(base, cwd="."):
    """{absolute path: {line numbers added or modified}} from the unified diff."""
    diff = _git(["diff", "--unified=0", base], cwd)
    if not diff:
        # No committed base to compare with, or nothing changed against it —
        # fall back to what is uncommitted.
        diff = _git(["diff", "--unified=0", "HEAD"], cwd)
    root = Path(cwd).resolve()

    changes = defaultdict(set)
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = (root / line[6:]).resolve()
        elif line.startswith("@@") and current is not None:
            # @@ -old,count +new,count @@
            try:
                new_part = line.split("+", 1)[1].split(" ", 1)[0]
                start, _, count = new_part.partition(",")
                start = int(start)
                count = int(count) if count else 1
            except (ValueError, IndexError):
                continue
            for offset in range(count):
                changes[current].add(start + offset)
    return changes


def analyze(path, base, ignore=()):
    ctx = build_context(path)
    if ctx is None:
        return [], None

    changes = changed_lines(base, path)
    if not changes:
        return [], {"changed_files": 0, "base": base, "skipped": WHOLE_TREE_ONLY}

    ignored = {i.strip() for i in ignore if i.strip()}
    findings = []
    for name, collect in LINE_ANCHORED.items():
        try:
            for f in collect(ctx):
                touched = changes.get(Path(f["file"]).resolve())
                if touched and f["line"] in touched:
                    findings.append(f)
        except Exception as exc:
            warn_detector_error(name, exc)

    stats = {"changed_files": len(changes), "base": base, "skipped": WHOLE_TREE_ONLY}
    return sort_findings([f for f in findings if f["smell_type"] not in ignored]), stats


def render(findings, stats):
    lines = ["\n🎸 DJANGO DIFF REVIEW", "=" * 66]
    if stats:
        lines.append(str(stats["changed_files"]) + " changed file(s) vs. " + stats["base"])
        lines.append("")
        lines.append("not run (these need the whole tree — use analyze_django.py):")
        for name, why in stats["skipped"]:
            lines.append("  " + name + " — " + why)
    lines.append("")
    if not findings:
        lines.append("✅ No findings on the changed lines.")
        return "\n".join(lines)

    by_severity = defaultdict(int)
    for f in findings:
        by_severity[f["severity"]] += 1
    lines.append(str(len(findings)) + " finding(s) on changed lines  (" +
                 SEVERITY_ICONS["high"] + " " + str(by_severity["high"]) + "  " +
                 SEVERITY_ICONS["medium"] + " " + str(by_severity["medium"]) + "  " +
                 SEVERITY_ICONS["low"] + " " + str(by_severity["low"]) + ")\n")
    for f in findings[:200]:
        lines.append(SEVERITY_ICONS[f["severity"]] + " [" + f["severity"].upper() + "] " +
                     str(f["file"]) + ":" + str(f["line"]) + "  " + f["smell_type"])
        lines.append("   " + f["description"])
        lines.append("   → " + f["suggestion"])
    return "\n".join(lines)


def main():
    configure_output()
    parser = argparse.ArgumentParser(
        description="Run the Django detectors against changed lines only")
    parser.add_argument("base", nargs="?", default=None,
                        help="Base ref (default: merge-base with the default branch)")
    parser.add_argument("--path", default=".", help="Project root")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", default="", help="Comma-separated smell types to drop")
    args = parser.parse_args()

    base = _base_ref(args.base, args.path)
    findings, stats = analyze(args.path, base, args.ignore.split(","))
    print(json.dumps(findings, indent=2) if args.format == "json" else render(findings, stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
