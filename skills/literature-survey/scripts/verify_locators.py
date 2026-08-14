#!/usr/bin/env python3
"""Re-resolve every locator in the corpus against the manifest. The gate.

Usage:
    python3 verify_locators.py --out ./research/team-knowledge \
        [--report summary.html] [--format text|json]

Exits non-zero if any locator does not resolve. This is the mechanical half of
the rigor gate: it cannot tell you a claim misreads its page, but it can tell you
the page was never opened, the file changed under it, or the paper does not exist.

Not to be confused with the sibling visualization skills' verify_citations.py,
which checks file:line citations in generated docs against source. Different job,
different inputs, deliberately different name.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from common import Reporter, load_notes, pdf_page_count

TEXT_SUFFIXES = (".html", ".htm", ".txt", ".tex", ".md", ".json", ".xml")
# Either quote style. Matching only double quotes meant a fragment written with
# single ones was not checked at all, and the gate reported zero failures over a
# report whose links it had never looked at.
HREF_RE = re.compile(r"""href=["'](docs/[^"']+)["']""")


def _normalize(text: str) -> str:
    """Collapse whitespace so a line break inside a sentence does not fail a quote."""
    return " ".join(text.split())


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text)


def run(out, report=None) -> dict:
    out = Path(out)
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("no manifest.json at " + str(manifest_path))
    entries = {
        a["artifact_id"]: a
        for a in json.loads(manifest_path.read_text(encoding="utf-8")).get("artifacts", [])
    }

    failures: list[dict] = []
    unverifiable: list[dict] = []
    checked = 0
    cache: dict[str, bytes] = {}

    for note in load_notes(out):
        for claim in note.claims:
            for locator in claim.locators:
                checked += 1
                context = {"note": note.artifact_id, "claim": claim.text[:80],
                           "artifact": locator.artifact_id}
                entry = entries.get(locator.artifact_id)
                if entry is None:
                    failures.append({**context, "reason": "artifact is not in the manifest"})
                    continue
                if entry.get("status") != "ok":
                    failures.append({**context, "reason": "artifact was never obtained ("
                                     + str(entry.get("status"))
                                     + "): it cannot support a claim"})
                    continue
                path = out / entry["path"]
                if not path.is_file():
                    failures.append({**context, "reason": "file missing at " + entry["path"]})
                    continue
                body = cache.get(entry["path"])
                if body is None:
                    body = path.read_bytes()
                    cache[entry["path"]] = body
                if hashlib.sha256(body).hexdigest() != entry.get("sha256"):
                    failures.append({**context, "reason": "sha256 no longer matches the "
                                     "manifest: the file changed after it was cited"})
                    continue
                if locator.page is not None:
                    pages = pdf_page_count(body)
                    if pages is None:
                        unverifiable.append({**context, "reason": "page count could not be "
                                             "determined for " + entry["path"]})
                    elif locator.page < 1 or locator.page > pages:
                        failures.append({**context, "reason": "page " + str(locator.page)
                                         + " is outside the document's " + str(pages)
                                         + " pages"})
                        continue
                if locator.quote:
                    if path.suffix.lower() in TEXT_SUFFIXES:
                        haystack = _normalize(_strip_tags(body.decode("utf-8", "replace")))
                        if _normalize(locator.quote) not in haystack:
                            failures.append({**context, "reason": "quote not found in "
                                             + entry["path"]})
                            continue
                    else:
                        unverifiable.append({**context, "reason": "quote cannot be checked in "
                                             "a binary artifact (" + path.suffix + ")"})
                if locator.section and not locator.quote and locator.page is None:
                    # A section name is a locator SKILL.md offers on equal terms with a
                    # page and a quote, and it used to be the only one nothing resolved:
                    # "Section 12: The Nonexistent Results" against a two-page paper
                    # exited 0 and was counted as checked. The cheapest locator to
                    # fabricate must not also be the one that always passes, so it is
                    # searched where that is possible and declared unverifiable where it
                    # is not. Unverifiable is not clean.
                    if path.suffix.lower() in TEXT_SUFFIXES:
                        haystack = _normalize(_strip_tags(body.decode("utf-8", "replace")))
                        if _normalize(locator.section) not in haystack:
                            failures.append({**context, "reason": "section "
                                             + repr(locator.section) + " not found in "
                                             + entry["path"]})
                            continue
                    else:
                        unverifiable.append({**context, "reason": "a section-only locator "
                                             "cannot be resolved in a binary artifact ("
                                             + path.suffix + "); cite a page or a quote to "
                                             "make this claim checkable"})

    if report is not None:
        report_path = Path(report)
        if report_path.is_file():
            for href in HREF_RE.findall(report_path.read_text(encoding="utf-8")):
                # `docs/papers/x.pdf#page=4` names the same file as `docs/papers/x.pdf`.
                # A deep link to the page a claim cites is exactly what the report
                # structure asks for, and failing it would have the author delete the
                # most useful thing in the citation to get past a blocking gate.
                target = href.split("#", 1)[0].split("?", 1)[0]
                if not target or not (out / target).exists():
                    failures.append({"note": "-", "claim": "-", "artifact": href,
                                     "reason": "report links to " + href
                                     + " which is not on disk"})

    return {"checked": checked, "failures": failures, "unverifiable": unverifiable}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    try:
        result = run(args.out, report=args.report)
    except ValueError as exc:
        # A note that will not parse is a real stop, but a traceback out of a
        # blocking gate reads as the gate breaking rather than as the run having a
        # bad note in it — and the difference decides whether anyone fixes the note.
        raise SystemExit("the gate did not run: " + str(exc)) from exc

    reporter = Reporter("verify_locators")
    reporter.headline(
        str(len(result["failures"])) + " unresolvable of " + str(result["checked"])
        + " locators checked, " + str(len(result["unverifiable"])) + " unverifiable"
    )
    if result["checked"] == 0:
        reporter.caveat("no locators were checked at all: with no notes on disk this gate "
                        "proves nothing about the report")
    for item in result["unverifiable"]:
        reporter.caveat(item["artifact"] + ": " + item["reason"])
    for failure in result["failures"]:
        reporter.row(failure)
    reporter.emit(args.format)
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
