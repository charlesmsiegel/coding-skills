#!/usr/bin/env python3
"""Follow the citation graph out from what has actually been read.

Usage:
    python3 snowball.py --out ./research/foia-exemption-5 --round 1 [--cap 250]
        [--format text|json]

Reads the artifact ids in <out>/docs/notes/, asks the graph for what each
authority cites and what cites it, subtracts everything already in
candidates.json, appends what is left, and writes a verdict to
<out>/snowball.json.

Two graphs, because a legal corpus has two halves:

  cases       CourtListener, in both directions -- the opinions this one relies
              on, and the opinions that have relied on it. Backwards finds the
              line of authority; forwards finds where the doctrine went, which
              is usually where the answer changed.
  secondary   OpenAlex, for the law reviews and working papers. Keyless and
              generous, which is why check_treatment and this stage both lean on
              open graphs rather than on a subscription.

The verdict is the point. "barren" means this round found nothing; two in a row
is "saturated". "cap_reached" is a different thing entirely and must never be
reported as saturation -- one means the authority is exhausted, the other means
we stopped looking. In a legal memo that distinction is the difference between
"there is no contrary authority" and "we did not finish checking".
"""

import argparse
import json
import os
import sys
from pathlib import Path

from common import Authority, Http, Reporter, artifact_id
from sources import dedupe, parse_courtlistener, parse_openalex

COURTLISTENER_SEARCH = "https://www.courtlistener.com/api/rest/v4/search/"
OPENALEX_WORK = "https://api.openalex.org/works"

# Each graph's maximum page size. Asking for more is refused, so these are the
# walk's real per-authority ceiling and the numbers the caveats have to name.
CL_PAGE_SIZE = 100
OPENALEX_PAGE_SIZE = 100


def _declared_total(body: bytes, key: str) -> int | None:
    """How many neighbours the graph says exist, or None if it did not say."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if key == "count":
        count = payload.get("count")
    else:
        count = (payload.get("meta") or {}).get("count")
    return count if isinstance(count, int) else None


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


def case_neighbour_urls(opinion_id: str) -> list[tuple[str, str]]:
    """(direction, url) for both halves of one opinion's citation graph."""
    return [
        ("cited by this opinion",
         COURTLISTENER_SEARCH + "?q=cited-by%3A(" + opinion_id + ")&type=o&page_size=100"),
        ("citing this opinion",
         COURTLISTENER_SEARCH + "?q=cites%3A(" + opinion_id + ")&type=o&page_size=100"),
    ]


def secondary_neighbour_urls(work_id: str) -> list[tuple[str, str]]:
    return [
        ("references", OPENALEX_WORK + "?filter=cited_by:" + work_id + "&per-page=100"),
        ("citers", OPENALEX_WORK + "?filter=cites:" + work_id + "&per-page=100"),
    ]


def _resolve_openalex_id(authority: Authority, http) -> str:
    """A work id (W...) for a secondary source, which is what OpenAlex filters on.

    A DOI is resolved through the works endpoint; anything else is unresolvable.
    Returning "" rather than guessing is deliberate: a wrong work id returns
    somebody else's citation graph, which is worse than a recorded gap.
    """
    ids = authority.external_ids
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


def _walk(authority: Authority, http, caveats: list[str], artifact: str) -> list[Authority]:
    """Every neighbour of one read authority, in both directions, with honest caveats."""
    discovered: list[Authority] = []
    if authority.authority_type == "case":
        opinion_ids = [p for p in
                       (authority.external_ids.get("cl_opinion") or "").split(",") if p]
        if not opinion_ids:
            caveats.append(
                "no CourtListener opinion id for " + artifact + ", so its citation graph was "
                "not walked: this round's coverage is incomplete by one read authority"
            )
            return discovered
        pairs = [pair for oid in opinion_ids for pair in case_neighbour_urls(oid)]
        parse, count_key, page_size = parse_courtlistener, "count", CL_PAGE_SIZE
    else:
        work_id = _resolve_openalex_id(authority, http)
        if not work_id:
            caveats.append(
                "no OpenAlex work id for " + artifact + ", so its citation graph was not "
                "walked: this round's coverage is incomplete by one read authority"
            )
            return discovered
        pairs = secondary_neighbour_urls(work_id)
        parse, count_key, page_size = parse_openalex, "meta", OPENALEX_PAGE_SIZE

    for direction, url in pairs:
        try:
            body = http.get(url).body
            discovered.extend(parse(body))
            # The graph reports how many neighbours exist; we ask for one page of
            # them. A leading case has thousands of citers, and taking the first
            # hundred without saying so is the same silent truncation the cap rule
            # exists to prevent one level up -- the round would report "barren"
            # over a graph it never finished walking.
            total = _declared_total(body, count_key)
            if total is not None and total > page_size:
                caveats.append(
                    "only the first " + str(page_size) + " of " + str(total) + " neighbours of "
                    + artifact + " were walked (" + direction + "): this round is bounded by "
                    "page size, not by the graph"
                )
        except Exception as exc:  # noqa: BLE001 - one dead neighbour is not a dead round
            caveats.append("neighbours of " + artifact + " partly unavailable ("
                           + direction + "): " + str(exc))
    return discovered


