#!/usr/bin/env python3
"""Enumerate where a repository produces numbers: metrics, thresholds, weights.

The first question of a measurement audit is "what numbers exist, and who reads
them" — and answering it by grepping from memory is how a metric that gates
releases gets missed. This finds five things:

  - metric definitions      (a function, assignment, or config key naming a measure)
  - thresholds              (a comparison or constant that turns a number into a decision)
  - composite weights       (a hand-written 0.4/0.2/0.4 that decides a headline)
  - renormalized composites (missing components dropped before averaging, so the
                             survivors' mean is reported as the whole)
  - zero defaults           (a metric whose error path returns 0.0, so "never ran"
                             and "ran badly" become the same number)

and then reports which defined metric names appear *nowhere else in the tree* —
the dead-measurement candidates, which are usually the larger finding.

Everything here is name-based and textual: it produces candidates, never findings.

Usage:
  python find_metrics.py .
  python find_metrics.py eval/ --format json
"""

import re
import sys
import argparse
from pathlib import Path

from common import (
    CODE_SUFFIXES, CONFIG_SUFFIXES, add_common_args, candidate, configure_output,
    emit, envelope, iter_files, read_source, rel, skipped_note,
)

# Tokens that make an identifier a measure on their own.
STRONG_TOKENS = frozenset({
    "accuracy", "acc", "precision", "recall", "f1", "fbeta", "auc", "auroc", "auprc",
    "ndcg", "mrr", "map", "correctness", "faithfulness", "groundedness", "grounding",
    "relevance", "helpfulness", "coherence", "fluency", "toxicity", "calibration",
    "brier", "rmse", "mse", "mae", "mape", "perplexity", "bleu", "rouge", "meteor",
    "bertscore", "score", "quality", "metric", "judge", "grade",
    "rating", "eval", "benchmark", "kappa", "agreement", "regret", "mean",
    "average", "avg", "median", "delta", "lift", "uplift", "pvalue", "baseline",
})
# Plurals on their own are usually a container (`scores = [...]`), not a measure.
# They still count next to a strong token: `metric_scores` matches on `metric`.
# "rate" is only a measure next to one of these; learning_rate and sample_rate are not.
RATE_PARTNERS = frozenset({
    "win", "pass", "hit", "success", "error", "failure", "fail", "accept", "reject",
    "click", "conversion", "completion", "refusal", "abandon", "match", "correct",
})
THRESHOLD_TOKENS = frozenset({
    "threshold", "cutoff", "min", "max", "target", "budget", "slo", "sla", "gate",
    "tolerance", "floor", "ceiling", "limit", "bar",
})

_SPLIT_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
# Leading-dot decimals and scientific notation are ordinary threshold literals:
# `quality_score > .75`, `pvalue < 1e-3`. Requiring a digit prefix missed both.
_NUMBER = r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"

# `def compute_accuracy(`, `func Accuracy(`, `fn recall(`, `func (r *Runner) Accuracy(`,
# and the C-family typed method `double accuracy(` / `public float winRate(`.
# A bare `qualityScore(` is deliberately NOT matched: it is indistinguishable from
# a call, and treating every call as a definition would bury the real ones.
_DEF_RE = re.compile(
    r"\b(?:def|function|func|fn)\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)\s*\("
    r"|^\s*(?:(?:public|private|protected|static|final|virtual|override|async|export)\s+)*"
    r"(?:[A-Za-z_][\w:<>,\[\] ]*?[\w>\]])\s+([A-Za-z_][\w]*)\s*\([^;]*\)\s*(?:const\s*)?\{"
)
# `accuracy = ...`, `self.win_rate = ...`, `const qualityScore = ...`, `NDCG_AT_10 = ...`
# The lookarounds keep `score == 0.75` out: a comparison is not a definition, and it
# is already caught (better) by _COMPARE_RE below.
_ASSIGN_RE = re.compile(r"^\s*(?:(?:export|default|const|let|var|final|readonly|"
                        r"public|private|protected|static)\s+)*"
                        r"(?:self\.|this\.)?([A-Za-z_][\w]*)\s*(?::[^=\n]+?)?"
                        r"(?<![=!<>+\-*/])=(?!=)\s*(.+)$")
