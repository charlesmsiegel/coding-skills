#!/usr/bin/env python3
"""
Run every detector over a Rust project and merge the output into one report.
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
    ("cargo", "find_cargo_issues", "Auditing Cargo.toml", TREE),
    ("error_handling", "find_error_handling", "Finding error-handling problems", FILE),
    ("unsafe_code", "find_unsafe_issues", "Auditing unsafe", FILE),
    ("concurrency", "find_concurrency_issues", "Finding async/threading bugs", FILE),
    ("type_issues", "find_type_issues", "Finding lossy casts and loose types", FILE),
    ("code_smells", "find_code_smells", "Finding code smells", FILE),
    ("complexity", "analyze_complexity", "Measuring complexity", FILE),
    ("ownership", "find_ownership_issues", "Finding clones and owned parameters", FILE),
    ("design_smells", "find_design_smells", "Finding design smells", FILE),
    ("api_hygiene", "find_api_hygiene", "Checking the public API", FILE),
    ("security", "find_security_issues", "Finding security leads", FILE),
    ("unrustic", "find_unrustic", "Finding unidiomatic Rust", FILE),
    ("loop_simplifications", "find_loop_simplifications", "Finding loop simplifications", FILE),
    ("outdated_idioms", "find_outdated_idioms", "Finding outdated idioms", FILE),
    ("naming_issues", "find_naming_issues", "Finding naming issues", FILE),
    ("comment_smells", "find_comment_smells", "Finding comment smells", FILE),
    ("debug_leftovers", "find_debug_leftovers", "Finding debug leftovers", FILE),
    ("ai_scaffolding", "find_ai_scaffolding", "Finding unfinished scaffolding", FILE),
    ("test_smells", "find_test_smells", "Finding test smells", FILE),
    ("module_issues", "find_module_issues", "Checking the module graph", TREE),
    ("dead_code", "find_dead_code", "Finding dead code", TREE),
    ("overengineering", "find_overengineering", "Detecting over-engineering", TREE),
    ("untested_modules", "find_untested_modules", "Finding untested modules", TREE),
    ("duplicates", "find_duplicates", "Finding duplication", TREE),
]

CATEGORIES = [category for category, _, _, _ in ANALYZERS]

# Advice keyed by category, printed when that category has any finding.
RECOMMENDATIONS = {
    "cargo": "• Fix the manifest first — the edition decides what the compiler was ever going to catch",
    "error_handling": "• Replace the unwraps that sit inside fallible functions with `?`, and stop swallowing errors",
    "unsafe_code": "• Every `unsafe` block needs a `// SAFETY:` argument someone can check",
    "concurrency": "• Fix the deadlocks first — a guard across an `.await`, a blocking call on the executor",
    "type_issues": "• Replace narrowing `as` casts with `try_into()`; a truncation that compiles is still a bug",
    "code_smells": "• Address the everyday smells — blanket `allow`s, magic numbers, `if` ladders that are `match`es",
    "complexity": "• Reduce complexity — `let … else`, `?` and early returns flatten a body fast",
    "ownership": "• Take `&str`/`&[T]` instead of `&String`/`&Vec<T>`, and get the clones out of the loops",
    "design_smells": "• Replace flag parameters with named enums; bundle data clumps into structs",
    "api_hygiene": "• Derive `Debug` on public types and document the public surface — both are breaking to add later",
    "security": "• Triage the security leads — shell strings, SQL interpolation, disabled TLS checks, secrets",
    "unrustic": "• Converge on the idiomatic spelling; each one is shorter and says more",
    "loop_simplifications": "• Replace index loops with iterators — the bounds check and the off-by-one go with them",
    "outdated_idioms": "• Modernise the spellings; it is also how an edition bump stops being a big-bang branch",
    "naming_issues": "• Fix names — `as_`/`to_`/`into_` are promises about cost, not decoration",
    "comment_smells": "• Delete commented-out code; move the TODOs into the tracker",
    "debug_leftovers": "• Remove `dbg!`, library `println!` and crate-level warning suppressions",
    "ai_scaffolding": "• Finish or delete the scaffolding — stubs, ignored parameters, placeholder prose",
    "test_smells": "• Fix hollow tests — an assertion that cannot fail is worse than no test",
    "module_issues": "• A file no `mod` reaches is never compiled; fix that before anything else here",
    "dead_code": "• Delete unreachable code and unreferenced private items",
    "overengineering": "• Delete traits with one implementor — in Rust a generic parameter already mocks",
    "untested_modules": "• Build the safety net first — characterize untested modules before refactoring",
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
    print("📊 RUST CODE ANALYSIS REPORT")
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
    print("These detectors read syntax, not types. Run `cargo check`, `cargo clippy` and")
    print("`cargo test` through run_external_tools.py — the compiler's findings outrank")
    print("anything here that overlaps them.")
    print()


def main():
    configure_output()
    parser = argparse.ArgumentParser(
        description="Comprehensive Rust code analyzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Runs every detector and merges the output:
  - Cargo.toml audit: edition, lint config, dependency reconciliation
  - error handling (unwrap in fallible fns, swallowed errors, hand-rolled `?`)
  - unsafe hygiene, lossy casts, async deadlocks and blocking calls
  - complexity, code smells, design smells, ownership and allocation
  - public API guidelines, security leads, unidiomatic Rust, dated idioms
  - the module graph (files rustc never compiles), dead code, duplication
  - safety net: untested modules and test smells

Examples:
  %(prog)s .                      # analyze the current crate or workspace
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
