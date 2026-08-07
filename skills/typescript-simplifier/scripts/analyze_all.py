#!/usr/bin/env python3
"""
Run every detector over a TypeScript project and merge the output into one report.
"""

import argparse
import io
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from common import SEVERITY_ICONS, configure_output

# category -> (script, progress label). One row per detector analyze_all runs.
ANALYZERS = [
    ("tsconfig", "find_tsconfig_issues.py", "Auditing tsconfig strictness"),
    ("type_gaps", "find_type_gaps.py", "Finding type-safety escape hatches"),
    ("async_issues", "find_async_issues.py", "Finding promise bugs"),
    ("complexity", "analyze_complexity.py", "Measuring complexity"),
    ("code_smells", "find_code_smells.py", "Finding code smells"),
    ("encapsulation", "find_encapsulation_issues.py", "Finding encapsulation failures"),
    ("mutation_hazards", "find_mutation_hazards.py", "Finding mutation hazards"),
    ("exception_issues", "find_exception_issues.py", "Finding error-handling problems"),
    ("resource_leaks", "find_resource_leaks.py", "Finding resource leaks"),
    ("security", "find_security_issues.py", "Finding security risks"),
    ("design_smells", "find_design_smells.py", "Finding design smells"),
    ("coupling", "find_coupling_issues.py", "Analyzing coupling/cohesion"),
    ("overengineering", "find_overengineering.py", "Detecting over-engineering"),
    ("loop_simplifications", "find_loop_simplifications.py", "Finding loop simplifications"),
    ("outdated_idioms", "find_outdated_idioms.py", "Finding outdated idioms"),
    ("naming_issues", "find_naming_issues.py", "Finding naming issues"),
    ("comment_smells", "find_comment_smells.py", "Finding comment smells"),
    ("debug_leftovers", "find_debug_leftovers.py", "Finding debug leftovers"),
    ("ai_scaffolding", "find_ai_scaffolding.py", "Finding unfinished scaffolding"),
    ("module_issues", "find_module_issues.py", "Finding import cycles / barrels"),
    ("dependency_issues", "find_dependency_issues.py", "Reconciling package.json"),
    ("dead_code", "find_dead_code.py", "Finding dead code"),
    ("untested_modules", "find_untested_modules.py", "Finding untested modules"),
    ("test_smells", "find_test_smells.py", "Finding test smells"),
    ("duplicates", "find_duplicates.py", "Finding duplication"),
]

CATEGORIES = [category for category, _, _ in ANALYZERS]

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


def run_analyzer(script_name: str, path: str) -> dict:
    script_path = Path(__file__).parent / script_name
    if not script_path.exists():
        return {"issues": [], "error": f"Script not found: {script_name}"}

    command = [sys.executable, str(script_path), path, "--format", "json"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        return {"issues": [], "error": result.stderr[-300:] if result.stderr else "No output"}
    except subprocess.TimeoutExpired:
        return {"issues": [], "error": "Analysis timed out"}
    except json.JSONDecodeError as exc:
        return {"issues": [], "error": f"JSON parse error: {exc}"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"issues": [], "error": str(exc)[:200]}


def generate_report(path: str, skip: set | None = None) -> dict:
    skip = skip or set()
    results = {}
    for category, script, label in ANALYZERS:
        if category in skip:
            continue
        print(f"🔍 {label}...", file=sys.stderr)
        results[category] = run_analyzer(script, path)

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
        """,
    )
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--skip", type=str, default="",
                        help="Comma-separated analyzer categories to skip "
                             f"(choices: {', '.join(CATEGORIES)})")
    parser.add_argument("--skip-duplicates", action="store_true",
                        help="Shorthand for --skip duplicates (the slowest analyzer)")
    parser.add_argument("--output", "-o", type=str, help="Output file")
    args = parser.parse_args()

    skip = set(args.skip.split(",")) if args.skip else set()
    if args.skip_duplicates:
        skip.add("duplicates")
    unknown = skip - set(CATEGORIES)
    if unknown:
        parser.error(f"--skip names unknown categories: {', '.join(sorted(unknown))}")

    report = generate_report(args.path, skip=skip)

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
