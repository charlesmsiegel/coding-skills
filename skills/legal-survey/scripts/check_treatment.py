#!/usr/bin/env python3
"""Ask the open citation graph whether each case is still good law.

Usage:
    python3 check_treatment.py --out ./research/foia-exemption-5 \
        [--citing-cap 25] [--format text|json]

Reads <out>/manifest.json and <out>/candidates.json, walks CourtListener for the
opinions that cite each archived case, looks for negative language in the window
around the citation, and writes <out>/treatment.json.

**This is not a citator, and nothing downstream may present it as one.**

Shepard's and KeyCite are editorial products: lawyers read citing opinions and
classify the treatment, and their signals mean something because a person stood
behind each one. This walks a public citation graph and matches words. It will
find "we overrule Smith" and it will miss an implicit abrogation that no citing
opinion announced, a reversal that happened before the citing corpus starts, and
a statute that superseded the holding without any court saying so.

So the vocabulary is deliberately weak:

  negative   language that announces the case is no longer good, in whole or part
  caution    language that limits, distinguishes or criticises it, or a pending
             grant of certiorari
  good       citing opinions were read and none of the above was found -- which is
             *not* the same statement as a green flag in a commercial citator
  unchecked  nothing has cited it, or the graph could not be reached. An
             uncited case is unvalidated, not clean, and this is the status that
             stops the run from quietly treating silence as approval.

The one thing this stage buys that no amount of careful reading does: it is
mechanical, so it happens for every case in the corpus rather than for the ones
somebody remembered to worry about.
"""

import argparse
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path

from common import Authority, Http, Reporter, Treatment, artifact_id, citation_pattern

COURTLISTENER_SEARCH = "https://www.courtlistener.com/api/rest/v4/search/"
COURTLISTENER_OPINION = "https://www.courtlistener.com/api/rest/v4/opinions/"

# How much text on each side of a citation mention to read. A court announces
# what it is doing to a case within a sentence or two of naming it; widening this
# mostly imports the treatment of the *next* case discussed, which produces
# confident false positives.
WINDOW = 320

# Ordered most severe first, because the first match decides the status. The
# patterns are deliberately conservative -- a false "negative" costs an author a
# few minutes checking, while a false "good" is the failure this stage exists to
# prevent, so borderline language belongs in `caution` rather than nowhere.
NEGATIVE_PATTERNS = (
    ("overruled", r"\boverrul(?:e|es|ed|ing)\b"),
    ("abrogated", r"\babrogat(?:e|es|ed|ing)\b"),
    ("superseded by statute", r"\bsuperseded by statute\b"),
    ("reversed", r"\brevers(?:e|es|ed|ing)\b"),
    ("vacated", r"\bvacat(?:e|es|ed|ing)\b"),
    ("no longer good law", r"\bno longer good law\b"),
)

CAUTION_PATTERNS = (
    ("declined to follow", r"\bdeclin(?:e|es|ed|ing) to follow\b"),
    ("distinguished", r"\bdistinguish(?:es|ed|ing)?\b"),  # bare present tense too: courts write "we distinguish"
    ("criticized", r"\bcriticiz(?:e|es|ed|ing)\b|\bcriticis(?:e|es|ed|ing)\b"),
    ("limited to its facts", r"\blimited to its facts\b"),
    ("called into question", r"\bcall(?:s|ed)? into question\b"),
    ("certiorari granted", r"\bcert(?:iorari)?\.? granted\b"),
    ("questioned", r"\bquestion(?:s|ed)\s+the\s+(?:continued\s+)?(?:validity|vitality)\b"),
)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text)


def find_signals(text: str, pattern: re.Pattern, citing: str = "") -> list[dict]:
    """Every treatment signal near a mention of the cited case in this opinion."""
    flat = _normalize(_strip_tags(text))
    signals = []
    for mention in pattern.finditer(flat):
        start = max(0, mention.start() - WINDOW)
        window = flat[start:mention.end() + WINDOW]
        lowered = window.lower()
        for severity, table in (("negative", NEGATIVE_PATTERNS), ("caution", CAUTION_PATTERNS)):
            for label, expression in table:
                if re.search(expression, lowered):
                    signals.append({
                        "severity": severity,
                        "label": label,
                        "citing": citing,
                        "context": window[:400],
                    })
    return signals


