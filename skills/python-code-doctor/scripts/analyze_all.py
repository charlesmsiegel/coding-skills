#!/usr/bin/env python3
"""
Comprehensive Python code analyzer - runs all checks and produces unified report.
"""

import io
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from common import SEVERITY_ICONS, configure_output
from runner import default_jobs, run_detectors


# category -> (module, progress label, kind). One row per detector analyze_all
# runs; find_redundant_comments and find_parameter_objects' diff lens are opt-in
# only. `kind` is how the row is scheduled, not what it looks for: a FILE
# detector answers from one parsed file and is sharded across the pool, a TREE
# detector needs the whole tree at once and runs in a task of its own.
FILE, TREE = "file", "tree"

ANALYZERS = [
    ('complexity', 'analyze_complexity', 'Analyzing complexity', FILE),
    ('code_smells', 'find_code_smells', 'Finding code smells', FILE),
    ('overengineering', 'find_overengineering', 'Detecting over-engineering', TREE),
    ('design_smells', 'find_design_smells', 'Finding classic design smells', FILE),
    ('pattern_issues', 'find_pattern_issues', 'Finding design-pattern issues', FILE),
    ('dead_code', 'find_dead_code', 'Finding dead code', FILE),
    ('unpythonic', 'find_unpythonic', 'Detecting unpythonic patterns', FILE),
    ('coupling', 'find_coupling_issues', 'Analyzing coupling/cohesion', FILE),
    ('mutation_hazards', 'find_mutation_hazards', 'Finding mutation hazards', FILE),
    ('exception_issues', 'find_exception_issues', 'Finding exception issues', FILE),
    ('global_state', 'find_global_state', 'Finding global state', FILE),
    ('parameter_objects', 'find_parameter_objects', 'Finding data clumps', TREE),
    ('boolean_params', 'find_boolean_params', 'Finding boolean-flag parameters', FILE),
    ('return_issues', 'find_return_issues', 'Finding return-statement problems', FILE),
    ('loop_simplifications', 'find_loop_simplifications', 'Finding loop simplifications', FILE),
    ('naming_issues', 'find_naming_issues', 'Finding naming issues', FILE),
    ('comment_smells', 'find_comment_smells', 'Finding comment smells', FILE),
    ('resource_leaks', 'find_resource_leaks', 'Finding resource leaks', FILE),
    ('security', 'find_security_issues', 'Finding security issues', FILE),
    ('import_cycles', 'find_import_cycles', 'Finding import cycles / god modules', TREE),
    ('debug_leftovers', 'find_debug_leftovers', 'Finding debug leftovers', FILE),
    ('outdated_idioms', 'find_outdated_idioms', 'Finding outdated idioms', FILE),
    ('missing_docstrings', 'find_missing_docstrings', 'Finding missing docstrings', FILE),
    ('type_gaps', 'find_type_gaps', 'Finding type-annotation gaps', FILE),
    ('dependency_issues', 'find_dependency_issues', 'Checking dependency hygiene', TREE),
    ('untested_modules', 'find_untested_modules', 'Finding untested modules', TREE),
    ('test_smells', 'find_test_smells', 'Finding test smells', FILE),
    ('ai_scaffolding', 'find_ai_scaffolding', 'Finding AI scaffolding/placeholders', FILE),
    ('duplicate_definitions', 'find_duplicate_definitions', 'Finding duplicate definitions / merge artifacts', FILE),
    ('unawaited_coroutines', 'find_unawaited_coroutines', 'Finding unawaited coroutines', FILE),
    ('local_imports', 'find_local_imports', 'Finding non-top-level imports', FILE),
    ('duplicates', 'find_duplicates', 'Finding duplicates', TREE),
]

