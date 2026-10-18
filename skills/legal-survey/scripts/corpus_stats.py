#!/usr/bin/env python3
"""Compute the masthead numbers from what is actually on disk.

Usage:
    python3 corpus_stats.py --out ./research/foia-exemption-5 \
        [--json-out meta.json] [--html-out strip.html] [--format text|json]

**Every number in the report's masthead comes from here.** A headline stat that
is itself an unverified claim undermines the report whose credibility it exists
to establish -- and in this skill the masthead carries the treatment counts,
which are the numbers a reader is most entitled to distrust.

Five groups: corpus counts and bytes, the binding/persuasive split, treatment,
read-vs-archived, and saturation and gaps. Anything that cannot be computed is
reported as unknown rather than zero -- "no case was validated" and "no case has
a negative signal" are different facts and only one of them is reassuring.
"""

import argparse
import json
import sys
from pathlib import Path

from common import DATED_TYPES, Reporter, load_notes, load_treatments

KINDS = ("case", "statute", "regulation", "constitution", "legislative_history",
         "secondary", "treaty")

# How the masthead groups them. Codified law is one idea to a reader even though
# the manifest keeps four types apart.
CODIFIED = ("statute", "regulation", "constitution", "treaty")


def human_bytes(count: int) -> str:
    if count < 1024:
        return str(count) + " B"
    value = float(count)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024.0 or unit == "TB":
            return ("%.1f " % value) + unit
    return str(count) + " B"


