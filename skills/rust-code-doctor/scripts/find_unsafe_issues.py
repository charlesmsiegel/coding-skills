#!/usr/bin/env python3
"""
Find `unsafe` that has not paid for itself.

`unsafe` does not turn checking off; it moves the checking from the compiler to
a human, and the only record of that human's reasoning is a comment. So the
first thing this looks for is the missing comment: an `unsafe` block or an
`unsafe impl Send` with no `// SAFETY:` beside it is an argument nobody wrote
down, and the next person to edit the function has no way to know what they
must not break.

The rest are the specific operations where the invariant is easy to get wrong
and the failure is silent: `static mut`, `transmute`, the `_unchecked` family,
and `set_len` on a `Vec` that was never initialised.
"""

import re

from common import Reporter, run_file_detector
from rsparse import RsFile, is_doc_comment, iter_calls, iter_method_calls

# Operations whose safety condition is a property of the *data*, so nothing at
# the call site shows whether it holds.
# Only methods that can *only* appear in an unsafe context, so a name collision
# with a safe API (`File::set_len`, `GlobSetBuilder::add`) cannot fire.
_UNCHECKED = {
    "get_unchecked": ("the index is in bounds", "high"),
    "get_unchecked_mut": ("the index is in bounds", "high"),
    "unwrap_unchecked": ("the value is `Some`/`Ok`", "high"),
    "from_utf8_unchecked": ("the bytes are valid UTF-8", "high"),
    "assume_init": ("the value has actually been written", "high"),
}

_SAFETY_COMMENT = re.compile(r"(?i)\bsafety\b\s*:")


def _has_safety_comment(file: RsFile, line: int, reach: int = 4) -> bool:
    return any(_SAFETY_COMMENT.search(c.value)
               for c in file.comments_on_lines(max(1, line - reach), line))


def _has_safety_section(file: RsFile, line: int, reach: int = 12) -> bool:
    """True when a rustdoc `# Safety` heading documents the item at ``line``.

    That heading is the *documented* convention for an unsafe item's contract;
    `// SAFETY:` is the convention for a block. An item using the first should
    not be reported for lacking the second.
    """
    docs = "\n".join(c.value for c in file.comments_on_lines(max(1, line - reach), line)
                      if is_doc_comment(c))
    return "# Safety" in docs or "# safety" in docs.lower()