CATEGORIES = [category for category, _, _, _ in ANALYZERS]


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
        'meta': {
            'analyzed_path': path,
            'timestamp': datetime.now().isoformat(),
            'analyzers_run': list(results.keys()),
            'analyzers_skipped': sorted(skip),
            # category -> error string for every analyzer that did not complete.
            # A zero count for one of these categories means "unknown", not "clean".
            'analyzer_errors': {}
        },
        'summary': {
            'total_issues': 0,
            # How many of `total_issues` are unverified leads rather than
            # asserted defects — records a detector marked `kind: "candidate"`
            # because one file cannot prove them. The other counts keep their
            # meaning ("what was reported"); this one says how much of it is
            # unproven, so a consumer that scores can subtract rather than
            # having to re-derive the split from the records.
            'total_candidates': 0,
            'by_severity': {'high': 0, 'medium': 0, 'low': 0},
            'by_category': {}
        },
        'categories': {}
    }

    for category, data in results.items():
        issues = []
        if isinstance(data, list):
            issues = data
        elif isinstance(data, dict):
            if 'issues' in data:
                issues = data['issues']
            if data.get('error'):
                report['meta']['analyzer_errors'][category] = str(data['error'])

        normalized = []
        for issue in issues:
            if isinstance(issue, dict):
                if 'severity' not in issue:
                    if 'confidence' in issue:
                        conf = issue['confidence']
                        issue['severity'] = 'high' if conf >= 90 else ('medium' if conf >= 70 else 'low')
                    else:
                        issue['severity'] = 'medium'
                issue['category'] = category
                normalized.append(issue)

        report['categories'][category] = {'issues': normalized, 'count': len(normalized)}
        report['summary']['total_issues'] += len(normalized)
        report['summary']['total_candidates'] += sum(1 for i in normalized
                                                     if i.get('kind') == 'candidate')
        report['summary']['by_category'][category] = len(normalized)

        for issue in normalized:
            sev = issue.get('severity', 'medium')
            if sev in report['summary']['by_severity']:
                report['summary']['by_severity'][sev] += 1

    return report


