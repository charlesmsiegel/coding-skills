#!/usr/bin/env python3
"""Follow the citation graph out from what has actually been read.

Usage:
    python3 snowball.py --out ./research/team-knowledge --round 1 [--cap 250]
        [--format text|json]

Reads the artifact ids in <out>/docs/notes/, asks OpenAlex for their references
and citers, subtracts everything already in candidates.json, appends what is
left, and writes a verdict to <out>/snowball.json.

OpenAlex rather than Semantic Scholar as the graph source: it needs no key and
its limits are generous enough for a real snowball round, which S2's are not.

The verdict is the point. "barren" means this round found nothing; two in a row
is "saturated". "cap_reached" is a different thing entirely and must never be
reported as saturation — one means the literature is exhausted, the other means
we stopped looking.
"""

import argparse
import json
import sys
from pathlib import Path

from common import Candidate, Http, Reporter, artifact_id
from sources import dedupe, parse_openalex

OPENALEX_WORK = "https://api.openalex.org/works"


def _read_ids(out: Path) -> list[str]:
    notes = out / "docs" / "notes"
    if not notes.is_dir():
        return []
    return sorted(p.stem for p in notes.glob("*.json"))


def _load_state(out: Path) -> dict:
    path = out / "snowball.json"
    if not path.is_file():
        return {"rounds": [], "barren_rounds": 0, "stopped_because": ""}
    return json.loads(path.read_text(encoding="utf-8"))


def _neighbour_urls(work_id: str) -> list[str]:
    """Both directions: what this work cites, and what cites it."""
    return [
        OPENALEX_WORK + "?filter=cited_by:" + work_id + "&per-page=100",
        OPENALEX_WORK + "?filter=cites:" + work_id + "&per-page=100",
    ]


def _resolve_work_id(artifact: str, candidate, http) -> str:
    """Turn a note's artifact id into something OpenAlex will filter on.

    A note is named by `artifact_id`, which prefers an arXiv id or a DOI — neither
    of which OpenAlex accepts in a `cites:` filter. Only a work id (W...) does. So
    an OpenAlex id is used directly, a DOI is resolved through the works endpoint,
    and anything else is unresolvable.

    Returning "" rather than guessing is deliberate: a wrong work id would return
    somebody else's citation graph, which is worse than a recorded gap.
    """
    if candidate is None:
        return ""
    ids = candidate.external_ids
    if ids.get("openalex"):
        return ids["openalex"]
    doi = ids.get("doi")
    if doi:
        try:
            payload = http.get(OPENALEX_WORK + "/https://doi.org/" + doi).body
            resolved = json.loads(payload.decode("utf-8")).get("id") or ""
            return resolved.rsplit("/", 1)[-1]
        except Exception:  # noqa: BLE001 - an unresolvable id is a caveat, not a crash
            return ""
    return ""


def run(out: Path, http, round_number: int, cap: int = 250) -> dict:
    out = Path(out)
    read = _read_ids(out)
    if not read:
        raise SystemExit(
            "no notes in " + str(out / "docs" / "notes")
            + " — snowballing from nothing read would look like a barren round rather than "
              "an unstarted one"
        )

    candidates_path = out / "candidates.json"
    payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    known = [Candidate.from_dict(c) for c in payload.get("candidates", [])]
    known_count = len(known)

    by_artifact = {artifact_id(c): c for c in known}
    discovered: list[Candidate] = []
    caveats: list[str] = []
    unresolved = 0
    for artifact in read:
        work_id = _resolve_work_id(artifact, by_artifact.get(artifact), http)
        if not work_id:
            unresolved += 1
            caveats.append(
                "no OpenAlex work id for " + artifact + ", so its citation graph was not "
                "walked: this round's coverage is incomplete by one read artifact"
            )
            continue
        for url in _neighbour_urls(work_id):
            try:
                discovered.extend(parse_openalex(http.get(url).body))
            except Exception as exc:  # noqa: BLE001 - one dead neighbour is not a dead round
                caveats.append("neighbours of " + artifact + " partly unavailable: " + str(exc))

    merged = dedupe(known + discovered)
    fresh = merged[known_count:] if len(merged) > known_count else []

    state = _load_state(out)
    if known_count >= cap:
        verdict = "cap_reached"
        state["barren_rounds"] = state.get("barren_rounds", 0)
        caveats.append(
            "stopped at the cap of " + str(cap) + " candidates, not at saturation: the "
            "corpus is bounded by budget rather than by the literature, and the report "
            "must say so"
        )
        fresh = []
    elif fresh:
        verdict = "productive"
        state["barren_rounds"] = 0
    else:
        state["barren_rounds"] = state.get("barren_rounds", 0) + 1
        verdict = "saturated" if state["barren_rounds"] >= 2 else "barren"

    if fresh:
        candidates_path.write_text(
            json.dumps({**payload, "candidates": [c.to_dict() for c in merged]}, indent=2),
            encoding="utf-8",
        )

    state["rounds"].append({"round": round_number, "verdict": verdict,
                            "new_candidates": len(fresh), "read_at_start": len(read),
                            "unresolved": unresolved})
    state["stopped_because"] = verdict if verdict in ("saturated", "cap_reached") else ""
    (out / "snowball.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    return {"verdict": verdict, "new_candidates": [c.to_dict() for c in fresh],
            "caveats": caveats, "read": len(read), "unresolved": unresolved}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--round", required=True, type=int, dest="round_number")
    ap.add_argument("--cap", type=int, default=250)
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    result = run(args.out, Http(), args.round_number, cap=args.cap)

    reporter = Reporter("snowball")
    reporter.headline(
        "round " + str(args.round_number) + ": " + result["verdict"] + ", "
        + str(len(result["new_candidates"])) + " new candidates from "
        + str(result["read"]) + " read artifacts"
    )
    for caveat in result["caveats"]:
        reporter.caveat(caveat)
    for candidate in result["new_candidates"]:
        reporter.row({"title": candidate["title"], "year": candidate["year"]})
    reporter.emit(args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
