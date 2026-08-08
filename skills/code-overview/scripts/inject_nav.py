#!/usr/bin/env python3
"""Inject the shared navigation bar into every document in the overview.

Runs last, over documents three different generators wrote — including
code-visualization's codemap.html, which this skill otherwise never touches. That
constraint shapes the whole design:

* The block is self-contained. Its CSS ships inside it and every custom property
  it uses carries a literal fallback (`var(--accent, #1f6fbd)`), so it renders
  correctly inside a page shell that has never heard of this skill.
* It is delimited and replaced, not appended. Re-running the overview must not
  stack nav bars.
* Links are relative and existence-checked. A package with no atlas gets a
  two-item row, never a dangling href.

Three rows: up to the repo-level document *of the same type*, across this
package's four documents, and sideways to the same type in sibling packages.

Usage:
  python inject_nav.py --map docs/code-overview.json --repo .
  python inject_nav.py --map docs/code-overview.json --repo . --check
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path
from urllib.parse import unquote

from common import (DOC_KINDS, DOC_TITLES, doc_path, esc, listed_packages, load_map,
                    rel_href, warn)

START = "<!-- code-overview:nav -->"
END = "<!-- /code-overview:nav -->"

_BLOCK_RE = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
_HEADER_END_RE = re.compile(r"</header>", re.IGNORECASE)
_BODY_START_RE = re.compile(r"<body[^>]*>", re.IGNORECASE)

HOME_LABELS = {"summary": "Overall Summary", "codemap": "Overall Code Map",
               "health": "Overall Health", "measurement": "Overall Measurement",
               "theory": "Overall Theory"}

STYLE = """<style>
.co-nav{position:sticky;top:0;z-index:60;max-width:1280px;margin:0 auto;padding:10px 28px;
  font-family:var(--mono,ui-monospace,SFMono-Regular,Menlo,monospace);font-size:12px;
  background:color-mix(in srgb,var(--bg,#0d1420) 92%,transparent);
  border-bottom:1px solid var(--border,rgba(150,180,230,0.14));
  backdrop-filter:blur(8px);display:flex;flex-direction:column;gap:6px}
.co-nav .co-row{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;min-width:0}
.co-nav a{color:var(--text-dim,#8fa1bd);text-decoration:none;padding:3px 9px;border-radius:999px;
  border:1px solid transparent;white-space:nowrap}
.co-nav a:hover{color:var(--accent,#1f6fbd);background:var(--accent-dim,rgba(31,111,189,0.10))}
.co-nav a[aria-current]{color:var(--accent,#1f6fbd);
  border-color:var(--accent,#1f6fbd);background:var(--accent-dim,rgba(31,111,189,0.10))}
.co-nav .co-here{color:var(--text,#dbe4f3);font-weight:600;padding:3px 0}
.co-nav .co-lbl{color:var(--text-faint,#5f7191);letter-spacing:.08em;text-transform:uppercase;
  font-size:10.5px;padding-right:2px}
.co-nav .co-sep{color:var(--text-faint,#5f7191);opacity:.6}
@media(max-width:720px){.co-nav{padding-left:16px;padding-right:16px}}
</style>"""


def nav_html(repo: Path, package: dict | None, kind: str, packages: list[dict],
             *, standalone: bool) -> str:
    """Build the block for one document.

    `standalone` covers the single-package repo whose package root is the repo
    root: there is no "up", because this document already is the overall one.
    """
    here = doc_path(repo, package, kind)
    rows: list[str] = []

    # Row 1 — up to the repo-level document of this same type.
    top = doc_path(repo, None, kind)
    if package is not None and top.is_file() and top.resolve() != here.resolve():
        rows.append(
            '<div class="co-row">'
            f'<a href="{esc(rel_href(here, top))}">⌂ {esc(HOME_LABELS[kind])}</a>'
            f'<span class="co-sep">/</span>'
            f'<span class="co-here">{esc(package["name"])}</span></div>'
        )
    elif not standalone:
        rows.append(
            '<div class="co-row"><span class="co-here">⌂ '
            f'{esc(HOME_LABELS[kind])}</span></div>'
        )

    # Row 2 — this unit's four documents.
    siblings = []
    for other in DOC_KINDS:
        target = doc_path(repo, package, other)
        if other == kind:
            siblings.append(f'<a href="#" aria-current="page">{esc(DOC_TITLES[other])}</a>')
        elif target.is_file():
            siblings.append(f'<a href="{esc(rel_href(here, target))}">{esc(DOC_TITLES[other])}</a>')
    label = esc(package["name"]) if package is not None else "This repository"
    rows.append(f'<div class="co-row"><span class="co-lbl">{label}</span>' + "".join(siblings) + "</div>")

    # Row 3 — the same document type in every other package. Compared by
    # resolved path, not by name: in a single-package repo the one package's
    # document *is* this document, and a nav that offers to link to itself is
    # worse than no third row.
    others = []
    for other in packages:
        if package is not None and other["name"] == package["name"]:
            continue
        target = doc_path(repo, other, kind)
        if target.is_file() and target.resolve() != here.resolve():
            others.append(f'<a href="{esc(rel_href(here, target))}">{esc(other["name"])}</a>')
    if others:
        rows.append('<div class="co-row"><span class="co-lbl">'
                    f'Other packages · {esc(DOC_TITLES[kind])}</span>' + "".join(others) + "</div>")

    return f'{START}{STYLE}<nav class="co-nav" aria-label="code overview">' + "".join(rows) + f"</nav>{END}"


def inject(path: Path, block: str) -> str:
    """Replace an existing block, or place a new one just after the header.

    After `</header>` rather than after `<body>` so the nav sits below the page
    title in code-visualization's shell, where its own tab bar would be — the
    two read as one strip instead of fighting for the top of the page.
    """
    text = path.read_text(encoding="utf-8")
    if _BLOCK_RE.search(text):
        return _BLOCK_RE.sub(lambda _: block, text, count=1)
    header = _HEADER_END_RE.search(text)
    if header:
        return text[:header.end()] + "\n" + block + text[header.end():]
    body = _BODY_START_RE.search(text)
    if body:
        return text[:body.end()] + "\n" + block + text[body.end():]
    warn(f"{path} has neither </header> nor <body> — nav prepended to the file")
    return block + "\n" + text


def units(repo: Path, packages: list[dict]) -> list[dict | None]:
    """The repo, then each package — skipping a package that *is* the repo."""
    return [None, *listed_packages(repo, packages)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--map", required=True, help="docs/code-overview.json")
    parser.add_argument("--repo", default=".", help="repository root (default: .)")
    parser.add_argument("--check", action="store_true",
                        help="report what would change and whether links resolve; write nothing")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    packages = listed_packages(repo, load_map(args.map).get("packages", []))
    listed = units(repo, packages)
    standalone = len(listed) == 1

    written, missing, broken = 0, [], []
    for package in listed:
        for kind in DOC_KINDS:
            path = doc_path(repo, package, kind)
            if not path.is_file():
                missing.append(str(path.relative_to(repo)))
                continue
            block = nav_html(repo, package, kind, packages, standalone=standalone)
            current = path.read_text(encoding="utf-8")
            updated = inject(path, block)
            # In check mode the interesting question is whether the set *on disk*
            # still holds together — a document deleted since the last run leaves
            # a dangling href in every nav that pointed at it. A freshly built
            # block cannot dangle, because its links were existence-checked as it
            # was assembled, so checking that instead would always pass.
            audited = _BLOCK_RE.search(current) if args.check else None
            for href in re.findall(r'href="([^"#][^"]*)"',
                                   audited.group(0) if audited else block):
                # An href is a URL inside HTML; the filesystem is neither. Both
                # layers have to come off, in order, or the gate lies in one
                # direction or the other: a docs directory named `a&b` is
                # written `a%26b` and escaped to `a%26b`, and resolving either
                # form literally reports every valid link to it as broken.
                href = unquote(html.unescape(href))
                if not (path.parent / href).resolve().is_file():
                    broken.append(f"{path.relative_to(repo)} → {href}")
            if args.check:
                written += updated != current
            else:
                path.write_text(updated, encoding="utf-8")
                written += 1

    verb = "would update" if args.check else "updated"
    print(f"{verb} {written} document(s)", file=sys.stderr)
    for path in missing:
        print(f"  not generated: {path}", file=sys.stderr)
    for link in broken:
        print(f"  BROKEN LINK: {link}", file=sys.stderr)
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
