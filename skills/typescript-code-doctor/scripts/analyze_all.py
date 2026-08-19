#!/usr/bin/env python3
"""
Run every detector over a TypeScript project and merge the output into one report.
"""

import argparse
import io
import json
import sys
from datetime import datetime
from pathlib import Path

from common import SEVERITY_ICONS, configure_output
from runner import default_jobs, run_detectors

# category -> (module, progress label, kind). One row per detector analyze_all
# runs. `kind` is how the row is scheduled, not what it looks for: FILE detectors
# answer from a single parsed file and are sharded across the pool, TREE
# detectors need the whole project at once and share one load of it.
FILE, TREE = "file", "tree"

ANALYZERS = [
    ("tsconfig", "find_tsconfig_issues", "Auditing tsconfig strictness", TREE),
    ("type_gaps", "find_type_gaps", "Finding type-safety escape hatches", FILE),
    ("async_issues", "find_async_issues", "Finding promise bugs", FILE),
    ("complexity", "analyze_complexity", "Measuring complexity", FILE),
    ("code_smells", "find_code_smells", "Finding code smells", FILE),
    ("encapsulation", "find_encapsulation_issues", "Finding encapsulation failures", FILE),
    ("mutation_hazards", "find_mutation_hazards", "Finding mutation hazards", FILE),
    ("exception_issues", "find_exception_issues", "Finding error-handling problems", FILE),
    ("resource_leaks", "find_resource_leaks", "Finding resource leaks", FILE),
    ("security", "find_security_issues", "Finding security risks", FILE),
    ("design_smells", "find_design_smells", "Finding design smells", FILE),
    ("coupling", "find_coupling_issues", "Analyzing coupling/cohesion", FILE),
    ("overengineering", "find_overengineering", "Detecting over-engineering", TREE),
    ("loop_simplifications", "find_loop_simplifications", "Finding loop simplifications", FILE),
    ("outdated_idioms", "find_outdated_idioms", "Finding outdated idioms", FILE),
    ("naming_issues", "find_naming_issues", "Finding naming issues", FILE),
    ("comment_smells", "find_comment_smells", "Finding comment smells", FILE),
    ("debug_leftovers", "find_debug_leftovers", "Finding debug leftovers", FILE),
    ("ai_scaffolding", "find_ai_scaffolding", "Finding unfinished scaffolding", FILE),
    ("module_issues", "find_module_issues", "Finding import cycles / barrels", TREE),
    ("dependency_issues", "find_dependency_issues", "Reconciling package.json", TREE),
    ("dead_code", "find_dead_code", "Finding dead code", TREE),
    ("untested_modules", "find_untested_modules", "Finding untested modules", TREE),
    ("test_smells", "find_test_smells", "Finding test smells", FILE),
    ("duplicates", "find_duplicates", "Finding duplication", TREE),
]

CATEGORIES = [category for category, _, _, _ in ANALYZERS]