# `"accuracy": 0.81` in JSON/dict, and `accuracy:` in YAML
_KEY_RE = re.compile(r"^\s*[\"']?([A-Za-z_][\w]*)[\"']?\s*:\s*(.*)$")
# `if score >= 0.75`, `while p < 0.05`
_COMPARE_RE = re.compile(r"([A-Za-z_][\w.]*)\s*(>=|<=|==|>|<)\s*(" + _NUMBER + r")\b")
# `[v for v in parts.values() if v is not None]` / `.filter(v => v !== null)` —
# the composite silently renormalizes over whichever components produced a value.
_RENORMALIZE_RE = re.compile(
    r"for\s+\w+\s+in\s+[^\n]*\bif\s+[\w.\[\]()]+\s+is\s+not\s+None"
    r"|\.filter\([^)]*(?:!==?\s*(?:null|undefined)|\bBoolean\b)"
    r"|\bdropna\(\)"
)
# `0.4 * relevance + 0.2 * latency`
_WEIGHTED_SUM_RE = re.compile(r"(?:" + _NUMBER + r"\s*\*\s*[A-Za-z_]|[A-Za-z_][\w.]*\s*\*\s*" + _NUMBER + r")")
# `return 0.0`, `except ...: return 0`, `.get("accuracy", 0)`, `or 0.0`
_ZERO_RETURN_RE = re.compile(r"\breturn\s+(?:0|0\.0+|0\.)\s*(?:#.*)?$")
_ZERO_DEFAULT_RE = re.compile(r"\.get\(\s*[\"'][^\"']+[\"']\s*,\s*(?:0|0\.0+)\s*\)|(?:\bor\s+(?:0|0\.0+))\b")

CONFIRM = {
    "metric_definition": "read the formula and its inputs; then find who consumes the number",
    "threshold": "find what decision this gates and where the value came from",
    "composite_weight": "ask where these weights were derived; unexplained weights decide the headline",
    "zero_default": "check whether a failed run is distinguishable from a genuine 0.0",
    "renormalized_composite": "check whether the composite reports how many components contributed",
    "no_consumer": "grep the name yourself before believing this; a runtime-built key reads as unused",
}


# pytest calls these by collection, not by a textual reference, so "referenced
# nowhere" is always true of them and says nothing. Left in `metric_definition`,
# excluded from the dead-measurement map.
_DISCOVERED_RE = re.compile(r"^(?:test_|Test)|_test$")


def tokens_of(name: str) -> frozenset:
    return frozenset(t.lower() for t in _SPLIT_RE.findall(name))


def is_metric_name(name: str) -> bool:
    toks = tokens_of(name)
    if toks & STRONG_TOKENS:
        return True
    return "rate" in toks and bool(toks & RATE_PARTNERS)


def is_threshold_name(name: str) -> bool:
    toks = tokens_of(name)
    if not (toks & THRESHOLD_TOKENS):
        return False
    # `max_retries` is a limit, not a measurement threshold; require a measure or a
    # bare threshold word ("THRESHOLD = 0.8") to keep this out of retry config.
    return bool(toks & STRONG_TOKENS) or bool(toks & {"threshold", "cutoff", "slo", "sla", "gate", "bar"})


def looks_numeric(value: str) -> bool:
    return re.match(r"^\s*" + _NUMBER + r"\s*[,;)}\]]?\s*(?:#.*)?$", value) is not None


def named_on(line: str, stripped: str) -> list:
    """The (name, value) pairs this line defines: a def, an assignment, or a key."""
    names = []
    definition = _DEF_RE.search(line)
    if definition:
        names.append((definition.group(1) or definition.group(2), ""))
    assignment = _ASSIGN_RE.match(line)
    if assignment:
        names.append((assignment.group(1), assignment.group(2)))
    else:
        key = _KEY_RE.match(line)
        if key and "=" not in stripped.split(":")[0]:
            names.append((key.group(1), key.group(2)))
    return names


def name_candidates(line: str, stripped: str, path_display: str, lineno: int, defined: dict) -> list:
    """Candidates from the names a line defines, recording module-level metrics."""
    out = []
    top_level = line[:1] not in (" ", "\t")
    for name, value in named_on(line, stripped):
        is_threshold = is_threshold_name(name) and looks_numeric(value)
        if is_threshold:
            out.append(candidate(
                "threshold", path_display, lineno,
                name + " = " + value.strip().rstrip(",;") + " — a constant that turns a number into a decision",
                CONFIRM["threshold"], stripped,
            ))
        if not is_metric_name(name):
            continue
        # Only module-level definitions are eligible for the dead-measurement check:
        # a local inside a function is a step in a computation, and "this local is
        # never read in another file" is not a finding.
        if top_level and not _DISCOVERED_RE.search(name):
            defined.setdefault(name, []).append((path_display, lineno))
        if not is_threshold:
            out.append(candidate(
                "metric_definition", path_display, lineno,
                name + " — a measure is defined here",
                CONFIRM["metric_definition"], stripped,
            ))
    return out


