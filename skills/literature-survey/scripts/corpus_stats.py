#!/usr/bin/env python3
"""Compute the masthead numbers from what is actually on disk.

Usage:
    python3 corpus_stats.py --out ./research/team-knowledge \
        [--json-out meta.json] [--html-out strip.html] [--format text|json]

**Every number in the report's masthead comes from here.** The reference run's
headline counts were correct because a model counted carefully; nothing preserved
that property, and a headline stat that is itself an unverified claim undermines
the report whose credibility it exists to establish.

Four groups: corpus counts and bytes, recency, read-vs-archived, saturation and
gaps. Anything that cannot be computed is reported as unknown rather than zero —
"no dates at all" and "0% recent" are different facts.
"""

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

from common import Reporter, load_notes

KINDS = ("paper", "web", "repo")


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


def compute(out, current_year: int | None = None) -> dict:
    out = Path(out)
    manifest = _load(out / "manifest.json", "manifest.json")
    artifacts = manifest.get("artifacts", [])

    by_kind = {}
    for kind in KINDS:
        rows = [a for a in artifacts if a.get("kind") == kind and a.get("status") == "ok"]
        key = "web" if kind == "web" else kind + "s"
        by_kind[key] = {
            "count": len(rows),
            "bytes": sum(int(a.get("bytes_len") or 0) for a in rows),
        }

    gap_rows = [a for a in artifacts if a.get("status") != "ok"]
    by_status: dict[str, int] = {}
    for row in gap_rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1

    notes = load_notes(out)

    candidates_path = out / "candidates.json"
    candidates = []
    if candidates_path.is_file():
        candidates = json.loads(candidates_path.read_text(encoding="utf-8")).get("candidates", [])
    year = current_year or _dt.datetime.now(_dt.timezone.utc).year
    dated = [c for c in candidates if isinstance(c.get("year"), int)]
    recent = [c for c in dated if c["year"] >= year - 1]
    percent = int(round(100.0 * len(recent) / len(dated))) if dated else None

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
        "papers": by_kind["papers"],
        "web": by_kind["web"],
        "repos": by_kind["repos"],
        "archived": len([a for a in artifacts if a.get("status") == "ok"]),
        "read_in_full": len(notes),
        "gaps": {"count": len(gap_rows), "by_status": dict(sorted(by_status.items()))},
        "recency": {"recent": len(recent), "dated": len(dated),
                    "undated": len(candidates) - len(dated), "percent": percent},
        "saturation": saturation,
        "candidates": len(candidates),
    }


def _cell(key: str, value: str, small: str = "") -> str:
    tail = ' <small>' + small + "</small>" if small else ""
    return ('<div><span class="k">' + key + '</span><span class="v">' + value + tail
            + "</span></div>")


def meta_strip_html(stats: dict) -> str:
    """The masthead strip, in the reference run's markup."""
    # "Read in full" counts notes, which cover papers *and* pages, so its
    # denominator must be every archived artifact. Comparing it against the paper
    # count alone produced "2 read in full / 1 archived" — a ratio above 1, which
    # reads as a bug in the report and undermines the number it exists to make
    # honest.
    cells = [
        _cell("Read in full", str(stats["read_in_full"]),
              "of " + str(stats["archived"]) + " archived"),
        _cell("Papers archived", str(stats["papers"]["count"]),
              human_bytes(stats["papers"]["bytes"]) if stats["papers"]["bytes"] else ""),
        _cell("Repositories cloned", str(stats["repos"]["count"]),
              "/ " + human_bytes(stats["repos"]["bytes"]) if stats["repos"]["bytes"] else ""),
        _cell("Web pages &amp; threads", str(stats["web"]["count"]),
              "/ " + human_bytes(stats["web"]["bytes"]) if stats["web"]["bytes"] else ""),
    ]
    recency = stats["recency"]
    if recency["percent"] is None:
        cells.append(_cell("Corpus recency", "unknown", "no publication dates"))
    else:
        cells.append(_cell("Corpus recency", str(recency["percent"]) + "%",
                           "of " + str(recency["dated"]) + " dated"))
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

    reporter = Reporter("corpus_stats")
    reporter.headline(
        str(stats["read_in_full"]) + " read in full of " + str(stats["archived"])
        + " archived; " + str(stats["gaps"]["count"]) + " unobtainable"
    )
    if stats["read_in_full"] == 0 and stats["archived"] > 0:
        reporter.caveat("nothing has been read: downloads are not readings, and synthesis "
                        "has no notes to work from")
    if stats["saturation"]["stopped_because"] == "cap_reached":
        reporter.caveat("the snowball stopped at its cap, not at saturation — the report must "
                        "say the corpus is bounded by budget rather than by the literature")
    if stats["recency"]["percent"] is None:
        reporter.caveat("no candidate carries a publication year, so recency is unknown "
                        "rather than zero")
    reporter.row({"papers": stats["papers"]["count"], "web": stats["web"]["count"],
                  "repos": stats["repos"]["count"], "gaps": stats["gaps"]["count"]})
    reporter.emit(args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
