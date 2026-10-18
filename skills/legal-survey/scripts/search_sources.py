#!/usr/bin/env python3
"""Query every legal source for a question and record everything seen.

Usage:
    python3 search_sources.py --question "FOIA exemption 5 deliberative process" \
        --forum "D.C. Circuit" [--issue "attorney work product" ...] \
        --out ./research/foia-exemption-5 [--as-of 2026-09-04] \
        [--source courtlistener --source ecfr] [--limit 50] [--format text|json]

Writes <out>/candidates.json. Four properties matter more than the search itself:

1. A source that fails is a caveat, not a crash -- four indexes are still a
   corpus. But *every* source failing raises, because an empty corpus caused by
   an unreachable network must never render as "there is no law on this".
2. A re-run merges rather than overwrites. Triage decisions already recorded
   survive, or the wide-triage stage would be redone on every search.
3. The forum and the as-of date are recorded in the file, not held in the
   agent's head. Every later stage needs them -- weight is relative to a forum
   and codified law is relative to a date -- and a run that cannot say which
   court it was researching produced an answer to no question.
4. An anonymous CourtListener run is reported as partial coverage. The API
   answers without a token but throttles hard and caps the result set, and a
   thin case corpus that looks deliberate is worse than one that says it is thin.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from common import Authority, Http, Reporter
from sources import DEFAULT_SOURCES, PARSERS, dedupe


def _load_existing(path: Path) -> list[Authority]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [Authority.from_dict(a) for a in payload.get("authorities", [])]


def run(question: str, issues: list[str], out: Path, http, forum: str = "",
        as_of: str = "", limit: int = 50, source_names=None) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    queries = [question] + list(issues)

    chosen = list(source_names or DEFAULT_SOURCES)
    unknown = [name for name in chosen if name not in PARSERS]
    if unknown:
        raise SystemExit("no such source(s): " + ", ".join(sorted(unknown))
                         + "; known sources are " + ", ".join(sorted(PARSERS)))

    found: list[Authority] = []
    # Per source: how many of its queries failed, and why. Counting rather than
    # keeping one flag matters -- a source that answered the main question and
    # timed out on two issues would otherwise be counted as searched *and* listed
    # as failed, so the headline says "5 of 5 sources" over a file that says
    # CourtListener was never reached. Half a source's queries silently not
    # running is exactly the partial coverage this skill refuses to let read as
    # complete.
    failures: dict[str, list[str]] = {}
    succeeded: dict[str, int] = {}
    for name in chosen:
        build_url, parse = PARSERS[name]
        for query in queries:
            try:
                version = {"as_of": as_of} if name == "ecfr" else {}
                response = http.get(build_url(query, limit=limit, **version))
                found.extend(parse(response.body, **version))
                succeeded[name] = succeeded.get(name, 0) + 1
            except Exception as exc:  # noqa: BLE001 - any source failure is a caveat
                failures.setdefault(name, []).append(query + ": " + str(exc))

    searched = sorted(succeeded)
    dead = sorted(set(chosen) - set(succeeded))
    partial = sorted(name for name in failures if name in succeeded)

    if not searched:
        raise SystemExit(
            "every source failed, so this is a connectivity problem rather than an absence of "
            "law: " + json.dumps({k: v for k, v in sorted(failures.items())}, indent=2)
        )

    merged = dedupe(_load_existing(out / "candidates.json") + found)
    (out / "candidates.json").write_text(
        json.dumps({
            "question": question,
            "issues": list(issues),
            "forum": forum,
            "as_of": as_of,
            "queries_per_source": len(queries),
            "sources_requested": chosen,
            "sources_searched": searched,
            "sources_failed": dead,
            "sources_partial": {name: len(failures[name]) for name in partial},
            "query_failures": {name: rows for name, rows in sorted(failures.items())},
            "authorities": [a.to_dict() for a in merged],
        }, indent=2),
        encoding="utf-8",
    )
    caveats = [name + " failed on every query: " + failures[name][0] for name in dead]
    caveats += [
        name + " answered " + str(succeeded[name]) + " of " + str(len(queries))
        + " queries; the rest were not run, so its coverage of this question is partial: "
        + failures[name][0]
        for name in partial
    ]
    return {
        "searched": len(searched),
        "requested": len(chosen),
        "complete": len(searched) - len(partial),
        "authorities": len(merged),
        "caveats": caveats,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--question", required=True,
                    help="the legal question, in the words a researcher would search")
    ap.add_argument("--issue", action="append", default=[], dest="issues",
                    help="a sub-issue to search separately; repeatable")
    ap.add_argument("--forum", default="",
                    help="the court whose law governs, e.g. 'D.C. Circuit' or 'N.D. Cal.'; "
                         "weight is relative to it and every later stage reads it back")
    ap.add_argument("--as-of", default="", dest="as_of",
                    help="the date the law should be read as of (YYYY-MM-DD); defaults to "
                         "current law, which is the wrong answer for past conduct")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--source", action="append", default=[], dest="sources",
                    help="restrict or extend the source set; repeatable. Known: "
                         + ", ".join(sorted(PARSERS)))
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    token = os.environ.get("COURTLISTENER_TOKEN", "")
    result = run(args.question, args.issues, args.out, Http(token=token),
                 forum=args.forum, as_of=args.as_of, limit=args.limit,
                 source_names=args.sources or None)

    reporter = Reporter("search_sources")
    reporter.headline(
        str(result["authorities"]) + " distinct authorities from "
        + str(result["searched"]) + " of " + str(result["requested"]) + " sources ("
        + str(result["complete"]) + " answered every query)"
    )
    for caveat in result["caveats"]:
        reporter.caveat(caveat)
    if not token:
        reporter.caveat(
            "no COURTLISTENER_TOKEN in the environment: CourtListener answered anonymously, "
            "which is rate-limited and returns a capped result set, so case-law coverage is "
            "partial. A free token materially widens it -- ask the user for one before "
            "triaging, and if they decline, say so in the report's method tab."
        )
    if not args.forum:
        reporter.caveat(
            "no --forum was given, so no authority can be weighed as binding or persuasive: "
            "weight is a claim about a particular court, and triage cannot set it without one"
        )
    if not args.as_of:
        reporter.caveat(
            "no --as-of was given, so statutes and regulations will be read as current law; "
            "if the question is about past conduct, that is the wrong text"
        )
    reporter.caveat("Every authority is status=new. Triage must set selected or dropped, "
                    "a dropped authority must carry its reason, and a selected one must "
                    "carry its weight in the forum.")
    reporter.emit(args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
