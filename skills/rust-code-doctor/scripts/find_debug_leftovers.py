#!/usr/bin/env python3
"""
Find debugging aids that were never taken out.

`dbg!` prints to stderr with a file and line and is never meant to ship.
`println!` in a library writes to a stream the caller may be using for data.
A crate-level `#![allow(warnings)]` turns off the compiler that would have
caught the rest of this file.
"""

from common import Reporter, is_test_file, run_file_detector
from rsparse import RsFile, is_doc_comment, iter_calls

# Printing macros and where each belongs.
_PRINT_MACROS = {
    "println!": "stdout",
    "print!": "stdout",
    "eprintln!": "stderr",
    "eprint!": "stderr",
}


def _is_binary(file: RsFile) -> bool:
    """A `main.rs`, an example or a bin target may legitimately print."""
    parts = [p.lower() for p in file.path.parts]
    return file.path.name in ("main.rs", "build.rs") or "bin" in parts or "examples" in parts


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    testish = is_test_file(file.path)
    binary = _is_binary(file)

    for index, callee in iter_calls(file):
        head = index - 2 if file.value(index - 1) == "!" else index - 1
        line = file.line_of(head)
        if callee == "dbg!":
            report.add(line, "dbg_macro",
                       "`dbg!(…)` left in the source",
                       "Remove it. It prints the file, the line and the expression to stderr on "
                       "every call, and it moves the value in and out, which is why deleting it "
                       "sometimes changes what compiles.",
                       "low" if testish else "high")
            continue
        stream = _PRINT_MACROS.get(callee)
        if stream is None or testish or binary or file.in_test_code(head):
            continue
        if stream == "stdout":
            report.add(line, "print_in_library",
                       f"`{callee}` writes to stdout from library code",
                       "A library that prints has decided what its caller's stdout is for — and "
                       "in a CLI that pipes data, this corrupts the output. Use the `log`/`tracing`"
                       " facade and let the binary choose where it goes.", "medium")
        else:
            report.add(line, "eprintln_instead_of_log",
                       f"`{callee}` writes to stderr from library code",
                       "`log::warn!`/`tracing::warn!` carries a level, a target and a timestamp, "
                       "and can be filtered or turned off by the application. `eprintln!` can "
                       "only be endured.", "low")

    for attribute in file.inner_attrs:
        compact = attribute.replace(" ", "")
        if compact.startswith("allow(") and any(
                lint in compact for lint in ("warnings", "unused", "dead_code")):
            report.add(1, "crate_level_warning_suppression",
                       f"`#![{attribute}]` silences a whole class of warnings for the crate",
                       "This is the compiler's own dead-code and unused-import analysis being "
                       "turned off. Fix the warnings, or scope the `allow` to the item that needs "
                       "it — a crate-level one also hides everything added later.", "medium")

    for comment in file.comments:
        if is_doc_comment(comment):
            continue  # a `///` example is documentation, and `cargo test` runs it
        text = comment.value.strip().lstrip("/").strip()
        if text.startswith(("println!", "print!", "eprintln!", "eprint!", "dbg!")):
            report.add(comment.line, "commented_out_debugging",
                       "a commented-out debug print",
                       "Delete it — git has the version where it was live.", "low")
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find debugging aids left in the source",
        "No debug leftovers found!",
        analyze,
    )
