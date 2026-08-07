#!/usr/bin/env python3
"""Find the places where measurement quietly stops measuring.

Six mechanical shapes, all of which read as a working system:

  error_becomes_zero  an exception handler that yields 0, 0.0, or "" — after which
                      a timeout, a refusal, and a genuinely bad answer are one number
  swallowed_error     a handler that passes/continues with nothing recorded
  default_off_flag    the better method behind a flag defaulting to off, usually
                      with no recorded A/B
  silent_cap          [:100], LIMIT, sample(, head( in a measurement path — a capped
                      run that reports no cap reads as "we measured everything"
  nondeterminism      temperature/sampling settings that make the metric a random
                      variable, reported once as a fact
  unpinned_model      a model id with no version or date, so the provider can move
                      the baseline under the trend chart

Language-agnostic and textual, because measurement pipelines are rarely all one
language. Produces candidates, never findings: a cap in a CLI flag is fine, a cap
in the eval loop is the finding, and only reading tells them apart.

Usage:
  python find_fail_soft.py .
  python find_fail_soft.py eval/ --kind error_becomes_zero --format json
"""

import re
import sys
import argparse
from pathlib import Path

from common import (
    CODE_SUFFIXES, CONFIG_SUFFIXES, add_common_args, candidate, configure_output,
    emit, envelope, iter_files, read_source, rel, skipped_note,
)

KIND_ORDER = ["error_becomes_zero", "swallowed_error", "default_off_flag",
              "silent_cap", "nondeterminism", "unpinned_model"]

CONFIRM = {
    "error_becomes_zero": "check whether a per-example error field distinguishes this from a real 0.0",
    "swallowed_error": "check what is lost here, and whether the aggregate's denominator still counts it",
    "default_off_flag": "find the recorded A/B for this component; if there is none, it was never measured",
    "silent_cap": "check whether the report says a cap was applied; the cap is fine, the silence is not",
    "nondeterminism": "check whether the run is repeated or seeded, and whether variance is reported",
    "unpinned_model": "check whether the run record pins the version; unpinned breaks comparability over time",
}

# Python/Ruby handlers open a line; a C-family `catch` may sit mid-line, either
# after the try's closing brace (`} catch (e) {`) or in a one-liner.
_HANDLER_RE = re.compile(
    r"^\s*(?:\}\s*)?(?:except\b[^:]*:|rescue\b)"
    r"|\bcatch\s*(?:\([^)]*\))?\s*\{"
)
# A complete zero literal, never a prefix of one: `0`, `0.`, `0.00` — but not the
# `0` inside `0.75`, which would make every "return a real low score" path a false
# error_becomes_zero. The empty-string forms carry no trailing word boundary,
# because there is no word character after a quote for one to sit against.
# `return None` is deliberately NOT here: null-for-unmeasurable is the practice
# this skill recommends, so calling it a zero would punish the good shape. It
# still records nothing, so _EMPTY_BODY_RE below catches it as a swallowed error.
_ZERO = r"""(?:0(?:\.0*)?(?![\d.])|""|'')"""
_ZERO_BODY_RE = re.compile(r"\breturn\s+" + _ZERO
                           + r"|=\s*" + _ZERO + r"\s*;?\s*$"
                           + r"|\bappend\(\s*" + _ZERO
                           + r"|\bscore\w*\s*=\s*" + _ZERO)
_EMPTY_BODY_RE = re.compile(r"^\s*(?:pass|continue|break|return(?:\s+(?:None|null|nil))?|\}|;)\s*$")
_SURFACED_RE = re.compile(r"\braise\b|\bthrow\b|\brethrow\b")
# `# cannot rethrow here` must not count as re-raising, or the `pass` under it
# never surfaces. Comments are stripped before the check.
_COMMENT_RE = re.compile(r"(?<![:\w])#.*$|//.*$")

# The flag word may open the name (`enable_reranker`) or sit inside it
# (`rag.use_reranker`), so the prefix is optional. `on` alone matches half the
# words in English, which is what _ENABLE_TOKEN_RE below exists to filter back out.
_FLAG_NAME = r"[\w.]*(?:enable|enabled|use|using|with|allow|feature|flag|active|on)[\w]*"
# An optional type annotation may sit between the name and the value:
# `ENABLE_RERANKER: bool = False`, `const useReranker: boolean = false`.
_ANNOTATED = r"\s*(?::\s*[A-Za-z_][\w.\[\]<>| ]*)?\s*[:=]\s*"
_DEFAULT_OFF_RE = re.compile(
    r"\b(" + _FLAG_NAME + r")" + _ANNOTATED + r"(?:false|0)\b"
    r"|\b(" + _FLAG_NAME + r")" + _ANNOTATED + r"[\"'](?:false|off|0|no)[\"']",
    re.IGNORECASE,
)
# The name is confirmed by its *tokens*, not by a substring: `connection_timeout`
# contains "on" and `user_id` contains "use", while `useReranker` and
# `ENABLE_RERANKER` are genuine switches. Splitting first is what tells them apart.
_TOKEN_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
FLAG_TOKENS = frozenset({"enable", "enabled", "disable", "use", "using", "with",
                         "allow", "feature", "flag", "active", "on", "toggle"})


