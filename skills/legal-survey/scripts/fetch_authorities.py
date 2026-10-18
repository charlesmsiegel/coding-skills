#!/usr/bin/env python3
"""Download the selected authorities and record every outcome in the manifest.

Usage:
    python3 fetch_authorities.py --out ./research/foia-exemption-5 \
        [--url "oip-guidance=https://www.justice.gov/oip/..." ...] \
        [--format text|json]

Reads <out>/candidates.json, fetches every authority whose status is "selected",
and writes <out>/manifest.json.

The manifest is the skill's ground truth. A citation resolves against it, the
headline counts are computed from it, and the gaps tab is built from it -- so an
authority that could not be obtained must appear as an entry with a reason. An
absent entry lets an unreachable case read as one nobody wanted, which in a legal
memo is the difference between "no authority is on point" and "we did not look".

`--url NAME=URL` is how anything the source APIs do not index gets into the
manifest on the same terms: agency guidance, a state court's own PDF, a bill as
enacted, a practitioner treatise chapter the author posted. An authority that is
not in the manifest cannot be cited, so this flag is what keeps that rule from
narrowing the corpus to whatever happens to have an API.
"""

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from common import (
    DATED_TYPES,
    Authority,
    FetchError,
    Http,
    ManifestEntry,
    Reporter,
    artifact_filename,
    artifact_id,
)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


# Anchored on the status Http reports, not on the digits appearing anywhere in
# the string. The reason carries the URL, and a bare substring test called a
# timeout on https://.../10.1177/0003603X403 a paywall -- which puts an authority
# in the gaps tab as deliberately inaccessible when the network merely flaked,
# and sends whoever reads that tab looking for a subscription they already have.
_PAYWALL_RE = re.compile(r"HTTP (401|402|403)\b")

# What a subscription wall says when it answers 200. Checked only against a small
# body, because these words appear legitimately in long documents -- "register"
# and "subscribe" are ordinary English, and a 200-page rulemaking docket that
# happens to contain them is not a paywall.
_WALL_MARKERS = (
    "sign in", "log in", "subscribe", "subscription", "create an account",
    "access denied", "not authorized", "purchase this",
)
_SMALL_BODY = 4096


def _looks_like_a_wall(body: bytes) -> str:
    """The marker that makes this body an access wall rather than a document, or ""."""
    if len(body) > _SMALL_BODY:
        return ""
    text = body.decode("utf-8", "replace").lower()
    for marker in _WALL_MARKERS:
        if marker in text:
            return marker
    return ""


def _classify_failure(reason: str) -> str:
    """A 401/402/403 is a paywall; anything else is just a failure. Both are gaps."""
    if _PAYWALL_RE.search(reason):
        return "paywalled"
    return "failed"


def _load_manifest(path: Path) -> dict[str, ManifestEntry]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {e["artifact_id"]: ManifestEntry.from_dict(e) for e in payload.get("artifacts", [])}


def _is_current(entry: ManifestEntry, out: Path) -> bool:
    """Whether the recorded artifact is still byte-for-byte what was recorded."""
    if entry.status != "ok":
        return False
    path = out / entry.path
    if not path.is_file():
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == entry.sha256


def _target(authority: Authority) -> tuple[str, str]:
    """(url, suffix) for the best available location, or ("", "").

    HTML is preferred over PDF for cases, which inverts what a research tool
    normally wants. The reason is the gate: a quote and a reporter citation can
    be checked literally in text and only guessed at in a PDF, and an authority
    whose citation cannot be re-resolved is one the report cannot rely on.
    """
    if authority.landing_url:
        suffix = ".xml" if authority.landing_url.endswith(".xml") else ".html"
        return authority.landing_url, suffix
    if authority.pdf_url:
        return authority.pdf_url, ".pdf"
    return "", ""


def _as_of_for(authority: Authority, today: str) -> str:
    """The version date to record for codified law.

    Falls back to today because that is literally what was fetched: a versionless
    request to eCFR or legislation.gov.uk returns the text as it stands now, and
    recording that is honest. What it must never do is stay empty -- the manifest
    rejects a dated authority with no date, and it should, because a regulation
    with no version is a citation to a text nobody can retrieve again.
    """
    if authority.authority_type not in DATED_TYPES:
        return ""
    return authority.as_of or authority.decided or today


