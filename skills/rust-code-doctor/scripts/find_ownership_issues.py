#!/usr/bin/env python3
"""
Find ownership and allocation habits that cost performance or clarity.

Most of these are the shapes a borrow checker argument leaves behind. `.clone()`
is the usual one: it compiles, so it ends the argument, and the copy stays there
forever. The others are signature-level — `&Vec<T>` and `&String` narrow what a
caller may pass for no benefit, and `Rc<RefCell<T>>` moves aliasing checks from
compile time to a runtime panic.

Nothing here is a bug. It is the difference between a function anyone can call
and one that makes every caller allocate first.
"""

import re

from common import Reporter, is_test_file, run_file_detector
from rsparse import (
    COPY_TYPES, RsFile, body_indices, iter_method_calls, receiver_text,
)

# Methods that only read, so a parameter taken by value for their sake did not
# need to be owned.
_READ_ONLY_METHODS = frozenset({
    "len", "is_empty", "iter", "get", "contains", "contains_key", "as_str",
    "as_slice", "as_bytes", "chars", "bytes", "starts_with", "ends_with",
    "find", "split", "trim", "to_uppercase", "to_lowercase", "keys", "values",
    "first", "last", "parse", "lines",
    # NOT `count`: `Iterator::count` takes `self` by value, so `it.clone().count()`
    # is how you count without consuming an iterator you still need. Suggesting
    # `it.count()` moves `it` and the later use stops compiling.
})

# Owned container types whose borrowed form is what a parameter usually wants.
_BY_REF_SUGGESTION = {
    "String": "&str",
    "Vec": "&[T]",
    "PathBuf": "&Path",
    "OsString": "&OsStr",
}


def _primitive_locals(file: RsFile) -> list[tuple[str, str, int, int]]:
    """`let x: u32 = …` bindings as (name, type, declaration index, scope end).

    Keyed by name across the whole file, a `let value: u32` in one function
    decided the type of `value.clone()` in another — telling the reader to drop
    a clone that a `String` needs to stay usable. Each binding now carries the
    block it lives in, and the call site resolves the one actually visible.
    """
    found: list[tuple[str, str, int, int]] = []
    for index, token in enumerate(file.tokens):
        if not token.is_name("let"):
            continue
        name_index = index + 1
        if file.value(name_index) == "mut":
            name_index += 1
        name = file.tok(name_index)
        if name is None or name.kind != "name" or not file.value(name_index + 1) == ":":
            continue
        annotation = file.tok(name_index + 2)
        if annotation is None or annotation.kind != "name" or annotation.value not in COPY_TYPES:
            continue
        scope_end = _enclosing_block_end(file, index)
        found.append((name.value, annotation.value, index, scope_end))
    return found


def _enclosing_block_end(file: RsFile, index: int) -> int:
    """Close index of the innermost `{ … }` containing ``index``."""
    best_open, best_close = -1, len(file.tokens)
    for opener, closer in file.match.items():
        if opener < closer and file.tokens[opener].is_op("{") and opener < index < closer:
            if opener > best_open:
                best_open, best_close = opener, closer
    return best_close


def _visible_primitive(bindings, name: str, at: int) -> str | None:
    """The `Copy` type of ``name`` as seen from token ``at``, if any."""
    best: tuple[int, str] | None = None
    for binding_name, type_name, declared, scope_end in bindings:
        if binding_name != name or not declared < at <= scope_end:
            continue
        if best is None or declared > best[0]:
            best = (declared, type_name)
    return best[1] if best else None


