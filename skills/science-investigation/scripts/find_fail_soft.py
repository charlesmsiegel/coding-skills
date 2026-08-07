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
    r"|\.catch\s*\(\s*(?:\([^)]*\)|[A-Za-z_$][\w$]*)?\s*=>\s*\{"   # judge(row).catch(() => { ... })
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
# never surfaces. Comments are stripped before the check — and so are string
# bodies, because `log("cannot rethrow")` above a `return 0.0` was reading as a
# re-raise and producing a clean headline over the exact shape this tool exists
# to find. The quotes are kept so `return ""` still matches _ZERO_BODY_RE.
_COMMENT_RE = re.compile(r"(?<![:\w])#.*$|//.*$")
_STRING_BODY_RE = re.compile(r"(['\"])(?:\\.|(?!\1).)*\1")

# `judge(row).catch(() => 0)` — a promise rejection handler, which is fail-soft in
# exactly the same way as a catch block and never opened one.
_PROMISE_CATCH_RE = re.compile(r"\.catch\s*\(\s*(?:\([^)]*\)|[A-Za-z_$][\w$]*)?\s*=>\s*(.+)$")
_ARROW_ZERO_RE = re.compile(r"^\s*(?:0(?:\.0*)?(?![\d.])|\"\"|'')")
_ARROW_EMPTY_RE = re.compile(r"^\s*(?:\{\s*\}|null|undefined|void\s+0)")

# The flag word may open the name (`enable_reranker`) or sit inside it
# (`rag.use_reranker`), so the prefix is optional. `on` alone matches half the
# words in English, which is what _ENABLE_TOKEN_RE below exists to filter back out.
_FLAG_NAME = r"[\w.]*(?:enable|enabled|use|using|with|allow|feature|flag|active|on)[\w]*"
# An optional type annotation may sit between the name and the value:
# `ENABLE_RERANKER: bool = False`, `const useReranker: boolean = false`.
# The optional `["']` carries JSON's `{"use_reranker": false}`, where the closing
# key quote sits between the name and the delimiter.
_ANNOTATED = r"[\"']?\s*(?::\s*[A-Za-z_][\w.\[\]<>| ]*)?\s*[:=]\s*"
_DEFAULT_OFF_RE = re.compile(
    r"\b(" + _FLAG_NAME + r")" + _ANNOTATED + r"(?:false|0)\b"
    r"|\b(" + _FLAG_NAME + r")" + _ANNOTATED + r"[\"'](?:false|off|0|no)[\"']",
    re.IGNORECASE,
)
# `disable_reranker: true` is the same configuration written the other way round.
_DISABLE_NAME = r"[\w.]*(?:disable|disabled|skip|off|bypass|ignore)[\w]*"
_DISABLE_ON_RE = re.compile(
    r"\b(" + _DISABLE_NAME + r")" + _ANNOTATED + r"(?:true|1)\b"
    r"|\b(" + _DISABLE_NAME + r")" + _ANNOTATED + r"[\"'](?:true|on|1|yes)[\"']",
    re.IGNORECASE,
)
DISABLE_TOKENS = frozenset({"disable", "disabled", "skip", "off", "bypass", "ignore"})
# The name is confirmed by its *tokens*, not by a substring: `connection_timeout`
# contains "on" and `user_id` contains "use", while `useReranker` and
# `ENABLE_RERANKER` are genuine switches. Splitting first is what tells them apart.
_TOKEN_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
FLAG_TOKENS = frozenset({"enable", "enabled", "disable", "use", "using", "with",
                         "allow", "feature", "flag", "active", "on", "toggle"})


_CAP_RE = re.compile(
    r"\[\s*:\s*\d{1,6}\s*\]"                             # rows[:100]
    r"|\.head\(\s*\d+"                                     # df.head(50)
    r"|\bislice\(\s*[^,]+,\s*\d+"                          # islice(rows, 50)
    r"|\blimit[\"']?\s*[=:(]\s*\d+"                        # limit=100, "limit": 100
    r"|\blimit\s+\d+"                                      # SQL LIMIT 100
    r"|\b(?:sample|choices|choice)\(\s*[^)]*(?:\b[nk]\s*=\s*\d|,\s*\d+\s*\))"  # sample(rows, 50) / (rows, k=50) / (n=50)
    r"|\bmax_(?:examples|rows|samples|cases|items|records)[\"']?\s*[=:]\s*\d+"
    r"|\bhead\s+-n?\s*\d+",                                # head -n 50 / head -50
    re.IGNORECASE,
)
_NONDET_RE = re.compile(
    r"\btemperature[\"']?\s*[=:]\s*([0-9]*\.?[0-9]+)"
    r"|\btop_p[\"']?\s*[=:]\s*([0-9]*\.?[0-9]+)"
    r"|\bdo_sample[\"']?\s*[=:]\s*true",
    re.IGNORECASE,
)
# Quoted in code, bare in config: `model="gpt-4o"`, `MODEL=gpt-4`, `model: gpt-4`.
# Requiring quotes meant the .env and YAML forms were scanned and never reported.
_MODEL_RE = re.compile(
    r"(?:[\"']|[=:]\s*)"
    r"((?:gpt|claude|gemini|llama|mistral|mixtral|command|titan|sonnet|opus|haiku|o[134]|text-embedding)"
    r"[A-Za-z0-9._-]*)"
    r"(?=[\"']|\s|,|$)"
)
# A full date, a compact date, a four-digit provider snapshot (gpt-4-0613,
# gpt-4-1106-preview), an explicit -v2, or an @-pinned tag.
_PINNED_RE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{6,8}|-\d{4}(?!\d)|-v\d+|@\d")