# Advice keyed by category, printed when that category has any finding.
RECOMMENDATIONS = {
    "tsconfig": "• Turn on `strict` (and `noUncheckedIndexedAccess`) — every other finding is cheaper afterwards",
    "type_gaps": "• Close the escape hatches — `any`, `as`, `!` and `@ts-ignore` are where the type system stops helping",
    "async_issues": "• Fix the promise bugs first — floating promises and async callbacks that never wait are real defects",
    "complexity": "• Reduce complexity — extract functions, use early returns, flatten nesting",
    "code_smells": "• Address the everyday smells — `==`, `var`, nested ternaries, switches with no default",
    "encapsulation": "• Close the leaks — `private`/`readonly` on fields, no exported mutable bindings",
    "mutation_hazards": "• Stop mutating things you do not own — arguments, imports, and shared constants",
    "exception_issues": "• Fix error handling — a swallowed error is a wrong answer, not a survived failure",
    "resource_leaks": "• Pair every acquire with a release; effects need cleanup functions",
    "security": "• Triage the security leads — eval, HTML sinks, shell interpolation, secrets",
    "design_smells": "• Replace type switches with discriminated unions; bundle data clumps into types",
    "coupling": "• Split low-cohesion classes and move envious methods to the data they use",
    "overengineering": "• Delete abstractions with one implementation (YAGNI)",
    "loop_simplifications": "• Replace hand-rolled loops with the array method that names the intent",
    "outdated_idioms": "• Modernize — ESM over CommonJS, `?.`/`??`, `const`, no `namespace`",
    "naming_issues": "• Fix names — PascalCase types, camelCase values, no shadowed globals",
    "comment_smells": "• Delete commented-out code; move TODOs into the tracker",
    "debug_leftovers": "• Remove `debugger`, console noise and blanket lint suppressions",
    "ai_scaffolding": "• Finish or delete the scaffolding — stubs, ignored options, duplicate definitions",
    "module_issues": "• Break import cycles, drop barrel files, alias the deep relative paths",
    "dependency_issues": "• Reconcile package.json — declare what you import, drop what you do not",
    "dead_code": "• Delete unreachable code and unused exports",
    "untested_modules": "• Build the safety net first — characterize untested modules before refactoring",
    "test_smells": "• Fix hollow tests — assertions that cannot fail are worse than no test",
    "duplicates": "• Extract genuine duplication; leave coincidental similarity alone",
}


def generate_report(path: str, skip: set | None = None, jobs: int | None = None) -> dict:
    skip = skip or set()
    scheduled = [row for row in ANALYZERS if row[0] not in skip]
    for _, _, label, _ in scheduled:
        print(f"🔍 {label}...", file=sys.stderr)

    results = run_detectors(
        path,
        [(category, module) for category, module, _, kind in scheduled if kind == FILE],
        [(category, module) for category, module, _, kind in scheduled if kind == TREE],
        jobs=jobs,
    )
    # Report in the table's order, not the order the pool happened to finish in.
    results = {category: results[category] for category, _, _, _ in scheduled}

    report = {
        "meta": {
            "analyzed_path": path,
            "timestamp": datetime.now().isoformat(),
            "analyzers_run": list(results.keys()),
            "analyzers_skipped": sorted(skip),
            # category -> error string for every analyzer that did not complete.
            # A zero count for one of these categories means "unknown", not "clean".
            "analyzer_errors": {},
        },
        "summary": {
            "total_issues": 0,
            "by_severity": {"high": 0, "medium": 0, "low": 0},
            "by_category": {},
        },
        "categories": {},
    }

    for category, data in results.items():
        issues = []
        if isinstance(data, list):
            issues = data
        elif isinstance(data, dict):
            issues = data.get("issues", [])
            if data.get("error"):
                report["meta"]["analyzer_errors"][category] = str(data["error"])

        normalized = []
        for issue in issues:
            if isinstance(issue, dict):
                issue.setdefault("severity", "medium")
                issue["category"] = category
                normalized.append(issue)

        report["categories"][category] = {"issues": normalized, "count": len(normalized)}
        report["summary"]["total_issues"] += len(normalized)
        report["summary"]["by_category"][category] = len(normalized)
        for issue in normalized:
            severity = issue.get("severity", "medium")
            if severity in report["summary"]["by_severity"]:
                report["summary"]["by_severity"][severity] += 1

    return report


