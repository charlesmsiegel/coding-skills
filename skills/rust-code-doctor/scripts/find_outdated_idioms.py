#!/usr/bin/env python3
"""
Find spellings that a later edition, or a later standard library, replaced.

None of this is broken. It is the accumulated residue of the edition the file
was written in — `extern crate`, `#[macro_use]`, `Box<Trait>` without `dyn`,
`lazy_static!` where `LazyLock` now exists, `try!` where `?` does — and every
item costs a reader who learned Rust after it changed.

Modernising is also how an edition bump stops being a big-bang branch: fix the
spellings first, on the old edition, where each change is independently
reviewable.
"""

import re

from common import Reporter, run_file_detector
from rsparse import RsFile, iter_calls, iter_method_calls

# Method renames the standard library made, old -> new.
_RENAMED_METHODS = {
    "trim_left": "trim_start",
    "trim_right": "trim_end",
    "trim_left_matches": "trim_start_matches",
    "trim_right_matches": "trim_end_matches",
    "description": "`Display` (`Error::description` is deprecated and returns a useless string)",
    "to_uppercase_ascii": "to_ascii_uppercase",
}

_FORMAT_MACROS = frozenset({
    "format!", "println!", "print!", "eprintln!", "eprint!", "write!", "writeln!",
    "panic!", "assert!", "format_args!",
})


def _check_edition_syntax(file: RsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if token.is_name("extern") and file.tok(index + 1) is not None \
                and file.tokens[index + 1].is_name("crate"):
            name = file.value(index + 2)
            report.add(token.line, "extern_crate",
                       f"`extern crate {name};` — the 2015 edition's import mechanism",
                       "Delete it. Since the 2018 edition a dependency in Cargo.toml is in scope "
                       "as `use <name>::…` with no declaration at all.", "low")
        if token.is_op("...") and index and not file.tokens[index - 1].is_op("("):
            report.add(token.line, "inclusive_range_dots",
                       "`...` as an inclusive range",
                       "`..=`. The three-dot form is deprecated outside patterns and removed in "
                       "later editions.", "low")

    for attribute in file.inner_attrs:
        if attribute.startswith("macro_use"):
            report.add(1, "macro_use_attribute",
                       "`#![macro_use]` pulls macros in by wildcard",
                       "`use some_crate::the_macro;` — macros have been importable by path since "
                       "the 2018 edition, and the wildcard is why nobody can tell where a macro "
                       "came from.", "low")

    for index, token in enumerate(file.tokens):
        if not token.is_op("#") or not file.value(index + 1) == "[":
            continue
        close = file.closer(index + 1)
        if close < 0:
            continue
        if file.slice(index + 2, close).strip().startswith("macro_use"):
            report.add(token.line, "macro_use_attribute",
                       "`#[macro_use]` pulls macros in by wildcard",
                       "Import the macro by path: `use some_crate::the_macro;`.", "low")


def _check_boxed_trait_without_dyn(file: RsFile, report: Reporter) -> None:
    """`Box<Trait>` — legal in 2015, a warning since 2018, an error in 2021."""
    for index, token in enumerate(file.tokens):
        if not token.is_name("Box", "Rc", "Arc"):
            continue
        if not file.tok(index + 1) or not file.tokens[index + 1].is_op("<"):
            continue
        # Step over a path: `std::fmt::Debug` is one name for this purpose.
        cursor = index + 2
        inner = file.tok(cursor)
        if inner is None or inner.kind != "name" or inner.value in ("dyn", "impl"):
            continue
        while file.value(cursor + 1) == "::" and file.tok(cursor + 2) is not None \
                and file.tokens[cursor + 2].kind == "name":
            cursor += 2
            inner = file.tokens[cursor]
        after = file.tok(cursor + 1)
        if after is None or not (after.is_op(">") or after.is_op("+")):
            continue
        if inner.value[:1].islower() or inner.value in ("String", "Self"):
            continue
        if after.is_op("+"):
            report.add(token.line, "bare_trait_object",
                       f"`{token.value}<{inner.value} + …>` — a trait object with no `dyn`",
                       f"`{token.value}<dyn {inner.value} + …>`. Without `dyn` nothing in the type "
                       "says the call is virtual, which is exactly why the keyword was added.",
                       "medium")


def _check_replaced_apis(file: RsFile, report: Reporter) -> None:
    for index, callee in iter_calls(file):
        line = file.line_of(index)
        if callee == "try!":
            report.add(line, "try_macro",
                       "`try!(…)` — replaced by `?` in Rust 1.13 and a reserved word since 2018",
                       "`expr?`.", "high")
        elif callee == "lazy_static!":
            report.add(line, "lazy_static_macro",
                       "`lazy_static!` for a lazily initialised global",
                       "`std::sync::LazyLock` (1.80+) or `OnceLock` — in the standard library, "
                       "no macro, and the type is visible at the declaration.", "low")

    for name_index, _paren, method in iter_method_calls(file):
        replacement = _RENAMED_METHODS.get(method)
        if replacement is None:
            continue
        report.add(file.line_of(name_index), "renamed_std_method",
                   f"`.{method}()` was renamed",
                   f"Use `.{replacement}()`." if not replacement.startswith("`")
                   else f"Use {replacement}.", "low")


def _check_format_arguments(file: RsFile, report: Reporter) -> None:
    """`format!("{}", name)` has read `format!("{name}")` since Rust 1.58."""
    for index, callee in iter_calls(file):
        if callee not in _FORMAT_MACROS:
            continue
        close = file.closer(index)
        if close < 0:
            continue
        text = file.slice(index + 1, close)
        parts = [p.strip() for p in text.split(",")]
        if len(parts) < 2 or not parts[0].startswith('"'):
            continue
        placeholders = re.findall(r"\{([^{}]*)\}", parts[0])
        if any(p and not p.startswith(":") for p in placeholders):
            continue  # already named or positional
        if len(placeholders) != len(parts) - 1:
            continue
        if all(re.fullmatch(r"[a-z_][a-z0-9_]*", p) for p in parts[1:]):
            report.add(file.line_of(index), "uninlined_format_args",
                       f"`{callee}` passes plain variables positionally",
                       "Inline them: `format!(\"{name} at {line}\")`. Since 1.58 the captured form "
                       "cannot desynchronise the placeholder order from the argument order.",
                       "low")


def _check_ref_patterns(file: RsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("ref"):
            continue
        head = file.tok(index - 1)
        # Only `let ref x = …`, where `let x = &…` is always the clearer form.
        # `ref` inside a `match` arm is often load-bearing, and match ergonomics
        # only sometimes remove the need for it — not a call a scanner can make.
        if head is None or not head.is_name("let"):
            continue
        report.add(token.line, "ref_pattern",
                   "`let ref x = …`",
                   "`let x = &…;` — the reference is then visible where it is taken rather than "
                   "hidden in the pattern.", "low")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_edition_syntax(file, report)
    _check_boxed_trait_without_dyn(file, report)
    _check_replaced_apis(file, report)
    _check_format_arguments(file, report)
    _check_ref_patterns(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find spellings a later edition or standard library replaced",
        "No outdated idioms found!",
        analyze,
    )
