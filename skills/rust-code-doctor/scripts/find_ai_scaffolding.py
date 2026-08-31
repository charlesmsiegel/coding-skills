#!/usr/bin/env python3
"""
Find the tells of code that was generated and never finished.

Generated Rust compiles — that is the whole difficulty. It type-checks, the
tests pass, and the parts that are missing are missing in ways the compiler
cannot see: a `todo!()` that satisfies a trait, a parameter the body never
reads so every caller's setting is silently ignored, a second definition of a
function where the later one wins, a constant with a plausible value and no
stated origin.

None of these is evidence of a machine author, and none is a reason to reject
the code. They are the specific places to look when the compiler has already
said yes.
"""

import re
from collections import defaultdict

from common import Reporter, is_test_file, run_file_detector
from rsparse import RsFile, body_indices

_STUB_BODY = re.compile(r"^(todo!|unimplemented!|panic!\s*\(\s*\"not\s+implemented)")

# Phrases that describe code that was never written.
_PLACEHOLDER_PROSE = re.compile(
    r"(?i)\b(in a real implementation|in production( you| ,|,)| for now, |placeholder|"
    r"this is a (simplified|basic|dummy|mock)|replace this with|"
    r"you (would|should|may) (want to|need to)|example implementation)\b"
)

_MERGE_MARKERS = ("<<<<<<< ", "=======" + "=", ">>>>>>> ")


def _check_stubs(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.has_body or func.body_close < 0:
            continue
        body = " ".join(file.slice(func.body_open + 1, func.body_close).split())
        if _STUB_BODY.match(body):
            report.add(func.line, "unfinished_stub",
                       f"`{func.qualname}` has a `{body.split('(')[0]}` body",
                       "Finish it or delete it. A stub that satisfies a trait compiles, ships, "
                       "and then panics for whichever caller reaches it first.", "high")
        elif body in ("", "()") and func.return_type.strip() in ("", "()") \
                and not func.trait_name and func.is_public:
            report.add(func.line, "empty_public_function",
                       f"public `{func.qualname}` has an empty body",
                       "Either it does nothing (say so in a doc comment) or it is unfinished. "
                       "Both are worth an explicit answer.", "medium")


def _check_ignored_parameters(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.has_body or func.body_close < 0 or func.trait_name is not None:
            continue
        span = body_indices(func)
        if not span:
            continue
        used = {file.tokens[i].value for i in span if file.tokens[i].kind == "name"}
        for param in func.params:
            if param.is_self or not param.name.isidentifier() or param.name.startswith("_"):
                continue
            if param.name in used:
                continue
            severity = "high" if param.name in ("options", "config", "opts", "settings",
                                                "params", "context", "ctx") else "medium"
            report.add(param.line, "ignored_parameter",
                       f"`{func.qualname}` never reads `{param.name}`",
                       f"Every caller that passes a `{param.name}` is being silently ignored. "
                       "Use it, or delete it from the signature — an underscore prefix only "
                       "silences the warning, it does not answer the question.", severity)


def _check_duplicate_definitions(file: RsFile, report: Reporter) -> None:
    by_name: dict[tuple, list] = defaultdict(list)
    for func in file.functions:
        if func.kind == "closure" or not func.name:
            continue
        # A name defined once in the module and once in its `#[cfg(test)] mod`
        # is two different items in two different scopes.
        by_name[(func.owner, func.trait_name, func.name,
                 file.in_test_code(func.start))].append(func)
    for (owner, _trait, name, _in_test), group in by_name.items():
        if len(group) < 2:
            continue
        # Two `cfg`-gated definitions of one name are the supported way to write
        # a platform shim; only an unconditional pair is a problem.
        if any(any(a.startswith("cfg") for a in f.attrs) for f in group):
            continue
        lines = [f.line for f in group]
        report.add(lines[0], "duplicate_definition",
                   f"`{name}` is defined {len(group)} times"
                   + (f" in `{owner}`" if owner else " in this file"),
                   "Rust rejects duplicate items in one scope, so either these are in different "
                   "`cfg` branches (fine — check both are maintained) or the file does not "
                   "compile. Either way, two definitions of one name is not what was intended.",
                   "high", related=lines[1:])


def _check_placeholder_prose(file: RsFile, report: Reporter) -> None:
    for comment in file.comments:
        match = _PLACEHOLDER_PROSE.search(comment.value)
        if not match:
            continue
        report.add(comment.line, "placeholder_narration",
                   f'comment says "{match.group(0).strip()}"',
                   "The comment is describing code that was not written. Write it, or make the "
                   "gap explicit — a `todo!()` at least fails loudly, where a plausible "
                   "simplification just returns the wrong answer.", "medium")


def _check_merge_markers(file: RsFile, report: Reporter) -> None:
    for number, text in enumerate(file.lines, 1):
        if text.startswith(_MERGE_MARKERS):
            report.add(number, "merge_conflict_marker",
                       "an unresolved merge conflict marker",
                       "Resolve the conflict. This file does not compile.", "high")


def _check_unexplained_constants(file: RsFile, report: Reporter) -> None:
    suspicious = ("TIMEOUT", "RETRY", "RETRIES", "MAX", "LIMIT", "SIZE", "CAPACITY",
                  "BUFFER", "THRESHOLD", "TTL", "BACKOFF")
    for binding in file.bindings:
        if not any(word in binding.name.upper() for word in suspicious):
            continue
        if not re.fullmatch(r"[\d_]+(?:\.\d+)?(?:[a-z0-9]+)?", binding.value_text.strip()):
            continue
        if file.doc_lines_before(binding.line) or file.comments_on_lines(binding.line - 1,
                                                                        binding.line):
            continue
        report.add(binding.line, "unexplained_tuning_constant",
                   f"`{binding.name} = {binding.value_text}` with no stated origin",
                   "Say where the number came from — a measurement, a vendor limit, a spec "
                   "section. A plausible constant nobody can justify is the hardest kind to "
                   "change later, because nobody knows what it would break.", "low")


def _check_tests_that_cannot_fail(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        if not any(a.split("(")[0].strip() in ("test", "tokio::test") for a in func.attrs):
            continue
        if not func.has_body or func.body_close < 0:
            continue
        body = file.slice(func.body_open + 1, func.body_close)
        # `assert!(true)` and `assert_eq!(x, x)` — not `assert_eq!(true, flag)`,
        # which compares a value against a literal and is a real assertion.
        tautologies = (r"assert!\s*\(\s*(?:true|1\s*==\s*1|!\s*false)\s*[,)]",
                       r"assert_eq!\s*\(\s*([\w.]+)\s*,\s*\1\s*[,)]",
                       r"assert_eq!\s*\(\s*(\d+)\s*,\s*\1\s*[,)]")
        if any(re.search(pattern, body) for pattern in tautologies):
            report.add(func.line, "tautological_assertion",
                       f"`{func.name}` asserts something that is true by construction",
                       "Assert on the value the code under test produced. A test that cannot fail "
                       "is worse than no test: it makes the coverage number say the code is "
                       "checked.", "high")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_stubs(file, report)
    _check_ignored_parameters(file, report)
    _check_duplicate_definitions(file, report)
    _check_placeholder_prose(file, report)
    _check_merge_markers(file, report)
    _check_tests_that_cannot_fail(file, report)
    if not is_test_file(file.path):
        _check_unexplained_constants(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find unfinished scaffolding and generated-code tells",
        "No scaffolding problems found!",
        analyze,
    )
