#!/usr/bin/env python3
"""Trace one threshold or one metric name across the whole tree.

Two questions this answers that a plain grep does not:

  - **Does this threshold have one meaning?** `0.7` written as a constant in one
    file and as a literal in three comparisons elsewhere is four independent
    values that look like one. Changing the constant moves nothing.
  - **Does this name mean one thing?** `quality_score` defined under `evaluation/`
    and again under `experiments/` is the two-systems-one-name trap: two
    measurement systems, different populations, one word, and someone will cite
    the wrong one to settle a question only the other could answer.

Hits are classified as definitions, declarations, comparisons, config, docs, and
tests, because the mix is the finding: many comparisons and no definition means
the value is repeated at the point of use.

Usage:
  python trace_value.py 0.7 .
  python trace_value.py quality_score . --format json
  python trace_value.py 'p9[59]' . --regex
"""

import re
import sys
import argparse
from pathlib import Path

# `python -I` (the safe way to run on an untrusted checkout) leaves this script's
# own directory off sys.path, so the sibling import below has to put it back.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    CODE_SUFFIXES, CONFIG_SUFFIXES, DOC_SUFFIXES, add_common_args, bound_lines, candidate, configure_output,
    coverage_note, emit, envelope, is_config, iter_files, looks_generated, mask_strings,
    read_source, rel, relocate, skipped_note, split_comment, unpack_source,
)

# Leading-dot and exponent forms count as numbers here too. Without this, tracing
# `.75` fell through to the identifier branch, where `\b` cannot match before a
# dot — so the tool reported the value appears nowhere while the tree was full of it.
_NUMERIC_RE = re.compile(r"^-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")
# `<-` is R's assignment, and must be recognized before the comparison test or
# its `<` reads as a relational operator — which classified every R metric
# definition as a comparison and headlined it "defined nowhere".
_R_ASSIGN_RE = re.compile(r"<-")
_ASSIGN_RE = re.compile(r"(?<![=!<>+\-*/])=(?!=)|<-")
_COMPARE_RE = re.compile(r"(?:>=|<=|==|!=|>|<)(?!-)")
# The needle is the thing being assigned: `quality_score = ...` / `quality_score: number =`
_TARGET_RE = re.compile(r"^\s*(?::[^=\n]+?)?(?:(?<![=!<>+\-*/])=(?!=)|<-)")
# ...or the thing being declared: `def quality_score(`, `const quality_score`
_DECLARE_RE = re.compile(r"\b(?:def|function|func|fun|fn|sub|class|const|let|var)\s+$"
                         # `double quality_score(` — the typed C-family form that
                         # find_metrics already reads as a definition.
                         r"|^\s*(?:(?:public|private|protected|static|final)\s+)*"
                         r"[A-Za-z_][\w:<>,\[\] ]*?[\w>\]]\s+$")

# `solve_rate: float | None` on a model, `solve_rate: number;` in an interface: a
# name given a type and no value. Counting that as a definition beside the one
# place that computes it reported one real repository's solve rate "defined
# independently in 2 places". Only a bare annotation, or one defaulting to null,
# declares without defining — `QUALITY_THRESHOLD: float = 0.7` sets the value.
# Plain JavaScript has no annotations, so there `name: rate` stays a definition.
_ANNOTATION_RE = re.compile(r"^\s*\??\s*:(?![:=])\s*([^=]*?)\s*"
                            r"(?:(?<![=!<>])=(?![=>])\s*(.*?))?\s*[;,]?\s*$")
_TYPE_SHAPED_RE = re.compile(r"^[A-Za-z_][\w.\[\]<>|, ?]*$")
_NULL_DEFAULT_RE = re.compile(r"^(?:None|null|nil|undefined)$")
# Where a declaration can start: a statement, a parameter, a field of self/this,
# or after a declaring keyword. Not after `cond ?`, whose `:` is a ternary.
_DECLARATION_SITE_RE = re.compile(r"(?:^|[({,;]|\b(?:self|this)\.|"
                                  r"\b(?:let|var|val|const|readonly|public|private|protected|static|declare)\s)\s*$")
