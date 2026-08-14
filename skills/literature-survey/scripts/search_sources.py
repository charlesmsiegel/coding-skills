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
    failed: dict[str, str] = {}
    searched = 0
    for name, (build_url, parse) in PARSERS.items():
        ok = False
        for query in queries:
            try:
                response = http.get(build_url(query, limit=limit))
                found.extend(parse(response.body))
                ok = True
            except Exception as exc:  # noqa: BLE001 - any source failure is a caveat
                failed[name] = str(exc)
        if ok:
            searched += 1

    if searched == 0:
        raise SystemExit(
            "every source failed, so this is a connectivity problem rather than an empty "
            "literature: " + json.dumps(failed, indent=2)
        )

    merged = dedupe(_load_existing(out / "candidates.json") + found)
    (out / "candidates.json").write_text(
        json.dumps({
            "topic": topic,
            "subtopics": list(subtopics),
            "sources_searched": sorted(set(PARSERS) - set(failed)),
            "sources_failed": sorted(failed),
            "candidates": [c.to_dict() for c in merged],
        }, indent=2),
        encoding="utf-8",
    )
    return {
        "searched": searched,
        "candidates": len(merged),
        "caveats": [name + " failed: " + reason for name, reason in sorted(failed.items())],
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
        + str(result["searched"]) + " of " + str(len(PARSERS)) + " sources"
    )
    for caveat in result["caveats"]:
        reporter.caveat(caveat)
    reporter.caveat("Every candidate is status=new. Triage must set selected or dropped, "
                    "and a dropped candidate must carry its reason.")
    reporter.emit(args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
