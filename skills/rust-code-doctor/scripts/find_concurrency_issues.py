#!/usr/bin/env python3
"""
Find async and threading bugs that compile and then deadlock.

Rust's type system makes data races impossible and says nothing at all about
the two failures that actually happen in async code: holding a lock guard
across an `.await`, and calling a blocking API on the executor's thread. The
first deadlocks the moment two tasks contend; the second stalls every other
task on the same worker, and neither is visible in a type.

The rest of the file is the smaller stuff: a spawned task whose handle is
dropped (so its panic is silent), sequential `.await` over independent work,
and an `async fn` with nothing to await.
"""

import re

from common import Reporter, run_file_detector
from rsparse import (
    RsFile, argument_spans, body_indices, iter_calls, iter_method_calls, receiver_text,
)

# Guard-producing methods. The value they return keeps the lock until it drops.
_GUARD_METHODS = frozenset({"lock", "read", "write", "borrow", "borrow_mut"})

# Calls that block the calling thread. On an async executor that is every task
# scheduled on the same worker, not just this one.
_BLOCKING_CALLS = {
    "std::thread::sleep": "std::thread::sleep",
    "thread::sleep": "std::thread::sleep",
    "std::fs::read": "std::fs",
    "std::fs::write": "std::fs",
    "std::fs::read_to_string": "std::fs",
    "std::fs::File::open": "std::fs",
    "fs::read_to_string": "std::fs",
    "fs::write": "std::fs",
    "fs::read": "std::fs",
    "reqwest::blocking::get": "reqwest::blocking",
    "std::io::stdin": "std::io",
    "std::process::Command::output": "std::process::Command",
}

_ASYNC_ALTERNATIVE = {
    "std::thread::sleep": "`tokio::time::sleep(…).await`",
    "std::fs": "`tokio::fs` (or `spawn_blocking` for the sync API)",
    "reqwest::blocking": "the async `reqwest::Client`",
    "std::io": "`tokio::io`",
    "std::process::Command": "`tokio::process::Command`",
}


def _async_functions(file: RsFile):
    return [f for f in file.functions if f.is_async and f.has_body and f.body_close > 0]


def _async_spans(file: RsFile) -> list[tuple[int, int]]:
    """Token ranges that execute on an async executor: `async fn` bodies and blocks."""
    spans = [(f.body_open, f.body_close) for f in _async_functions(file)]
    for closure in file.closures:
        if closure.is_async and closure.body_close > 0:
            spans.append((closure.body_open, closure.body_close))
    for index, token in enumerate(file.tokens):
        if token.is_name("async") and file.value(index + 1) == "{":
            close = file.closer(index + 1)
            if close > 0:
                spans.append((index + 1, close))
    return spans


def _in_spans(spans, index) -> bool:
    return any(start < index < end for start, end in spans)


def _blocking_pool_spans(file: RsFile) -> list[tuple[int, int]]:
    """Argument ranges of `spawn_blocking(…)` and `block_in_place(…)`.

    Code inside them runs on the blocking pool, not the executor thread — it is
    the fix this detector recommends, so reporting it would be recommending the
    wrapper already in use.
    """
    spans = []
    for index, callee in iter_calls(file):
        if callee.rsplit("::", 1)[-1] not in ("spawn_blocking", "block_in_place"):
            continue
        close = file.closer(index)
        if close > index:
            spans.append((index, close))
    return spans


