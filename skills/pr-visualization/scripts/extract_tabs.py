#!/usr/bin/env python3
"""Extract tab fragments from a previously assembled report HTML.

Usage: python extract_tabs.py REPORT.html --out-dir TABS_DIR

Reverses assemble.py: writes NN-slug.html fragment files (with their
'<!-- tab: Title -->' headers) recovered from the report's panels. Known tabs
get their canonical NN prefix back — NOT their display position — so an atlas
that legitimately dropped a tab (e.g. no git history → no Hotspots) re-extracts
without shifting every later fragment onto the wrong number. Re-running the
analyzers then overwrites exactly the fragments they own, and documented
--fragments lists stay valid. Also prints the report's meta line (which by
convention holds the commit sha the report was generated at) so an updater can
diff since then.
"""
import argparse
import json
import re
import sys
from pathlib import Path

# The fragment numbering each skill's workflow assigns. Both skills share this
# file byte-for-byte, so both tab sets live in one map; the ids are disjoint.
CANONICAL_PREFIX = {
    # code-visualization atlas
    "overview": 1, "inventory": 2, "dependencies": 3, "hotspots": 4,
    "flows": 5, "boundaries": 6, "invariants": 7, "glossary": 8, "coverage": 9,
    # pr-visualization review report
    "summary": 1, "footprint": 2, "contracts-tests": 3, "blast-radius": 4,
    "flow-impact": 5, "review-walkthrough": 6,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    html = Path(args.report).read_text(encoding="utf-8", errors="replace")
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    titles = {}
    for m in re.finditer(r'<button role="tab" data-tab="([^"]+)"[^>]*>(.*?)</button>', html, re.S):
        titles[m.group(1)] = re.sub(r"\s+", " ", m.group(2)).strip()

    panels = re.findall(
        r'<section class="panel[^"]*" id="([^"]+)" role="tabpanel">\n(.*?)\n</section>',
        html, re.S)
    if not panels:
        print("error: no panels found — is this an assembled report?", file=sys.stderr)
        return 1

    # Known tabs first, so an unknown (custom) tab can never steal a canonical
    # number; customs then keep display order in the remaining slots.
    used = {CANONICAL_PREFIX[pid] for pid, _ in panels if pid in CANONICAL_PREFIX}
    written = []
    for i, (pid, body) in enumerate(panels, start=1):
        title = titles.get(pid, pid.replace("-", " ").title())
        # unescape the HTML title text for the tab comment
        title = title.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
        n = CANONICAL_PREFIX.get(pid)
        if n is None:
            n = i
            while n in used:
                n += 1
            used.add(n)
        fname = f"{n:02d}-{pid}.html"
        (out / fname).write_text(f"<!-- tab: {title} -->\n{body}\n", encoding="utf-8")
        written.append(fname)
    written.sort()

    meta = re.search(r'<div class="doc-meta">(.*?)</div>', html, re.S)
    doc_title = re.search(r'<h1 class="doc-title">(.*?)</h1>', html, re.S)
    print(json.dumps({
        "fragments": written,
        "title": re.sub(r"\s+", " ", doc_title.group(1)).strip() if doc_title else None,
        "meta": re.sub(r"\s+", " ", meta.group(1)).strip() if meta else None,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