def print_text_report(report: dict):
    meta = report['meta']
    summary = report['summary']

    print("\n" + "=" * 70)
    print("📊 PYTHON CODE ANALYSIS REPORT")
    print("=" * 70)
    print(f"Path: {meta['analyzed_path']}")
    print(f"Time: {meta['timestamp']}")
    print()

    print("📈 SUMMARY")
    print("-" * 40)
    print(f"Total issues found: {summary['total_issues']}")
    candidate_count = summary.get('total_candidates', 0)
    if candidate_count:
        print(f"  ❓ of which {candidate_count} are candidates — unverified leads, not defects")
    print()

    severity_icons = SEVERITY_ICONS
    print("By severity:")
    for sev, count in summary['by_severity'].items():
        if count > 0:
            print(f"  {severity_icons[sev]} {sev.upper()}: {count}")
    print()

    print("By category:")
    for cat, count in sorted(summary['by_category'].items(), key=lambda x: -x[1]):
        if count > 0:
            print(f"  {cat}: {count}")
    print()

    analyzer_errors = meta.get('analyzer_errors') or {}
    if analyzer_errors:
        print("⚠️  ANALYSIS INCOMPLETE — these analyzers did not finish; their")
        print("    categories show what was found before failure, not a clean bill:")
        for cat, err in sorted(analyzer_errors.items()):
            print(f"    • {cat}: {err}")
        print()

    if summary['total_issues'] == 0:
        if analyzer_errors:
            print("No issues found by the analyzers that completed (see warnings above).")
        else:
            print("✅ No issues found! Your code looks great!")
        return

    print("=" * 70)
    print("🔴 HIGH SEVERITY ISSUES")
    print("=" * 70)

    def _describe(issue):
        file_loc = f"{issue.get('file', '?')}:{issue.get('line', '?')}"
        print(f"\n📍 {file_loc}")
        print(f"   [{issue['category']}] {issue.get('issue_type', issue.get('smell_type', issue.get('pattern_type', '?')))}")
        if 'description' in issue:
            print(f"   {issue['description']}")
        if 'suggestion' in issue:
            print(f"   → {issue['suggestion']}")

    # A candidate is kept out of this list on purpose. It is printed under its
    # own heading below with what to check, because listing a lead among the
    # high-severity defects is how a reader acts on one without confirming it.
    high_issues = []
    candidates = []
    for cat, data in report['categories'].items():
        for issue in data['issues']:
            if issue.get('kind') == 'candidate':
                candidates.append(issue)
            elif issue.get('severity') == 'high':
                high_issues.append(issue)

    if not high_issues:
        print("None found!")
    else:
        for issue in high_issues[:20]:
            _describe(issue)

        if len(high_issues) > 20:
            print(f"\n... and {len(high_issues) - 20} more high severity issues")

    print()

    if candidates:
        print("=" * 70)
        print("❓ CANDIDATES — leads to confirm, not defects")
        print("=" * 70)
        print("Each names something one file cannot prove. Rule out the benign")
        print("explanation before changing anything, and note that graders")
        print("exclude these from a code-health score.")
        for issue in candidates[:20]:
            _describe(issue)
        if len(candidates) > 20:
            print(f"\n... and {len(candidates) - 20} more candidates")
        print()
    print("=" * 70)
    print("💡 RECOMMENDATIONS")
    print("=" * 70)

    # Recommendations are instructions to change code, so they are counted off
    # the asserted defects only. `by_category` deliberately counts everything
    # reported — telling someone to "address security risks: eval/exec,
    # shell=True" because a PRAGMA lead was raised is advice about a defect
    # nothing found.
    summary = {**summary, 'by_category': {
        category: sum(1 for issue in data['issues'] if issue.get('kind') != 'candidate')
        for category, data in report['categories'].items()
    }}

    recommendations = []
    if summary['by_category'].get('complexity', 0) > 5:
        recommendations.append("• Reduce function complexity - extract methods, use early returns")
    if summary['by_category'].get('code_smells', 0) > 5:
        recommendations.append("• Address code smells - fix mutable defaults, bare excepts")
    if summary['by_category'].get('overengineering', 0) > 0:
        recommendations.append("• Simplify architecture - remove unused abstractions (YAGNI)")
    if summary['by_category'].get('design_smells', 0) > 0:
        recommendations.append("• Apply the classic refactorings - dispatch over type-switches, untangle intimate classes (see references/refactoring-techniques.md)")
    if summary['by_category'].get('pattern_issues', 0) > 0:
        recommendations.append("• Right-size design patterns - replace hand-rolled singletons/builders/iterators with Python-native forms; type string state machines (see references/design-patterns.md)")
    if summary['by_category'].get('dead_code', 0) > 5:
        recommendations.append("• Clean up dead code - remove unused imports and functions")
    if summary['by_category'].get('duplicates', 0) > 0:
        recommendations.append("• Extract duplicate code into shared functions")
    if summary['by_category'].get('coupling', 0) > 3:
        recommendations.append("• Improve class design - increase cohesion, reduce coupling")
    if summary['by_category'].get('mutation_hazards', 0) > 0:
        recommendations.append("• Fix mutation hazards - shared mutable state is a correctness bug")
    if summary['by_category'].get('exception_issues', 0) > 0:
        recommendations.append("• Fix exception handling - chain with 'from', catch narrow, never swallow")
    if summary['by_category'].get('global_state', 0) > 0:
        recommendations.append("• Remove global mutable state - encapsulate or inject it")
    if summary['by_category'].get('parameter_objects', 0) > 0:
        recommendations.append("• Bundle recurring parameter groups into dataclasses (data clumps)")
    if summary['by_category'].get('boolean_params', 0) > 0:
        recommendations.append("• Replace boolean flag parameters - split functions or use enums")
    if summary['by_category'].get('return_issues', 0) > 0:
        recommendations.append("• Make returns consistent and simplify boolean-return conditionals")
    if summary['by_category'].get('loop_simplifications', 0) > 0:
        recommendations.append("• Convert manual loops to comprehensions / any()/all() / ''.join()")
    if summary['by_category'].get('naming_issues', 0) > 0:
        recommendations.append("• Fix names - stop shadowing builtins, follow snake_case/PascalCase")
    if summary['by_category'].get('comment_smells', 0) > 0:
        recommendations.append("• Delete commented-out code; move TODOs into the tracker")
    if summary['by_category'].get('resource_leaks', 0) > 0:
        recommendations.append("• Wrap file/socket handles in 'with' so they close deterministically")
    if summary['by_category'].get('security', 0) > 0:
        recommendations.append("• Address security risks - eval/exec, shell=True, unsafe yaml/pickle, hardcoded secrets")
    if summary['by_category'].get('import_cycles', 0) > 0:
        recommendations.append("• Break import cycles and split god modules; thin out __init__.py")
    if summary['by_category'].get('untested_modules', 0) > 0:
        recommendations.append("• Build the safety net first - characterize untested modules before refactoring")
    if summary['by_category'].get('test_smells', 0) > 0:
        recommendations.append("• Fix hollow tests - add assertions, cut over-mocking, remove logic from tests")
    if summary['by_category'].get('dependency_issues', 0) > 0:
        recommendations.append("• Reconcile dependencies - declare missing, drop unused, pin versions")
    if summary['by_category'].get('debug_leftovers', 0) > 0:
        recommendations.append("• Remove debugger calls and stray prints left in the source")
    if summary['by_category'].get('outdated_idioms', 0) > 0:
        recommendations.append("• Modernize idioms - f-strings, builtin generics, pathlib, bare super()")
    if summary['by_category'].get('type_gaps', 0) > 0:
        recommendations.append("• Add type annotations at API boundaries; adopt mypy/pyright incrementally")
    if summary['by_category'].get('missing_docstrings', 0) > 0:
        recommendations.append("• Document the public API surface with intent-revealing docstrings")
    if summary['by_category'].get('ai_scaffolding', 0) > 0:
        recommendations.append("• Finish or remove AI scaffolding - stubs, placeholders, unused **kwargs")
    if summary['by_category'].get('duplicate_definitions', 0) > 0:
        recommendations.append("• Resolve duplicate definitions / merge-conflict markers (a later def silently wins)")
    if summary['by_category'].get('unawaited_coroutines', 0) > 0:
        recommendations.append("• Await coroutines - an un-awaited async call silently does nothing")
    if summary['by_category'].get('local_imports', 0) > 0:
        recommendations.append("• Move imports to module top; fix the circular import instead of deferring it")

    if not recommendations:
        recommendations.append("• Your code is in good shape! Consider minor improvements.")

    for rec in recommendations:
        print(rec)
    print()


