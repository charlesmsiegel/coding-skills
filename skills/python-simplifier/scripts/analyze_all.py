#!/usr/bin/env python3
"""
Comprehensive Python code analyzer - runs all checks and produces unified report.
"""

import io
import sys
import json
import argparse
import subprocess
from pathlib import Path
from datetime import datetime


def run_analyzer(script_name: str, path: str) -> dict:
    script_path = Path(__file__).parent / script_name
    if not script_path.exists():
        return {'issues': [], 'error': f'Script not found: {script_name}'}

    cmd = [sys.executable, str(script_path), path, '--format', 'json']
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        return {'issues': [], 'error': result.stderr[:200] if result.stderr else 'No output'}
    except subprocess.TimeoutExpired:
        return {'issues': [], 'error': 'Analysis timed out'}
    except json.JSONDecodeError as e:
        return {'issues': [], 'error': f'JSON parse error: {e}'}
    except Exception as e:
        return {'issues': [], 'error': str(e)[:200]}


def generate_report(path: str, skip_duplicates: bool = False) -> dict:
    results = {}

    print("🔍 Analyzing complexity...", file=sys.stderr)
    results['complexity'] = run_analyzer('analyze_complexity.py', path)

    print("🔍 Finding code smells...", file=sys.stderr)
    results['code_smells'] = run_analyzer('find_code_smells.py', path)

    print("🔍 Detecting over-engineering...", file=sys.stderr)
    oe_result = run_analyzer('find_overengineering.py', path)
    results['overengineering'] = oe_result.get('issues', []) if isinstance(oe_result, dict) else oe_result

    print("🔍 Finding classic design smells...", file=sys.stderr)
    results['design_smells'] = run_analyzer('find_design_smells.py', path)

    print("🔍 Finding design-pattern issues...", file=sys.stderr)
    results['pattern_issues'] = run_analyzer('find_pattern_issues.py', path)

    print("🔍 Finding dead code...", file=sys.stderr)
    results['dead_code'] = run_analyzer('find_dead_code.py', path)

    print("🔍 Detecting unpythonic patterns...", file=sys.stderr)
    results['unpythonic'] = run_analyzer('find_unpythonic.py', path)

    print("🔍 Analyzing coupling/cohesion...", file=sys.stderr)
    results['coupling'] = run_analyzer('find_coupling_issues.py', path)

    print("🔍 Finding mutation hazards...", file=sys.stderr)
    results['mutation_hazards'] = run_analyzer('find_mutation_hazards.py', path)

    print("🔍 Finding exception issues...", file=sys.stderr)
    results['exception_issues'] = run_analyzer('find_exception_issues.py', path)

    print("🔍 Finding global state...", file=sys.stderr)
    results['global_state'] = run_analyzer('find_global_state.py', path)

    print("🔍 Finding data clumps...", file=sys.stderr)
    results['parameter_objects'] = run_analyzer('find_parameter_objects.py', path)

    print("🔍 Finding boolean-flag parameters...", file=sys.stderr)
    results['boolean_params'] = run_analyzer('find_boolean_params.py', path)

    print("🔍 Finding return-statement problems...", file=sys.stderr)
    results['return_issues'] = run_analyzer('find_return_issues.py', path)

    print("🔍 Finding loop simplifications...", file=sys.stderr)
    results['loop_simplifications'] = run_analyzer('find_loop_simplifications.py', path)

    print("🔍 Finding naming issues...", file=sys.stderr)
    results['naming_issues'] = run_analyzer('find_naming_issues.py', path)

    print("🔍 Finding comment smells...", file=sys.stderr)
    results['comment_smells'] = run_analyzer('find_comment_smells.py', path)

    print("🔍 Finding resource leaks...", file=sys.stderr)
    results['resource_leaks'] = run_analyzer('find_resource_leaks.py', path)

    print("🔍 Finding security issues...", file=sys.stderr)
    results['security'] = run_analyzer('find_security_issues.py', path)

    print("🔍 Finding import cycles / god modules...", file=sys.stderr)
    results['import_cycles'] = run_analyzer('find_import_cycles.py', path)

    print("🔍 Finding debug leftovers...", file=sys.stderr)
    results['debug_leftovers'] = run_analyzer('find_debug_leftovers.py', path)

    print("🔍 Finding outdated idioms...", file=sys.stderr)
    results['outdated_idioms'] = run_analyzer('find_outdated_idioms.py', path)

    print("🔍 Finding missing docstrings...", file=sys.stderr)
    results['missing_docstrings'] = run_analyzer('find_missing_docstrings.py', path)

    print("🔍 Finding type-annotation gaps...", file=sys.stderr)
    results['type_gaps'] = run_analyzer('find_type_gaps.py', path)

    print("🔍 Checking dependency hygiene...", file=sys.stderr)
    results['dependency_issues'] = run_analyzer('find_dependency_issues.py', path)

    print("🔍 Finding untested modules...", file=sys.stderr)
    results['untested_modules'] = run_analyzer('find_untested_modules.py', path)

    print("🔍 Finding test smells...", file=sys.stderr)
    results['test_smells'] = run_analyzer('find_test_smells.py', path)

    print("🔍 Finding AI scaffolding/placeholders...", file=sys.stderr)
    results['ai_scaffolding'] = run_analyzer('find_ai_scaffolding.py', path)

    print("🔍 Finding duplicate definitions / merge artifacts...", file=sys.stderr)
    results['duplicate_definitions'] = run_analyzer('find_duplicate_definitions.py', path)

    print("🔍 Finding unawaited coroutines...", file=sys.stderr)
    results['unawaited_coroutines'] = run_analyzer('find_unawaited_coroutines.py', path)

    print("🔍 Finding non-top-level imports...", file=sys.stderr)
    results['local_imports'] = run_analyzer('find_local_imports.py', path)

    if not skip_duplicates:
        print("🔍 Finding duplicates...", file=sys.stderr)
        results['duplicates'] = run_analyzer('find_duplicates.py', path)

    report = {
        'meta': {
            'analyzed_path': path,
            'timestamp': datetime.now().isoformat(),
            'analyzers_run': list(results.keys()),
            # category -> error string for every analyzer that did not complete.
            # A zero count for one of these categories means "unknown", not "clean".
            'analyzer_errors': {}
        },
        'summary': {
            'total_issues': 0,
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
    print()

    severity_icons = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}
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

    high_issues = []
    for cat, data in report['categories'].items():
        for issue in data['issues']:
            if issue.get('severity') == 'high':
                high_issues.append(issue)

    if not high_issues:
        print("None found!")
    else:
        for issue in high_issues[:20]:
            file_loc = f"{issue.get('file', '?')}:{issue.get('line', '?')}"
            print(f"\n📍 {file_loc}")
            print(f"   [{issue['category']}] {issue.get('issue_type', issue.get('smell_type', issue.get('pattern_type', '?')))}")
            if 'description' in issue:
                print(f"   {issue['description']}")
            if 'suggestion' in issue:
                print(f"   → {issue['suggestion']}")

        if len(high_issues) > 20:
            print(f"\n... and {len(high_issues) - 20} more high severity issues")

    print()
    print("=" * 70)
    print("💡 RECOMMENDATIONS")
    print("=" * 70)

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
  - Outdated idioms (%/format, old typing, os.path, super(args))
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
        """
    )
    parser.add_argument('path', nargs='?', default='.', help='File or directory')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    parser.add_argument('--skip-duplicates', action='store_true')
    parser.add_argument('--output', '-o', type=str, help='Output file')

    args = parser.parse_args()
    report = generate_report(args.path, skip_duplicates=args.skip_duplicates)

    if args.format == 'json':
        output = json.dumps(report, indent=2)
    else:
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        print_text_report(report)
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout

    if args.output:
        Path(args.output).write_text(output)
        print(f"Report saved to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == '__main__':
    main()