def _load(path: Path, what: str) -> dict:
    if not path.is_file():
        raise SystemExit("no " + what + " at " + str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def compute(out) -> dict:
    out = Path(out)
    manifest = _load(out / "manifest.json", "manifest.json")
    artifacts = manifest.get("artifacts", [])

    ok_rows = [a for a in artifacts if a.get("status") == "ok"]
    by_kind = {}
    for kind in KINDS:
        rows = [a for a in ok_rows if a.get("kind") == kind]
        by_kind[kind] = {"count": len(rows),
                         "bytes": sum(int(a.get("bytes_len") or 0) for a in rows)}

    codified = {
        "count": sum(by_kind[k]["count"] for k in CODIFIED),
        "bytes": sum(by_kind[k]["bytes"] for k in CODIFIED),
    }
    # A dated authority with no as_of cannot happen through fetch_authorities --
    # the manifest invariant rejects it -- but a hand-edited manifest can, and a
    # regulation whose version is unknown is exactly the citation that looks
    # fine and cannot be reproduced.
    undated = [a for a in ok_rows if a.get("kind") in DATED_TYPES and not a.get("as_of")]

    gap_rows = [a for a in artifacts if a.get("status") != "ok"]
    by_status: dict[str, int] = {}
    for row in gap_rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1

    # "Read in full" is a claim about this corpus, so a note only counts when it
    # names an authority the corpus actually holds. Counting every file in notes/
    # produces "3 read in full of 1 archived" -- a ratio above one, in the strip
    # whose whole job is that its numbers can be checked. Orphans are reported
    # rather than dropped: a note nothing archived is a reading that will fail
    # the gate, and silence about it is how it stays unnoticed until then.
    archived_ids = {a.get("artifact_id") for a in ok_rows}
    all_notes = load_notes(out)
    notes = [n for n in all_notes if n.artifact_id in archived_ids]
    orphan_notes = len(all_notes) - len(notes)

    candidates_path = out / "candidates.json"
    candidates: list[dict] = []
    forum = ""
    as_of = ""
    if candidates_path.is_file():
        payload = json.loads(candidates_path.read_text(encoding="utf-8"))
        candidates = payload.get("authorities", [])
        forum = payload.get("forum", "")
        as_of = payload.get("as_of", "")
    selected = [c for c in candidates if c.get("status") == "selected"]
    weights: dict[str, int] = {}
    for row in selected:
        weights[row.get("weight") or "unweighed"] = weights.get(
            row.get("weight") or "unweighed", 0) + 1

    treatments = load_treatments(out)
    treatment_counts: dict[str, int] = {}
    for record in treatments.values():
        treatment_counts[record.status] = treatment_counts.get(record.status, 0) + 1
    cases_archived = by_kind["case"]["count"]
    cases_checked = sum(v for k, v in treatment_counts.items() if k != "not_applicable")

    snowball_path = out / "snowball.json"
    if snowball_path.is_file():
        state = json.loads(snowball_path.read_text(encoding="utf-8"))
        stopped = state.get("stopped_because") or "in-progress"
        saturation = {
            "rounds": len(state.get("rounds") or []),
            "stopped_because": stopped,
            "saturated": stopped == "saturated",
        }
    else:
        saturation = {"rounds": 0, "stopped_because": "not-run", "saturated": False}

    return {
        "forum": forum,
        "as_of": as_of,
        "by_kind": by_kind,
        "cases": by_kind["case"],
        "codified": codified,
        "secondary": by_kind["secondary"],
        "undated": len(undated),
        "archived": len(archived_ids),
        "read_in_full": len(notes),
        "orphan_notes": orphan_notes,
        "weights": dict(sorted(weights.items())),
        "selected": len(selected),
        "treatment": {
            "checked": cases_checked,
            "cases": cases_archived,
            "by_status": dict(sorted(treatment_counts.items())),
            "is_citator": False,
        },
        "gaps": {"count": len(gap_rows), "by_status": dict(sorted(by_status.items()))},
        "saturation": saturation,
        "candidates": len(candidates),
    }


def _cell(key: str, value: str, small: str = "") -> str:
    tail = ' <small>' + small + "</small>" if small else ""
    return ('<div><span class="k">' + key + '</span><span class="v">' + value + tail
            + "</span></div>")


def meta_strip_html(stats: dict) -> str:
    """The masthead strip. Never written by hand."""
    treatment = stats["treatment"]
    # "Read in full" counts notes, which cover cases *and* codified law *and*
    # commentary, so its denominator must be every archived artifact. Comparing
    # it against the case count alone produces "8 read in full / 5 archived" -- a
    # ratio above 1, which reads as a bug in the report and undermines the number
    # it exists to make honest.
    cells = [
        _cell("Read in full", str(stats["read_in_full"]),
              "of " + str(stats["archived"]) + " archived"),
        _cell("Cases", str(stats["cases"]["count"]),
              human_bytes(stats["cases"]["bytes"]) if stats["cases"]["bytes"] else ""),
        _cell("Statutes &amp; regulations", str(stats["codified"]["count"]),
              human_bytes(stats["codified"]["bytes"]) if stats["codified"]["bytes"] else ""),
        _cell("Secondary sources", str(stats["secondary"]["count"]),
              human_bytes(stats["secondary"]["bytes"]) if stats["secondary"]["bytes"] else ""),
    ]

    weights = stats["weights"]
    if not weights:
        cells.append(_cell("Binding authority", "unknown", "nothing selected yet"))
    else:
        detail = ", ".join(str(v) + " " + k for k, v in weights.items()
                           if k != "binding") or "none other"
        cells.append(_cell("Binding authority", str(weights.get("binding", 0)),
                           "in " + stats["forum"] + "; " + detail if stats["forum"]
                           else detail + "; no forum recorded"))

    # The treatment cell is the one a reader will act on, so it says what it is.
    # "12 good" with no qualifier reads as a citator's green flag, which is a
    # claim this skill has no way to make.
    if not treatment["by_status"]:
        cells.append(_cell("Treatment", "unchecked", "check_treatment.py has not run"))
    else:
        by_status = treatment["by_status"]
        bad = by_status.get("negative", 0)
        warn = by_status.get("caution", 0)
        unchecked = by_status.get("unchecked", 0)
        headline = str(bad) + " negative"
        detail = (str(warn) + " caution, " + str(unchecked) + " unvalidated, of "
                  + str(treatment["checked"]) + " cases; open citation graph, not a citator")
        cells.append(_cell("Treatment", headline, detail))

    saturation = stats["saturation"]
    if saturation["stopped_because"] == "cap_reached":
        detail = "stopped at the cap, not saturated"
    elif saturation["saturated"]:
        detail = "saturated"
    elif saturation["stopped_because"] == "not-run":
        detail = "no snowball rounds run"
    else:
        detail = saturation["stopped_because"]
    cells.append(_cell("Snowball", str(saturation["rounds"]) + " rounds", detail))
    cells.append(_cell("Unobtainable", str(stats["gaps"]["count"]),
                       "see the gaps tab" if stats["gaps"]["count"] else ""))
    return '<div class="meta-strip">' + "".join(cells) + "</div>"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--json-out", type=Path)
    ap.add_argument("--html-out", type=Path)
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    stats = compute(args.out)
    if args.json_out:
        args.json_out.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    if args.html_out:
        args.html_out.write_text(meta_strip_html(stats), encoding="utf-8")

    treatment = stats["treatment"]
    reporter = Reporter("corpus_stats")
    reporter.headline(
        str(stats["read_in_full"]) + " read in full of " + str(stats["archived"])
        + " archived; " + str(treatment["by_status"].get("negative", 0))
        + " with negative treatment; " + str(stats["gaps"]["count"]) + " unobtainable"
    )
    if stats["orphan_notes"]:
        reporter.caveat(
            str(stats["orphan_notes"]) + " note(s) name an authority the manifest does not "
            "hold, so they are not counted as read: every claim in them will fail "
            "verify_authorities.py"
        )
    if stats["read_in_full"] == 0 and stats["archived"] > 0:
        reporter.caveat("nothing has been read: downloads are not readings, and synthesis has "
                        "no notes to work from")
    if stats["weights"].get("unweighed"):
        reporter.caveat(
            str(stats["weights"]["unweighed"]) + " selected authority(ies) carry no weight, so "
            "the report cannot say whether they bind the forum"
        )
    if not stats["forum"]:
        reporter.caveat("no forum is recorded in candidates.json, so 'binding' is a claim "
                        "about no particular court")
    if treatment["cases"] and not treatment["by_status"]:
        reporter.caveat("check_treatment.py has not run, so every case in this corpus is "
                        "uncited-and-unvalidated as far as the report can tell")
    if stats["undated"]:
        reporter.caveat(
            str(stats["undated"]) + " archived statute(s)/regulation(s) carry no as_of date, "
            "so the text they hold cannot be reproduced or dated"
        )
    if stats["saturation"]["stopped_because"] == "cap_reached":
        reporter.caveat("the snowball stopped at its cap, not at saturation -- the report must "
                        "say the corpus is bounded by budget rather than by the law")
    reporter.row({"cases": stats["cases"]["count"], "codified": stats["codified"]["count"],
                  "secondary": stats["secondary"]["count"], "gaps": stats["gaps"]["count"]})
    reporter.emit(args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