def run(out: Path, http, round_number: int, cap: int = 250) -> dict:
    out = Path(out)
    read = _read_ids(out)
    if not read:
        raise SystemExit(
            "no notes in " + str(out / "docs" / "notes")
            + " -- snowballing from nothing read would look like a barren round rather than "
              "an unstarted one"
        )

    candidates_path = out / "candidates.json"
    if not candidates_path.is_file():
        raise SystemExit(
            "no candidates.json in " + str(out) + " -- run search_sources.py first; the "
            "snowball subtracts what is already known, and with nothing known every "
            "neighbour would re-enter triage as a discovery"
        )
    payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    known = [Authority.from_dict(a) for a in payload.get("authorities", [])]
    known_count = len(known)

    by_artifact = {artifact_id(a): a for a in known}
    discovered: list[Authority] = []
    caveats: list[str] = []
    unresolved = 0
    for artifact in read:
        authority = by_artifact.get(artifact)
        if authority is None:
            unresolved += 1
            caveats.append(
                "note " + artifact + " names an authority that is not in candidates.json, so "
                "its citation graph was not walked"
            )
            continue
        before = len(caveats)
        found = _walk(authority, http, caveats, artifact)
        if not found and len(caveats) > before:
            unresolved += 1
        discovered.extend(found)

    merged = dedupe(known + discovered)
    fresh = merged[known_count:] if len(merged) > known_count else []

    state = _load_state(out)
    if known_count >= cap:
        verdict = "cap_reached"
        state["barren_rounds"] = state.get("barren_rounds", 0)
        caveats.append(
            "stopped at the cap of " + str(cap) + " authorities, not at saturation: the corpus "
            "is bounded by budget rather than by the law, and the report must say so"
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
            json.dumps({**payload, "authorities": [a.to_dict() for a in merged]}, indent=2),
            encoding="utf-8",
        )

    state["rounds"].append({"round": round_number, "verdict": verdict,
                            "new_authorities": len(fresh), "read_at_start": len(read),
                            "unresolved": unresolved})
    state["stopped_because"] = verdict if verdict in ("saturated", "cap_reached") else ""
    (out / "snowball.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    return {"verdict": verdict, "new_authorities": [a.to_dict() for a in fresh],
            "caveats": caveats, "read": len(read), "unresolved": unresolved}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--round", required=True, type=int, dest="round_number")
    ap.add_argument("--cap", type=int, default=250)
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    result = run(args.out, Http(token=os.environ.get("COURTLISTENER_TOKEN", "")),
                 args.round_number, cap=args.cap)

    reporter = Reporter("snowball")
    reporter.headline(
        "round " + str(args.round_number) + ": " + result["verdict"] + ", "
        + str(len(result["new_authorities"])) + " new authorities from "
        + str(result["read"]) + " read"
    )
    for caveat in result["caveats"]:
        reporter.caveat(caveat)
    if result["new_authorities"]:
        reporter.caveat("New authorities re-enter triage, and anything selected must be "
                        "fetched and re-validated: check_treatment.py runs again after this.")
    for authority in result["new_authorities"]:
        reporter.row({"title": authority["title"], "citation": authority["citation"],
                      "decided": authority["decided"]})
    reporter.emit(args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