def is_flag_name(name: str) -> bool:
    return bool({t.lower() for t in _TOKEN_RE.findall(name)} & FLAG_TOKENS)

_CAP_RE = re.compile(
    r"\[\s*:\s*\d{1,6}\s*\]"                             # rows[:100]
    r"|\.head\(\s*\d+"                                     # df.head(50)
    r"|\bislice\(\s*[^,]+,\s*\d+"                          # islice(rows, 50)
    r"|\blimit\s*[=:(]\s*\d+"                              # limit=100
    r"|\blimit\s+\d+"                                      # SQL LIMIT 100
    r"|\b(?:sample|choices|choice)\(\s*[^)]*(?:\b[nk]\s*=\s*\d|,\s*\d+\s*\))"  # sample(rows, 50) / (rows, k=50) / (n=50)
    r"|\bmax_(?:examples|rows|samples|cases|items|records)\s*[=:]\s*\d+",
    re.IGNORECASE,
)
_NONDET_RE = re.compile(
    r"\btemperature\s*[=:]\s*([0-9]*\.?[0-9]+)"
    r"|\btop_p\s*[=:]\s*([0-9]*\.?[0-9]+)"
    r"|\bdo_sample\s*[=:]\s*true",
    re.IGNORECASE,
)
_MODEL_RE = re.compile(
    r"[\"']((?:gpt|claude|gemini|llama|mistral|mixtral|command|titan|sonnet|opus|haiku|o[134]|text-embedding)"
    r"[A-Za-z0-9._-]*)[\"']"
)
# A full date, a compact date, a four-digit provider snapshot (gpt-4-0613,
# gpt-4-1106-preview), an explicit -v2, or an @-pinned tag.
_PINNED_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{6,8}|-\d{4}(?!\d)|-v\d+|@\d")


def handler_body(lines: list, index: int, max_lines: int = 8) -> list:
    """The lines belonging to the handler that opens at `lines[index]`.

    Indentation for Python and brace-closing for the C-family, approximated: this
    only has to be right enough to tell "swallowed" from "logged and re-raised".
    """
    opener = lines[index]
    if "catch" in opener and "{" in opener:
        inline = opener.rsplit("{", 1)[1]          # `} catch (e) { return 0; }`
    elif ":" in opener and opener.strip().startswith(("except", "rescue")):
        inline = opener.split(":", 1)[1]           # `except Exception: return 0.0`
    else:
        inline = ""
    body = [inline] if inline.strip("} \t") else []
    indent = len(opener) - len(opener.lstrip())
    for line in lines[index + 1: index + 1 + max_lines]:
        if not line.strip():
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= indent and not line.strip().startswith(("}", ")")):
            break
        if line.strip() == "}":
            break
        body.append(line)
    return body


def scan_handlers(lines: list, display: str) -> list:
    out = []
    for index, line in enumerate(lines):
        if not _HANDLER_RE.search(line):
            continue
        body = handler_body(lines, index)
        code = [_COMMENT_RE.sub("", b).rstrip() for b in body]
        code = [b for b in code if b.strip()]
        joined = "\n".join(code)
        if _SURFACED_RE.search(joined):
            continue  # re-raised: the error is not being hidden
        if _ZERO_BODY_RE.search(joined):
            out.append(candidate(
                "error_becomes_zero", display, index + 1,
                "a failure here yields 0/empty — indistinguishable from a genuine low score",
                CONFIRM["error_becomes_zero"], line.strip() + " ... " + " ".join(b.strip() for b in body)[:120],
            ))
        elif code and all(_EMPTY_BODY_RE.match(b) for b in code):
            out.append(candidate(
                "swallowed_error", display, index + 1,
                "handler records nothing — the example is lost without a trace",
                CONFIRM["swallowed_error"], line.strip(),
            ))
        elif not code:
            out.append(candidate(
                "swallowed_error", display, index + 1,
                "empty handler",
                CONFIRM["swallowed_error"], line.strip(),
            ))
    return out