def main():
    configure_output()
    parser = argparse.ArgumentParser(
        description="Comprehensive Python code analyzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Runs all analysis checks:
  - Complexity (cyclomatic, cognitive, nesting)
  - Code smells (mutable defaults, bare excepts, etc.)
  - Over-engineering (unused abstractions, YAGNI)
  - Design smells from the classic catalog (type-switches, refused bequest, intimacy)
  - Design-pattern issues (hand-rolled singletons/builders/iterators, string state machines)
  - Dead code (unused imports, functions)
  - Unpythonic patterns (range(len), == True)
  - Coupling/cohesion (feature envy, message chains)
  - Mutation hazards (shared mutable state, modify-during-iteration)
  - Exception issues (raise-without-from, unreachable except, assert validation)
  - Global state (mutated module globals, global rebinds)
  - Data clumps (recurring parameter groups)
  - Boolean-flag parameters
  - Return-statement problems (inconsistent returns, boolean returns)
  - Loop simplifications (comprehensions, any/all, join)
  - Naming issues (shadowed builtins, casing)
  - Comment smells (commented-out code, TODOs)
  - Resource leaks (open/socket without a context manager)
  - Security issues (eval/exec, shell=True, unsafe yaml/pickle, secrets)
  - Import cycles & god modules (circular imports, wildcard imports)
  - Debug leftovers (pdb/breakpoint/stray prints)
  - Outdated idioms (%%/format, old typing, os.path, super(args))
  - Missing docstrings (public API surface)
  - Type-annotation gaps (missing annotations, Any, broad type:ignore)
  - Dependency hygiene (missing/unused/unpinned deps)
  - Untested modules (safety-net gaps before refactoring)
  - Test smells (assertion-less/trivial tests, over-mocking, logic in tests)
  - AI scaffolding (stubs, placeholders, unused **kwargs)
  - Duplicate definitions & merge-conflict markers
  - Unawaited coroutines (silent async no-ops)
  - Non-top-level imports (deferred/circular-workaround imports)
  - Duplicate code (AST-based similarity)

Examples:
  %(prog)s .                    # Analyze current directory
  %(prog)s myproject/           # Analyze specific project
  %(prog)s . --format json      # JSON output for CI
  %(prog)s . --jobs 1           # analyze in this process, no pool
        """
    )
    parser.add_argument('path', nargs='?', default='.', help='File or directory')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--skip', type=str, default='',
                        help='Comma-separated analyzer categories to skip '
                             f"(choices: {', '.join(CATEGORIES)})")
    parser.add_argument('--skip-duplicates', action='store_true',
                        help='Shorthand for --skip duplicates (the slowest analyzer)')
    parser.add_argument('--jobs', '-j', type=int, default=None,
                        help='Worker processes to analyze with '
                             f'(default: {default_jobs()}; 1 runs in this process)')
    parser.add_argument('--output', '-o', type=str, help='Output file')

    args = parser.parse_args()
    if args.jobs is not None and args.jobs < 1:
        parser.error('--jobs must be at least 1')
    skip = set(args.skip.split(',')) if args.skip else set()
    if args.skip_duplicates:
        skip.add('duplicates')
    unknown = skip - set(CATEGORIES)
    if unknown:
        parser.error(f"--skip names unknown categories: {', '.join(sorted(unknown))}")
    report = generate_report(args.path, skip=skip, jobs=args.jobs)

    if args.format == 'json':
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


if __name__ == '__main__':
    main()
