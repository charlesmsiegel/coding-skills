#!/usr/bin/env python3
"""
Find the modules nothing tests — the safety net you need before refactoring.

Rust puts unit tests inside the module they test (`#[cfg(test)] mod tests`) and
integration tests in `tests/`, which makes this answerable from syntax alone:
a module with no test module of its own, whose public items no integration test
names, is untested.

"No tests anywhere in the crate" is reported as a separate, louder finding. It
is not one item among many — it is the condition under which every refactoring
in the rest of the report is rewriting and hoping.
"""

import re
from pathlib import Path

from common import Finding, is_test_file, run_tree_detector
from rsproject import load_project

# A module this small is glue; the absence of a test for it says little.
MIN_ITEMS_TO_MATTER = 3


def _finding(path, line, smell, description, suggestion, severity, related=None):
    return Finding(file=str(path), line=line, smell_type=smell, description=description,
                   suggestion=suggestion, severity=severity, related_lines=related or [])


def _integration_test_text(project) -> str:
    parts = []
    for path, rsfile in project.files.items():
        if is_test_file(path):
            parts.append(rsfile.text)
    return "\n".join(parts)


def analyze(root: Path, ignore: set[str], args) -> list:
    project = load_project(root)
    findings: list[Finding] = []

    source_files = {path: rsfile for path, rsfile in project.files.items()
                    if not is_test_file(path) and path.name != "build.rs"}
    if not source_files:
        return []

    inline_tested = {path for path, rsfile in project.files.items() if rsfile.test_spans}
    integration = _integration_test_text(project)
    # Only tests in files rustc actually compiles. A `#[test]` in an orphan
    # file — one no `mod` declaration reaches — never runs, and counting it
    # would suppress the `no_tests_at_all` blocker for a crate where
    # `cargo test` genuinely runs zero tests.
    compiled = project.compiled_files()
    total_tests = sum(
        1 for path, rsfile in project.files.items() if path in compiled
        for func in rsfile.functions
        if any(a.split("(")[0].strip() in ("test", "tokio::test", "async_std::test", "rstest",
                                           "proptest", "quickcheck")
               for a in func.attrs))

    if total_tests == 0:
        findings.append(_finding(
            next(iter(source_files)), 1, "no_tests_at_all",
            f"no `#[test]` function anywhere in {len(source_files)} source file(s)",
            "Stop before refactoring anything. Write characterization tests for the highest-churn "
            "modules first — they do not have to be good tests, they have to pin the behaviour "
            "that exists so a refactor can be shown not to have changed it. "
            "`references/safety-net-and-testing.md` has the procedure.", "high"))
        return [f for f in findings if f.smell_type not in ignore]

    untested = []
    for path, rsfile in source_files.items():
        if path in inline_tested:
            continue
        items = [f for f in rsfile.functions if f.has_body and f.kind != "closure"]
        if len(items) < MIN_ITEMS_TO_MATTER:
            continue
        public_names = [f.name for f in items if f.is_public] + \
                       [t.name for t in rsfile.types if t.is_public]
        if public_names and any(re.search(r"\b" + re.escape(name) + r"\b", integration)
                                for name in public_names):
            continue
        untested.append((path, len(items)))

    for path, count in sorted(untested, key=lambda item: -item[1]):
        findings.append(_finding(
            path, 1, "untested_module",
            f"{count} function(s) here, no `#[cfg(test)] mod` and no integration test naming its "
            "public items",
            "Add a `#[cfg(test)] mod tests` at the bottom of the file. In Rust it can reach the "
            "private items directly, so a characterization test needs no visibility changes — "
            "which is the usual reason people skip writing one.", "medium"))

    ratio = total_tests / max(1, sum(len(f.functions) for f in source_files.values()))
    if ratio < 0.1 and total_tests:
        findings.append(_finding(
            next(iter(source_files)), 1, "thin_test_coverage",
            f"{total_tests} test function(s) for "
            f"{sum(len(f.functions) for f in source_files.values())} source functions",
            "A ratio this low means the suite pins a few behaviours and nothing else. Before "
            "treating a green run as permission to refactor, check that the tests cover the code "
            "you are about to change — `cargo llvm-cov` answers that where this cannot.", "low"))

    return [f for f in findings if f.smell_type not in ignore]


if __name__ == "__main__":
    run_tree_detector(
        "Find modules with no tests, and the no-tests-at-all alarm",
        "Every substantial module has tests!",
        analyze,
    )
