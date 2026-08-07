#!/usr/bin/env python3
"""Count the real examples behind each dataset, and how many carry a label.

"n=30" and "3 of 30 labeled" are the two numbers a measurement report is most
often wrong about, because both are recalled rather than derived. This derives
them: for every JSON/JSONL/CSV dataset under a path, how many records it holds
and how many of those records populate each field.

The number that matters is the second one. A reference metric — accuracy,
recall, correctness, calibration — needs ground truth, so its real n is the count
of rows that *have* ground truth, not the size of the file. Eight metrics defined
against a 400-row eval set of which three rows are labeled is an ordinary finding,
and it is invisible until someone counts.

Presence is not correctness: a populated field may hold a placeholder. This
produces candidates, never findings.

Usage:
  python count_examples.py .
  python count_examples.py data/ --format json
  python count_examples.py data/eval.jsonl --max-rows 5000
"""

import csv
import sys
import json
import argparse
from pathlib import Path

from common import (
    EXCLUDE_DIRS, add_common_args, candidate, configure_output, emit, envelope, rel,
)

DATA_SUFFIXES = frozenset({".jsonl", ".ndjson", ".json", ".csv", ".tsv"})

# Field names that carry ground truth. Deliberately excludes bare `answer` and
# `output`: in an eval file those hold what the *system* produced, and counting
# them as labels reports a fully-labeled set that has no labels at all. A
# project-specific name is counted as an ordinary field, which is why the
# per-field table is printed too.
LABEL_FIELDS = frozenset({
    "expected", "expected_answer", "expected_output", "expected_result", "ground_truth",
    "groundtruth", "gold", "gold_answer", "gold_passage", "gold_label", "label", "labels",
    "correct_answer", "correct", "target", "targets", "reference",
    "references", "y_true", "ytrue", "truth", "human_score", "human_label",
    "human_rating", "annotation", "annotations", "rating", "relevance", "relevant",
    "is_correct", "verdict",
})
# Where a JSON object hides its records.
RECORD_KEYS = ("data", "examples", "rows", "records", "cases", "items", "tests",
               "samples", "results", "questions", "evals", "dataset")
# JSON files that are configuration, not data, however they parse.
CONFIG_NAMES = frozenset({
    "package.json", "package-lock.json", "tsconfig.json", "composer.json",
    "composer.lock", "renovate.json", "manifest.json", "angular.json", "nx.json",
})

CONFIRM = {
    "partial_labels": "open the file and confirm the sparse field is the ground truth the metric reads",
    "no_label_field": "check whether ground truth lives elsewhere (a sidecar file, a DB) before concluding",
    "small_n": "compare this n against the ship rule; a rule finer than the instrument is the finding",
    "unparseable_rows": "read the failing rows; malformed rows dropped before scoring inflate every metric",
    "unparseable_dataset": "read the file; a corrupted eval set is not the same as an absent one, and a "
                           "pipeline that skips it silently reports metrics over whatever survived",
    "empty_dataset": "check what writes this file; every metric reading it has n=0, which is not 0.0",
    "dataset": "confirm this file is what the metric actually reads at run time",
}


def is_populated(value) -> bool:
    """Present and non-empty. `0` and `False` are real values; "" and [] are not."""
    if value is None:
        return False
    if isinstance(value, (str, list, dict, tuple)):
        return len(value) > 0
    return True


def tally(records, counts: dict) -> None:
    for record in records:
        if not isinstance(record, dict):
            continue
        for key, value in record.items():
            if is_populated(value):
                counts[key] = counts.get(key, 0) + 1


def read_jsonl(path: Path, max_rows: int) -> dict:
    rows, bad, fields = 0, 0, {}
    truncated = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                if rows >= max_rows:
                    truncated = True
                    break
                rows += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                tally([record], fields)
    except OSError as exc:
        # Same shape as the other readers: an unreadable eval set is reported, not
        # dropped into the "no datasets found" bucket.
        return {"parse_error": type(exc).__name__}
    return {"rows": rows, "bad_rows": bad, "fields": fields, "truncated": truncated}


def records_from_json(payload):
    """The record list inside a parsed .json file, or None if it holds no dataset.

    An *empty* list is a dataset with zero rows, not a non-dataset: `[]` in
    eval.json says the measurement input exists and has n=0, which is a different
    fact from "there is no eval set" and a much more actionable one.

    The whole list decides, not its first element. Testing `payload[:1]` made the
    answer depend on record order — `["bad", {...}]` vanished while
    `[{...}, "bad"]` was accepted — and non-object entries are counted as
    malformed rows rather than silently inflating or deleting N.
    """
    if isinstance(payload, list):
        return payload if not payload or any(isinstance(r, dict) for r in payload) else None
    if isinstance(payload, dict):
        for key in RECORD_KEYS:
            value = payload.get(key)
            if isinstance(value, list) and (not value or any(isinstance(r, dict) for r in value)):
                return value
    return None


