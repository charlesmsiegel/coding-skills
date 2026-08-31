#!/usr/bin/env python3
"""
Measure the complexity of each function: branching, nesting, size and arity.

Cyclomatic complexity counts decision points — how many paths a test suite has
to cover. Cognitive complexity weights them by how hard they are to hold in
your head, so a flat `match` with nine arms scores low (it is a table) while
three levels of nested `if` inside a loop scores high (it is not).

Rust-specific counting: `?` is a branch (it can return), each `match` arm past
the first is a decision, and `&&`/`||` short-circuit. A long `match` on an enum
is deliberately cheap — exhaustiveness is the point of the language feature.
"""

from common import Reporter, is_test_file, run_file_detector
from rsparse import RsFile, body_indices, split_top_level

# Thresholds. Chosen to fire on genuinely hard functions rather than on merely
# long ones — a 60-line function of straight-line setup is not the problem.
MAX_CYCLOMATIC = 12
MAX_COGNITIVE = 18
MAX_NESTING = 4
MAX_LINES = 80
MAX_PARAMS = 6
MAX_MATCH_ARMS = 14

_BRANCH_KEYWORDS = frozenset({"if", "while", "for", "loop"})

# The function body's own brace, which is not nesting the reader has to hold.
_BODY_DEPTH = 1


def _match_arm_count(file: RsFile, index: int) -> int:
    brace = file.find_op("{", index + 1, min(index + 120, len(file.tokens)))
    if brace < 0:
        return 0
    close = file.closer(brace)
    if close < 0:
        return 0
    return sum(1 for start, end in split_top_level(file, brace + 1, close)
               if file.find_op("=>", start, end) >= 0)


def _metrics(file: RsFile, func) -> dict:
    span = body_indices(func)
    cyclomatic, cognitive, depth, max_depth = 1, 0, 0, 0
    match_arms = 0
    for index in span:
        token = file.tokens[index]
        if token.kind == "op":
            if token.value == "{":
                depth += 1
                max_depth = max(max_depth, depth)
            elif token.value == "}":
                depth = max(0, depth - 1)
            elif token.value in ("&&", "||"):
                cyclomatic += 1
                cognitive += 1
            elif token.value == "?":
                cyclomatic += 1
            continue
        if token.kind != "name":
            continue
        if token.value in _BRANCH_KEYWORDS:
            if token.value == "if" and file.tokens[index - 1].is_name("else"):
                cognitive += 1  # an `else if` chain nests conceptually, not structurally
            else:
                cognitive += max(1, depth - _BODY_DEPTH)
            cyclomatic += 1
        elif token.value == "match":
            arms = _match_arm_count(file, index)
            match_arms = max(match_arms, arms)
            cyclomatic += max(0, arms - 1)
            # A `match` is a table: one unit of cognitive load regardless of width.
            cognitive += max(1, depth - _BODY_DEPTH)
    return {
        "cyclomatic": cyclomatic,
        "cognitive": cognitive,
        "nesting": max(0, max_depth - 1),
        "lines": file.line_of(func.body_close) - func.line + 1 if func.body_close > 0 else 0,
        "params": len([p for p in func.params if not p.is_self]),
        "match_arms": match_arms,
    }


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    lenient = is_test_file(file.path)
    for func in file.functions:
        if not func.has_body or func.body_close < 0:
            continue
        metrics = _metrics(file, func)
        name = func.qualname

        if metrics["cyclomatic"] > MAX_CYCLOMATIC:
            report.add(func.line, "high_cyclomatic_complexity",
                       f"`{name}` has cyclomatic complexity {metrics['cyclomatic']} "
                       f"(threshold {MAX_CYCLOMATIC})",
                       "Every decision point is a path a test has to reach. Extract the "
                       "independent branches into named functions — the name is the documentation "
                       "the branch currently lacks.",
                       "high" if metrics["cyclomatic"] > MAX_CYCLOMATIC * 2 else "medium")

        if metrics["cognitive"] > MAX_COGNITIVE:
            report.add(func.line, "high_cognitive_complexity",
                       f"`{name}` has cognitive complexity {metrics['cognitive']} "
                       f"(threshold {MAX_COGNITIVE})",
                       "Nesting is what costs the reader, not branch count. Use `let … else` and "
                       "`?` to return early, and flatten the happy path to the left margin.",
                       "medium")

        if metrics["nesting"] > MAX_NESTING:
            report.add(func.line, "deep_nesting",
                       f"`{name}` nests {metrics['nesting']} levels deep",
                       "`let Some(x) = opt else { return … };` and `?` remove a level each. Rust "
                       "gives you more early-exit forms than most languages precisely so bodies "
                       "stay flat.", "medium")

        if metrics["lines"] > MAX_LINES and not lenient:
            report.add(func.line, "long_function",
                       f"`{name}` is {metrics['lines']} lines",
                       "Length alone is not a defect, but a function this size usually has "
                       "sections with names — extract each section into the function it wants to "
                       "be.", "low")

        if metrics["params"] > MAX_PARAMS:
            report.add(func.line, "too_many_parameters",
                       f"`{name}` takes {metrics['params']} parameters",
                       "Group the ones that always travel together into a struct. Rust has no "
                       "keyword arguments, so a six-argument call site is six positions a reader "
                       "has to count.", "medium")

        if metrics["match_arms"] > MAX_MATCH_ARMS:
            report.add(func.line, "very_wide_match",
                       f"`{name}` contains a `match` with {metrics['match_arms']} arms",
                       "Exhaustiveness is worth having, so this is not automatically wrong. But "
                       "if the arms are dispatching behaviour rather than mapping values, that is "
                       "a trait or a method on the enum.", "low")
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Measure per-function complexity, nesting, size and arity",
        "No complexity problems found!",
        analyze,
    )
