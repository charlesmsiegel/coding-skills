#!/usr/bin/env python3
"""
Find source modules no test reaches — the safety-net gap you need closed before
refactoring anything.

This answers "does any test import this module", which is cheap and reliable.
It does not answer "is this code executed", which only a coverage run knows;
`run_external_tools.py --run-coverage` drives that.
"""

from pathlib import Path

from common import Finding, is_declaration_file, run_tree_detector
from tsproject import load_project

# Modules whose job is wiring, not logic. A missing test there is a choice.
LOW_VALUE_STEMS = frozenset({"index", "main", "types", "constants", "setup", "test-setup"})
BIG_MODULE_LINES = 150


def analyze(root: Path, ignore: set[str], _args) -> list[Finding]:
    project = load_project(root)
    findings: list[Finding] = []

    def add(path, line, smell, description, suggestion, severity):
        if smell not in ignore:
            findings.append(Finding(file=str(path), line=line, smell_type=smell,
                                    description=description, suggestion=suggestion, severity=severity))

    sources = [p for p in project.sources if not is_declaration_file(p)]
    tests = project.tests
    if not sources:
        return findings

    if not tests:
        add(project.root, 1, "no_tests_at_all",
            f"No test files found under this path, beside {len(sources)} source module(s)",
            "Build the safety net before refactoring anything. Start with a characterization test "
            "for the module you are about to change: assert what it does today, not what it should "
            "do — see references/safety-net-and-testing.md.", "high")
        return findings

    covered: set[Path] = set()
    for test in tests:
        for target, _line, _spec in project.internal_imports(test):
            covered.add(target)
        # A test may reach a module through a helper, so give it one more hop.
        for target, _line, _spec in list(project.internal_imports(test)):
            for indirect, _l, _s in project.internal_imports(target):
                covered.add(indirect)

    untested = [p for p in sources if p not in covered]
    for path in sorted(untested):
        tsfile = project.files[path]
        lines = len(tsfile.lines)
        exported = [e for e in tsfile.exports if not e.is_type_only]
        if not exported:
            continue
        if path.stem in LOW_VALUE_STEMS and lines < BIG_MODULE_LINES:
            continue
        severity = "high" if lines >= BIG_MODULE_LINES else "medium"
        add(path, 1, "untested_module",
            f"No test imports {path.name} ({lines} lines, {len(exported)} export(s))",
            "Pin its current behaviour with a characterization test before changing it. Refactoring "
            "without one is not refactoring — it is rewriting and hoping.", severity)

    ratio = len(untested) / len(sources)
    if ratio > 0.5 and len(sources) >= 5:
        add(project.root, 1, "thin_test_coverage",
            f"{len(untested)} of {len(sources)} source modules are imported by no test ({ratio:.0%})",
            "Cover the modules you are about to touch first, in churn order — "
            "`git log --since='1 year ago' --name-only --pretty=format: | grep -E '\\.tsx?$' | "
            "sort | uniq -c | sort -rn | head -20`.", "medium")
    return findings


if __name__ == "__main__":
    run_tree_detector(
        "Find source modules no test imports",
        "Every source module is reached by a test!",
        analyze,
    )
