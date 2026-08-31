#!/usr/bin/env python3
"""
Find error handling that turns a recoverable failure into a crash or a lie.

Rust's whole error story is `Result` plus `?`, and almost every problem here is
a place where that story was abandoned: `.unwrap()` inside a function that
already returns `Result` (where `?` was one character away), a `match` that
hand-rolls what `?` does, an error swallowed by `.ok();`, a cause thrown away by
`.map_err(|_| …)`, or a `panic!` in a library that gives the caller no choice.

Test code is held to a different standard on purpose: `.unwrap()` in a test is
an assertion, not a landmine, so the unwrap checks stay quiet there.
"""

import re

from common import Reporter, is_build_script, is_test_file, run_file_detector
from rsparse import RsFile, argument_spans, iter_calls, iter_method_calls, receiver_text

# Macros that end the process (or the thread) rather than returning a failure.
_PANIC_MACROS = {"panic!": "high", "todo!": "high", "unimplemented!": "high",
                 "unreachable!": "medium", "assert!": None, "expect!": None}

# `.expect("…")` messages that carry no information the panic did not already.
_USELESS_EXPECT = re.compile(
    r'^"(?:|failed|error|oops|should not happen|unwrap failed|expected value|'
    r'this should never happen|no|bad|todo|fixme|\W*)"$',
    re.IGNORECASE,
)


def _returns_fallible(func) -> bool:
    head = func.return_type.replace(" ", "")
    return head.startswith(("Result<", "Option<", "anyhow::Result", "io::Result",
                            "crate::Result", "Result <")) or head in ("Result", "Option")


def _is_main(func) -> bool:
    return func.name == "main" and func.owner is None


def _check_unwrap(file: RsFile, report: Reporter, testish: bool) -> None:
    for name_index, paren, method in iter_method_calls(file):
        if method not in ("unwrap", "expect", "unwrap_err", "expect_err"):
            continue
        if file.in_test_code(name_index) or testish:
            continue
        func = file.enclosing_function(name_index)
        line = file.line_of(name_index)
        receiver = receiver_text(file, name_index - 1)

        if func is not None and _returns_fallible(func):
            spans = argument_spans(file, paren)
            stated = method == "expect" and spans and len(file.slice(*spans[0]).strip()) > 12
            report.add(line, "unwrap_in_fallible_fn",
                       f"`.{method}()` inside `{func.qualname}`, which already returns "
                       f"{func.return_type or 'a fallible type'}",
                       "Use `?`. The function is already allowed to fail, so the panic buys "
                       "nothing and loses the caller's chance to handle it."
                       + (" The message states an invariant, so this may be deliberate — but the "
                          "invariant is not visible from the signature." if stated else ""),
                       "medium" if stated else "high")
            continue

        if method == "expect":
            spans = argument_spans(file, paren)
            message = file.slice(*spans[0]).strip() if spans else '""'
            if _USELESS_EXPECT.match(message):
                report.add(line, "uninformative_expect",
                           f"`.expect({message})` — the message says nothing the backtrace "
                           "would not have said",
                           "Say what invariant was violated and why it should hold: "
                           '`.expect("config was validated at startup")`.', "low")
            continue

        if receiver.endswith(".lock") or receiver.endswith(".read") or receiver.endswith(".write"):
            report.add(line, "unwrap_on_lock",
                       f"`{receiver}().unwrap()` panics when another thread panicked while holding "
                       "the lock",
                       "Often correct — poisoning usually *is* unrecoverable. Make it deliberate: "
                       "`.expect(\"<lock> poisoned\")`, or handle `PoisonError` where the data is "
                       "still usable.", "low")
            continue

        report.add(line, "unwrap_outside_tests",
                   f"`.{method}()` in production code turns a recoverable failure into a panic",
                   "Return `Result` and use `?`, or handle the `None`/`Err` case. If the value "
                   "genuinely cannot be absent, `.expect(\"why\")` documents the invariant.",
                   "medium")


