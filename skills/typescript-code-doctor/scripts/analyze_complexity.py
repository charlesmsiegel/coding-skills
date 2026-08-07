#!/usr/bin/env python3
"""
Measure per-function complexity: cyclomatic, cognitive, nesting, size, arity.

Cyclomatic counts decisions. Cognitive counts how hard the decisions are to
*follow* — the same branch costs more three levels deep than at the top — which
is the number that tracks how a reader actually experiences the function.
"""

from common import Reporter, run_file_detector
from tsparse import TsFile

# Thresholds. Deliberately loose: the point is to rank files, not to fail a
# build, and a detector that fires on everything gets ignored on everything.
CYCLOMATIC_HIGH, CYCLOMATIC_MEDIUM = 20, 11
COGNITIVE_HIGH, COGNITIVE_MEDIUM = 25, 15
NESTING_HIGH, NESTING_MEDIUM = 6, 4
LINES_HIGH, LINES_MEDIUM = 120, 60
PARAMS_HIGH, PARAMS_MEDIUM = 7, 5
FILE_LINES_HIGH, FILE_LINES_MEDIUM = 1000, 500

# Keywords that branch. `case` counts, `default` does not (it is the fall-through).
BRANCH_KEYWORDS = frozenset({"if", "for", "while", "case", "catch"})
# Blocks that nest. `switch` nests its cases; `try` does not add a decision.
NESTING_KEYWORDS = frozenset({"if", "for", "while", "switch", "try", "catch", "do"})
LOGICAL_OPS = frozenset({"&&", "||", "??"})


def _is_ternary(file: TsFile, index: int) -> bool:
    """A `?` that branches, as opposed to an optional parameter or property."""
    token = file.tokens[index]
    if not token.is_op("?"):
        return False
    following = file.tokens[index + 1] if index + 1 < len(file) else None
    return following is not None and not following.is_op(":", ")", ",")


def _measure(file: TsFile, func) -> dict:
    """Cyclomatic, cognitive and nesting for one function body."""
    body = range(func.body_open + 1, func.body_close)
    cyclomatic, cognitive, max_nesting = 1, 0, 0
    # Brace indices that opened a nesting construct, innermost last.
    nesting_stack: list[int] = []
    inner_bodies = [
        (other.body_open, other.body_close) for other in file.functions
        if other is not func and other.has_body
        and func.body_open < other.body_open < func.body_close
    ]

    for index in body:
        token = file.tokens[index]
        while nesting_stack and index > nesting_stack[-1]:
            nesting_stack.pop()
        # A nested function's own complexity is reported against that function.
        if any(start < index < end for start, end in inner_bodies):
            continue
        depth = len(nesting_stack)
        if token.kind == "name" and token.value in BRANCH_KEYWORDS:
            cyclomatic += 1
            cognitive += 1 + depth
        elif token.is_name("else"):
            following = file.tokens[index + 1] if index + 1 < len(file) else None
            if following is None or not following.is_name("if"):
                cognitive += 1  # `else if` is one decision, already counted
        elif token.kind == "op" and token.value in LOGICAL_OPS:
            cyclomatic += 1
            previous = file.tokens[index - 1]
            if not (previous.kind == "op" and previous.value in LOGICAL_OPS):
                cognitive += 1  # a run of && / || reads as one condition
        elif _is_ternary(file, index):
            cyclomatic += 1
            cognitive += 1 + depth
        if token.kind == "name" and token.value in NESTING_KEYWORDS:
            block = _block_after(file, index, func.body_close)
            if block >= 0:
                nesting_stack.append(file.closer(block))
                max_nesting = max(max_nesting, len(nesting_stack))
    return {"cyclomatic": cyclomatic, "cognitive": cognitive, "nesting": max_nesting}


def _block_after(file: TsFile, keyword_index: int, stop: int) -> int:
    """The `{` belonging to a control keyword, or -1 for a braceless body."""
    cursor = keyword_index + 1
    if cursor < stop and file.tokens[cursor].is_op("("):
        cursor = file.skip_group(cursor)
    if cursor < stop and file.tokens[cursor].is_op("{") and file.closer(cursor) > 0:
        return cursor
    return -1


def _grade(value: int, medium: int, high: int) -> str | None:
    if value >= high:
        return "high"
    if value >= medium:
        return "medium"
    return None


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    for func in file.functions:
        if not func.has_body:
            continue
        name = func.qualname if func.name != "<anonymous>" else f"anonymous {func.kind}"
        metrics = _measure(file, func)

        severity = _grade(metrics["cyclomatic"], CYCLOMATIC_MEDIUM, CYCLOMATIC_HIGH)
        if severity:
            report.add(func.line, "high_cyclomatic_complexity",
                       f"{name} has cyclomatic complexity {metrics['cyclomatic']}",
                       "Extract the independent branches into named functions; replace an "
                       "if/else-if ladder over a tag with a lookup or a discriminated union.", severity)

        severity = _grade(metrics["cognitive"], COGNITIVE_MEDIUM, COGNITIVE_HIGH)
        if severity:
            report.add(func.line, "high_cognitive_complexity",
                       f"{name} has cognitive complexity {metrics['cognitive']} — nested decisions",
                       "Flatten with early returns/guard clauses. Depth costs a reader more than "
                       "breadth, so removing one level of nesting beats removing one branch.", severity)

        severity = _grade(metrics["nesting"], NESTING_MEDIUM, NESTING_HIGH)
        if severity:
            report.add(func.line, "deep_nesting",
                       f"{name} nests control flow {metrics['nesting']} levels deep",
                       "Invert the conditions and return early, or extract the inner block.", severity)

        lines = file.line_of(func.body_close) - func.line
        severity = _grade(lines, LINES_MEDIUM, LINES_HIGH)
        if severity:
            report.add(func.line, "long_function",
                       f"{name} is {lines} lines long",
                       "Split it along the seams already there — the comment headers inside it are "
                       "usually the function names you want.", severity)

        arity = len([p for p in func.params if p.accessibility is None])
        severity = _grade(arity, PARAMS_MEDIUM, PARAMS_HIGH)
        if severity and func.kind != "constructor":
            report.add(func.line, "long_parameter_list",
                       f"{name} takes {arity} parameters",
                       "Bundle the ones that travel together into an options object with a named "
                       "type — call sites become self-documenting and order stops mattering.", severity)

    total_lines = len(file.lines)
    severity = _grade(total_lines, FILE_LINES_MEDIUM, FILE_LINES_HIGH)
    if severity:
        report.add(1, "long_file",
                   f"{file.path.name} is {total_lines} lines with {len(file.exports)} export(s)",
                   "Split by responsibility, not by size. A module this long usually has two or "
                   "three groups of exports that never reference each other.", severity)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Measure cyclomatic/cognitive complexity, nesting, function size and arity",
        "No complexity problems found!",
        analyze,
    )
