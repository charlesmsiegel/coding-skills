#!/usr/bin/env python3
"""
Find hand-rolled loops that an iterator adaptor already expresses.

The rule is not "prefer functional style". It is that `xs.iter().sum()` states
the goal, while a loop with an accumulator states a procedure and leaves the
reader to infer the goal — and that `for i in 0..xs.len() { xs[i] }` reintroduces
the bounds check and the off-by-one that `for x in &xs` removed.

Where a loop is genuinely the clearer form — early exit with side effects, an
`.await` per item, two collections advanced at different rates — this stays
quiet.
"""

import re

from common import Reporter, run_file_detector
from rsparse import RsFile, iter_method_calls, receiver_text

_ACCUMULATOR_OPS = {"+=": ("sum", "adds"), "*=": ("product", "multiplies")}


def _loops(file: RsFile):
    """Yield (keyword_index, header_text, body_open, body_close) for each loop."""
    for index, token in enumerate(file.tokens):
        if not token.is_name("for", "while", "loop") or file.in_macro_body(index):
            continue
        # `while let Some(x) = it.next()` is a loop too; the header text below
        # carries the pattern, and the callers that care match on it.
        brace = file.find_op("{", index + 1, min(index + 80, len(file.tokens)))
        if brace < 0:
            continue
        close = file.closer(brace)
        if close < 0:
            continue
        yield index, file.slice(index + 1, brace).strip(), brace, close


def _check_index_loops(file: RsFile, report: Reporter) -> None:
    for index, header, brace, close in _loops(file):
        if not file.tokens[index].is_name("for"):
            continue
        match = re.match(r"^(\w+)\s+in\s+0\s*\.\.\s*([\w.]+)\s*\.\s*len\s*\(\s*\)\s*$",
                         " ".join(header.split()))
        if not match:
            continue
        counter, collection = match.group(1), match.group(2)
        body = file.slice(brace, close + 1)
        indexed = set(re.findall(r"(\w+)\s*\[\s*" + re.escape(counter) + r"\s*\]", body))
        other_uses = len(re.findall(r"\b" + re.escape(counter) + r"\b", body)) - sum(
            len(re.findall(re.escape(name) + r"\s*\[\s*" + re.escape(counter) + r"\s*\]", body))
            for name in indexed)

        if len(indexed) > 1:
            others = ", ".join(sorted(indexed - {collection})) or "another slice"
            report.add(file.tokens[index].line, "index_loop_over_two_slices",
                       f"`for {counter} in 0..{collection}.len()` indexing {len(indexed)} "
                       "collections in step",
                       f"`{collection}.iter().zip({others}.iter())` — but this is not a "
                       "behaviour-preserving rewrite: indexing panics when the second slice is "
                       "shorter, and `zip` silently stops early instead. Decide which you want. "
                       "If the lengths are an invariant, assert it first "
                       f"(`assert_eq!({collection}.len(), {others}.len())`) and then `zip`; if a "
                       "mismatch is a real error, return it rather than truncating.", "medium")
        elif other_uses > 0:
            report.add(file.tokens[index].line, "index_loop_needing_position",
                       f"`for {counter} in 0..{collection}.len()` where the index is used for more "
                       "than indexing",
                       f"`for ({counter}, item) in {collection}.iter().enumerate()` keeps the "
                       "position and removes the bounds check on every access.", "low")
        else:
            report.add(file.tokens[index].line, "index_loop_over_len",
                       f"`for {counter} in 0..{collection}.len()` whose body only does "
                       f"`{collection}[{counter}]`",
                       f"`for item in &{collection}` — no index to keep in sync, no bounds check "
                       "per access, and it works for any iterable.", "medium")


def _check_while_index_loops(file: RsFile, report: Reporter) -> None:
    for index, header, brace, close in _loops(file):
        if not file.tokens[index].is_name("while"):
            continue
        match = re.match(r"^(\w+)\s*<\s*([\w.]+)\s*\.\s*len\s*\(\s*\)\s*$", " ".join(header.split()))
        if not match:
            continue
        counter = match.group(1)
        body = file.slice(brace, close + 1)
        if not re.search(re.escape(counter) + r"\s*\+=\s*1", body):
            continue
        report.add(file.tokens[index].line, "while_index_loop",
                       f"`while {counter} < {match.group(2)}.len()` with a manual `{counter} += 1`",
                       f"`for item in &{match.group(2)}` (or `.iter().enumerate()`). A `continue` "
                       "anywhere in this body skips the increment and hangs the program.", "high")


