#!/usr/bin/env python3
"""Assemble tab fragments into a single self-contained tabbed HTML file.

Usage:
    python3 assemble.py --tabs-dir DIR --out FILE.html \
        --title "my-repo — Codebase Atlas" [--label "CODEBASE ATLAS"] \
        [--subtitle "..."] [--meta "..."] [--footer "..."] \
        [--template /path/to/template.html]

Fragment protocol
-----------------
The tabs dir contains files named  NN-slug.html  (NN = 2-digit order prefix).
Line 1 of each fragment must be:   <!-- tab: Human Readable Title -->
The rest of the file is the panel's inner HTML.
Fragments are assembled in filename order. A fragment whose body is empty
(after the tab comment) is skipped.

--title/--label/--meta/--subtitle are HTML-escaped. --footer is injected as
raw HTML (it legitimately carries entities and links) — escape it yourself if
it ever contains untrusted text.

Renderers
---------
A skill that ships assets/renderers.css and assets/renderers.js (sortable
tables, force graph, treemap, Mermaid) has them inlined into the page whatever
--template names: the shell decides the look, never whether the graphs draw.
A skill that ships neither gets its shell unchanged.
"""
import argparse
import html
import re
import sys
from pathlib import Path


ASSETS = Path(__file__).resolve().parent.parent / "assets"


def with_renderers(shell: str) -> str:
    """The page shell with this skill's renderers inlined, if it ships any.

    They live once, in assets/renderers.{css,js}, rather than inside the
    default template: a caller that passes its own shell (code-overview does,
    so its five documents read as one) would otherwise get an atlas whose
    graphs never draw and whose diagrams stay Mermaid source.
    """
    css, js = ASSETS / "renderers.css", ASSETS / "renderers.js"
    if not (css.is_file() and js.is_file()):
        return shell
    style = f'<style id="atlas-renderers-css">\n{css.read_text(encoding="utf-8")}</style>\n'
    script = f'<script id="atlas-renderers">\n{js.read_text(encoding="utf-8")}</script>\n'
    head = shell.lower().find("</head>")
    if head < 0:
        script, style = style + script, ""
    else:
        shell = shell[:head] + style + shell[head:]
    body = shell.lower().rfind("</body>")
    return shell + script if body < 0 else shell[:body] + script + shell[body:]


def slug_id(name: str) -> str:
    s = re.sub(r"^\d+-", "", Path(name).stem)
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", s).strip("-").lower()
    return s or "tab"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tabs-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--label", default="PULL REQUEST REVIEW")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--meta", default="")
    ap.add_argument("--footer", default="")
    ap.add_argument("--template", default=str(ASSETS / "template.html"))
    args = ap.parse_args()

    tabs_dir = Path(args.tabs_dir)
    frags = sorted(p for p in tabs_dir.glob("*.html") if p.is_file())
    if not frags:
        print(f"error: no *.html fragments in {tabs_dir}", file=sys.stderr)
        return 1

    nav_parts, panel_parts, seen_ids = [], [], set()
    for i, frag in enumerate(frags):
        text = frag.read_text(encoding="utf-8")
        m = re.match(r"\s*<!--\s*tab:\s*(.+?)\s*-->", text)
        if not m:
            print(f"warning: {frag.name} missing '<!-- tab: Title -->' header, using filename", file=sys.stderr)
            title, body = slug_id(frag.name).replace("-", " ").title(), text
        else:
            title, body = m.group(1), text[m.end():]
        if not body.strip():
            print(f"note: skipping empty fragment {frag.name}", file=sys.stderr)
            continue
        tid = slug_id(frag.name)
        while tid in seen_ids:
            tid += "-x"
        seen_ids.add(tid)
        sel = "true" if len(nav_parts) == 0 else "false"
        nav_parts.append(
            f'<button role="tab" data-tab="{tid}" aria-selected="{sel}" '
            f'aria-controls="{tid}">{html.escape(title)}</button>'
        )
        active = " active" if len(panel_parts) == 0 else ""
        panel_parts.append(
            f'<section class="panel{active}" id="{tid}" role="tabpanel">\n{body}\n</section>'
        )

    tpl = with_renderers(Path(args.template).read_text(encoding="utf-8"))
    out_html = (
        tpl.replace("<!--DOC_TITLE-->", html.escape(args.title))
        .replace("<!--DOC_LABEL-->", html.escape(args.label))
        .replace("<!--DOC_SUBTITLE-->", html.escape(args.subtitle))
        .replace("<!--DOC_META-->", html.escape(args.meta))
        .replace("<!--DOC_FOOTER-->", args.footer)
        .replace("<!--TABS_NAV-->", "\n".join(nav_parts))
        .replace("<!--TABS_PANELS-->", "\n".join(panel_parts))
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(out_html, encoding="utf-8")
    print(f"wrote {out} ({len(panel_parts)} tabs: {', '.join(sorted(seen_ids))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
