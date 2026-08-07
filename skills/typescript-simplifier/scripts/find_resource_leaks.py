#!/usr/bin/env python3
"""
Find resources acquired and never released: timers, listeners, subscriptions,
observers, file handles and abort controllers.

JavaScript has no `with` and no destructor, so release is always something a
human remembered to write. In long-lived processes and single-page apps these
are the leaks that show up as "it gets slower the longer you leave it open".
"""

from common import Reporter, is_test_file, run_file_detector
from tsparse import TsFile, iter_calls

# acquire -> (release, what leaks, severity)
PAIRS = {
    "setInterval": ("clearInterval", "the timer keeps firing after the owner is gone", "high"),
    "addEventListener": ("removeEventListener", "the listener keeps the handler and its closure alive", "medium"),
    "subscribe": ("unsubscribe", "the subscription keeps delivering into a dead component", "medium"),
    "observe": ("disconnect", "the observer keeps the observed nodes alive", "medium"),
    "createReadStream": ("close", "the file descriptor stays open", "medium"),
    "createWriteStream": ("close", "the file descriptor stays open, and buffered writes may be lost", "medium"),
    "open": ("close", "the file descriptor stays open", "medium"),
    "connect": ("end", "the connection stays open", "low"),
}
# The release call may be spelled either way round.
RELEASE_ALIASES = {
    "clearInterval": {"clearInterval"},
    "removeEventListener": {"removeEventListener", "abort"},
    "unsubscribe": {"unsubscribe", "remove", "dispose", "destroy"},
    "disconnect": {"disconnect", "unobserve"},
    "close": {"close", "closeSync", "destroy", "end"},
    "end": {"end", "close", "destroy"},
}

# React cleanup lives in the function an effect returns; a `return` inside a
# useEffect callback is the release path.
EFFECT_HOOKS = frozenset({"useEffect", "useLayoutEffect", "useInsertionEffect"})


def _released_names(file: TsFile) -> set[str]:
    """Every release call made anywhere in the file, by method name."""
    released: set[str] = set()
    for _, callee in iter_calls(file):
        released.add(callee.rsplit(".", 1)[-1])
    return released


def _enclosing_effect(file: TsFile, index: int) -> int:
    """Token index of the `useEffect` call containing ``index``, or -1."""
    for paren, callee in iter_calls(file):
        if callee.rsplit(".", 1)[-1] not in EFFECT_HOOKS:
            continue
        close = file.closer(paren)
        if 0 <= paren < index < close:
            return paren
    return -1


def _effect_has_cleanup(file: TsFile, paren: int) -> bool:
    close = file.closer(paren)
    if close < 0:
        return False
    callback = file.enclosing_function(paren + 2)
    for probe in range(paren, close):
        if not file.tokens[probe].is_name("return"):
            continue
        following = file.tokens[probe + 1] if probe + 1 < len(file) else None
        if following is None or following.is_op(";", "}"):
            continue
        owner = file.enclosing_function(probe)
        if owner is not None and (callback is None or owner.body_open >= paren):
            return True
    return False


def _check_pairs(file: TsFile, report: Reporter) -> None:
    released = _released_names(file)
    for paren, callee in iter_calls(file):
        method = callee.rsplit(".", 1)[-1]
        if method not in PAIRS:
            continue
        release, cost, severity = PAIRS[method]
        aliases = RELEASE_ALIASES.get(release, {release})
        if aliases & released:
            continue
        effect = _enclosing_effect(file, paren)
        if effect >= 0:
            if _effect_has_cleanup(file, effect):
                continue
            report.add(file.tokens[paren].line, "effect_without_cleanup",
                       f"`{callee}(…)` inside an effect with no cleanup function — {cost}",
                       f"Return a cleanup from the effect: `return () => {release}(…)`. Without it "
                       "every re-run adds another live resource.", "high")
            continue
        report.add(file.tokens[paren].line, "unreleased_resource",
                   f"`{callee}(…)` with no matching `{release}` anywhere in this file — {cost}",
                   f"Pair it with `{release}` on every exit path, including the error path. If the "
                   "resource is meant to live for the process, say so in a comment so the next "
                   "reader does not go looking.", severity)


def _check_abort_controllers(file: TsFile, report: Reporter) -> None:
    """A fetch inside an effect with no AbortController races its own unmount."""
    for paren, callee in iter_calls(file):
        if callee.rsplit(".", 1)[-1] not in EFFECT_HOOKS:
            continue
        close = file.closer(paren)
        if close < 0:
            continue
        fetches = [p for p, c in iter_calls(file, paren, close) if c.rsplit(".", 1)[-1] == "fetch"]
        if not fetches:
            continue
        window = file.slice(paren, close)
        if "AbortController" in window or "signal" in window or "cancel" in window:
            continue
        report.add(file.tokens[fetches[0]].line, "uncancelled_request_in_effect",
                   "`fetch` inside an effect with no AbortController",
                   "Create an `AbortController`, pass its `signal`, and abort it in the cleanup. "
                   "Otherwise a response that arrives after unmount writes to state that is gone, "
                   "and two quick re-runs can land out of order.", "medium")


def _check_timeout_handles(file: TsFile, report: Reporter) -> None:
    """`setTimeout` whose handle is discarded inside a component or effect."""
    for paren, callee in iter_calls(file):
        if callee.rsplit(".", 1)[-1] != "setTimeout":
            continue
        if _enclosing_effect(file, paren) < 0:
            continue
        start = paren - 1 - 2 * callee.count(".")
        previous = file.tokens[start - 1] if start else None
        assigned = previous is not None and (previous.is_op("=") or previous.kind == "name")
        if assigned or "clearTimeout" in file.text:
            continue
        report.add(file.tokens[paren].line, "uncleared_timeout_in_effect",
                   "`setTimeout` inside an effect with its handle discarded",
                   "Keep the id and `clearTimeout` it in the cleanup. A pending timeout that fires "
                   "after unmount updates state on a component that no longer exists.", "medium")


def analyze(file: TsFile, ignore: set[str]) -> list:
    report = Reporter(file, ignore)
    if is_test_file(file.path):
        return report.findings  # a test process exits; leaks there mislead more than they help
    _check_pairs(file, report)
    _check_abort_controllers(file, report)
    _check_timeout_handles(file, report)
    return report.findings


if __name__ == "__main__":
    run_file_detector(
        "Find unreleased resources: timers, listeners, subscriptions, streams",
        "No resource leaks found!",
        analyze,
    )
