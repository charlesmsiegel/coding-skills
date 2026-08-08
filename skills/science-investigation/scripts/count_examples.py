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

import re
import csv
import sys
import json
import argparse
from pathlib import Path

from common import (
    EXCLUDE_DIRS, add_common_args, candidate, configure_output, emit, envelope, rel,
)

DATA_SUFFIXES = frozenset({".jsonl", ".ndjson", ".json", ".csv", ".tsv"})

# A .json array has to be decoded whole before any row can be counted, so
# --max-rows cannot bound the read the way it does for JSONL and CSV. This bounds
# it by size instead, and says so, rather than exhausting memory on a file the
# caller asked to sample.
MAX_JSON_BYTES = 200_000_000

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
# Names and directories that make a file plausibly measurement data. A malformed
# `settings.json` in a tree with no eval data is a broken config file, and
# reporting it as corrupted measurement input manufactures a finding.
DATASET_HINTS = ("eval", "test", "data", "dataset", "bench", "gold", "label", "annot",
                 "sample", "record", "case", "example", "qa", "question", "golden",
                 "fixture", "ground", "truth", "run", "result", "score")
# JSON files that are configuration, not data, however they parse.
CONFIG_NAMES = frozenset({
    "package.json", "package-lock.json", "tsconfig.json", "composer.json",
    "composer.lock", "renovate.json", "manifest.json", "angular.json", "nx.json",
})

CONFIRM = {
    "partial_labels": "open the file and confirm the sparse field is the ground truth the metric reads",
    "no_label_field": "check whether ground truth lives elsewhere (a sidecar file, a DB) before concluding",
    "small_n": "compute the interval over the per-item deltas before concluding anything; whether this n "
               "resolves the effect depends on their spread, not on n alone",
    "unparseable_rows": "read the failing rows; malformed rows dropped before scoring inflate every metric",
    "unparseable_dataset": "read the file; a corrupted eval set is not the same as an absent one, and a "
                           "pipeline that skips it silently reports metrics over whatever survived",
    "empty_dataset": "check what writes this file; every metric reading it has n=0, which is not 0.0",
    "dataset": "confirm this file is what the metric actually reads at run time",
}


