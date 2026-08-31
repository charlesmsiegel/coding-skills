#!/usr/bin/env python3
"""
Find tests that cannot fail, cannot be trusted, or are quietly not running.

A test suite is a claim about the code, and these are the ways the claim turns
out to be empty: a `#[test]` with no assertion, an `#[ignore]` nobody
remembers, a `#[should_panic]` with no `expected` (so it passes on the wrong
panic), a sleep standing in for synchronisation, and a `#[cfg(test)] mod` that
no `mod tests;` reaches.

Read this alongside `find_untested_modules.py`: that one answers "is anything
testing this file", this one answers "does the test prove anything".
"""

import re

from common import Reporter, is_test_file, run_file_detector
from rsparse import RsFile, argument_spans, body_indices, iter_calls, iter_method_calls

_ASSERTION_MACROS = frozenset({
    # NOT `matches!`: on its own it computes a boolean and throws it away, so a
    # test whose only "assertion" is `matches!(v, P);` passes when the pattern
    # does not match. That is precisely the mistake this check exists to find.
    "assert!", "assert_eq!", "assert_ne!", "debug_assert!", "debug_assert_eq!",
    "debug_assert_ne!", "panic!", "unreachable!",
    "assert_matches!", "insta::assert_snapshot!", "assert_snapshot!",
    "assert_yaml_snapshot!", "assert_json_snapshot!", "assert_debug_snapshot!",
    "pretty_assertions::assert_eq!", "claim::assert_ok!", "assert_ok!", "assert_err!",
})

_TEST_ATTRS = ("test", "tokio::test", "async_std::test", "rstest", "proptest", "bench",
               "quickcheck")


def _tests(file: RsFile):
    for func in file.functions:
        heads = [a.split("(")[0].strip() for a in func.attrs]
        if any(h in _TEST_ATTRS for h in heads) and func.has_body and func.body_close > 0:
            yield func


def _check_assertionless(file: RsFile, report: Reporter) -> None:
    for func in _tests(file):
        span = body_indices(func)
        if not span:
            continue
        # A project's own `assert_paths(…)` helper is an assertion too; anything
        # whose name starts with `assert` counts, macro or function.
        has_assertion = any(callee in _ASSERTION_MACROS
                            or callee.rstrip("!").rsplit("::", 1)[-1].startswith("assert")
                            for _, callee in iter_calls(file, func.body_open, func.body_close))
        if has_assertion:
            continue
        # `?` in a test body is an assertion: the test fails if the call errors.
        if any(file.tokens[i].is_op("?") for i in span):
            continue
        if any(m in ("unwrap", "expect") for _, _, m in
               iter_method_calls(file, func.body_open, func.body_close)):
            continue
        if any(a.split("(")[0].strip() in ("should_panic", "bench") for a in func.attrs):
            continue
        report.add(func.line, "test_without_assertion",
                   f"`{func.name}` has no visible assertion",
                   "The test passes as long as nothing panics, which is a much weaker claim than "
                   "the name implies — assert on the value that came back. If the crate has its "
                   "own harness (a builder ending in `.test()`, a golden-file comparison), this "
                   "scan cannot see through it; confirm the harness fails when the code is "
                   "wrong.", "medium")


def _check_should_panic(file: RsFile, report: Reporter) -> None:
    for func in _tests(file):
        for attribute in func.attrs:
            if not attribute.replace(" ", "").startswith("should_panic"):
                continue
            if "expected" in attribute:
                continue
            report.add(func.line, "should_panic_without_expected",
                       f"`#[should_panic]` on `{func.name}` with no `expected = …`",
                       "The test passes on *any* panic — including one from a typo in the setup, "
                       "or from an unwrap three layers down. Name the message it should carry.",
                       "medium")


