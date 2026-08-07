#!/usr/bin/env python3
"""Write the summary document that ties a unit's code map and health together.

This is the page a reader lands on. It carries the grade, says what the unit is,
and sends them to the two documents that show the working. It invents nothing:
the grade and category scores are read back out of health.html's metadata block,
and the code map is described from its own header rather than re-derived.

Prose is the agent's job, not the script's. `--intro-file` takes an HTML fragment
you wrote after reading the code; `--highlight` takes one bullet at a time. With
neither, the page is still correct — just thinner, and it says so.

Usage:
  python build_summary.py --out src/billing/docs/summary.html --name billing \\
      --health src/billing/docs/health.html --codemap src/billing/docs/codemap.html \\
      --intro-file intro.html --highlight "Two import cycles through models.py"

  python build_summary.py --root --out docs/summary.html --name my-repo \\
      --map docs/code-overview.json --health docs/health.html --codemap docs/codemap.html
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

import rubric
from build_health import (grade_class, headline_badges, render_category_rows,
                          render_package_table, render_top_findings)
from common import (DOC_TITLES, doc_path, esc, load_map, read_asset, read_meta,
                    rel_href, render, warn)

_TITLE_RE = re.compile(r'<h1 class="doc-title">(.*?)</h1>', re.DOTALL)
_META_RE = re.compile(r'<div class="doc-meta">(.*?)</div>', re.DOTALL)
_TAB_RE = re.compile(r'<nav class="tabs"[^>]*>(.*?)</nav>', re.DOTALL)
_BUTTON_RE = re.compile(r">([^<>]+)</button>")


def describe_codemap(path: Path) -> dict:
    """What the atlas says about itself: its meta line and its tab names."""
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    meta = _META_RE.search(text)
    tabs = _TAB_RE.search(text)
    return {
        "title": (_TITLE_RE.search(text).group(1).strip() if _TITLE_RE.search(text) else ""),
        "meta": (meta.group(1).strip() if meta else ""),
        "tabs": [t.strip() for t in _BUTTON_RE.findall(tabs.group(1))] if tabs else [],
    }


def doc_link(kind: str, href: str, description: str, exists: bool) -> str:
    disabled = "" if exists else ' aria-disabled="true"'
    target = href if exists else "#"
    note = description if exists else "not generated"
    return (f'<a class="doclink" href="{esc(target)}"{disabled}>'
            f'<div class="k">{esc(kind)}</div>'
            f'<div class="t">{esc(DOC_TITLES[kind])}</div>'
            f'<div class="d">{esc(note)}</div></a>')


def render_highlights(highlights: list[str]) -> str:
    if not highlights:
        return ""
    items = "".join(f"<li>{esc(h)}</li>" for h in highlights)
    return f"<h2>What stands out</h2><ul>{items}</ul>"


def render_package_links(repo: Path, packages: list[dict], out: Path) -> str:
    """The root portal's way down: one row per package, linking its summary."""
    if not packages:
        return ""
    rows = []
    for package in packages:
        summary = doc_path(repo, package, "summary")
        health = read_meta(doc_path(repo, package, "health")) or {}
        grade = health.get("grade", rubric.UNGRADED)
        score = health.get("score")
        size = health.get("size", {})
        name = esc(package["name"])
        label = (f'<a href="{esc(rel_href(out, summary))}">{name}</a>'
                 if summary.is_file() else name)
        rows.append(
            f"<tr><td>{label}<br><span class=\"faint mono\">"
            f'{esc(", ".join(package["roots"]))}</span></td>'
            f'<td><span class="badge neutral">{esc(package.get("language") or "?")}</span></td>'
            f'<td class="num">{size.get("loc", "—")}</td>'
            f'<td class="num">{"—" if score is None else f"{score:.1f}"}</td>'
            f'<td class="{grade_class(grade)}" style="color:var(--grade);font-family:var(--mono);'
            f'font-weight:600">{esc(grade)}</td>'
            f'<td class="num">{health.get("findings_total", "—")}</td></tr>'
        )
    return ("<h2>Packages</h2>"
            '<p class="dim">Each package has its own summary, code map, and health page.</p>'
            '<div class="tbl-wrap"><table><thead><tr><th>Package</th><th>Language</th>'
            '<th class="num">Lines</th><th class="num">Score</th><th>Grade</th>'
            '<th class="num">Findings</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table></div>")


def render_codemap_block(described: dict, href: str, exists: bool) -> str:
    if not exists:
        return ('<div class="callout warn">No code map was generated for this unit, so the '
                "structural half of the overview is missing.</div>")
    if not described.get("tabs"):
        return ""
    tabs = ", ".join(esc(t) for t in described["tabs"])
    meta = f'<p class="faint mono">{esc(described["meta"])}</p>' if described.get("meta") else ""
    return (f'<div class="card"><strong>Code map</strong> — <a href="{esc(href)}">'
            f"{esc(described.get('title') or 'codemap.html')}</a>"
            f"<p class=\"dim\">Tabs: {tabs}</p>{meta}</div>")


def build(args) -> str:
    repo = Path(args.repo).resolve()
    out = Path(args.out)
    health_path = Path(args.health) if args.health else out.parent / "health.html"
    codemap_path = Path(args.codemap) if args.codemap else out.parent / "codemap.html"

    meta = read_meta(health_path) or {}
    if not meta:
        warn(f"no code-health metadata at {health_path} — the summary will carry no grade")

    grade = meta.get("grade", rubric.UNGRADED)
    score = meta.get("score")
    categories = meta.get("categories", [])

    packages = []
    if args.root and args.map:
        packages = load_map(args.map).get("packages", [])

    described = describe_codemap(codemap_path)
    codemap_href = rel_href(out, codemap_path)
    links = "".join((
        doc_link("codemap", codemap_href,
                 "Structure, dependencies, hotspots, and the judgment tabs",
                 codemap_path.is_file()),
        doc_link("health", rel_href(out, health_path),
                 f"Graded findings — {meta.get('findings_total', 0)} in total",
                 health_path.is_file()),
    ))

    intro = Path(args.intro_file).read_text(encoding="utf-8") if args.intro_file else ""
    if not intro and not args.highlight:
        intro = ('<div class="callout">This summary was generated without a written '
                 "overview. The grade and the links below are accurate; the reading of "
                 "the code that would explain them has not been added.</div>")

    category_block = ""
    if categories:
        category_block = ("<h2>Category scores</h2>"
                          f'<div class="card">{render_category_rows(categories)}</div>')

    subject = args.name or meta.get("package") or repo.name
    body = render(read_asset("summary-body.html"), {
        "INTRO": intro,
        "GRADE": esc(grade),
        "GRADE_CLASS": grade_class(grade),
        "SCORE": "—" if score is None else f"{score:.1f}",
        "SUBJECT": esc(subject),
        "SUBJECT_DETAIL": esc(args.subtitle
                              or ("the whole repository" if args.root
                                  else ", ".join(meta.get("roots", []) or ["."]))),
        "HEADLINE_BADGES": headline_badges(meta) if meta else "",
        "DOC_LINKS": links,
        "HIGHLIGHTS": render_highlights(args.highlight) + render_codemap_block(
            described, codemap_href, codemap_path.is_file()),
        "CATEGORY_BLOCK": category_block,
        "PACKAGE_TABLE": (render_package_links(repo, packages, out) if args.root
                          else render_package_table(meta.get("packages", []), {})),
        "TOP_FINDINGS": (("<h2>Worst findings</h2>" + render_top_findings(meta["top_findings"][:10]))
                         if meta.get("top_findings") else ""),
        "CAVEATS": render_caveats(meta),
    })

    scope = "repository" if args.root else "package"
    return render(read_asset("template.html"), {
        "DOC_TITLE": esc(f"{subject} — Overview"),
        "DOC_LABEL": "CODE OVERVIEW",
        "DOC_SUBTITLE": esc(args.subtitle or f"Summary of the {subject} {scope}: what it is, "
                                             "how healthy it is, and where to read next."),
        "DOC_META": esc(" · ".join(part for part in (
            f"generated {args.date or dt.date.today().isoformat()}",
            meta.get("commit", ""),
            f"grade {grade}" if grade != rubric.UNGRADED else "",
            f"{len(packages)} packages" if packages else "",
        ) if part)),
        "DOC_BODY": body,
        "DOC_FOOTER": ("Generated by code-overview. The grade comes from health.html; the "
                       "structure comes from codemap.html. Both are linked above."),
    })


def render_caveats(meta: dict) -> str:
    parts = []
    if meta.get("ungraded"):
        listed = ", ".join(esc(rubric.CATEGORY_LABELS.get(k, k)) for k in meta["ungraded"])
        parts.append(f'<div class="callout warn">Ungraded: {listed}. Nothing measured those, '
                     "so the grade covers less than the full rubric.</div>")
    if meta.get("analyzer_errors"):
        listed = ", ".join(f"<code>{esc(k)}</code>" for k in sorted(meta["analyzer_errors"]))
        parts.append(f'<div class="callout bad">Detectors that did not complete: {listed}. '
                     "The grade is an upper bound.</div>")
    return ("<h2>Caveats</h2>" + "".join(parts)) if parts else ""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", required=True, help="path to write summary.html to")
    parser.add_argument("--name", default="", help="package name (or repo name with --root)")
    parser.add_argument("--repo", default=".", help="repository root (default: .)")
    parser.add_argument("--health", default="", help="health.html to read the grade from")
    parser.add_argument("--codemap", default="", help="codemap.html to link and describe")
    parser.add_argument("--root", action="store_true", help="repo-level portal")
    parser.add_argument("--map", default="", help="package map, for the --root package table")
    parser.add_argument("--intro-file", default="",
                        help="HTML fragment you wrote — what this unit is and why it looks this way")
    parser.add_argument("--highlight", action="append", default=[],
                        help="one bullet for 'What stands out'; repeatable")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--date", default="")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(args), encoding="utf-8")
    print(f"{out}: written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