def _check_guard_across_await(file: RsFile, report: Reporter) -> None:
    """A `MutexGuard` that is still alive at an `.await` deadlocks under contention."""
    spans = _async_spans(file)
    for name_index, paren, method in iter_method_calls(file):
        if method not in _GUARD_METHODS or not _in_spans(spans, name_index):
            continue
        close = file.closer(paren)
        if close > 0 and file.value(close + 1) == "." and file.tok(close + 2) is not None \
                and file.tokens[close + 2].is_name("await"):
            continue  # `.lock().await` is an async-aware lock: its guard may be held
        statement = _statement_head(file, name_index)
        if statement < 0 or not file.tokens[statement].is_name("let"):
            continue
        binding = file.tok(statement + 1)
        if binding is None:
            continue
        if binding.is_name("mut"):
            binding = file.tok(statement + 2)
        if binding is None or binding.kind != "name" or binding.value == "_":
            continue
        block_open, block_close = _enclosing_block(file, name_index)
        if block_close < 0:
            continue
        # `drop(guard)` is the idiomatic way to end a critical section early;
        # awaits after it are not holding anything.
        released = _dropped_at(file, binding.value, name_index, block_close, block_open)
        awaits = [i for i in range(name_index, min(released, block_close))
                  if file.tokens[i].is_name("await") and file.tokens[i - 1].is_op(".")]
        if not awaits:
            continue
        report.add(file.line_of(name_index), "guard_held_across_await",
                   f"`{binding.value}` holds a `{method}()` guard and the same scope has an "
                   f"`.await` at line {file.line_of(awaits[0])}",
                   "The guard is not dropped at the suspension point, so a second task that "
                   "wants the lock waits for a task that is itself suspended. Narrow the scope "
                   "(`let value = { let g = m.lock()?; g.clone() };`) or use the executor's "
                   "async-aware lock (`tokio::sync::Mutex`), whose guard is safe to hold.",
                   "high", related=[file.line_of(awaits[0])])


def _dropped_at(file: RsFile, binding: str, start: int, stop: int, block_open: int) -> int:
    """Index of an *unconditional* `drop(binding)`, or ``stop`` when there is none.

    A `drop` nested in an `if` or a loop runs on only one path, so treating it as
    the release point hides the deadlock on every other path — which is the case
    this check exists to catch. Only a drop in the guard's own block counts.
    """
    for index, callee in iter_calls(file, start, stop):
        if callee != "drop":
            continue
        if _enclosing_block(file, index)[0] != block_open:
            continue
        spans = argument_spans(file, index)
        if spans and file.slice(*spans[0]).strip() == binding:
            return index
    return stop


def _statement_head(file: RsFile, index: int) -> int:
    cursor = index
    while cursor > 0:
        previous = file.tokens[cursor - 1]
        if previous.kind == "op" and previous.value in (";", "{", "}"):
            return cursor
        cursor -= 1
    return -1


def _enclosing_block(file: RsFile, index: int) -> tuple[int, int]:
    best = (-1, -1)
    for opener, closer in file.match.items():
        if opener < closer and file.tokens[opener].is_op("{") and opener < index < closer:
            if best[0] < opener:
                best = (opener, closer)
    return best


def _check_blocking_in_async(file: RsFile, report: Reporter) -> None:
    spans = _async_spans(file)
    if not spans:
        return
    offloaded = _blocking_pool_spans(file)
    for index, callee in iter_calls(file):
        if not _in_spans(spans, index) or _in_spans(offloaded, index):
            continue
        family = _BLOCKING_CALLS.get(callee)
        if family is None:
            continue
        report.add(file.line_of(index), "blocking_call_in_async",
                   f"`{callee}(…)` blocks the executor thread inside async code",
                   f"Use {_ASYNC_ALTERNATIVE[family]}, or move the call into "
                   "`tokio::task::spawn_blocking`. While this call runs, every other task on the "
                   "same worker thread is stopped — including the timeout that was supposed to "
                   "cancel it.", "high")

    for name_index, paren, method in iter_method_calls(file):
        if method not in ("block_on", "recv", "join") or not _in_spans(spans, name_index) \
                or _in_spans(offloaded, name_index):
            continue
        receiver = receiver_text(file, name_index - 1)
        if method == "block_on":
            report.add(file.line_of(name_index), "block_on_inside_async",
                       f"`{receiver}.block_on(…)` inside async code",
                       "Driving a runtime from inside a runtime panics on tokio and deadlocks "
                       "elsewhere. `.await` the future instead.", "high")
        elif method == "recv":
            close = file.closer(paren)
            if close > 0 and file.value(close + 1) == "." and file.tok(close + 2) is not None \
                    and file.tokens[close + 2].is_name("await"):
                continue  # an async channel: `.recv().await` yields, it does not block
            report.add(file.line_of(name_index), "blocking_recv_in_async",
                       f"`{receiver}.recv()` blocks the executor thread until a message arrives",
                       "A `std::sync::mpsc` receiver parks the whole worker. Use the runtime's "
                       "channel (`tokio::sync::mpsc`) and `.recv().await`, or move the receive "
                       "into `spawn_blocking`.", "high")
        elif method == "join" and receiver.endswith("handle"):
            report.add(file.line_of(name_index), "thread_join_inside_async",
                       f"`{receiver}.join()` blocks the executor until the thread finishes",
                       "Await the `JoinHandle` from `tokio::spawn`, or do the wait in "
                       "`spawn_blocking`.", "medium")