def _check_panic_macros(file: RsFile, report: Reporter, testish: bool) -> None:
    if testish:
        return
    for index, callee in iter_calls(file):
        severity = _PANIC_MACROS.get(callee)
        if severity is None:
            continue
        head = index - 2 if file.value(index - 1) == "!" else index - 1
        if file.in_test_code(head):
            continue
        line = file.line_of(head)
        if callee in ("todo!", "unimplemented!"):
            report.add(line, "unfinished_panic",
                       f"`{callee}` — the function compiles but cannot run",
                       "Finish it or delete it. Shipped `todo!()` is a runtime crash wearing a "
                       "type signature.", "high")
        elif callee == "panic!":
            func = file.enclosing_function(head)
            fallible = func is not None and _returns_fallible(func)
            report.add(line, "panic_in_library",
                       "`panic!` aborts the caller's program instead of returning an error"
                       + (f", and `{func.qualname}` already returns {func.return_type}"
                          if fallible else ""),
                       "Return `Err(...)` and let the caller decide."
                       if fallible else
                       "Reserve `panic!` for an invariant *this* code maintains and has just "
                       "broken — where continuing would produce wrong data. If the caller could "
                       "have caused it, return an error instead; if not, say so in the message "
                       "and document it under `# Panics`.",
                       "high" if fallible else "medium")
        else:
            report.add(line, "unreachable_assertion",
                       "`unreachable!()` claims a state cannot happen",
                       "If the type system can express the claim, make it — an exhaustive `match` "
                       "on an enum needs no unreachable arm. If not, say why in the message.",
                       "medium")


def _check_manual_question_mark(file: RsFile, report: Reporter) -> None:
    """`match f() { Ok(v) => v, Err(e) => return Err(e) }` is `f()?`."""
    for index, token in enumerate(file.tokens):
        if not token.is_name("match"):
            continue
        brace = file.find_op("{", index + 1, min(index + 80, len(file.tokens)))
        if brace < 0:
            continue
        close = file.closer(brace)
        if close < 0 or close - brace > 90:
            continue
        body = file.slice(brace, close + 1)
        compact = " ".join(body.split())
        if "Ok(" not in compact or "Err(" not in compact:
            continue
        rethrown = (
            r"Err\s*\(\s*(\w+)\s*\)\s*=>\s*return\s+Err\s*\(\s*\1\s*\)",
            r"Err\s*\(\s*(\w+)\s*\)\s*=>\s*return\s+Err\s*\(\s*\1\s*\.\s*into\s*\(\s*\)\s*\)",
            r"Err\s*\(\s*(\w+)\s*\)\s*=>\s*return\s+Err\s*\(\s*[\w:]+::from\s*\(\s*\1\s*\)\s*\)",
        )
        if any(re.search(pattern, compact) for pattern in rethrown):
            report.add(token.line, "manual_question_mark",
                       "`match` that returns `Ok` through and re-returns `Err` unchanged",
                       "That is exactly `expr?`. `?` also applies `From` for the error type, so "
                       "the explicit conversion goes too.", "medium")


def _check_swallowed_errors(file: RsFile, report: Reporter, testish: bool) -> None:
    for name_index, paren, method in iter_method_calls(file):
        line = file.line_of(name_index)
        close = file.closer(paren)
        if method == "ok" and close >= 0 and file.value(close + 1) == ";":
            if not testish and not file.in_test_code(name_index):
                report.add(line, "error_discarded_by_ok",
                           "`.ok();` converts a `Result` to `Option` and then drops it — the error "
                           "is gone and nothing says the operation failed",
                           "Handle it, propagate it with `?`, or log it. If discarding is truly "
                           "intended, `let _ = …;` with a comment says so out loud.", "high")
        if method == "map_err":
            spans = argument_spans(file, paren)
            if spans and re.match(r"^\|\s*_\s*\|", file.slice(*spans[0]).strip()):
                report.add(line, "error_cause_dropped",
                           "`.map_err(|_| …)` throws the original error away",
                           "Keep the cause: `.map_err(MyError::Io)`, `#[from]` with thiserror, or "
                           "`anyhow::Context::context`. A wrapped error with no source is a dead "
                           "end during debugging.", "medium")
        if method in ("unwrap_or_default", "unwrap_or_else") and not testish:
            receiver = receiver_text(file, name_index - 1)
            if receiver.endswith(("?", ")")) and method == "unwrap_or_default":
                report.add(line, "error_replaced_by_default",
                           "`.unwrap_or_default()` turns a failure into an empty value, which the "
                           "caller cannot tell from a successful empty result",
                           "Propagate the error, or make the empty case explicit in the return "
                           "type.", "medium")

    for index, token in enumerate(file.tokens):
        if not token.is_name("Err"):
            continue
        if file.value(index + 1) != "(" or file.value(index - 1) == ".":
            continue
        close = file.closer(index + 1)
        if close < 0 or not file.value(close + 1) == "=>":
            continue
        body_start = close + 2
        empty = (file.value(body_start) == "{" and file.value(body_start + 1) == "}") \
            or (file.value(body_start) == "(" and file.value(body_start + 1) == ")")
        if empty and not file.in_test_code(index):
            report.add(token.line, "empty_error_arm",
                       "`Err(...) => {}` — the failure path does nothing at all",
                       "A silently ignored error is a wrong answer, not a survived failure. Log "
                       "it, propagate it, or state in a comment why dropping it is correct.",
                       "high")


