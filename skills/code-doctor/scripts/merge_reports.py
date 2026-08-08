#!/usr/bin/env python3
"""Union several doctors' reports into one envelope, attributing every record.

Two properties define this script.

**It attributes.** Nothing inside a report says who wrote it, and the same
report gets handed to several consumers. An unattributed TypeScript report
reaching a Python package's grade credits Python coverage that nothing
measured — an A+ from another language's analysis. Every record leaves here
stamped with its doctor.

**It does not deduplicate.** Two security detectors legitimately flag the same
hardcoded key, and collapsing them changes a count that something downstream
divides by. Which severity survives, and whether one identity counts once or
twice, are grading decisions — they belong wherever the grade is computed, with
the metadata field that records how many were merged. A merge that quietly
halved a finding count would make every number after it unexplainable.

A report that could not be read is a named entry in `doctor_errors`, never zero
findings: a doctor that produced no output failed, and "failed" and "found
nothing" must not arrive at a grader as the same fact.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common import configure_output

MERGE_SCHEMA = "code-doctor-merge/1"


def _blank_report() -> dict:
    return {"records": [], "analyzers_run": None, "analyzers_skipped": [],
            "analyzer_errors": {}, "completeness": {}, "error": ""}


def _from_categories(data: dict, report: dict) -> None:
    """A specialist's analyze_all envelope: sections plus a meta block."""
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    listed = meta.get("analyzers_run")
    categories = data.get("categories")

    report["analyzers_run"] = ([str(name) for name in listed] if isinstance(listed, list)
                               else sorted(categories))
    report["analyzers_skipped"] = [str(name) for name in (meta.get("analyzers_skipped") or [])]
    report["analyzer_errors"] = {str(k): str(v)
                                 for k, v in (meta.get("analyzer_errors") or {}).items()}

    for name, payload in categories.items():
        issues = payload.get("issues", []) if isinstance(payload, dict) else []
        for issue in issues:
            if isinstance(issue, dict):
                issue.setdefault("category", name)
                report["records"].append(issue)


def read_report(path: Path) -> dict:
    """Parse one doctor's JSON into a common record shape.

    A non-empty ``error`` means nothing else in the returned dict is
    trustworthy, and the caller must treat the doctor as having failed rather
    than as having found nothing.
    """
    report = _blank_report()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        report["error"] = f"report could not be read ({exc})"
        return report

    if not text.strip():
        report["error"] = (
            "empty report — a doctor that produced no output failed; it did not find nothing. "
            "Re-run it, or declare what the surviving reports examined."
        )
        return report

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        report["error"] = f"report is not valid JSON ({exc})"
        return report

    if isinstance(data, list):
        # A bare list carries no evidence of what ran. analyze_django.py and a
        # single detector's --format json emit the same shape, so nothing here
        # distinguishes a full run from one detector that found nothing.
        report["records"] = [item for item in data if isinstance(item, dict)]
        return report

    if not isinstance(data, dict):
        report["error"] = f"unrecognised report shape: {type(data).__name__}"
        return report

    if isinstance(data.get("categories"), dict):
        _from_categories(data, report)
        return report

    if isinstance(data.get("findings"), list):
        report["records"] = [item for item in data["findings"] if isinstance(item, dict)]
        completeness = data.get("completeness")
        report["completeness"] = completeness if isinstance(completeness, dict) else {}
        # code-doctor's own detectors inventory what ran under
        # `categories_run`. An absent key is unknown coverage, NOT an empty
        # run: defaulting to [] here would tell a grader that nothing ran,
        # which reads as "every category is unmeasured" for a scan that
        # actually completed. Leaving it None routes the doctor to
        # coverage_unknown, which is the honest answer.
        ran = report["completeness"].get("categories_run")
        report["analyzers_run"] = sorted(str(name) for name in ran) if isinstance(ran, list) else None
        # The other two halves of the same inventory. Without them a category
        # analyze_all deselected or whose detector crashed reached a grader as
        # merely *absent* from `categories_run` — indistinguishable from a
        # category this doctor has never had a detector for, and impossible to
        # attribute back to the run that lost it.
        skipped = report["completeness"].get("categories_skipped")
        if isinstance(skipped, list):
            report["analyzers_skipped"] = sorted(str(name) for name in skipped)
        failed = report["completeness"].get("categories_failed")
        if isinstance(failed, dict):
            report["analyzer_errors"] = {str(k): str(v) for k, v in failed.items()}
        return report

    if isinstance(data.get("issues"), list):
        report["records"] = [item for item in data["issues"] if isinstance(item, dict)]
        return report

    report["error"] = "unrecognised report shape: no findings, categories, or issues"
    return report