_SEGMENT_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def looks_like_data(display: str) -> bool:
    """Whether an unparseable file is plausibly a dataset rather than a config.

    Tokenized rather than substring-matched: `metadata.json` contains "data" and
    is not a dataset, and calling a broken config file corrupted measurement input
    is the manufacturing error this gate exists to prevent. A token may extend a
    hint (`testdata`, `results`) but may not merely contain one.
    """
    # Segments, not prefixes. `runtime.json` starts with the hint `run` and is not a
    # run record; `casual` is not a case. A hint counts when it is a whole segment of
    # the name — separator-delimited or camelCase — or that segment's plural.
    segments = []
    for token in re.split(r"[^A-Za-z0-9]+", display):
        segments += [part.lower() for part in _SEGMENT_RE.findall(token)]
    return any(segment == hint or segment == hint + "s"
               for segment in segments for hint in DATASET_HINTS)


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
    """Count every record; parse only the first `max_rows` of them.

    Counting a line is cheap and parsing it is not, so the cap applies to the
    tally rather than to the count. Reporting the cap as the dataset's size would
    be a silent truncation inside the tool whose whole job is deriving N.
    """
    rows, read, bad, fields = 0, 0, 0, {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                rows += 1
                if read >= max_rows:
                    continue
                read += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                if not isinstance(record, dict):
                    bad += 1          # a bare scalar or list is not a record
                    continue
                tally([record], fields)
    except OSError as exc:
        # Same shape as the other readers: an unreadable eval set is reported, not
        # dropped into the "no datasets found" bucket.
        return {"parse_error": type(exc).__name__}
    return {"rows": rows, "read": read, "bad_rows": bad, "fields": fields, "truncated": read < rows}


def records_from_json(payload, data_like: bool = False):
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
        if not payload or any(isinstance(r, dict) for r in payload):
            return payload
        # No object at all: a dataset only if the name says so. `eval.json` holding
        # `[null, "broken"]` has two rows that cannot be scored and returning None
        # deleted both; `allow.json` holding `["a", "b"]` is an allowlist.
        return payload if data_like else None
    if isinstance(payload, dict):
        # A populated list wins over an earlier empty one: `{"data": [],
        # "examples": [...]}` held real records under the second key, and taking
        # the first match reported the file as an empty dataset.
        empty = None
        for key in RECORD_KEYS:
            value = payload.get(key)
            if not isinstance(value, list):
                continue
            if value:
                # Unambiguously dataset-shaped: `{"data": [null, "broken"]}` is two
                # rows that cannot be scored, not an absent dataset.
                return value
            if empty is None:
                empty = value
        if empty is not None:
            return empty
        # `eval.json` holding one record directly (`{"input": ..., "expected": ...}`)
        # is a dataset of one, not a config file. A recognized ground-truth field is
        # the evidence; without one this stays None and ordinary config is unaffected.
        if any(str(key).strip().lower() in LABEL_FIELDS for key in payload):
            return [payload]
    return None


def read_json(path: Path, max_rows: int, data_like: bool = False) -> dict:
    """Parsed records, {} if the file is simply not a dataset, or a parse error.

    The three outcomes are kept distinct on purpose. Collapsing "this JSON is
    corrupt" into "this is not a dataset" is what makes a broken eval set look
    exactly like an absent one.
    """
    try:
        size = path.stat().st_size
        if size > MAX_JSON_BYTES:
            return {"parse_error": "too large to decode whole (" + str(size) + " bytes); --max-rows "
                                   "cannot bound a single JSON array — convert to JSONL to count it"}
        payload = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except json.JSONDecodeError as exc:
        return {"parse_error": "invalid JSON at line " + str(exc.lineno)}
    except (OSError, ValueError) as exc:
        return {"parse_error": type(exc).__name__}
    wrapped = wrapper_key(payload)
    records = records_from_json(payload, data_like)
    if records is None:
        return {}
    fields: dict = {}
    kept = records[:max_rows]
    tally(kept, fields)
    bad = sum(1 for record in kept if not isinstance(record, dict))
    return {"rows": len(records), "read": len(kept), "bad_rows": bad, "fields": fields,
            "wrapped": wrapped, "truncated": len(kept) < len(records)}


def read_delimited(path: Path, max_rows: int) -> dict:
    delimiter = "\t" if path.suffix == ".tsv" else ","
    rows, read, bad, fields = 0, 0, 0, {}
    try:
        # utf-8-sig: an exported eval CSV often carries a BOM, which otherwise
        # survives into the first field name as `\ufeffexpected` and hides the label.
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            # strict=True so an unterminated quoted field raises instead of
            # swallowing the following lines into one record and reporting a
            # confident, wrong N with bad_rows=0.
            reader = csv.DictReader(handle, delimiter=delimiter, strict=True)
            header = list(reader.fieldnames or [])
            for record in reader:
                rows += 1
                if read >= max_rows:
                    continue
                read += 1
                if None in record:
                    bad += 1      # more fields than the header: a malformed row
                for key, value in record.items():
                    if key and isinstance(value, str) and value.strip():
                        fields[key] = fields.get(key, 0) + 1
    except (OSError, csv.Error, UnicodeError) as exc:
        return {"parse_error": type(exc).__name__}
    # The header is kept separately: a header-only `expected,input` populates no
    # field counts, and losing it dropped an empty *labeled* eval set as if it
    # were an unrelated spreadsheet.
    return {"rows": rows, "read": read, "bad_rows": bad, "fields": fields,
            "header": header, "truncated": read < rows}


def read_dataset(path: Path, max_rows: int, data_like: bool = False) -> dict:
    if path.name in CONFIG_NAMES:
        return {}
    if path.suffix in (".jsonl", ".ndjson"):
        return read_jsonl(path, max_rows)
    if path.suffix == ".json":
        return read_json(path, max_rows, data_like)
    return read_delimited(path, max_rows)


def looks_like_a_record(fields) -> bool:
    return any(str(name).strip().lower() in LABEL_FIELDS for name in fields)


def wrapper_key(payload) -> bool:
    """Whether a dict payload holds its records under a dataset-only wrapper key."""
    return isinstance(payload, dict) and any(
        isinstance(payload.get(key), list) for key in RECORD_KEYS
    )


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


def unreadable_row(display: str, reason: str) -> dict:
    return candidate(
        "unparseable_dataset", display, 1,
        "looks like a dataset but could not be read (" + reason + ") — it supplies "
        "no examples to anything, and nothing here says so",
        CONFIRM["unparseable_dataset"],
    )


def empty_row(display: str) -> dict:
    return candidate(
        "empty_dataset", display, 1,
        "parses as a dataset and holds zero records — every metric reading it has n=0, "
        "which must be reported as 'not measured' rather than as a score",
        CONFIRM["empty_dataset"],
    )


def blank_summary(display: str) -> dict:
    return {"file": display, "rows": 0, "read": 0, "labeled": 0, "labels": [],
            "bad_rows": 0, "label_fields": [], "truncated": False}


def is_measurement_data(path: Path, display: str, stats: dict) -> bool:
    """Whether a parsed file is plausibly measurement data at all.

    A parsed file is not automatically a dataset: `inventory.csv` with sku,price
    and `users.json` with [{"name": ...}] both produced "no recognized
    ground-truth field: reference metrics have no inputs here" — a measurement
    conclusion about an unrelated asset. Evidence is a dataset-like name, a
    recognized ground-truth field, or (for JSON) records under a wrapper key that
    only datasets use.
    """
    if stats.get("wrapped"):
        return True
    fields = list(stats.get("fields") or {}) + list(stats.get("header") or [])
    return looks_like_data(display) or looks_like_a_record(fields)


def analyze(root: Path, max_rows: int) -> tuple:
    """(candidates, per-dataset summaries, unparseable files)."""
    rows_out, summaries, unparseable = [], [], []
    for path in data_files(root):
        display = rel(path, root)
        stats = read_dataset(path, max_rows, looks_like_data(display))
        if stats.get("parse_error"):
            if not looks_like_data(display):
                continue  # a broken config file is not corrupted measurement input
            unparseable.append(display)
            rows_out.append(unreadable_row(display, stats["parse_error"]))
            continue
        if stats.get("rows") == 0 and "fields" in stats:
            if not is_measurement_data(path, display, stats):
                continue   # a header-only inventory.csv is not empty measurement input
            summaries.append(blank_summary(display))
            rows_out.append(empty_row(display))
            continue
        if not stats or not stats.get("rows"):
            continue
        if not is_measurement_data(path, display, stats):
            continue
        rows = stats["rows"]
        read = stats.get("read", rows)
        labels = label_coverage(stats["fields"])
        best = labels[0][1] if labels else 0
        summaries.append({"file": display, "rows": rows, "read": read, "labeled": best, "labels": labels,
                          "bad_rows": stats.get("bad_rows", 0),
                          "label_fields": [name for name, _ in labels], "truncated": stats["truncated"]})

        detail = str(rows) + " record(s)"
        if stats["truncated"]:
            detail += " (fields counted over the first " + str(read) + " only, --max-rows)"
        detail += "; fields: " + (describe(stats["fields"], rows) or "none")
        rows_out.append(candidate("dataset", display, 1, detail, CONFIRM["dataset"]))

        for name, count in labels:
            if count >= read:
                continue
            pct = round(100.0 * count / read, 1)
            over = str(read) + " record(s)" + (" read of " + str(rows) if stats["truncated"] else "")
            rows_out.append(candidate(
                "partial_labels", display, 1,
                name + " populated on " + str(count) + " of " + over + " (" + str(pct)
                + "%) — a reference metric reading this field has n=" + str(count) + ", not " + str(rows),
                CONFIRM["partial_labels"],
            ))
        if not labels:
            # Scoped to what was read: with the read capped, an unlabeled prefix
            # says nothing about the rows past the cap, and claiming the whole
            # file has no ground truth is a conclusion from rows nobody saw.
            scope = (str(read) + " of " + str(rows) + " record(s) read"
                     if stats["truncated"] else str(rows) + " record(s)")
            rows_out.append(candidate(
                "no_label_field", display, 1,
                scope + ", no recognized ground-truth field in them — accuracy, recall, and "
                "correctness cannot be computed from what was read",
                CONFIRM["no_label_field"],
            ))

        if 0 < rows < 30:
            rows_out.append(candidate(
                "small_n", display, 1,
                "n=" + str(rows) + " — small enough that resolvability is worth calculating rather than "
                "assuming; low-variance paired deltas can resolve a small effect here, a noisy binary "
                "metric cannot resolve a large one",
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
    if summaries and unparseable:
        return (str(len(unparseable)) + " file(s) that look like datasets could not be read ("
                + ", ".join(unparseable[:3]) + ") beside " + str(len(summaries))
                + " that could — settle what the unreadable ones were feeding before drawing "
                "any conclusion from the rest.")
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

    sparse = [(s, name, n) for s in summaries for name, n in s["labels"] if n < s["read"]]
    if sparse:
        worst_set, worst_field, worst_n = min(sparse, key=lambda t: t[2] / max(t[0]["rows"], 1))
        return (worst_set["file"] + ": " + str(worst_set["rows"]) + " record(s), " + worst_field
                + " populated on " + str(worst_n)
                + " — every reference metric reading that field has it as its n.")

    all_bad = [s for s in summaries if s.get("bad_rows") and s["bad_rows"] >= s["read"] > 0]
    if all_bad:
        worst = all_bad[0]
        return (worst["file"] + ": every one of the " + str(worst["read"]) + " record(s) read failed to "
                "parse — nothing here can be scored, which is not the same as scoring badly.")

    unlabeled = [s for s in summaries if not s["label_fields"]]
    if len(unlabeled) == len(summaries) and any(s["truncated"] for s in summaries):
        return ("no recognized ground-truth field in the records read, but the read was capped "
                "(--max-rows) — raise the cap before concluding the datasets are unlabeled.")
    if len(unlabeled) == len(summaries):
        total = sum(s["rows"] for s in summaries)
        return (str(len(summaries)) + " dataset(s), " + str(total) + " record(s), no recognized "
                "ground-truth field in any of them: reference metrics have no inputs here.")

    # A truncated read cannot support a full-coverage claim: the rows past the cap
    # were never looked at, and calling the prefix "fully labeled" is the silent
    # truncation this skill exists to catch, committed by the tool that counts.
    complete = [s for s in summaries if not s["truncated"]]
    if not complete:
        biggest = max(summaries, key=lambda s: s["read"])
        return (str(len(summaries)) + " dataset(s), all read only in part (" + biggest["file"] + ": "
                + str(biggest["read"]) + " of " + str(biggest["rows"]) + " records) — raise --max-rows "
                "before quoting any coverage figure from this run.")
    labeled = max(complete, key=lambda s: s["labeled"])
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

    if args.max_rows < 0:
        # JSON slicing reads all but the last record; JSONL reads none. Silently
        # format-dependent coverage is exactly the kind of number this tool exists
        # to stop anyone quoting.
        parser.error("--max-rows must be zero or greater")
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
        "records_read": sum(s["read"] for s in summaries),
        "records_labeled_max": max([s["labeled"] for s in summaries], default=0),
        "candidates_total": len(rows),
    }
    emit(envelope("count_examples", root, headline_for(summaries, unparseable), CAVEAT, counts,
                  rows[:args.limit]), args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