def _check_ignored(file: RsFile, report: Reporter) -> None:
    for func in _tests(file):
        for attribute in func.attrs:
            head = attribute.replace(" ", "")
            if not head.startswith("ignore"):
                continue
            reason = "=" in head or file.doc_lines_before(func.line) \
                or bool(file.comments_on_lines(func.line - 2, func.line))
            report.add(func.line, "ignored_test",
                       f"`{func.name}` is `#[ignore]`d"
                       + ("" if reason else " with no reason given"),
                       "An ignored test is a test that is not run and not deleted. Give the "
                       "reason (`#[ignore = \"needs a live database\"]`) and, if it needs an "
                       "environment CI does not have, gate it on a feature so it runs somewhere.",
                       "low" if reason else "medium")


def _check_sleeps(file: RsFile, report: Reporter) -> None:
    for func in _tests(file):
        for index, callee in iter_calls(file, func.body_open, func.body_close):
            if callee.rsplit("::", 1)[-1] != "sleep":
                continue
            report.add(file.line_of(index), "sleep_in_test",
                       f"`{callee}` inside `{func.name}`",
                       "A sleep is a guess about timing: too short and the test is flaky, too "
                       "long and the suite is slow, and it is both on a loaded CI machine. "
                       "Synchronise on the thing you are waiting for — a channel, a barrier, a "
                       "condvar, or the runtime's own time control (`tokio::time::pause`).",
                       "medium")


def _check_over_broad_assertions(file: RsFile, report: Reporter) -> None:
    for func in _tests(file):
        for index, callee in iter_calls(file, func.body_open, func.body_close):
            if callee != "assert!":
                continue
            spans = argument_spans(file, index)
            if not spans:
                continue
            argument = " ".join(file.slice(*spans[0]).split())
            if re.fullmatch(r"[\w.]+\s*==\s*[\w.\"']+", argument):
                report.add(file.line_of(index), "assert_instead_of_assert_eq",
                           f"`assert!({argument})` for an equality check",
                           "`assert_eq!` prints both values on failure; `assert!` prints only "
                           "that a boolean was false, which is the moment you most want the "
                           "numbers.", "low")
            elif argument.strip() in ("true", "1 == 1", "!false"):
                report.add(file.line_of(index), "tautological_assertion",
                           f"`assert!({argument})` cannot fail",
                           "Assert on the value the code produced.", "high")


def _check_unreachable_test_module(file: RsFile, report: Reporter) -> None:
    """A `#[cfg(test)] mod tests;` whose file is missing runs nothing."""
    for declaration in file.mods:
        if not declaration.is_test_mod or declaration.inline:
            continue
        sibling = file.path.parent / f"{declaration.name}.rs"
        nested = file.path.parent / declaration.name / "mod.rs"
        if sibling.exists() or nested.exists():
            continue
        report.add(declaration.line, "missing_test_module_file",
                   f"`mod {declaration.name};` names no file next to this one",
                   "rustc rejects this outright, so the crate does not build — but under "
                   "`#[cfg(test)]` it only breaks `cargo test`, which is how it survives a green "
                   "`cargo build`.", "high")


def _check_test_mod_placement(file: RsFile, report: Reporter) -> None:
    for declaration in file.mods:
        if declaration.name not in ("tests", "test") or not declaration.inline:
            continue
        if declaration.is_test_mod:
            continue
        report.add(declaration.line, "test_module_without_cfg_test",
                   f"`mod {declaration.name}` has no `#[cfg(test)]`",
                   "Without it the tests and their dependencies compile into the release binary. "
                   "Add `#[cfg(test)]` above the module.", "medium")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_assertionless(file, report)
    _check_should_panic(file, report)
    _check_ignored(file, report)
    _check_sleeps(file, report)
    _check_over_broad_assertions(file, report)
    _check_test_mod_placement(file, report)
    # The conventional `#[cfg(test)] mod tests;` in an ordinary `lib.rs` creates
    # no inline test span and the file is not itself a test file, so gating on
    # either meant this documented check never ran at all.
    if any(d.is_test_mod and not d.inline for d in file.mods) \
            or is_test_file(file.path) or file.test_spans:
        _check_unreachable_test_module(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find tests that cannot fail or are quietly not running",
        "No test smells found!",
        analyze,
    )
