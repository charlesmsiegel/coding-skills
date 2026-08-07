#!/usr/bin/env python3
"""
Find hand-rolled loops that a built-in array method already does.

The rule is not "prefer functional style". It is that `items.some(isReady)`
states the goal, while an index loop with a flag and a break states a procedure
and leaves the reader to infer the goal. Where a loop is genuinely the clearer
form — early exit with side effects, or `await` per item — this stays quiet.
"""

from common import Reporter, run_file_detector
from tsparse import TsFile, argument_spans, iter_calls


def _check_index_loops(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("for") or index + 1 >= len(file):
            continue
        clause = index + 1
        if not file.tokens[clause].is_op("("):
            continue
        clause_end = file.closer(clause)
        if clause_end < 0:
            continue
        text = file.slice(clause + 1, clause_end)
        if " in " in f" {text} " or text.lstrip().startswith(("const ", "let ")) and " of " in text:
            continue
        if ".length" not in text or "++" not in text:
            continue
        body = clause_end + 1
        if body >= len(file) or not file.tokens[body].is_op("{"):
            continue
        close = file.closer(body)
        counter = ""
        for probe in range(clause + 1, clause_end):
            if file.tokens[probe].is_name("let", "var") and probe + 1 < clause_end:
                counter = file.tokens[probe + 1].value
                break
        if counter and _index_used_beyond_element(file, counter, body, close):
            continue
        report.add(token.line, "index_loop_over_array",
                   "Classic `for (let i = 0; i < xs.length; i++)` loop",
                   "Use `for (const x of xs)` when you need the element, or `map`/`filter`/`reduce` "
                   "when you are building a value. The index is bookkeeping the reader has to "
                   "verify — off-by-one lives here.", "medium")


def _index_used_beyond_element(file: TsFile, counter: str, body: int, close: int) -> bool:
    """True when the index is used for something other than `xs[i]`."""
    if close < 0:
        return True
    for probe in range(body, close):
        token = file.tokens[probe]
        if token.kind != "name" or token.value != counter:
            continue
        previous = file.tokens[probe - 1] if probe else None
        following = file.tokens[probe + 1] if probe + 1 < len(file) else None
        indexing = previous is not None and previous.is_op("[") and following is not None and following.is_op("]")
        if not indexing:
            return True
    return False


def _check_for_in_over_array(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("in") or index + 1 >= len(file):
            continue
        enclosing_for = index - 1
        while enclosing_for > 0 and not file.tokens[enclosing_for].is_op("("):
            enclosing_for -= 1
        if enclosing_for <= 0 or not file.tokens[enclosing_for - 1].is_name("for"):
            continue
        subject = file.tokens[index + 1]
        if subject.kind != "name":
            continue
        report.add(token.line, "for_in_loop",
                   f"`for (… in {subject.value})` iterates string keys, including inherited ones",
                   "Use `for (const x of xs)` for arrays and `Object.entries(obj)` for objects. "
                   "`for…in` gives you string indices in unspecified order.", "high")


def _check_push_loops(file: TsFile, report: Reporter) -> None:
    """A loop whose entire body pushes into an array is a `map` or a `filter`."""
    for index, token in enumerate(file.tokens):
        if not token.is_name("for") or index + 1 >= len(file):
            continue
        clause = index + 1
        if not file.tokens[clause].is_op("("):
            continue
        body = file.skip_group(clause)
        if body >= len(file) or not file.tokens[body].is_op("{"):
            continue
        close = file.closer(body)
        if close < 0 or close - body > 40:
            continue
        calls = list(iter_calls(file, body, close))
        pushes = [c for c in calls if c[1].rsplit(".", 1)[-1] == "push"]
        if len(pushes) != 1 or len(calls) > 3:
            continue
        has_control = any(file.tokens[p].is_name("break", "continue", "return", "await")
                          for p in range(body, close))
        conditional = any(file.tokens[p].is_name("if") for p in range(body, close))
        if has_control:
            continue
        report.add(token.line, "loop_building_array",
                   "Loop whose only effect is pushing into an array",
                   "`const out = xs.filter(…).map(…)` says what is being built; the loop makes the "
                   "reader reconstruct it." if conditional else
                   "`const out = xs.map(…)` — and `out` can then be `const`.", "medium")


def _check_string_accumulation(file: TsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_op("+=") or index == 0:
            continue
        following = file.tokens[index + 1] if index + 1 < len(file) else None
        if following is None or following.kind not in ("str", "template", "name"):
            continue
        enclosing = _enclosing_loop(file, index)
        if enclosing < 0:
            continue
        if following.kind == "name" and not any(
                file.tokens[p].kind in ("str", "template") for p in range(index, min(index + 6, len(file)))):
            continue
        report.add(token.line, "string_concat_in_loop",
                   f"`{file.tokens[index - 1].value} += …` builds a string one iteration at a time",
                   "Collect the pieces and `join('')` once. Repeated concatenation is quadratic in "
                   "the worst case and hides what the final shape is.", "low")


def _enclosing_loop(file: TsFile, index: int) -> int:
    for probe in range(index, max(index - 400, -1), -1):
        token = file.tokens[probe]
        if token.is_name("for", "while") and probe + 1 < len(file) and file.tokens[probe + 1].is_op("("):
            body = file.skip_group(probe + 1)
            if body < len(file) and file.tokens[body].is_op("{") and file.closer(body) > index:
                return probe
    return -1


def _check_array_method_chains(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        method = callee.rsplit(".", 1)[-1]
        close = file.closer(paren)
        if close < 0 or "." not in callee:
            continue
        after = file.tokens[close + 1] if close + 1 < len(file) else None
        line = file.tokens[paren].line
        if method == "filter" and after is not None:
            if after.is_op(".") and file.tokens[close + 2].is_name("length") \
                    and close + 3 < len(file) and file.tokens[close + 3].is_op(">", "===", "!==", "==", ">="):
                report.add(line, "filter_length_instead_of_some",
                           "`.filter(…).length > 0` builds a whole array to answer a yes/no question",
                           "Use `.some(…)` — it stops at the first match.", "low")
            elif after.is_op("[") and file.tokens[close + 2].kind == "num" and file.tokens[close + 2].value == "0":
                report.add(line, "filter_first_instead_of_find",
                           "`.filter(…)[0]` scans everything to take the first match",
                           "Use `.find(…)`, which returns `T | undefined` and stops early.", "low")
        elif method == "map" and after is not None and after.is_op("."):
            follower = file.tokens[close + 2]
            if follower.is_name("filter") and close + 3 < len(file):
                inner = argument_spans(file, close + 3)
                if inner and file.slice(*inner[0]).strip() == "Boolean":
                    report.add(line, "map_filter_boolean",
                               "`.map(…).filter(Boolean)` walks the list twice and does not narrow the type",
                               "Use `.flatMap(x => cond ? [value] : [])`, which produces the narrowed "
                               "element type directly.", "low")
        elif method == "forEach":
            spans = argument_spans(file, paren)
            body_text = file.slice(*spans[0]) if spans else ""
            if body_text.count("push(") == 1 and "if" not in body_text:
                report.add(line, "foreach_building_array",
                           "`.forEach(…)` whose body only pushes",
                           "That is `.map(…)`. `forEach` returns nothing, so the result has to be "
                           "carried in a mutable variable declared outside.", "low")


def _check_object_keys_iteration(file: TsFile, report: Reporter) -> None:
    for paren, callee in iter_calls(file):
        if callee != "Object.keys":
            continue
        close = file.closer(paren)
        if close < 0 or close + 2 >= len(file):
            continue
        if not file.tokens[close + 1].is_op("."):
            continue
        method = file.tokens[close + 2]
        if not method.is_name("forEach", "map", "filter", "reduce"):
            continue
        subject = file.slice(paren + 1, close).strip()
        window = file.slice(close, min(close + 60, len(file)))
        if f"{subject}[" not in window:
            continue
        report.add(file.tokens[paren].line, "object_keys_then_lookup",
                   f"`Object.keys({subject}).{method.value}(…)` followed by `{subject}[key]`",
                   "Use `Object.entries(obj)` and destructure `[key, value]` — one lookup instead of "
                   "two, and the value keeps its type.", "low")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_index_loops(file, report)
    _check_for_in_over_array(file, report)
    _check_push_loops(file, report)
    _check_string_accumulation(file, report)
    _check_array_method_chains(file, report)
    _check_object_keys_iteration(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find loops that a built-in array method already expresses",
        "No loop simplifications found!",
        analyze,
    )