def _check_error_types(file: RsFile, report: Reporter) -> None:
    displayed = {block.type_name.split("<")[0].strip()
                 for block in file.impls if block.trait_name and
                 block.trait_name.split("<")[0].strip().rsplit("::", 1)[-1] in ("Display", "Error")}
    for definition in file.types:
        if definition.kind not in ("struct", "enum") or not definition.name.endswith("Error"):
            continue
        derives = {d.rsplit("::", 1)[-1] for d in definition.derives}
        if derives & {"Error", "Display"} or definition.name in displayed:
            continue
        report.add(definition.line, "error_type_without_display",
                   f"`{definition.name}` is an error type with no `Display` or `Error` impl in "
                   "this file",
                   "Derive them (`thiserror::Error` gives both, plus `#[from]` for `?`), or write "
                   "the impls. Without `Display` the type cannot be reported to a user, and "
                   "without `Error` it does not compose with `Box<dyn Error>` or `anyhow`.",
                   "medium")

    for func in file.functions:
        if not func.is_public:
            continue
        head = func.return_type.replace(" ", "")
        if "Box<dynError" in head or "Box<dynstd::error::Error" in head:
            report.add(func.line, "boxed_error_in_public_api",
                       f"public `{func.qualname}` returns `Box<dyn Error>`, which callers can only "
                       "print — not match on",
                       "Return a concrete error enum (thiserror) from a library. `Box<dyn Error>` "
                       "and `anyhow::Error` belong in applications, where nothing downstream needs "
                       "to branch on the variant.", "medium")
        if re.search(r"Result<[^>]*,\s*String\s*>", func.return_type.replace(" ", " ")):
            report.add(func.line, "stringly_typed_error",
                       f"public `{func.qualname}` returns `Result<_, String>`",
                       "A `String` error cannot be matched, cannot carry a source, and cannot be "
                       "converted by `?`. Define an error enum.", "medium")


def _check_process_exit(file: RsFile, report: Reporter) -> None:
    for index, callee in iter_calls(file):
        if callee.rsplit("::", 1)[-1] != "exit" or "process" not in callee:
            continue
        func = file.enclosing_function(index)
        if func is not None and _is_main(func):
            continue
        if file.in_test_code(index):
            continue
        report.add(file.line_of(index), "process_exit_outside_main",
                   "`process::exit` ends the program from inside a function that is not `main` — "
                   "destructors do not run and callers have no say",
                   "Return an error up to `main` and exit there. Buffers that have not been "
                   "flushed are lost at the `exit` call.", "high")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    # A `build.rs` runs on the developer's machine at compile time: an unwrap
    # there is a deliberate build failure with a backtrace, which SKILL.md
    # already says. `is_build_script` existed for this and was never called.
    testish = is_test_file(file.path) or is_build_script(file.path)
    _check_unwrap(file, report, testish)
    _check_panic_macros(file, report, testish)
    _check_manual_question_mark(file, report)
    _check_swallowed_errors(file, report, testish)
    _check_error_types(file, report)
    _check_process_exit(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find error handling that panics, swallows, or hand-rolls `?`",
        "No error-handling problems found!",
        analyze,
    )
