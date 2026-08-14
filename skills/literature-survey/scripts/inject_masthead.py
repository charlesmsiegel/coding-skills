#!/usr/bin/env python3
"""Inject the computed masthead strip and JSON meta block into the report.

Usage:
    python3 inject_masthead.py --report ./research/x/summary.html \
        --strip strip.html --meta meta.json

assemble.py is byte-identical across three skills but for its --label default, and
CI enforces that. Rather than give it a raw-HTML flag only this skill needs, the
computed strip goes in afterwards at a marker — the same idempotent,
existence-checked move inject_nav.py already makes.

Idempotent by construction: the injected region is delimited, so a second run
replaces it instead of stacking a second copy of last run's numbers.
"""

import argparse
import json
import sys
from pathlib import Path

STRIP_MARKER = "<!--META_STRIP-->"
JSON_MARKER = "<!--SURVEY_META_JSON-->"
STRIP_OPEN = "<!--META_STRIP:BEGIN-->"
STRIP_CLOSE = "<!--META_STRIP:END-->"
JSON_OPEN = "<!--SURVEY_META_JSON:BEGIN-->"
JSON_CLOSE = "<!--SURVEY_META_JSON:END-->"


def _replace_region(text: str, marker: str, open_tag: str, close_tag: str, payload: str) -> str:
    block = open_tag + payload + close_tag
    if open_tag in text and close_tag in text:
        head = text[:text.index(open_tag)]
        tail = text[text.index(close_tag) + len(close_tag):]
        return head + block + tail
    if marker in text:
        return text.replace(marker, block, 1)
    raise SystemExit(
        "the report carries neither " + marker + " nor an injected region for it, so there is "
        "nowhere to put the computed numbers — was it built from this skill's template?"
    )


def run(report, strip_html: str, meta: dict) -> None:
    report = Path(report)
    text = report.read_text(encoding="utf-8")
    text = _replace_region(text, STRIP_MARKER, STRIP_OPEN, STRIP_CLOSE, strip_html)
    # A value containing "</script>" would close the block early and spill markup
    # into the document; the escape is the standard one for JSON in HTML.
    payload = json.dumps(meta, indent=2).replace("</", "<\\/")
    block = ('<script type="application/json" id="literature-survey-meta">'
             + payload + "</script>")
    text = _replace_region(text, JSON_MARKER, JSON_OPEN, JSON_CLOSE, block)
    report.write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", required=True, type=Path)
    ap.add_argument("--strip", required=True, type=Path)
    ap.add_argument("--meta", required=True, type=Path)
    args = ap.parse_args()
    run(args.report, args.strip.read_text(encoding="utf-8"),
        json.loads(args.meta.read_text(encoding="utf-8")))
    print("injected masthead into " + str(args.report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