def print_text_report(report: dict) -> None:
    meta, summary = report["meta"], report["summary"]

    print("\n" + "=" * 70)
    print("📊 TYPESCRIPT CODE ANALYSIS REPORT")
    print("=" * 70)
    print(f"Path: {meta['analyzed_path']}")
    print(f"Time: {meta['timestamp']}")
    print()

    print("📈 SUMMARY")
    print("-" * 40)
    print(f"Total issues found: {summary['total_issues']}")
    print()
    print("By severity:")
    for severity, count in summary["by_severity"].items():
        if count:
            print(f"  {SEVERITY_ICONS[severity]} {severity.upper()}: {count}")
    print()
    print("By category:")
    for category, count in sorted(summary["by_category"].items(), key=lambda item: -item[1]):
        if count:
            print(f"  {category}: {count}")
    print()

    analyzer_errors = meta.get("analyzer_errors") or {}
    if analyzer_errors:
        print("⚠️  ANALYSIS INCOMPLETE — these analyzers did not finish; their")
        print("    categories show what was found before failure, not a clean bill:")
        for category, error in sorted(analyzer_errors.items()):
            print(f"    • {category}: {error}")
        print()

    if summary["total_issues"] == 0:
        if analyzer_errors:
            print("No issues found by the analyzers that completed (see warnings above).")
        else:
            print("✅ No issues found! Your code looks great!")
        return

    print("=" * 70)
    print("🔴 HIGH SEVERITY ISSUES")
    print("=" * 70)
    high = [issue for data in report["categories"].values()
            for issue in data["issues"] if issue.get("severity") == "high"]
    if not high:
        print("None found!")
    else:
        for issue in high[:25]:
            print(f"\n📍 {issue.get('file', '?')}:{issue.get('line', '?')}")
            print(f"   [{issue['category']}] {issue.get('smell_type', '?')}")
            if issue.get("description"):
                print(f"   {issue['description']}")
            if issue.get("suggestion"):
                print(f"   → {issue['suggestion']}")
        if len(high) > 25:
            print(f"\n... and {len(high) - 25} more high severity issues")

    print()
    print("=" * 70)
    print("💡 RECOMMENDATIONS")
    print("=" * 70)
    advice = [RECOMMENDATIONS[category] for category in CATEGORIES
              if summary["by_category"].get(category, 0) and category in RECOMMENDATIONS]
    for line in advice or ["• Your code is in good shape! Consider minor improvements."]:
        print(line)
    print()


def main():
    configure_output()
    parser = argparse.ArgumentParser(
        description="Comprehensive TypeScript code analyzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Runs every detector and merges the output:
  - tsconfig strictness, type-safety escape hatches (any / as / ! / @ts-ignore)
  - promise bugs (floating promises, async callbacks that never wait)
  - complexity, code smells, design smells, coupling and cohesion
  - encapsulation failures, mutation hazards, error handling, resource leaks
  - security leads, over-engineering, dated idioms, naming
  - import cycles and barrels, dependency hygiene, dead code
  - safety net (untested modules, test smells) and duplication

Examples:
  %(prog)s .                      # analyze the current project
  %(prog)s src/ --format json     # JSON for tooling
  %(prog)s . --skip duplicates    # drop the slowest category
  %(prog)s . --jobs 1             # analyse in this process, no pool
        """,
    )
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--skip", type=str, default="",
                        help="Comma-separated analyzer categories to skip "
                             f"(choices: {', '.join(CATEGORIES)})")
    parser.add_argument("--skip-duplicates", action="store_true",
                        help="Shorthand for --skip duplicates (the slowest analyzer)")
    parser.add_argument("--jobs", "-j", type=int, default=None,
                        help="Worker processes to analyse with "
                             f"(default: {default_jobs()}; 1 runs in this process)")
    parser.add_argument("--output", "-o", type=str, help="Output file")
    args = parser.parse_args()

    if args.jobs is not None and args.jobs < 1:
        parser.error("--jobs must be at least 1")

    skip = set(args.skip.split(",")) if args.skip else set()
    if args.skip_duplicates:
        skip.add("duplicates")
    unknown = skip - set(CATEGORIES)
    if unknown:
        parser.error(f"--skip names unknown categories: {', '.join(sorted(unknown))}")

    report = generate_report(args.path, skip=skip, jobs=args.jobs)

    if args.format == "json":
        output = json.dumps(report, indent=2)
    else:
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        print_text_report(report)
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Report saved to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