def scan_line(line: str, path_display: str, lineno: int, defined: dict) -> list:
    """Candidates from one line. `defined` accumulates metric name -> first site."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "//", "*", "<!--")):
        return []

    out = name_candidates(line, stripped, path_display, lineno, defined)

    for left, op, number in _COMPARE_RE.findall(line):
        leaf = left.split(".")[-1]
        if is_metric_name(leaf) or is_threshold_name(leaf):
            out.append(candidate(
                "threshold", path_display, lineno,
                leaf + " " + op + " " + number + " — a measure gates control flow here",
                CONFIRM["threshold"], stripped,
            ))

    if len(_WEIGHTED_SUM_RE.findall(line)) >= 2 and "+" in line:
        out.append(candidate(
            "composite_weight", path_display, lineno,
            "a weighted sum of " + str(len(_WEIGHTED_SUM_RE.findall(line))) + " terms",
            CONFIRM["composite_weight"], stripped,
        ))
    elif "weight" in stripped.lower() and re.search(r"0\.\d+", stripped):
        out.append(candidate(
            "composite_weight", path_display, lineno,
            "hand-written weights",
            CONFIRM["composite_weight"], stripped,
        ))

    if _RENORMALIZE_RE.search(line):
        out.append(candidate(
            "renormalized_composite", path_display, lineno,
            "missing values are dropped here — the survivors' aggregate can be reported as the whole",
            CONFIRM["renormalized_composite"], stripped,
        ))

    if _ZERO_RETURN_RE.search(stripped) or _ZERO_DEFAULT_RE.search(stripped):
        out.append(candidate(
            "zero_default", path_display, lineno,
            "a zero default — check it cannot stand in for 'never measured'",
            CONFIRM["zero_default"], stripped,
        ))

    return out


def scan(root: Path) -> tuple:
    """(candidates, defined-name -> sites, corpus, files skipped unread)."""
    files = iter_files(root, CODE_SUFFIXES | CONFIG_SUFFIXES)
    found = []
    defined: dict = {}
    corpus = {}
    skipped = []
    for path in files:
        lines, reason = read_source(path)
        display = rel(path, root)
        if reason:
            skipped.append((display, reason))
            continue
        corpus[display] = lines
        for lineno, line in enumerate(lines, 1):
            found += scan_line(line, display, lineno, defined)
    return found, defined, corpus, skipped


def dead_metrics(defined: dict, corpus: dict) -> list:
    """Module-level metric names that appear nowhere but their own definitions.

    Deliberately strict: one reference anywhere — the same file included — is
    enough to clear a name. A helper called only by its own module is not dead
    measurement, and a detector that says it is gets switched off.

    *Every* definition site is excluded, not just the first. Two files defining
    the same unused metric would otherwise each count as the other's consumer,
    and the pair would clear each other.
    """
    out = []
    for name, sites in sorted(defined.items()):
        if len(name) < 4:
            continue  # too short to grep meaningfully; `acc` matches everything
        pattern = re.compile(r"\b" + re.escape(name) + r"\b")
        definition_sites = set(sites)
        uses = 0
        for other, lines in corpus.items():
            for other_line, text in enumerate(lines, 1):
                if (other, other_line) in definition_sites:
                    continue
                uses += len(pattern.findall(text))
        if uses:
            continue
        for display, lineno in sites:
            out.append(candidate(
                "no_consumer", display, lineno,
                name + " is defined here and referenced nowhere in the tree — candidate dead measurement",
                CONFIRM["no_consumer"],
            ))
    return out


MEASURE_KINDS = ("metric_definition", "threshold", "composite_weight", "renormalized_composite")


def scope_zero_defaults(rows: list) -> list:
    """Drop zero-default rows from files with no measurement content at all.

    `return 0` in a CLI's main() and `settings.get("retries", 0)` are not
    measurement, and reporting them as "zero-default sites" in an unrelated
    subtree manufactures measurement content — the one thing this skill tells its
    reader never to do. A zero default inside an exception handler is still caught
    by find_fail_soft.py, which reads the handler rather than the name.
    """
    measured = {r["file"] for r in rows if r["kind"] in MEASURE_KINDS}
    return [r for r in rows if r["kind"] != "zero_default" or r["file"] in measured]


def dedupe(rows: list, limit: int) -> list:
    """One row per (kind, file, detail); kinds ordered by how often they matter."""
    order = ["no_consumer", "renormalized_composite", "zero_default", "composite_weight",
             "threshold", "metric_definition"]
    seen = set()
    unique = []
    for row in rows:
        key = (row["kind"], row["file"], row["detail"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    unique.sort(key=lambda r: (order.index(r["kind"]) if r["kind"] in order else 99, r["file"], r["line"]))
    return unique[:limit]


def headline_for(rows: list, defined: dict, files: int) -> str:
    """The headline the rows support.

    Definition counts come from the emitted rows, not from `defined`: that map
    holds only module-level names (it exists for the dead-metric check), so a
    metric defined under an indented YAML key produced a "No metric definitions
    found" headline sitting directly above the definition row it had just
    printed. A report that contradicts its own body stops the reader.
    """
    if not files:
        return "Nothing to scan: no source or config files found under this path."
    dead = [r for r in rows if r["kind"] == "no_consumer"]
    renorm = [r for r in rows if r["kind"] == "renormalized_composite"]
    zeros = [r for r in rows if r["kind"] == "zero_default"]
    weights = [r for r in rows if r["kind"] == "composite_weight"]
    if dead:
        unique = sorted({r["detail"].split(" ")[0] for r in dead})
        names = ", ".join(unique[:4])
        return (str(len(unique)) + " metric name(s) defined and referenced nowhere else in the tree, at "
                + str(len(dead)) + " site(s) (" + names
                + ") — a number nobody reads is dead measurement, and usually the bigger finding.")
    if renorm:
        return (str(len(renorm)) + " site(s) drop missing values before aggregating: a run where half the "
                "components skipped can report the survivors' average as if it were the whole.")
    if zeros:
        return (str(len(zeros)) + " zero-default site(s) alongside " + str(len(defined))
                + " metric name(s): check that 'never measured' cannot be reported as 0.0.")
    if weights:
        return (str(len(weights)) + " composite/weight site(s): ask where each weight was derived, "
                "since an unexplained weight decides the headline.")
    named = {r["detail"].split(" ")[0] for r in rows if r["kind"] == "metric_definition"}
    if named:
        return (str(len(named)) + " metric name(s) across " + str(files)
                + " file(s). Build the metric x inputs x consumer x N inventory from these.")
    thresholds = [r for r in rows if r["kind"] == "threshold"]
    if thresholds:
        return (str(len(thresholds)) + " threshold site(s) but no metric definition: the decisions are "
                "visible here and whatever produces the numbers they gate is not.")
    return ("No metric definitions found under this path — if that is unexpected, measurement lives "
            "elsewhere; if it is expected, say so in one line and stop.")


CAVEAT = (
    "Name-based and textual. A measure computed through a name this vocabulary does not know is "
    "invisible, and an ordinary variable that happens to be called `score` shows up here. "
    "Definitions are recognized by a keyword (`def`/`func`/`function`), a Go receiver, or a typed "
    "C-family signature; a bare class method (`qualityScore(rows) {`) is NOT matched, because it is "
    "textually identical to a call and matching it would report every call site as a definition. "
    "Consumer counts are literal text matches, so a metric read through a key built at runtime "
    "reads as unused, and a name matched inside a comment reads as used. Confirm every row by reading."
)


def main() -> int:
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(parser)
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        print("error: no such path: " + str(root), file=sys.stderr)
        return 2

    rows, defined, corpus, skipped = scan(root)
    rows += dead_metrics(defined, corpus)
    rows = scope_zero_defaults(rows)
    shown = dedupe(rows, args.limit)
    counts = {
        "files_scanned": len(corpus),
        "files_skipped_unread": len(skipped),
        "metric_names": len(defined),
        "candidates_total": len(rows),
        "candidates_shown": len(shown),
    }
    headline = headline_for(rows, defined, len(corpus)) + skipped_note(skipped)
    emit(envelope("find_metrics", root, headline, CAVEAT, counts, shown), args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