UNTYPED_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs"})

CONFIRM = {
    "text":
        "the value appears inside a string here, so it gates nothing — confirm it is not a "
        "message that has drifted from the live value it describes",
    "comment":
        "a commented-out site: confirm whether the live value moved on without it",
    "definition": "confirm this is the source of truth, and that the other sites read it rather than repeat it",
    "declaration": "the name is typed here and given no value; confirm the place that computes it is the only one",
    "comparison": "confirm what decision this gates and that it means the same thing as the other sites",
    "config": "confirm which run actually loads this config; a stale config reads as an active threshold",
    "doc": "confirm the documented value still matches the code — docs drift silently",
    "test": "a test pinning the value is good; confirm it pins the meaning too, not just the number",
    "other": "read the line and decide which of the above it is",
}


def build_pattern(needle: str, as_regex: bool) -> re.Pattern:
    """Match the value, and only the value.

    A numeric needle must not match inside a longer number — searching `0.7` and
    hitting `0.75` is exactly the kind of wrong count this skill exists to catch.
    """
    if as_regex:
        return re.compile(needle)
    if _NUMERIC_RE.match(needle):
        # Reject every numeric continuation, not just a trailing digit: 0.7 is not
        # 0.75, not 0.7e3, and not 0.7_5. A tool that inflates its own site count
        # has no business auditing anyone else's.
        # An unsigned needle must not match `-0.7`: a tree holding both signs
        # would otherwise report two sites for a value that appears once, and can
        # reach the "defined independently" headline on the strength of it.
        lead = r"(?<![\w.])" if needle.startswith("-") else r"(?<![\w.\-])"
        return re.compile(lead + re.escape(needle) + r"(?![\d_.]|[eE][-+]?\d)")
    return re.compile(r"\b" + re.escape(needle) + r"\b")


def declares_only(before: str, after: str) -> bool:
    """Whether the name between `before` and `after` is annotated and given no value."""
    if not _DECLARATION_SITE_RE.search(before):
        return False
    annotation = _ANNOTATION_RE.match(after)
    if annotation is None or not _TYPE_SHAPED_RE.match(annotation.group(1)):
        return False
    default = annotation.group(2)
    return default is None or bool(_NULL_DEFAULT_RE.match(default))


def line_views(line: str, path: Path) -> tuple:
    """(length of the code before any comment, the line with string bodies masked).

    Computed once per line rather than once per occurrence: a one-letter needle
    in a long line was masking the whole line again for every hit.
    """
    code = split_comment(line)[0]
    return len(code), (line if is_config(path) else mask_strings(line))