def _check_dropped_join_handles(file: RsFile, report: Reporter) -> None:
    for index, callee in iter_calls(file):
        leaf = callee.rsplit("::", 1)[-1]
        if leaf != "spawn" or "spawn_blocking" in callee:
            continue
        close = file.closer(index)
        if close < 0:
            continue
        following = file.tok(close + 1)
        head = _statement_head(file, index)
        if following is not None and following.is_op("."):
            continue  # chained: `.await`, `.abort()`, stored via a builder
        if head >= 0 and file.tokens[head].is_name("let", "return"):
            continue
        if following is not None and following.is_op(";"):
            report.add(file.line_of(index), "join_handle_dropped",
                       f"`{callee}(…)` with the `JoinHandle` discarded",
                       "Nothing observes the task after this line: a panic inside it is silent, "
                       "and on tokio the task is detached rather than cancelled when the parent "
                       "returns. Keep the handle and await or abort it — or bind it to `_guard` "
                       "with a comment saying the detachment is deliberate.", "medium")


def _check_await_in_loop(file: RsFile, report: Reporter) -> None:
    for index, token in enumerate(file.tokens):
        if not token.is_name("for", "while"):
            continue
        brace = file.find_op("{", index + 1, min(index + 60, len(file.tokens)))
        if brace < 0:
            continue
        close = file.closer(brace)
        if close < 0:
            continue
        awaits = [i for i in range(brace, close)
                  if file.tokens[i].is_name("await") and file.tokens[i - 1].is_op(".")]
        if not awaits:
            continue
        body = file.slice(brace, close)
        if "?" in body and "break" in body:
            continue
        report.add(token.line, "sequential_await_in_loop",
                   f"`.await` inside a `{token.value}` loop — each iteration waits for the last",
                   "When the iterations are independent, `futures::future::join_all` or a "
                   "`buffer_unordered(n)` stream runs them concurrently. When they are not — a "
                   "rate limit, an ordered write, each step feeding the next — keep the loop and "
                   "say so in a comment, because the next reader will ask.", "low")


def _check_async_without_await(file: RsFile, report: Reporter) -> None:
    for func in _async_functions(file):
        span = body_indices(func)
        if not span or func.trait_name is not None:
            continue  # a trait impl must match the trait's signature
        if any(file.tokens[i].is_name("await") for i in span):
            continue
        if any(file.tokens[i].is_name("async") for i in span):
            continue
        report.add(func.line, "async_fn_never_awaits",
                   f"`async fn {func.qualname}` has no `.await` in its body",
                   "The `async` makes every caller await a future that resolves immediately, and "
                   "makes the function unusable from sync code. Drop the `async`, or return the "
                   "future the body was supposed to have.", "low")


def _check_std_mutex_in_async(file: RsFile, report: Reporter) -> None:
    if not _async_functions(file):
        return
    imports_std_mutex = any(re.search(r"\bstd::sync::.*\bMutex\b", use.path)
                            or "Mutex" in use.names and use.path.startswith("std::sync")
                            for use in file.uses)
    if not imports_std_mutex:
        return
    report.add(next((u.line for u in file.uses if "Mutex" in u.names), 1),
               "std_mutex_in_async_module",
               "`std::sync::Mutex` in a module that also contains `async fn`",
               "Its guard is not `Send`, so holding it across an `.await` either fails to compile "
               "or (behind an `Arc`) deadlocks. Use `tokio::sync::Mutex` where the lock is held "
               "across a suspension, and keep the std one only for short critical sections that "
               "never await.", "low")


def analyze(file: RsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    _check_guard_across_await(file, report)
    _check_blocking_in_async(file, report)
    _check_dropped_join_handles(file, report)
    _check_await_in_loop(file, report)
    _check_async_without_await(file, report)
    _check_std_mutex_in_async(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find async and threading bugs that compile and then deadlock",
        "No concurrency problems found!",
        analyze,
    )
