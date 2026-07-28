#!/usr/bin/env python3
"""Lint tab fragments against the fragment protocol before assembly.

Usage: python lint_fragments.py --tabs-dir TABS_DIR

Checks every *.html fragment for:
  - a '<!-- tab: Title -->' header on line 1
  - no <style> blocks (the template owns all styling)
  - no <script> blocks EXCEPT <script type="application/json"> data blocks,
    and each of those must be paired with a .viz container in the same fragment
  - non-empty body (empty fragments are legal — the assembler drops them —
    but are reported so a missing tab is a choice, not an accident)

Exit 0 when clean, 1 when any fragment violates the protocol. A stray <script>
would ship and execute in the assembled report; this is the mechanical check
that none does.
"""
import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_RE = re.compile(r"<script\b([^>]*)>", re.I)
STYLE_RE = re.compile(r"<style\b", re.I)
JSON_TYPE_RE = re.compile(r"""type\s*=\s*["']application/json["']""", re.I)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tabs-dir", required=True)
    args = ap.parse_args()
    tabs = Path(args.tabs_dir)

    problems, notes, checked = [], [], []
    for frag in sorted(p for p in tabs.glob("*.html") if p.is_file()):
        checked.append(frag.name)
        text = frag.read_text(encoding="utf-8", errors="replace")
        first = text.split("\n", 1)[0]
        if not re.match(r"\s*<!--\s*tab:\s*.+?\s*-->", first):
            problems.append({"fragment": frag.name, "problem": "missing '<!-- tab: Title -->' header on line 1"})
        body = text.split("\n", 1)[1] if "\n" in text else ""
        if not body.strip():
            notes.append({"fragment": frag.name, "note": "empty body — assembler will drop this tab"})
        if STYLE_RE.search(body):
            problems.append({"fragment": frag.name, "problem": "<style> block — styling belongs to the template"})
        json_blocks = 0
        for m in SCRIPT_RE.finditer(body):
            if JSON_TYPE_RE.search(m.group(1)):
                json_blocks += 1
            else:
                problems.append({"fragment": frag.name,
                                 "problem": "executable <script> block — only application/json data blocks are allowed"})
        if json_blocks and 'class="viz"' not in body and "class='viz'" not in body:
            problems.append({"fragment": frag.name,
                             "problem": "application/json data block without a .viz container to render it"})

    print(json.dumps({"checked": checked, "problems": problems, "notes": notes}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