def read_json(path: Path, max_rows: int) -> dict:
    """Parsed records, {} if the file is simply not a dataset, or a parse error.

    The three outcomes are kept distinct on purpose. Collapsing "this JSON is
    corrupt" into "this is not a dataset" is what makes a broken eval set look
    exactly like an absent one.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return {"parse_error": "invalid JSON at line " + str(exc.lineno)}
    except (OSError, ValueError) as exc:
        return {"parse_error": type(exc).__name__}
    records = records_from_json(payload)
    if records is None:
        return {}
    truncated = len(records) > max_rows
    fields: dict = {}
    kept = records[:max_rows]
    tally(kept, fields)
    bad = sum(1 for record in kept if not isinstance(record, dict))
    return {"rows": len(kept), "bad_rows": bad, "fields": fields, "truncated": truncated}


def read_delimited(path: Path, max_rows: int) -> dict:
    delimiter = "\t" if path.suffix == ".tsv" else ","
    rows, fields = 0, {}
    truncated = False
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            for record in reader:
                if rows >= max_rows:
                    truncated = True
                    break
                rows += 1
                for key, value in record.items():
                    if key and isinstance(value, str) and value.strip():
                        fields[key] = fields.get(key, 0) + 1
    except (OSError, csv.Error, UnicodeError) as exc:
        return {"parse_error": type(exc).__name__}
    return {"rows": rows, "bad_rows": 0, "fields": fields, "truncated": truncated}


def read_dataset(path: Path, max_rows: int) -> dict:
    if path.name in CONFIG_NAMES:
        return {}
    if path.suffix in (".jsonl", ".ndjson"):
        return read_jsonl(path, max_rows)
    if path.suffix == ".json":
        return read_json(path, max_rows)
    return read_delimited(path, max_rows)


def data_files(root: Path) -> list:
    if root.is_file():
        return [root] if root.suffix in DATA_SUFFIXES else []
    found = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix in DATA_SUFFIXES
        and EXCLUDE_DIRS.isdisjoint(p.relative_to(root).parts)
    ]
    return sorted(found)


def label_coverage(fields: dict) -> list:
    """[(field, populated_count)] for every recognized ground-truth field, richest first.

    Every field, not the fullest one: a file with `expected` on 100/100 and
    `human_rating` on 5/100 has two different n's, and a metric reading the second
    has n=5. Reporting only the maximum is how the sparse field disappears.
    """
    hits = [(name, n) for name, n in fields.items() if name.strip().lower() in LABEL_FIELDS]
    return sorted(hits, key=lambda pair: -pair[1])


def describe(fields: dict, rows: int, top: int = 6) -> str:
    ranked = sorted(fields.items(), key=lambda pair: (-pair[1], pair[0]))[:top]
    return ", ".join(name + " " + str(n) + "/" + str(rows) for name, n in ranked)


def analyze(root: Path, max_rows: int) -> tuple:
    """(candidates, per-dataset summaries, unparseable files)."""
    rows_out, summaries, unparseable = [], [], []
    for path in data_files(root):
        stats = read_dataset(path, max_rows)
        display = rel(path, root)
        if stats.get("parse_error"):
            unparseable.append(display)
            rows_out.append(candidate(
                "unparseable_dataset", display, 1,
                "looks like a dataset but does not parse (" + stats["parse_error"] + ") — it supplies "
                "no examples to anything, and nothing here says so",
                CONFIRM["unparseable_dataset"],
            ))
            continue
        if stats.get("rows") == 0 and "fields" in stats:
            summaries.append({"file": display, "rows": 0, "labeled": 0, "labels": [],
                              "label_fields": [], "truncated": False})
            rows_out.append(candidate(
                "empty_dataset", display, 1,
                "parses as a dataset and holds zero records — every metric reading it has n=0, "
                "which must be reported as 'not measured' rather than as a score",
                CONFIRM["empty_dataset"],
            ))
            continue
        if not stats or not stats.get("rows"):
            continue
        rows = stats["rows"]
        labels = label_coverage(stats["fields"])
        best = labels[0][1] if labels else 0
        summaries.append({"file": display, "rows": rows, "labeled": best, "labels": labels,
                          "label_fields": [name for name, _ in labels], "truncated": stats["truncated"]})

        detail = str(rows) + " record(s)"
        if stats["truncated"]:
            detail += " (read capped at --max-rows)"
        detail += "; fields: " + (describe(stats["fields"], rows) or "none")
        rows_out.append(candidate("dataset", display, 1, detail, CONFIRM["dataset"]))

        for name, count in labels:
            if count >= rows:
                continue
            pct = round(100.0 * count / rows, 1)
            rows_out.append(candidate(
                "partial_labels", display, 1,
                name + " populated on " + str(count) + " of " + str(rows) + " record(s) (" + str(pct)
                + "%) — a reference metric reading this field has n=" + str(count) + ", not " + str(rows),
                CONFIRM["partial_labels"],
            ))
        if not labels:
            rows_out.append(candidate(
                "no_label_field", display, 1,
                str(rows) + " record(s), no recognized ground-truth field — accuracy, recall, and "
                "correctness cannot be computed from this file as it stands",
                CONFIRM["no_label_field"],
            ))

        if 0 < rows < 30:
            rows_out.append(candidate(
                "small_n", display, 1,
                "n=" + str(rows) + " — small enough that most claimed effects will not resolve",
                CONFIRM["small_n"],
            ))
        if stats.get("bad_rows"):
            rows_out.append(candidate(
                "unparseable_rows", display, 1,
                str(stats["bad_rows"]) + " of " + str(rows) + " record(s) did not parse",
                CONFIRM["unparseable_rows"],
            ))
    return rows_out, summaries, unparseable


def headline_for(summaries: list, unparseable: list) -> str:
    if not summaries:
        if unparseable:
            return (str(len(unparseable)) + " file(s) look like datasets but do not parse ("
                    + ", ".join(unparseable[:3]) + ") and no dataset parsed at all — corrupted "
                    "measurement input is not the same as absent measurement input.")
        return ("No JSON/JSONL/CSV datasets found under this path. If metrics are reported anyway, "
                "find what they read — measurement with no stored examples cannot be re-derived.")

    empty = [s for s in summaries if s["rows"] == 0]
    if empty and len(empty) == len(summaries):
        return (str(len(empty)) + " dataset(s) parse but hold zero records (" + empty[0]["file"]
                + ") — the measurement input exists and is empty, which is not the same as absent "
                "and not the same as a score of 0.")

    sparse = [(s, name, n) for s in summaries for name, n in s["labels"] if n < s["rows"]]
    if sparse:
        worst_set, worst_field, worst_n = min(sparse, key=lambda t: t[2] / max(t[0]["rows"], 1))
        return (worst_set["file"] + ": " + str(worst_set["rows"]) + " record(s), " + worst_field
                + " populated on " + str(worst_n)
                + " — every reference metric reading that field has it as its n.")

    unlabeled = [s for s in summaries if not s["label_fields"]]
    if len(unlabeled) == len(summaries):
        total = sum(s["rows"] for s in summaries)
        return (str(len(summaries)) + " dataset(s), " + str(total) + " record(s), no recognized "
                "ground-truth field in any of them: reference metrics have no inputs here.")

    labeled = max(summaries, key=lambda s: s["labeled"])
    return (str(len(summaries)) + " dataset(s); the largest fully-labeled set is " + labeled["file"]
            + " at n=" + str(labeled["labeled"]) + ". Check that n against the ship rule before "
            "trusting any comparison.")


CAVEAT = (
    "Ground-truth fields are recognized by a fixed vocabulary, so a label stored under a "
    "project-specific name is counted as an ordinary field — read the per-field table rather than "
    "trusting the label count alone. Presence is not correctness: a populated field may hold a "
    "placeholder, an empty-ish sentinel, or a stale answer. Only top-level fields are counted, and "
    "data held in a database, a warehouse, or a service is invisible here."
)


def main() -> int:
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(parser, root_help="dataset file, or directory to scan (default: .)")
    parser.add_argument("--max-rows", type=int, default=200_000,
                        help="stop reading a file after this many records (default: 200000)")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        print("error: no such path: " + str(root), file=sys.stderr)
        return 2

    rows, summaries, unparseable = analyze(root, args.max_rows)
    order = ["unparseable_dataset", "empty_dataset", "partial_labels", "no_label_field",
             "small_n", "unparseable_rows", "dataset"]
    rows.sort(key=lambda r: (order.index(r["kind"]) if r["kind"] in order else 99, r["file"]))
    counts = {
        "datasets": len(summaries),
        "datasets_unparseable": len(unparseable),
        "records_total": sum(s["rows"] for s in summaries),
        "records_labeled_max": max([s["labeled"] for s in summaries], default=0),
        "candidates_total": len(rows),
    }
    emit(envelope("count_examples", root, headline_for(summaries, unparseable), CAVEAT, counts,
                  rows[:args.limit]), args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