def _check_clones(file: RsFile, report: Reporter) -> None:
    primitives = _primitive_locals(file)
    loops = _loop_spans(file)
    for name_index, paren, method in iter_method_calls(file):
        if method not in ("clone", "to_owned", "to_vec"):
            continue
        line = file.line_of(name_index)
        subject = receiver_text(file, name_index - 1)
        previous = file.tok(name_index - 2)

        if method == "clone" and previous is not None and previous.kind == "num":
            report.add(line, "clone_on_copy",
                       f"`{previous.value}.clone()` on a value that is `Copy`",
                       "Drop the `.clone()`. On a `Copy` type it compiles to the same move and "
                       "reads as if something expensive were happening.", "low")
            continue
        visible = _visible_primitive(primitives, subject, name_index) if subject else None
        if method == "clone" and visible is not None:
            report.add(line, "clone_on_copy",
                       f"`{subject}.clone()` where `{subject}: {visible}` is `Copy`",
                       "Drop the `.clone()` — assignment already copies.", "low")
            continue

        following = file.tok(file.closer(paren) + 1) if file.closer(paren) >= 0 else None
        if method in ("clone", "to_owned") and following is not None and following.is_op("."):
            follower = file.tok(file.closer(paren) + 2)
            if follower is not None and follower.is_name(*_READ_ONLY_METHODS):
                report.add(line, "clone_then_read",
                           f"`.{method}()` followed by `.{follower.value}()`, which only reads",
                           "Call the read on the borrow. The copy exists for the length of one "
                           "method call and is then dropped.", "medium")
                continue

        for keyword, start, end, kind in loops:
            if start < name_index < end and _declared_outside(file, subject, keyword,
                                                              start, name_index):
                report.add(line, "clone_inside_loop",
                           f"`{subject}.{method}()` inside a `{kind}` loop, on a value from "
                           "outside it",
                           "Hoist the clone out of the loop, or borrow. One allocation per "
                           "iteration is the most common accidental cost in Rust.", "medium")
                break


def _declared_outside(file: RsFile, subject: str, keyword: int, brace: int,
                      clone: int) -> bool:
    """True when ``subject`` is bound before the loop rather than inside it."""
    if not subject or not subject.isidentifier():
        return False
    for index in range(keyword, brace):
        token = file.tokens[index]
        if token.kind == "name" and token.value == subject:
            return False  # the loop header itself binds it
    # A `let` in the body above the clone builds a *new* value each iteration,
    # so there is nothing to hoist: the clone feeds a second consumer of a value
    # that did not exist before this iteration started.
    for index in range(brace, clone):
        if not file.tokens[index].is_name("let"):
            continue
        cursor = index + 1
        if file.value(cursor) == "mut":
            cursor += 1
        if file.value(cursor) == subject:
            return False
    return True


def _loop_spans(file: RsFile) -> list[tuple[int, int, int, str]]:
    """(keyword index, body open, body close, keyword) for each loop."""
    spans = []
    for index, token in enumerate(file.tokens):
        if not token.is_name("for", "while", "loop"):
            continue
        brace = file.find_op("{", index + 1, min(index + 60, len(file.tokens)))
        if brace < 0:
            continue
        close = file.closer(brace)
        if close > 0:
            spans.append((index, brace, close, token.value))
    return spans