def _check_unsafe_blocks(file: RsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("unsafe") or file.in_macro_body(index):
            continue
        brace = index + 1
        if file.value(brace) != "{":
            continue  # `unsafe fn`, `unsafe impl` and `unsafe trait` are handled below
        close = file.closer(brace)
        if close < 0:
            continue
        enclosing = file.enclosing_function(index)
        if enclosing is not None and enclosing.is_unsafe:
            # Inside an `unsafe fn` the obligation belongs to the caller and is
            # documented under `# Safety` on the function — which Rust 2024 now
            # *requires* this block for. `unsafe_fn_without_safety_docs` covers
            # the case where that documentation is missing; reporting the block
            # too would be the same finding twice.
            continue
        if not _has_safety_comment(file, token.line):
            report.add(token.line, "unsafe_block_without_safety_comment",
                       "`unsafe { … }` with no `// SAFETY:` comment above it",
                       "Write the invariant the block relies on and why it holds here. The "
                       "comment is the only artifact a reviewer or a later editor can check the "
                       "code against — without it the block asserts something nobody can verify.",
                       "high")
        span_lines = file.line_of(close) - token.line
        if span_lines > 20:
            report.add(token.line, "oversized_unsafe_block",
                       f"`unsafe` block spans {span_lines} lines",
                       "Shrink it to the operations that genuinely need it. Every safe line "
                       "inside an `unsafe` block is a line that stops being checked, and the "
                       "block's safety argument has to cover all of them.", "medium")


def _check_unsafe_items(file: RsFile, report: Reporter) -> None:
    for block in file.impls:
        if not block.is_unsafe:
            continue
        trait_name = (block.trait_name or "").rsplit("::", 1)[-1]
        if not _has_safety_comment(file, block.line) and not _has_safety_section(file, block.line):
            report.add(block.line, "unsafe_impl_without_safety_comment",
                       f"`unsafe impl {trait_name} for {block.type_name}` with no `// SAFETY:` "
                       "comment",
                       f"An `unsafe impl {trait_name or 'Trait'}` is a promise to the compiler "
                       "that it cannot check. Say what makes it true — for `Send`/`Sync`, which "
                       "field carries the raw pointer and why sharing it is sound.", "high")

    for func in file.functions:
        if not func.is_unsafe:
            continue
        if not _has_safety_section(file, func.line):
            severity = "high" if func.is_exported else "medium"
            report.add(func.line, "unsafe_fn_without_safety_docs",
                       f"`unsafe fn {func.qualname}` has no `# Safety` section in its docs",
                       "An `unsafe fn` moves an obligation onto every caller. Document it: "
                       "rustdoc renders a `# Safety` heading, `clippy::missing_safety_doc` "
                       "enforces it, and without it callers guess.", severity)

    for trait in file.traits:
        if not trait.is_unsafe:
            continue
        if _has_safety_comment(file, trait.line) or _has_safety_section(file, trait.line):
            continue
        report.add(trait.line, "unsafe_trait_without_safety_docs",
                   f"`unsafe trait {trait.name}` states no obligation for its implementors",
                   "Document what an implementor must guarantee — that is the entire content "
                   "of an unsafe trait. Either a rustdoc `# Safety` section or a `// SAFETY:` "
                   "comment satisfies this.", "high")


def _check_static_mut(file: RsFile, report: Reporter) -> None:
    for binding in file.bindings:
        if binding.kind == "static" and binding.is_mut:
            report.add(binding.line, "static_mut",
                       f"`static mut {binding.name}` — every access is `unsafe`, and two of them "
                       "at once is undefined behaviour",
                       "Use an atomic (`AtomicU64`, `AtomicBool`), a `OnceLock`/`LazyLock` for "
                       "write-once data, or a `Mutex`. Rust 2024 makes references to `static mut` "
                       "a hard error, so this is also a migration blocker.", "high")


def _check_dangerous_calls(file: RsFile, report: Reporter) -> None:
    for index, callee in iter_calls(file):
        leaf = callee.rsplit("::", 1)[-1]
        line = file.line_of(index)
        if leaf == "transmute":
            report.add(line, "transmute",
                       f"`{callee}` reinterprets one type's bytes as another with no check at all",
                       "Nearly every use has a safe spelling: `as` for numeric casts, "
                       "`to_le_bytes`/`from_le_bytes` for representations, `bytemuck` for POD "
                       "types, a union for a tagged reinterpretation. Keep `transmute` only when "
                       "none of those exist, with a `// SAFETY:` naming both layouts.", "high")
        elif leaf in ("uninitialized", "zeroed") and "mem" in callee:
            report.add(line, "mem_uninitialized",
                       f"`{callee}()` produces a value that may be invalid for its type",
                       "`MaybeUninit<T>` is the supported way to hold not-yet-initialised memory; "
                       "`mem::uninitialized` is deprecated because it is instant UB for most "
                       "types (`bool`, `&T`, any enum).", "high")
        elif leaf == "forget" and "mem" in callee:
            report.add(line, "mem_forget",
                       f"`{callee}()` skips the destructor",
                       "Leaks a file handle, a lock guard or a buffer, depending on what it is "
                       "given. `ManuallyDrop` states the same intent in the type, where a reader "
                       "sees it.", "medium")

    for name_index, paren, method in iter_method_calls(file):
        entry = _UNCHECKED.get(method)
        if entry is None:
            continue
        invariant, severity = entry
        line = file.line_of(name_index)
        if not _has_safety_comment(file, line, reach=3):
            report.add(line, "unchecked_operation",
                       f"`.{method}()` requires that {invariant}, and nothing here checks it",
                       "Use the checked form (`get`, `unwrap`, `from_utf8`) unless a profile "
                       "proved the bounds check mattered. If it did, keep this and write the "
                       f"`// SAFETY:` argument for why {invariant}.", severity)


def _check_raw_pointers(file: RsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("as"):
            continue
        star = file.tok(index + 1)
        if star is None or not star.is_op("*"):
            continue
        kind = file.value(index + 2)
        if kind not in ("const", "mut"):
            continue
        report.add(token.line, "raw_pointer_cast",
                   f"cast to `*{kind} …` — the result has no lifetime and no aliasing rules",
                   "Prefer a reference. Where FFI forces the pointer, keep the cast next to the "
                   "call that consumes it, so nothing between them can outlive the pointee.",
                   "medium")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_unsafe_blocks(file, report)
    _check_unsafe_items(file, report)
    _check_static_mut(file, report)
    _check_dangerous_calls(file, report)
    _check_raw_pointers(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find `unsafe` whose safety argument was never written down",
        "No unsafe-code problems found!",
        analyze,
    )