def scan_line(line: str, display: str, lineno: int) -> list:
    out = []
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "//", "*", "<!--")):
        return out

    match = _DEFAULT_OFF_RE.search(line)
    if match and is_flag_name(match.group(1) or match.group(2) or ""):
        out.append(candidate(
            "default_off_flag", display, lineno,
            "a component defaults to off here",
            CONFIRM["default_off_flag"], stripped,
        ))

    if _CAP_RE.search(line):
        out.append(candidate(
            "silent_cap", display, lineno,
            "a cap or sample is applied here",
            CONFIRM["silent_cap"], stripped,
        ))

    nondet = _NONDET_RE.search(line)
    if nondet:
        value = nondet.group(1) or nondet.group(2)
        if value is None or float(value) > 0:
            out.append(candidate(
                "nondeterminism", display, lineno,
                "sampling is on, so the metric is a random variable",
                CONFIRM["nondeterminism"], stripped,
            ))

    for model in _MODEL_RE.findall(line):
        if not _PINNED_RE.search(model):
            out.append(candidate(
                "unpinned_model", display, lineno,
                model + " carries no version or date",
                CONFIRM["unpinned_model"], stripped,
            ))
    return out


def scan(root: Path) -> tuple:
    files = iter_files(root, CODE_SUFFIXES | CONFIG_SUFFIXES)
    rows, skipped, read = [], [], 0
    for path in files:
        lines, reason = read_source(path)
        display = rel(path, root)
        if reason:
            skipped.append((display, reason))
            continue
        read += 1
        rows += scan_handlers(lines, display)
        for lineno, line in enumerate(lines, 1):
            rows += scan_line(line, display, lineno)
    return rows, read, skipped


def headline_for(rows: list, files: int) -> str:
    if not files:
        return "Nothing to scan: no source or config files found under this path."
    by_kind = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row)

    zeros = by_kind.get("error_becomes_zero") or []
    if zeros:
        where = ", ".join(sorted({r["file"] for r in zeros})[:3])
        return (str(len(zeros)) + " site(s) turn a failure into 0/empty (" + where
                + ") — until a per-example error field exists, a batch of timeouts and a real "
                "regression produce the same number.")
    swallowed = by_kind.get("swallowed_error") or []
    if swallowed:
        return (str(len(swallowed)) + " handler(s) swallow an error with nothing recorded — check whether "
                "the aggregate's denominator still counts the lost examples.")
    off = by_kind.get("default_off_flag") or []
    if off:
        return (str(len(off)) + " component(s) default to off — if the better method is one of them, "
                "look for the recorded A/B that justified building it.")
    caps = by_kind.get("silent_cap") or []
    if caps:
        return (str(len(caps)) + " cap/sample site(s) — check whether any of them sits in the "
                "measurement path and goes unreported.")
    nondet = by_kind.get("nondeterminism") or []
    if nondet:
        return (str(len(nondet)) + " sampling site(s): the metric is a random variable, so check for "
                "repeats, a seed, and reported variance before believing a single run.")
    unpinned = by_kind.get("unpinned_model") or []
    if unpinned:
        return (str(len(unpinned)) + " unpinned model reference(s) — the provider can move the baseline "
                "under any trend chart drawn across them.")
    return ("No fail-soft, default-off, cap, or sampling candidates found in " + str(files)
            + " file(s). Read the caveat before reporting this as clean.")


CAVEAT = (
    "Textual and context-free. It cannot tell a cap in an eval loop (the finding) from a cap in a CLI "
    "flag or a UI page size (fine), nor a default-off feature flag from a default-off debug switch. "
    "Handler bodies are read by indentation and brace heuristics, so an unusual layout may be "
    "misclassified. Errors swallowed inside a library you call, or in a service, are invisible here."
)


def main() -> int:
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(parser)
    parser.add_argument("--kind", choices=KIND_ORDER, help="show only this kind of candidate")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        print("error: no such path: " + str(root), file=sys.stderr)
        return 2

    rows, files, skipped = scan(root)
    headline = headline_for(rows, files) + skipped_note(skipped)
    if args.kind:
        rows = [r for r in rows if r["kind"] == args.kind]
    rows.sort(key=lambda r: (KIND_ORDER.index(r["kind"]) if r["kind"] in KIND_ORDER else 99, r["file"], r["line"]))

    counts = {"files_scanned": files, "files_skipped_unread": len(skipped), "candidates_total": len(rows)}
    for kind in KIND_ORDER:
        found = sum(1 for r in rows if r["kind"] == kind)
        if found:
            counts[kind] = found
    emit(envelope("find_fail_soft", root, headline, CAVEAT, counts, rows[:args.limit]), args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