def _check_reference_parameters(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        for param in func.params:
            if param.is_self:
                continue
            annotation = " ".join(param.type_text.split())
            # `&mut String` and `&mut Vec<T>` exist so the callee can `clear`,
            # `push` or `truncate`. Recommending `&str`/`&[T]` removes the
            # operations the function is for, and does not compile.
            match = re.match(r"^&\s*(?:'\w+\s+)?([A-Za-z_][\w:]*)\s*(<.*>)?$", annotation)
            if not match:
                continue
            base = match.group(1).rsplit("::", 1)[-1]
            if base == "String":
                report.add(param.line, "ref_string_parameter",
                           f"`{func.qualname}` takes `{param.name}: {annotation}`",
                           "`&str` accepts a `String`, a `&str`, a literal and a `Cow` — `&String` "
                           "accepts only the first, and forces every other caller to allocate.",
                           "medium")
            elif base == "Vec":
                inner = (match.group(2) or "<T>")[1:-1]
                report.add(param.line, "ref_vec_parameter",
                           f"`{func.qualname}` takes `{param.name}: {annotation}`",
                           f"`&[{inner}]` accepts a `Vec`, an array, a slice of either, and a "
                           "sub-slice. `&Vec` accepts only a whole `Vec`.", "medium")
            elif base in ("Box", "Rc", "Arc") and match.group(2):
                report.add(param.line, "ref_smart_pointer_parameter",
                           f"`{func.qualname}` takes `{param.name}: {annotation}`",
                           "Take `&T` — the caller's pointer type is not this function's business, "
                           "and deref coercion supplies it for free.", "low")


def _check_needless_owned_parameters(file: RsFile, report: Reporter) -> None:
    for func in file.functions:
        if not func.has_body or not func.is_public:
            continue
        span = body_indices(func)
        if not span:
            continue
        body = file.slice(func.body_open, func.body_close)
        for param in func.params:
            if param.is_self or not param.name.isidentifier():
                continue
            base = param.type_text.split("<")[0].strip()
            if base not in _BY_REF_SUGGESTION:
                continue
            uses = [i for i in span if file.tokens[i].kind == "name" and file.tokens[i].value == param.name]
            if not uses:
                continue
            consumed = False
            for use in uses:
                following = file.tok(use + 1)
                if following is None or not following.is_op("."):
                    consumed = True  # moved, returned, or stored somewhere
                    break
                method = file.tok(use + 2)
                if method is None or method.kind != "name" or method.value not in _READ_ONLY_METHODS:
                    consumed = True
                    break
            if consumed or f"{param.name}," in body and "return" in body:
                continue
            report.add(param.line, "needless_owned_parameter",
                       f"`{func.qualname}` takes `{param.name}: {param.type_text}` by value but "
                       "only reads it",
                       f"Take `{_BY_REF_SUGGESTION[base]}`. Owning a value the function never keeps "
                       "makes every caller either clone or give up its own copy.", "medium")


def _check_smart_pointer_shapes(file: RsFile, report: Reporter) -> None:
    for definition in file.types:
        for field in definition.fields:
            compact = field.type_text.replace(" ", "")
            if re.search(r"\b(Rc|Arc)<(?:[\w:]*::)?RefCell<", compact):
                report.add(field.line, "shared_mutable_field",
                           f"`{definition.name}.{field.name}: {field.type_text}` moves aliasing "
                           "checks to runtime",
                           "`RefCell` turns a compile error into a panic at the second borrow. "
                           "Reach for it when a graph genuinely needs shared mutation; otherwise "
                           "restructure so one owner holds the value and hands out `&mut`.",
                           "medium")
            elif re.match(r"^Vec<Box<(?!dyn)", compact):
                report.add(field.line, "boxed_sized_element",
                           f"`{definition.name}.{field.name}: {field.type_text}` boxes an element "
                           "that already has a known size",
                           "`Vec<T>` already heap-allocates its buffer; the inner `Box` adds a "
                           "second indirection per element. Keep it only for a recursive type or "
                           "a `dyn Trait`.", "low")


def _check_deref_clone(file: RsFile, report: Reporter) -> None:
    for name_index, paren, method in iter_method_calls(file):
        if method != "clone":
            continue
        previous = file.tok(name_index - 2)
        if previous is not None and previous.is_op(")"):
            opener = file.closer(name_index - 2)
            if opener >= 0 and file.tok(opener + 1) is not None and file.tokens[opener + 1].is_op("*"):
                report.add(file.line_of(name_index), "deref_then_clone",
                           "`(*value).clone()` — the dereference selects which `Clone` runs",
                           "Check which one you meant, and do not assume `value.clone()` is "
                           "equivalent: on an `Rc<T>`/`Arc<T>` the explicit deref clones the "
                           "inner `T` (a deep copy, a different type) while `value.clone()` "
                           "clones the pointer and bumps the refcount. On a plain `&T` the two "
                           "are the same and the deref is noise — that is the case worth "
                           "removing.", "low")
        head = file.tok(name_index - 3)
        if head is not None and head.is_name("as_ref") and previous is not None and previous.is_op(")"):
            report.add(file.line_of(name_index), "as_ref_then_clone",
                       "`.as_ref().clone()`",
                       "`.clone()` on the value itself, or `.cloned()` on the `Option`.", "low")


def _check_clone_in_for_header(file: RsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("for"):
            continue
        brace = file.find_op("{", index + 1, min(index + 60, len(file.tokens)))
        if brace < 0:
            continue
        header = file.slice(index, brace)
        if re.search(r"\.clone\s*\(\s*\)\s*$", header.strip()) or ".clone()." not in header and \
                re.search(r"in\s+[\w.]+\.clone\s*\(\s*\)", header):
            report.add(token.line, "clone_to_iterate",
                       "`for … in x.clone()` copies the whole collection to walk it",
                       "`for … in &x` borrows; `for … in x` moves when you are done with it. The "
                       "clone is there to end a borrow-checker argument, and it costs one full "
                       "copy every time the loop runs.", "medium")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    testish = is_test_file(file.path)
    _check_clones(file, report)
    _check_reference_parameters(file, report)
    if not testish:
        _check_needless_owned_parameters(file, report)
    _check_smart_pointer_shapes(file, report)
    _check_deref_clone(file, report)
    _check_clone_in_for_header(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find clones, allocations and signatures that own more than they need",
        "No ownership or allocation problems found!",
        analyze,
    )