def is_flag_name(name: str, tokens=FLAG_TOKENS) -> bool:
    return bool({t.lower() for t in _TOKEN_RE.findall(name)} & tokens)


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
        surfaced = _SURFACED_RE.search(_STRING_BODY_RE.sub(r"\1\1", joined))
        zeroed = _ZERO_BODY_RE.search(joined)
        # A raise clears the handler only when nothing else in it produces a score.
        # `if isinstance(exc, Fatal): raise` followed by `return 0.0` re-raises one
        # class and scores every other failure, and reading the raise as covering
        # the whole handler reported that as clean.
        if surfaced and not zeroed:
            continue
        if zeroed:
            detail = "a failure here yields 0/empty — indistinguishable from a genuine low score"
            if surfaced:
                detail += " (another path re-raises — check which errors take which)"
            out.append(candidate(
                "error_becomes_zero", display, index + 1, detail,
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


def scan_promise_catches(line: str, display: str, lineno: int) -> list:
    """`.catch(() => 0)` and friends: fail-soft with no catch block to open."""
    match = _PROMISE_CATCH_RE.search(line)
    if not match:
        return []
    body = match.group(1)
    if _SURFACED_RE.search(_STRING_BODY_RE.sub(r"\1\1", body)):
        return []
    if _ARROW_ZERO_RE.match(body) or _ZERO_BODY_RE.search(body):
        return [candidate(
            "error_becomes_zero", display, lineno,
            "a rejected promise resolves to 0/empty — indistinguishable from a genuine low score",
            CONFIRM["error_becomes_zero"], line.strip(),
        )]
    if _ARROW_EMPTY_RE.match(body):
        return [candidate(
            "swallowed_error", display, lineno,
            "a rejected promise is discarded here — the example is lost without a trace",
            CONFIRM["swallowed_error"], line.strip(),
        )]
    return []


_SAMPLING_OFF_RE = re.compile(r"\bdo_sample[\"']?\s*[=:]\s*false", re.IGNORECASE)


def scan_line(line: str, display: str, lineno: int, sampling_off: bool = False) -> list:
    out = []
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "//", "*", "<!--")):
        return out

    out += scan_promise_catches(line, display, lineno)

    match = _DEFAULT_OFF_RE.search(line)
    if match and is_flag_name(match.group(1) or match.group(2) or ""):
        out.append(candidate(
            "default_off_flag", display, lineno,
            "a component defaults to off here",
            CONFIRM["default_off_flag"], stripped,
        ))
    else:
        negated = _DISABLE_ON_RE.search(line)
        if negated and is_flag_name(negated.group(1) or negated.group(2) or "", DISABLE_TOKENS):
            out.append(candidate(
                "default_off_flag", display, lineno,
                "a component is disabled here by a negative switch defaulting to on",
                CONFIRM["default_off_flag"], stripped,
            ))

    if _CAP_RE.search(line):
        out.append(candidate(
            "silent_cap", display, lineno,
            "a cap or sample is applied here",
            CONFIRM["silent_cap"], stripped,
        ))

    nondet = _NONDET_RE.search(line)
    if nondet and not sampling_off:
        value = nondet.group(1) or nondet.group(2)
        # top_p = 1.0 is the neutral setting, not a sampling knob turned up.
        if nondet.group(2) is not None and float(nondet.group(2)) >= 1.0:
            value = "0"
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
        sampling_off = bool(_SAMPLING_OFF_RE.search("\n".join(lines)))
        for lineno, line in enumerate(lines, 1):
            rows += scan_line(line, display, lineno, sampling_off)
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
        # The headline still describes the whole tree — suppressing a more serious
        # candidate because it was filtered out would be the worse failure — but it
        # has to say so, or the headline and the rows below it appear to disagree.
        shown = [r for r in rows if r["kind"] == args.kind]
        if shown and headline_for(shown, files) != headline:
            headline = ("[--kind " + args.kind + ": " + str(len(shown)) + " row(s) shown; this "
                        "headline covers the whole tree] " + headline)
        rows = shown
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