def run(out: Path, http, now=_now, today=_today, extra_urls=None) -> dict:
    out = Path(out)
    candidates_path = out / "candidates.json"
    if not candidates_path.is_file():
        raise SystemExit("no candidates.json in " + str(out) + " -- run search_sources.py first")
    payload = json.loads(candidates_path.read_text(encoding="utf-8"))
    selected = [Authority.from_dict(a) for a in payload.get("authorities", [])
                if a.get("status") == "selected"]

    manifest = _load_manifest(out / "manifest.json")
    fetched = skipped = gaps = 0
    stamp = today()

    queue: list[tuple[str, Authority]] = [(artifact_id(a), a) for a in selected]
    for spec in extra_urls or []:
        # A hand-added URL is a secondary artifact unless the caller says
        # otherwise: it has no reporter citation and no version, and calling it a
        # statute would trip the manifest's as-of invariant for no gain.
        queue.append((spec["name"], Authority(
            title=spec["name"],
            authority_type="secondary",
            landing_url=spec["url"],
            sources=["hand-added"],
        )))

    for aid, authority in queue:
        existing = manifest.get(aid)
        if existing is not None and _is_current(existing, out):
            skipped += 1
            continue

        url, suffix = _target(authority)
        kind = authority.authority_type
        as_of = _as_of_for(authority, stamp)
        if not url:
            manifest[aid] = ManifestEntry(
                artifact_id=aid, kind=kind, url="", status="failed",
                citation=authority.citation,
                failure_reason="no retrievable location: neither a document URL nor a PDF",
            )
            gaps += 1
            continue

        if hasattr(http, "robots_allows") and not http.robots_allows(url):
            manifest[aid] = ManifestEntry(
                artifact_id=aid, kind=kind, url=url, status="robots_blocked",
                citation=authority.citation,
                failure_reason="robots.txt disallows this path for our user agent",
            )
            gaps += 1
            continue

        try:
            response = http.get(url)
        except (FetchError, OSError) as exc:
            reason = str(exc)
            manifest[aid] = ManifestEntry(
                artifact_id=aid, kind=kind, url=url, citation=authority.citation,
                status=_classify_failure(reason), failure_reason=reason,
            )
            gaps += 1
            continue

        # A 200 is not an opinion. Legal publishers answer a document URL with an
        # interstitial -- "sign in to continue" -- and saving that under .html
        # archives the wall as though it were the law: counted in "authorities
        # archived", absent from the gaps tab, and available to be cited. The
        # gaps tab exists because "the law says" and "the law we could reach
        # says" are different claims, and this is the most common way in this
        # domain for the difference to disappear.
        if suffix == ".pdf" and b"%PDF" not in response.body[:1024]:
            manifest[aid] = ManifestEntry(
                artifact_id=aid, kind=kind, url=url, status="paywalled",
                citation=authority.citation,
                failure_reason="the server returned "
                + (getattr(response, "content_type", "") or "a non-PDF body")
                + " rather than a PDF, which is what an access wall looks like; nothing was "
                "archived, because a page locator cannot resolve in it",
            )
            gaps += 1
            continue
        marker = _looks_like_a_wall(response.body)
        if marker:
            manifest[aid] = ManifestEntry(
                artifact_id=aid, kind=kind, url=url, status="paywalled",
                citation=authority.citation,
                failure_reason="the server returned a " + str(len(response.body))
                + "-byte body containing " + repr(marker) + ", which is an access wall rather "
                "than a document; nothing was archived",
            )
            gaps += 1
            continue

        rel = "docs/" + kind + "/" + artifact_filename(authority, suffix)
        destination = out / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.body)
        manifest[aid] = ManifestEntry(
            artifact_id=aid, kind=kind, url=url, status="ok", path=rel,
            sha256=hashlib.sha256(response.body).hexdigest(),
            bytes_len=len(response.body), fetched_at=now(),
            content_type=getattr(response, "content_type", ""),
            citation=authority.citation, as_of=as_of,
        )
        fetched += 1

    (out / "manifest.json").write_text(
        json.dumps({"artifacts": [manifest[k].to_dict() for k in sorted(manifest)]}, indent=2),
        encoding="utf-8",
    )
    return {"fetched": fetched, "skipped": skipped, "gaps": gaps, "selected": len(queue)}


def _parse_url_flag(raw: str) -> dict:
    name, sep, url = raw.partition("=")
    if not sep or not name or not url:
        raise argparse.ArgumentTypeError("--url takes NAME=URL, got " + repr(raw))
    return {"name": name, "url": url}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--url", action="append", default=[], dest="urls",
                    type=_parse_url_flag, metavar="NAME=URL",
                    help="an authority the source APIs do not index; repeatable")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    result = run(args.out, Http(token=os.environ.get("COURTLISTENER_TOKEN", "")),
                 extra_urls=args.urls)

    reporter = Reporter("fetch_authorities")
    reporter.headline(
        str(result["fetched"]) + " fetched, " + str(result["skipped"]) + " already current, "
        + str(result["gaps"]) + " unobtainable, of " + str(result["selected"]) + " selected"
    )
    if result["gaps"]:
        reporter.caveat(
            str(result["gaps"]) + " authorities could not be obtained and are recorded as gaps. "
            "The report must say so: 'the law says' and 'the law we could reach says' are "
            "different claims, and a memo that conflates them is the one that loses."
        )
    reporter.caveat("Nothing is validated yet. Run check_treatment.py before reading: an "
                    "overruled case reads exactly like a good one.")
    reporter.emit(args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
