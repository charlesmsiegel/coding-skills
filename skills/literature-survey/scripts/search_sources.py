#!/usr/bin/env python3
"""Query every scholarly source for a topic and record everything seen.

Usage:
    python3 search_sources.py --topic "shared mental models" \
        [--subtopic "transactive memory" ...] --out ./research/team-knowledge \
        [--limit 50] [--format text|json]

Writes <out>/candidates.json. Two properties matter more than the search itself:

1. A source that fails is a caveat, not a crash — three indexes are still a
   corpus. But *every* source failing raises, because an empty corpus caused by
   an unreachable network must never render as "the literature is empty".
2. A re-run merges rather than overwrites. Triage decisions already recorded
   survive, or the wide-triage stage would be redone on every search.
"""

import argparse
import json
import sys
from pathlib import Path

from common import Candidate, Http, Reporter
from sources import PARSERS, dedupe


def _load_existing(path: Path) -> list[Candidate]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [Candidate.from_dict(c) for c in payload.get("candidates", [])]


def run(topic: str, subtopics: list[str], out: Path, http, limit: int = 50) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    queries = [topic] + list(subtopics)

    found: list[Candidate] = []
    # Per source: how many of its queries failed, and why. Counting rather than
    # keeping one flag matters — a source that answered the topic query and timed
    # out on two subtopics used to be counted as searched *and* listed as failed,
    # so the headline said "4 of 4 sources" over a file that said arXiv was never
    # searched. Half a source's queries silently not running is exactly the kind of
    # partial coverage this skill refuses to let read as complete.
    failures: dict[str, list[str]] = {}
    succeeded: dict[str, int] = {}
    for name, (build_url, parse) in PARSERS.items():
        for query in queries:
            try:
                response = http.get(build_url(query, limit=limit))
                found.extend(parse(response.body))
                succeeded[name] = succeeded.get(name, 0) + 1
            except Exception as exc:  # noqa: BLE001 - any source failure is a caveat
                failures.setdefault(name, []).append(query + ": " + str(exc))

    searched = sorted(succeeded)
    dead = sorted(set(PARSERS) - set(succeeded))
    partial = sorted(name for name in failures if name in succeeded)

    if not searched:
        raise SystemExit(
            "every source failed, so this is a connectivity problem rather than an empty "
            "literature: " + json.dumps({k: v for k, v in sorted(failures.items())}, indent=2)
        )

    merged = dedupe(_load_existing(out / "candidates.json") + found)
    (out / "candidates.json").write_text(
        json.dumps({
            "topic": topic,
            "subtopics": list(subtopics),
            "queries_per_source": len(queries),
            "sources_searched": searched,
            "sources_failed": dead,
            "sources_partial": {name: len(failures[name]) for name in partial},
            "query_failures": {name: rows for name, rows in sorted(failures.items())},
            "candidates": [c.to_dict() for c in merged],
        }, indent=2),
        encoding="utf-8",
    )
    caveats = [name + " failed on every query: " + failures[name][0] for name in dead]
    caveats += [
        name + " answered " + str(succeeded[name]) + " of " + str(len(queries))
        + " queries; the rest were not run, so its coverage of this topic is partial: "
        + failures[name][0]
        for name in partial
    ]
    return {
        "searched": len(searched),
        "complete": len(searched) - len(partial),
        "candidates": len(merged),
        "caveats": caveats,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--subtopic", action="append", default=[], dest="subtopics")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    result = run(args.topic, args.subtopics, args.out, Http(), limit=args.limit)

    reporter = Reporter("search_sources")
    reporter.headline(
        str(result["candidates"]) + " distinct candidates from "
        + str(result["searched"]) + " of " + str(len(PARSERS)) + " sources ("
        + str(result["complete"]) + " answered every query)"
    )
    for caveat in result["caveats"]:
        reporter.caveat(caveat)
    reporter.caveat("Every candidate is status=new. Triage must set selected or dropped, "
                    "and a dropped candidate must carry its reason.")
    reporter.emit(args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