def classify(line: str, display: str, path: Path, span: tuple, numeric: bool = True, views=None) -> str:
    """Which kind of site this is.

    Deliberately classifies on the path *relative to the scanned root*: an
    absolute path carries the checkout's own directory names, and a repo cloned
    under ~/tests/ would otherwise report every site as a test.
    """
    lowered = display.lower().replace('\\', "/")
    if re.search(r"(?:^|[/_.-])(?:tests?|spec|specs|__tests__)(?:[/_.-]|$)", lowered):
        return "test"
    # Docs first: `# Release threshold 0.7` is a Markdown heading, and reading its
    # `#` as a source comment gave the auditor the wrong thing to confirm.
    if path.suffix in DOC_SUFFIXES:
        return "doc"
    if is_config(path):
        return "config"

    code_length, scannable = views if views is not None else line_views(line, path)
    if span[0] >= code_length:
        return "comment"     # `# QUALITY_THRESHOLD = 0.7` is not a source of truth

    # Masked so the operators around the match are read from code, not from a
    # message: `message = "quality_score > 0.7"` is not a live comparison.
    if scannable[span[0]:span[1]] != line[span[0]:span[1]]:
        return "text"     # the match is inside a string literal, not in code
    before, after = scannable[:span[0]], scannable[span[1]:]
    if _R_ASSIGN_RE.match(after.lstrip()[:2]):
        return "definition"          # `quality_score <- ...`: the needle is the target
    if _R_ASSIGN_RE.search(before[-3:]) and not numeric:
        return "other"               # `reported <- quality_score`: the needle is read
    if _COMPARE_RE.search(before[-4:]) or _COMPARE_RE.match(after.lstrip()[:2]):
        return "comparison"
    # The needle may be the value (`CUTOFF = 0.75`) or the name being defined
    # (`quality_score = compute()`, `def quality_score(`, `quality_score:`).
    # An assignment *before* the match only makes this a definition when the needle
    # is a literal being assigned. For a name, sitting on the right-hand side means
    # it is being read — `reported = quality_score` is a consumer, and counting it
    # as a second definition produced a false "defined independently" headline.
    if numeric and _ASSIGN_RE.search(before) and not before.rstrip().endswith(","):
        return "definition"
    if not numeric and path.suffix not in UNTYPED_SUFFIXES and declares_only(before, after):
        return "declaration"
    if _TARGET_RE.match(after) or _DECLARE_RE.search(before):
        return "definition"
    if not before.strip(" \t\"'-") and after.lstrip().startswith(":"):
        return "definition"
    return "other"


def scan(root: Path, pattern: re.Pattern, numeric: bool = True) -> tuple:
    """(rows, unread files, scanned count, generated files, truncated line count).

    Every occurrence on a line is classified, not just the first — `score > 0.7 and
    backup > 0.7` is two consumers, and minified config puts many on one line. Rows
    are then one per (line, role): repeating an identical row for each occurrence
    would pad the list an auditor has to read without adding a place to look.
    """
    rows, skipped, generated, seen = [], [], [], set()
    notebooks: dict = {}
    scanned, truncated = 0, 0
    for path in iter_files(root, CODE_SUFFIXES | CONFIG_SUFFIXES | DOC_SUFFIXES):
        display = rel(path, root)
        lines, reason, locations = unpack_source(read_source(path))
        if reason:
            skipped.append((display, reason))
            continue
        if looks_generated(path, lines):
            # A bundle's sites cannot be opened (one 145K-character line) and repeat
            # the source it was built from; tracing a short name through one took
            # minutes. Named in the caveat rather than dropped silently.
            generated.append(display)
            continue
        lines, cut = bound_lines(lines)
        truncated += cut
        scanned += 1
        if locations:
            notebooks[display] = locations
        for lineno, line in enumerate(lines, 1):
            views = None
            for match in pattern.finditer(line):
                if views is None:
                    views = line_views(line, path)
                kind = classify(line, display, path, match.span(),
                                numeric or bool(_NUMERIC_RE.match(match.group(0))), views)
                if (display, lineno, kind) in seen:
                    continue
                seen.add((display, lineno, kind))
                rows.append(candidate(kind, display, lineno, kind + " site", CONFIRM[kind], line.strip()))
    return relocate(rows, notebooks), skipped, scanned, generated, truncated