def _opinion_ids(authority: Authority) -> list[str]:
    raw = authority.external_ids.get("cl_opinion") or ""
    return [part for part in raw.split(",") if part]


def _citing_search_url(opinion_id: str, page: int = 1) -> str:
    return (COURTLISTENER_SEARCH + "?q=cites%3A(" + opinion_id + ")&type=o&order_by=dateFiled"
            "+desc&page=" + str(page))


def _opinion_text_url(opinion_id: str) -> str:
    return COURTLISTENER_OPINION + opinion_id + "/?format=json"


def _opinion_text(payload: dict) -> str:
    """CourtListener stores an opinion's body under whichever field the source gave.

    Trying only `plain_text` returns "" for every opinion harvested from a court's
    HTML, which is most of the modern corpus -- so every citing opinion reads as
    empty and every case comes back "good" having been checked against nothing.
    """
    for field in ("plain_text", "html_with_citations", "html", "html_lawbox",
                  "html_columbia", "xml_harvard"):
        value = payload.get(field)
        if value:
            return value
    return ""


def check_one(authority: Authority, http, citing_cap: int, now=_now) -> Treatment:
    aid = artifact_id(authority)
    if authority.authority_type != "case":
        return Treatment(artifact_id=aid, status="not_applicable",
                         source="courtlistener", checked_at=now())

    pattern = citation_pattern(authority.citation)
    opinion_ids = _opinion_ids(authority)
    if not opinion_ids:
        return Treatment(
            artifact_id=aid, status="unchecked", source="courtlistener", checked_at=now(),
            reason="no CourtListener opinion id, so its citing opinions cannot be requested; "
                   "this case was not validated at all",
        )
    if pattern is None:
        return Treatment(
            artifact_id=aid, status="unchecked", source="courtlistener", checked_at=now(),
            reason="no parsable reporter citation " + repr(authority.citation) + ", so a "
                   "mention of this case inside a citing opinion cannot be located",
        )

    signals: list[dict] = []
    checked = 0
    total: int | None = None
    for opinion_id in opinion_ids:
        # A cluster carries several opinions, and the cap is shared across them.
        # Without this the walk keeps issuing one citing-search call per remaining
        # opinion id after the budget is spent, reading nothing from any of them --
        # traffic on somebody else's server that buys no coverage.
        if checked >= citing_cap:
            break
        try:
            found = http.get_json(_citing_search_url(opinion_id))
        except Exception as exc:  # noqa: BLE001 - an unreachable graph is a status, not a crash
            return Treatment(
                artifact_id=aid, status="unchecked", source="courtlistener", checked_at=now(),
                citing_checked=checked,
                reason="the citation graph was unreachable: " + str(exc),
            )
        count = found.get("count")
        if isinstance(count, int):
            total = (total or 0) + count
        for result in (found.get("results") or []):
            if checked >= citing_cap:
                break
            for nested in (result.get("opinions") or [{"id": result.get("id")}]):
                citing_id = nested.get("id")
                if not citing_id:
                    continue
                try:
                    body = http.get_json(_opinion_text_url(str(citing_id)))
                except Exception:  # noqa: BLE001 - one dead citer is not a dead check
                    continue
                checked += 1
                signals.extend(find_signals(_opinion_text(body), pattern,
                                            citing=str(citing_id)))
                break

    if not checked:
        return Treatment(
            artifact_id=aid, status="unchecked", source="courtlistener", checked_at=now(),
            citing_checked=0, citing_total=total,
            reason="nothing in the citing corpus was readable for this case"
                   + (" (the graph reports " + str(total) + " citers)" if total else
                      " and nothing has cited it")
                   + ": an uncited authority is unvalidated, not clean",
        )

    worst = "good"
    if any(s["severity"] == "negative" for s in signals):
        worst = "negative"
    elif signals:
        worst = "caution"
    return Treatment(artifact_id=aid, status=worst, signals=signals,
                     citing_checked=checked, citing_total=total,
                     source="courtlistener", checked_at=now())