def merge(reports: list[tuple[str, Path]]) -> dict:
    envelope = {
        "schema": MERGE_SCHEMA,
        "doctors_run": [],
        "analyzers_run": {},
        "analyzers_skipped": {},
        "analyzer_errors": {},
        "doctor_errors": {},
        "completeness": {},
        "coverage_unknown": [],
        "findings": [],
        "candidates": [],
    }

    for doctor, path in reports:
        parsed = read_report(path)
        if parsed["error"]:
            envelope["doctor_errors"][doctor] = parsed["error"]
            continue

        envelope["doctors_run"].append(doctor)
        if parsed["analyzers_run"] is None:
            envelope["coverage_unknown"].append(doctor)
            envelope["analyzers_run"][doctor] = []
        else:
            envelope["analyzers_run"][doctor] = parsed["analyzers_run"]
        envelope["analyzers_skipped"][doctor] = parsed["analyzers_skipped"]
        if parsed["analyzer_errors"]:
            envelope["analyzer_errors"][doctor] = parsed["analyzer_errors"]
        if parsed["completeness"]:
            envelope["completeness"][doctor] = parsed["completeness"]

        for record in parsed["records"]:
            attributed = {**record, "doctor": doctor}
            bucket = "candidates" if record.get("kind") == "candidate" else "findings"
            envelope[bucket].append(attributed)

    return envelope


def _parse_report_argument(value: str) -> tuple[str, Path]:
    doctor, separator, path = value.partition(":")
    # A Windows drive letter makes the first colon ambiguous, so require a
    # non-empty label AND a non-empty remainder rather than splitting blindly.
    if not separator or not doctor.strip() or not path.strip() or len(doctor.strip()) == 1:
        raise argparse.ArgumentTypeError(
            f"--report takes doctor:path (got {value!r}); the label is how a finding "
            "keeps the name of the doctor that produced it"
        )
    return doctor.strip(), Path(path.strip())


def _print_text(envelope: dict) -> None:
    for doctor in envelope["doctors_run"]:
        analyzers = envelope["analyzers_run"].get(doctor, [])
        coverage = (f"{len(analyzers)} analyzer(s)" if analyzers
                    else "no coverage evidence — a bare list says nothing about what ran")
        print(f"✅ {doctor}: {coverage}")
    for doctor, message in envelope["doctor_errors"].items():
        print(f"⚠️  {doctor}: failed — {message}")
    print()
    print(f"{len(envelope['findings'])} finding(s), {len(envelope['candidates'])} candidate(s) "
          f"from {len(envelope['doctors_run'])} doctor(s). Not deduplicated — the same defect "
          "reported by two doctors appears twice, and collapsing it is the grader's decision.")


def main(argv: list[str] | None = None) -> int:
    configure_output()
    parser = argparse.ArgumentParser(
        description="Union several doctors' reports into one attributed envelope."
    )
    parser.add_argument("--report", action="append", required=True, type=_parse_report_argument,
                        metavar="DOCTOR:PATH", help="A doctor's JSON report, labelled")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--out", type=Path, default=None,
                        help="Write the JSON envelope here as well as reporting")
    args = parser.parse_args(argv)

    envelope = merge(args.report)
    serialized = json.dumps(envelope, indent=2)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized, encoding="utf-8")

    if args.format == "json":
        print(serialized)
    else:
        _print_text(envelope)

    # Every report failing is not a clean repository; it is a merge with no
    # input. Exiting zero here would let a caller write an A+ health page from
    # nothing at all.
    return 1 if not envelope["doctors_run"] else 0


if __name__ == "__main__":
    sys.exit(main())