def _check_accumulator_loops(file: RsFile, report: Reporter) -> None:
    for index, header, brace, close in _loops(file):
        if not file.tokens[index].is_name("for"):
            continue
        statements = [i for i in range(brace + 1, close)
                      if file.tokens[i].kind == "op" and file.tokens[i].value == ";"]
        if len(statements) > 1:
            continue
        body = " ".join(file.slice(brace + 1, close).split())
        for operator, (adaptor, verb) in _ACCUMULATOR_OPS.items():
            match = re.fullmatch(r"(\w+)\s*" + re.escape(operator) + r"\s*([^;]+);?", body)
            if not match:
                continue
            subject = re.match(r"^\s*\w+\s+in\s+(.+)$", " ".join(header.split()))
            source = subject.group(1) if subject else "the iterator"
            accumulator = match.group(1)
            # `.sum()` starts from the identity. An accumulator seeded with
            # anything else would silently lose that seed in the rewrite.
            seed = _accumulator_seed(file, accumulator, index)
            identity = "0" if adaptor == "sum" else "1"
            if seed is not None and seed not in (identity, f"{identity}u32", f"{identity}u64",
                                                 f"{identity}usize", f"{identity}i32",
                                                 f"{identity}i64", f"{identity}.0"):
                report.add(file.tokens[index].line, "manual_fold",
                           f"loop that {verb} into `{accumulator}`, which starts at `{seed}` "
                           f"rather than {identity}",
                           f"`{source}.fold({seed}, |acc, x| acc {operator[0]} …)` — "
                           f"`.{adaptor}()` starts "
                           f"from {identity} and would silently drop the `{seed}`.", "low")
                break
            report.add(file.tokens[index].line, "manual_" + adaptor,
                       f"loop whose whole body {verb} into `{accumulator}`",
                       f"`let {accumulator} = {source}.map(|x| …).{adaptor}::<_>();` — the "
                       f"adaptor names the operation and cannot forget to initialise the "
                       "accumulator.", "low")
            break


def _check_push_loops(file: RsFile, report: Reporter) -> None:
    for index, header, brace, close in _loops(file):
        if not file.tokens[index].is_name("for") or close - brace > 60:
            continue
        pushes = [(n, p) for n, p, m in iter_method_calls(file, brace, close) if m == "push"]
        if len(pushes) != 1:
            continue
        body_span = range(brace + 1, close)
        if any(file.tokens[i].is_name("break", "continue", "return") for i in body_span):
            continue
        if any(file.tokens[i].is_name("await") for i in body_span):
            continue
        # `.filter(…)` changes how many elements come out, so it is only the
        # right suggestion when the conditional actually guards the push. An
        # `if` that wraps a side effect beside an unconditional push is not one.
        push_index = pushes[0][0]
        conditional = any(_encloses(file, i, push_index)
                          for i in body_span if file.tokens[i].is_name("if"))
        target = receiver_text(file, pushes[0][0] - 1)
        source = re.match(r"^\s*(.+?)\s+in\s+(.+)$", " ".join(header.split()))
        origin = source.group(2) if source else "the iterator"
        report.add(file.tokens[index].line, "loop_building_collection",
                   f"loop whose only effect is `{target}.push(…)`",
                   (f"`let {target}: Vec<_> = {origin}.filter(…).map(…).collect();` — the filter "
                    "and the transform become visible instead of implied."
                    if conditional else
                    f"`let {target}: Vec<_> = {origin}.map(…).collect();` — and `{target}` can then "
                    "be immutable."), "medium")


def _encloses(file: RsFile, keyword: int, index: int) -> bool:
    """True when the `if` at ``keyword`` has a block containing ``index``."""
    brace = file.find_op("{", keyword + 1, min(keyword + 40, len(file.tokens)))
    if brace < 0:
        return False
    close = file.closer(brace)
    if close > 0 and brace < index < close:
        return True
    # `else { … }`, which guards the push just as much as the `then` branch.
    if close > 0 and file.tok(close + 1) is not None and file.tokens[close + 1].is_name("else"):
        tail = file.find_op("{", close + 1, min(close + 6, len(file.tokens)))
        if tail > 0:
            end = file.closer(tail)
            return end > 0 and tail < index < end
    return False