def run(out: Path, http, citing_cap: int = 25, now=_now) -> dict:
    out = Path(out)
    candidates_path = out / "candidates.json"
    manifest_path = out / "manifest.json"
    if not candidates_path.is_file():
        raise SystemExit("no candidates.json in " + str(out) + " -- run search_sources.py first")
    if not manifest_path.is_file():
        raise SystemExit(
            "no manifest.json in " + str(out) + " -- run fetch_authorities.py first; validating "
            "authorities that were never obtained would put a treatment flag on a case nobody "
            "can read"
        )
    payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    archived = {
        a["artifact_id"]
        for a in json.loads(manifest_path.read_text(encoding="utf-8")).get("artifacts", [])
        if a.get("status") == "ok"
    }
    authorities = [Authority.from_dict(a) for a in payload.get("authorities", [])
                   if a.get("status") == "selected"]

    records = []
    for authority in authorities:
        if artifact_id(authority) not in archived:
            continue
        records.append(check_one(authority, http, citing_cap, now=now))

    by_status: dict[str, int] = {}
    for record in records:
        by_status[record.status] = by_status.get(record.status, 0) + 1

    truncated = [r for r in records
                 if r.citing_total is not None and r.citing_total > r.citing_checked]

    (out / "treatment.json").write_text(
        json.dumps({
            "citing_cap": citing_cap,
            "source": "courtlistener",
            "is_citator": False,
            "disclaimer":
                "Signals come from matching language in citing opinions on an open citation "
                "graph. This is not Shepard's or KeyCite: 'good' means no negative signal was "
                "found in the opinions actually read, not that a person validated the case.",
            "by_status": dict(sorted(by_status.items())),
            "treatments": [r.to_dict() for r in records],
        }, indent=2),
        encoding="utf-8",
    )
    return {"checked": len(records), "by_status": by_status,
            "truncated": len(truncated), "cap": citing_cap}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--citing-cap", type=int, default=25, dest="citing_cap",
                    help="how many citing opinions to read per case (default 25)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    result = run(args.out, Http(token=os.environ.get("COURTLISTENER_TOKEN", "")),
                 citing_cap=args.citing_cap)

    counts = result["by_status"]
    reporter = Reporter("check_treatment")
    # The `or` has to guard the join, not the whole concatenation: a non-empty
    # prefix makes the expression truthy no matter what follows, so wrapping the
    # sentence instead of the tail turns "0 authorities checked" into a headline
    # claiming a breakdown it does not have.
    breakdown = ", ".join(str(v) + " " + k for k, v in sorted(counts.items())) or "none"
    reporter.headline(str(result["checked"]) + " authorities checked: " + breakdown)
    reporter.caveat(
        "This is not a citator. 'good' means no negative language was found in the citing "
        "opinions that were read -- it is not a green flag, and the report must say which it "
        "is. Anything the answer turns on should be confirmed in Shepard's or KeyCite."
    )
    if counts.get("negative"):
        reporter.caveat(
            str(counts["negative"]) + " authority(ies) carry negative signals. The gate refuses "
            "a claim resting on one unless the claim carries a treatment_ack saying why it is "
            "cited anyway."
        )
    if counts.get("unchecked"):
        reporter.caveat(
            str(counts["unchecked"]) + " authority(ies) could not be validated at all. An "
            "uncited or unreachable case is unvalidated, not clean."
        )
    if result["truncated"]:
        reporter.caveat(
            str(result["truncated"]) + " case(s) have more citing opinions than the cap of "
            + str(result["cap"]) + " read: the check is bounded by budget, not by the graph, "
            "and a later overruling is exactly the thing a truncated walk misses."
        )
    for status, count in sorted(counts.items()):
        reporter.row({"status": status, "authorities": count})
    reporter.emit(args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