def headline_for(needle: str, rows: list, files: int = 1) -> str:
    if not files:
        return ("Nothing to scan: no source, config, or documentation files found under this "
                "path. This is not evidence about '" + needle + "' either way.")
    if not rows:
        return ("'" + needle + "' appears nowhere under this path. If a report cites it, the number "
                "is produced somewhere this scan cannot see — find that before trusting it.")

    kinds = {}
    for row in rows:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    files = sorted({r["file"] for r in rows})
    definitions = [r for r in rows if r["kind"] == "definition"]
    mix = ", ".join(str(n) + " " + kind for kind, n in sorted(kinds.items(), key=lambda pair: -pair[1]))

    if len(definitions) > 1:
        where = ", ".join(sorted({r["file"] + ":" + str(r["line"]) for r in definitions})[:4])
        return ("'" + needle + "' is defined independently in " + str(len(definitions)) + " place(s) ("
                + where + "): changing one will not move the others, and they may already disagree.")

    if not definitions and kinds.get("comparison") and kinds.get("config"):
        configs = sorted({r["file"] for r in rows if r["kind"] == "config"})
        return ("'" + needle + "' is configured in " + ", ".join(configs[:3]) + " but written as a "
                "literal in " + str(kinds["comparison"]) + " comparison(s) — confirm the code reads the "
                "config rather than repeating the number.")

    if not definitions and kinds.get("comparison"):
        return ("'" + needle + "' is compared against in " + str(kinds["comparison"]) + " place(s) and "
                "defined nowhere — a literal repeated at the point of use has no single meaning to change.")

    # Same file counts too: `QUALITY_THRESHOLD = 0.7` with `if score > 0.7` three
    # lines below is still a literal that the definition cannot move.
    elsewhere = [r for r in rows if r["kind"] == "comparison"] if definitions else []
    if elsewhere:
        return ("'" + needle + "' is defined once (" + definitions[0]["file"] + ":" + str(definitions[0]["line"])
                + ") but written as a literal in " + str(len(elsewhere)) + " comparison(s) ("
                + ", ".join(sorted({r["file"] for r in elsewhere})[:3])
                + ") — changing the definition will not move them.")

    roots = sorted({f.split("/")[0] for f in files if "/" in f})
    if len(roots) > 1:
        return ("'" + needle + "' spans " + str(len(roots)) + " top-level directories (" + ", ".join(roots[:4])
                + ") across " + str(len(rows)) + " site(s) — confirm it means the same thing in each; "
                "one name spanning two measurement systems is its own finding.")

    return ("'" + needle + "': " + str(len(rows)) + " site(s) in " + str(len(files)) + " file(s) (" + mix
            + "). Confirm every consumer means the same thing by it.")


CAVEAT = (
    "A textual match. It cannot see a value assembled at run time, read from an environment variable, "
    "stored in a database, or scaled on the way in (0.7 written as 70 elsewhere is invisible). "
    "Classification is heuristic — a multi-line expression or an unusual layout can land in 'other'. "
    "A site is one (line, role) pair, so a line using the value twice the same way counts once. "
    "A numeric needle deliberately does not match inside a longer number, so search 0.75 separately. "
    "A name annotated with no value, or a null default (`solve_rate: float | None`), is a declaration, "
    "not a definition; a declaration that the code then assigns elsewhere is the definition's job to find."
)


def main() -> int:
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("needle", help="the threshold, literal, or metric name to trace")
    add_common_args(parser)
    parser.add_argument("--regex", action="store_true", help="treat the needle as a regular expression")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        print("error: no such path: " + str(root), file=sys.stderr)
        return 2

    try:
        pattern = build_pattern(args.needle, args.regex)
    except re.error as exc:
        print("error: bad regular expression: " + str(exc), file=sys.stderr)
        return 2

    rows, skipped, scanned, generated, truncated = scan(root, pattern, bool(_NUMERIC_RE.match(args.needle)) and not args.regex)
    order = ["definition", "declaration", "comparison", "config", "doc", "test", "comment", "text", "other"]
    rows.sort(key=lambda r: (order.index(r["kind"]) if r["kind"] in order else 99, r["file"], r["line"]))
    counts = {"needle": args.needle, "sites": len(rows), "files": len({r["file"] for r in rows}),
              "files_scanned": scanned, "files_skipped_unread": len(skipped),
              "files_skipped_generated": len(generated), "lines_truncated": truncated}
    for kind in order:
        found = sum(1 for r in rows if r["kind"] == kind)
        if found:
            counts[kind] = found
    headline = headline_for(args.needle, rows, scanned) + skipped_note(skipped)
    caveat = CAVEAT + coverage_note(generated, truncated)
    emit(envelope("trace_value", root, headline, caveat, counts, rows[:args.limit]), args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
