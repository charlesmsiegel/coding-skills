#!/usr/bin/env python3
"""Re-resolve every citation in the corpus against the manifest. The gate.

Usage:
    python3 verify_authorities.py --out ./research/foia-exemption-5 \
        [--report summary.html] [--format text|json]

Exits non-zero if any citation does not resolve. This is the mechanical half of
the rigor gate: it cannot tell you a claim misreads its page, but it can tell you
the page was never opened, the file changed under it, the case does not exist, or
the case exists and was overruled.

Four checks, in the order they catch things:

  1. **The authority exists and is unchanged.** In the manifest, status ok, file
     present, sha256 matching. A citation to a document nobody obtained is the
     fabricated-citation failure with extra steps.
  2. **The pincite lands inside the document.** A page beyond the end, a quote
     that is not there, a section name that does not appear.
  3. **The document is the authority it claims to be.** The reporter citation the
     manifest records for a case must actually appear in the fetched text. This
     is the check that catches a real file standing in for a cite that was
     invented -- the failure mode that put lawyers in front of judges.
  4. **The authority is still good law.** A claim resting on a case with negative
     treatment fails unless it carries a treatment_ack saying why it is cited
     anyway. Caution and unvalidated are reported *unverifiable* rather than
     passing.

A check it cannot perform is reported unverifiable rather than passing: a page in
a PDF whose page count cannot be determined, a quote or a citation string in a
binary artifact, a section name with no page or quote beside it. **Unverifiable
is not clean.**

Not to be confused with the visualization skills' verify_citations.py, which
checks file:line citations in generated docs against source. Different job,
different inputs, deliberately different name.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from common import Reporter, citation_pattern, load_notes, load_treatments, pdf_page_count

TEXT_SUFFIXES = (".html", ".htm", ".txt", ".xml", ".md", ".json")
# Either quote style. Matching only double quotes means a fragment written with
# single ones is not checked at all, and the gate reports zero failures over a
# report whose links it never looked at.
HREF_RE = re.compile(r"""href\s*=\s*["'](docs/[^"']+)["']""")


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
    treatments = load_treatments(out)

    failures: list[dict] = []
    unverifiable: list[dict] = []
    checked = 0
    cache: dict[str, bytes] = {}

    def text_of(entry) -> str:
        body = cache.get(entry["path"])
        if body is None:
            body = (out / entry["path"]).read_bytes()
            cache[entry["path"]] = body
        return _normalize(_strip_tags(body.decode("utf-8", "replace")))

    for note in load_notes(out):
        for claim in note.claims:
            for locator in claim.locators:
                checked += 1
                context = {"note": note.artifact_id, "claim": claim.text[:80],
                           "authority": locator.artifact_id}
                entry = entries.get(locator.artifact_id)
                if entry is None:
                    failures.append({**context, "reason": "authority is not in the manifest"})
                    continue
                if entry.get("status") != "ok":
                    failures.append({**context, "reason": "authority was never obtained ("
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
                is_text = path.suffix.lower() in TEXT_SUFFIXES

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
                    if is_text:
                        if _normalize(locator.quote) not in text_of(entry):
                            failures.append({**context, "reason": "quote not found in "
                                             + entry["path"]})
                            continue
                    else:
                        unverifiable.append({**context, "reason": "quote cannot be checked in "
                                             "a binary artifact (" + path.suffix + ")"})

                if locator.section and not locator.quote and locator.page is None:
                    # A section name is a locator the reading guide offers on equal
                    # terms with a page and a quote, and it is the cheapest of the
                    # three to fabricate. It must not also be the one that always
                    # passes, so it is searched where that is possible and declared
                    # unverifiable where it is not.
                    if is_text:
                        if _normalize(locator.section) not in text_of(entry):
                            failures.append({**context, "reason": "section "
                                             + repr(locator.section) + " not found in "
                                             + entry["path"]})
                            continue
                    else:
                        unverifiable.append({**context, "reason": "a section-only locator "
                                             "cannot be resolved in a binary artifact ("
                                             + path.suffix + "); cite a page or a quote to "
                                             "make this claim checkable"})

                # Does this file hold the authority it is filed under? A real
                # opinion archived under a citation that belongs to a different
                # case resolves every check above and is still a fabricated cite.
                if entry.get("kind") == "case" and entry.get("citation"):
                    pattern = citation_pattern(entry["citation"])
                    if pattern is None:
                        unverifiable.append({
                            **context,
                            "reason": "the manifest's citation " + repr(entry["citation"])
                            + " does not parse as a reporter citation, so the document cannot "
                            "be confirmed to be this authority"})
                    elif not is_text:
                        unverifiable.append({
                            **context,
                            "reason": "the reporter citation cannot be located in a binary "
                            "artifact (" + path.suffix + "); archive the HTML version to make "
                            "the authority's identity checkable"})
                    elif not pattern.search(text_of(entry)):
                        failures.append({
                            **context,
                            "reason": "the document at " + entry["path"] + " does not contain "
                            "its own reporter citation " + repr(entry["citation"])
                            + ": the file and the cite are not the same authority"})
                        continue

                # Still good law? A negative signal is a failure unless the claim
                # says out loud why the authority is cited anyway.
                treatment = treatments.get(locator.artifact_id)
                if entry.get("kind") == "case" and not claim.treatment_ack:
                    if treatment is None:
                        unverifiable.append({
                            **context,
                            "reason": "no treatment record: check_treatment.py has not run "
                            "over this authority, so nothing has asked whether it is still "
                            "good law"})
                    elif treatment.status == "negative":
                        failures.append({
                            **context,
                            "reason": "negative treatment ("
                            + ", ".join(sorted({s.get("label", "") for s in treatment.signals}))
                            + ") and the claim carries no treatment_ack: citing an authority "
                            "that is no longer good law is the failure this gate exists to "
                            "stop. Say why it is cited anyway, or drop the claim."})
                        continue
                    elif treatment.status in ("caution", "unchecked"):
                        unverifiable.append({
                            **context,
                            "reason": "treatment is " + treatment.status + " ("
                            + (treatment.reason or ", ".join(
                                sorted({s.get("label", "") for s in treatment.signals}))
                               or "no detail")
                            + "): this claim rests on an authority nothing has confirmed is "
                            "still good"})

    if report is not None:
        report_path = Path(report)
        if not report_path.is_file():
            failures.append({"note": "-", "claim": "-", "authority": str(report_path),
                             "reason": "report file missing at " + str(report_path)})
        else:
            for href in HREF_RE.findall(report_path.read_text(encoding="utf-8")):
                # `docs/case/x.pdf#page=4` names the same file as `docs/case/x.pdf`.
                # A deep link to the page a claim cites is exactly what the report
                # structure asks for, and failing it would have the author delete
                # the most useful thing in the citation to get past a blocking gate.
                target = href.split("#", 1)[0].split("?", 1)[0]
                if not target or not (out / target).exists():
                    failures.append({"note": "-", "claim": "-", "authority": href,
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
        # bad note in it -- and the difference decides whether anyone fixes the note.
        raise SystemExit("the gate did not run: " + str(exc)) from exc

    reporter = Reporter("verify_authorities")
    reporter.headline(
        str(len(result["failures"])) + " unresolvable of " + str(result["checked"])
        + " citations checked, " + str(len(result["unverifiable"])) + " unverifiable"
    )
    if result["checked"] == 0:
        reporter.caveat("no citations were checked at all: with no notes on disk this gate "
                        "proves nothing about the report")
    for item in result["unverifiable"]:
        reporter.caveat(item["authority"] + ": " + item["reason"])
    for failure in result["failures"]:
        reporter.row(failure)
    reporter.emit(args.format)
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