def _accumulator_seed(file: RsFile, name: str, before: int) -> str | None:
    """The initialiser of `let mut <name> = …;` above ``before``, or None."""
    for index in range(before - 1, -1, -1):
        if not file.tokens[index].is_name("let"):
            continue
        cursor = index + 1
        if file.value(cursor) == "mut":
            cursor += 1
        if file.value(cursor) != name:
            continue
        equals = file.find_op("=", cursor, min(cursor + 8, len(file.tokens)))
        semi = file.find_op(";", cursor, len(file.tokens))
        if equals < 0 or semi < 0 or equals > semi:
            return None
        return file.slice(equals + 1, semi).strip()
    return None


def _check_manual_find(file: RsFile, report: Reporter) -> None:
    for index, _header, brace, close in _loops(file):
        if not file.tokens[index].is_name("for") or close - brace > 60:
            continue
        body = " ".join(file.slice(brace + 1, close).split())
        if not re.search(r"\bbreak\b", body):
            continue
        if re.search(r"return\s+(Some|true|false)\b", body) or re.search(r"=\s*Some\s*\(", body):
            report.add(file.tokens[index].line, "manual_find",
                       "loop that scans for the first match and then breaks",
                       "`.find(|x| …)` (or `.any(|x| …)` when only the yes/no matters, "
                       "`.position(|x| …)` when it is the index). All three stop at the first "
                       "match, and none of them can forget the `break`.", "low")


def _check_loop_break_condition(file: RsFile, report: Reporter) -> None:
    for index, _header, brace, close in _loops(file):
        if not file.tokens[index].is_name("loop"):
            continue
        first = brace + 1
        if not file.tokens[first].is_name("if"):
            continue
        inner_brace = file.find_op("{", first, min(first + 30, close))
        if inner_brace < 0:
            continue
        inner_close = file.closer(inner_brace)
        if inner_close < 0:
            continue
        inner = " ".join(file.slice(inner_brace + 1, inner_close).split())
        if inner.strip(";").strip() != "break":
            continue
        condition = " ".join(file.slice(first + 1, inner_brace).split())
        report.add(file.tokens[index].line, "loop_with_leading_break",
                   f"`loop {{ if {condition} {{ break }} … }}`",
                   f"`while !({condition}) {{ … }}` — the exit condition belongs in the header "
                   "where a reader looks for it.", "low")


def _check_explicit_iter(file: RsFile, report: Reporter) -> None:
    for index, header, brace, close in _loops(file):
        if not file.tokens[index].is_name("for"):
            continue
        compact = " ".join(header.split())
        match = re.match(r"^\w+\s+in\s+([\w.]+)\.iter\(\)\s*$", compact)
        if match:
            report.add(file.tokens[index].line, "explicit_iter_in_for",
                       f"`for … in {match.group(1)}.iter()`",
                       f"`for … in &{match.group(1)}` — `IntoIterator` does the same thing with "
                       "less punctuation, and the `&` makes the borrow visible.", "low")


def _check_manual_flatten(file: RsFile, report: Reporter) -> None:
    for index, header, brace, close in _loops(file):
        if not file.tokens[index].is_name("for"):
            continue
        first = brace + 1
        if not (file.tokens[first].is_name("if") and file.tok(first + 1) is not None
                and file.tokens[first + 1].is_name("let")):
            continue
        pattern = file.slice(first + 2, min(first + 6, close))
        if not pattern.startswith(("Some", "Ok")):
            continue
        inner_brace = file.find_op("{", first, close)
        if inner_brace < 0 or file.closer(inner_brace) != close - 1:
            continue  # the `if let` must be the whole body
        report.add(file.tokens[index].line, "manual_flatten",
                   f"`for … {{ if let {pattern.split('(')[0]}(…) = … }}` — the whole body is one "
                   "unwrapping test",
                   "`.flatten()` on the iterator (or `.filter_map(|x| x)`) drops the `None`s "
                   "before the loop body, leaving one level of indentation instead of two.", "low")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_index_loops(file, report)
    _check_while_index_loops(file, report)
    _check_accumulator_loops(file, report)
    _check_push_loops(file, report)
    _check_manual_find(file, report)
    _check_loop_break_condition(file, report)
    _check_explicit_iter(file, report)
    _check_manual_flatten(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find loops an iterator adaptor already expresses",
        "No loop simplifications found!",
        analyze,
    )
