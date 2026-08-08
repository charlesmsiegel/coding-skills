#!/usr/bin/env python3
"""
Run every code-doctor detector and merge the output into one report.

Detectors run as subprocesses rather than imports, so one that crashes
degrades the report by exactly one category instead of taking the run down.
Which categories ran, and which did not, is part of the output — a report that
silently lost a detector reads as a clean repository.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from common import configure_output, emit, Finding

SCRIPTS_DIR = Path(__file__).resolve().parent

# category -> script. Later plans append their detectors here.
DETECTORS = {
    "hygiene": "find_hygiene_issues.py",
    "secrets": "find_secrets.py",
}


def run_detector(script: str, path: Path, ignore: str) -> tuple[list[dict], dict, str | None]:
    """Returns (records, completeness, error). A failure is reported, never swallowed.

    The detector's own completeness record comes back with its findings.
    Dropping it would delete exactly the warnings that make a degraded run
    legible — hygiene's merge_state note, and every history, resolution, and
    test-classification warning the later plans add — while the aggregate went
    on reporting the category as run.
    """
    command = [sys.executable, str(SCRIPTS_DIR / script), str(path), "--format", "json"]
    if ignore:
        command += ["--ignore", ignore]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [], {}, f"{script}: did not complete ({exc})"
    if result.returncode != 0:
        return [], {}, f"{script}: exited {result.returncode} ({result.stderr.strip()[-200:]})"
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return [], {}, f"{script}: emitted invalid JSON ({exc})"
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)], {}, None
    if not isinstance(payload, dict):
        return [], {}, f"{script}: emitted {type(payload).__name__}, expected a list or object"
    # Validate the nested field TYPES too. {"findings": 42} would raise
    # TypeError in the comprehension, and a non-mapping completeness would
    # crash on .items() in main — both outside the per-category isolation
    # this function exists to provide, taking every other detector's valid
    # results down with them.
    records = payload.get("findings") or []
    if not isinstance(records, list):
        return [], {}, f"{script}: 'findings' is {type(records).__name__}, expected a list"
    notes = payload.get("completeness") or {}
    if not isinstance(notes, dict):
        return [], {}, f"{script}: 'completeness' is {type(notes).__name__}, expected an object"
    return [r for r in records if isinstance(r, dict)], notes, None


def main() -> int:
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=".")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", default="", help="Comma-separated finding types to suppress")
    parser.add_argument("--skip", default="", help="Comma-separated categories to drop")
    parser.add_argument("--only", default="", help="Comma-separated categories to run alone")
    args = parser.parse_args()

    skip = {c.strip() for c in args.skip.split(",") if c.strip()}
    only = {c.strip() for c in args.only.split(",") if c.strip()}

    unknown = (skip | only) - set(DETECTORS)
    if unknown:
        print(f"error: unknown categor(ies) {sorted(unknown)}; "
              f"known: {sorted(DETECTORS)}", file=sys.stderr)
        return 2

    selected = [c for c in DETECTORS if c not in skip and (not only or c in only)]

    findings: list[Finding] = []
    failures: list[str] = []
    merged_notes: dict[str, str] = {}
    rejected: dict[str, list[str]] = {}
    for category in selected:
        found, notes, error = run_detector(DETECTORS[category], Path(args.path), args.ignore)
        if error:
            failures.append(error)
            continue
        # Reconstruct here, while the category is still known. Finding's
        # __post_init__ re-validates, so one malformed record from a buggy
        # detector fails that category instead of aborting the whole report
        # and discarding every other category's valid results.
        for record in found:
            try:
                findings.append(Finding(**record))
            except (TypeError, ValueError) as exc:
                rejected.setdefault(category, []).append(str(exc))
        for label, note in notes.items():
            merged_notes[f"{category}.{label}"] = note

    completeness = {
        "categories_run": ", ".join(selected) or "none",
        "categories_skipped": ", ".join(sorted(set(DETECTORS) - set(selected))) or "none",
    }
    completeness.update(merged_notes)
    if failures:
        completeness["detectors_failed"] = "; ".join(failures)
    for category, errors in rejected.items():
        completeness[f"{category}.records_rejected"] = (
            f"{len(errors)} record(s) did not satisfy the findings schema and were "
            f"dropped: {errors[0]}"
        )

    emit(findings, args.format, "No problems found", completeness=completeness)
    return 0


if __name__ == "__main__":
    sys.exit(main())
