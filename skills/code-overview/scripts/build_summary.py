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

import common
import rubric
import theory_rubric as tr
from health_render import (grade_class, headline_badges, render_category_rows,
                           render_package_table, render_top_findings)
from common import (DOC_TITLES, doc_path, esc, grouped_by_doctor, listed_packages, load_map,
                    read_asset, read_meta, rel_href, render, warn)

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


def render_measurement_card(meta: dict | None) -> str:
    """The second grade, read from measurement.html rather than passed in."""
    if meta is None:
        return ""
    score = meta.get("score")
    if score is None:
        return ('<div class="callout"><strong>Measurement: no measurement content.</strong> '
                "Nothing in this unit produces a quality or accuracy number, so there is "
                "nothing to grade. That is a null, not a pass.</div>")
    grade = str(meta.get("grade", "—"))
    return (f'<section class="gradecard {grade_class(grade)}">'
            f'<div><div class="letter">{esc(grade)}</div>'
            f'<div class="score">{score:.1f} / 100</div></div>'
            '<div class="what"><h2>Measurement coverage</h2>'
            '<p class="dim">How much of what matters here is actually measured — '
            "importance-weighted measured things over measurable things. A different "
            "question from the health grade, and often a more uncomfortable one.</p>"
            "</div></section>")


def render_theory_card(meta: dict | None) -> str:
    """The third grade, read from theory.html rather than passed in.

    Deliberately worded to keep it from being mistaken for the other two: it is
    a judgment, and a reader comparing three letters side by side needs to know
    that one of them was produced differently.
    """
    if meta is None:
        return ""
    score = meta.get("score")
    # Computed before the exempt branch, not inside the graded one. A unit two
    # judges called trivial can still split the panel two rungs, and testing
    # exemption first threw that away — the portal is where readers land, so a
    # disagreement dropped here is dropped for most of them. Labels, not keys:
    # theory.html says "World-mapping" and this said `world_mapping`.
    disputed = meta.get("disputed") or []
    note = ""
    if disputed:
        note = ('<p class="dim"><strong>The panel disagreed</strong> on '
                + esc(", ".join(tr.DIMENSION_LABELS.get(str(key), str(key))
                                for key in disputed))
                + " — see the Theory document.</p>")
    if score is None:
        return ('<div class="callout"><strong>Theory: too small to warrant one.</strong> '
                f'{esc(meta.get("exempt_reason") or "")} Scored null — not zero, not a pass.'
                f"{note}</div>")
    grade = str(meta.get("grade", "—"))
    return (f'<section class="gradecard {grade_class(grade)}">'
            f'<div><div class="letter">{esc(grade)}</div>'
            f'<div class="score">{score:.1f} / 100</div></div>'
            '<div class="what"><h2>Theory</h2>'
            '<p class="dim">Whether the code expresses a coherent theory of its problem, '
            "judged by a panel of three. A reading, not a measurement — read its evidence "
            f"rather than this letter.</p>{note}</div></section>")


def measurement_cell(meta: dict | None) -> tuple[str, str]:
    """(score, state) as displayed, for one package's measurement document.

    Three outcomes, deliberately distinct: no document, a document that found
    nothing measurable, and a graded one. Collapsing the first two into a dash
    is how a package nobody audited comes to look like a package with nothing
    to audit.
    """
    if meta is None:
        return "—", "not generated"
    score = meta.get("score")
    if score is None:
        return "—", "no measurement content"
    return f"{score:.1f}", str(meta.get("grade", "—"))


def theory_cell(meta: dict | None) -> tuple[str, str]:
    """(score, state) as displayed, for one package's theory document.

    The same three outcomes `measurement_cell` keeps apart, for the same
    reason: a package no panel has read and a package a panel judged too small
    to warrant a theory are different facts, and one dash states neither. This
    is the only place on the repository landing page a package's theory grade
    appears at all — without the column it is invisible until you open the
    package's own portal.
    """
    if meta is None:
        return "—", "not generated"
    score = meta.get("score")
    if score is None:
        return "—", "too small to warrant a theory"
    return f"{score:.1f}", str(meta.get("grade", "—"))


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
    ungenerated = 0
    for package in packages:
        summary = doc_path(repo, package, "summary")
        health = read_meta(doc_path(repo, package, "health"))
        measurement = read_meta(doc_path(repo, package, "measurement"),
                                common.MEASUREMENT_BLOCK_ID)
        # No health page is a documented choice ("codemap only" for a language
        # with no doctor). A row of bare em-dashes reads as an unexplained hole,
        # so say which it is.
        generated = health is not None
        health = health or {}
        if not generated:
            ungenerated += 1
        grade = health.get("grade", rubric.UNGRADED)
        score = health.get("score")
        size = health.get("size", {})
        m_score, m_state = measurement_cell(measurement)
        t_score, t_state = theory_cell(read_meta(doc_path(repo, package, "theory"),
                                                 common.THEORY_BLOCK_ID))
        name = esc(package["name"])
        label = (f'<a href="{esc(rel_href(out, summary))}">{name}</a>'
                 if summary.is_file() else name)
        findings = ('<span class="badge neutral">not generated</span>' if not generated
                    else health.get("findings_total", "—"))
        rows.append(
            f"<tr><td>{label}<br><span class=\"faint mono\">"
            f'{esc(", ".join(package["roots"]))}</span></td>'
            f'<td><span class="badge neutral">{esc(package.get("language") or "?")}</span></td>'
            f'<td class="num">{size.get("loc", "—")}</td>'
            f'<td class="num">{"—" if score is None else f"{score:.1f}"}</td>'
            f'<td class="{grade_class(grade)}" style="color:var(--grade);font-family:var(--mono);'
            f'font-weight:600">{esc(grade)}</td>'
            f'<td class="num">{findings}</td>'
            f'<td class="num">{esc(m_score)}</td>'
            f'<td>{esc(m_state)}</td>'
            f'<td class="num">{esc(t_score)}</td>'
            f'<td>{esc(t_state)}</td></tr>'
        )
    caption = ("Each package has its own summary, code map, and health page."
               if not ungenerated else
               f"Each package has its own summary and code map. {ungenerated} of them "
               "has no health page and is therefore ungraded — marked <em>not generated</em> "
               "below.")
    return ("<h2>Packages</h2>"
            f'<p class="dim">{caption}</p>'
            # Measurement and theory each occupy two cells — a score and the
            # state behind it — so each gets two headers. One `Measurement`
            # header over two columns left every header from `Lines` rightward
            # sitting above the wrong column.
            '<div class="tbl-wrap"><table><thead><tr><th>Package</th><th>Language</th>'
            '<th class="num">Lines</th><th class="num">Score</th><th>Grade</th>'
            '<th class="num">Findings</th>'
            '<th class="num">Measurement</th><th>Measurement state</th>'
            '<th class="num">Theory</th><th>Theory state</th></tr></thead><tbody>'
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

    measurement_path = Path(args.out).parent / "measurement.html"
    measurement = read_meta(measurement_path, common.MEASUREMENT_BLOCK_ID)

    theory_path = Path(args.out).parent / "theory.html"
    theory = read_meta(theory_path, common.THEORY_BLOCK_ID)

    grade = meta.get("grade", rubric.UNGRADED)
    score = meta.get("score")
    categories = meta.get("categories", [])

    packages = []
    if args.root and args.map:
        # A package whose docs directory *is* the repo's has no separate document
        # set — listing it would advertise one that does not exist and link the
        # row back to this very page. Navigation already drops it; so does this.
        packages = listed_packages(repo, load_map(args.map).get("packages", []))

    described = describe_codemap(codemap_path)
    codemap_href = rel_href(out, codemap_path)
    links = "".join((
        doc_link("codemap", codemap_href,
                 "Structure, dependencies, hotspots, and the judgment tabs",
                 codemap_path.is_file()),
        doc_link("health", rel_href(out, health_path),
                 f"Graded findings — {meta.get('findings_total', 0)} in total",
                 health_path.is_file()),
        doc_link("measurement", "measurement.html",
                 "Can the numbers this unit reports be believed? Importance-weighted "
                 "measurement coverage, with the inventory it was computed from.",
                 measurement_path.is_file()),
        doc_link("theory", "theory.html",
                 "Does this unit's code express a coherent theory of its problem? A "
                 "panel of three judges, their evidence, and any dimension they read "
                 "differently.",
                 theory_path.is_file()),
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
        "MEASUREMENT_CARD": render_measurement_card(measurement),
        "THEORY_CARD": render_theory_card(theory),
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
    """Caveats for the portal. Must say the same thing health.html says.

    This page is where readers land, so a claim softened or overstated here
    reaches more people than the one on the health page.
    """
    parts = []
    # First, and unconditional on anything being ungraded. `ungraded` is the
    # *consequence* of a doctor failing only where that doctor was the sole
    # cover for a category; when a surviving doctor covers the same ground,
    # nothing is ungraded and every other caveat here stays silent — so the
    # portal showed a clean grade over a crashed doctor and said nothing. The
    # health page names the failure in its Coverage tab whether or not it cost
    # a category, and this page has to say the same thing.
    if meta.get("doctor_errors"):
        listed = "; ".join(f"<strong>{esc(name)}</strong> ({esc(reason)})"
                           for name, reason in meta["doctor_errors"].items())
        parts.append(f'<div class="callout bad">A doctor did not complete: {listed}. '
                     "Whatever it alone covered is unknown rather than clean, and where "
                     "another doctor covers the same ground the grade above rests on that "
                     "one alone. See the Coverage tab on the health page.</div>")
    if meta.get("ungraded"):
        listed = ", ".join(esc(rubric.CATEGORY_LABELS.get(k, k)) for k in meta["ungraded"])
        parts.append(f'<div class="callout warn">Ungraded: {listed}. Nothing measured those, '
                     "so the grade covers less than the full rubric.</div>")
    if meta.get("analyzers_skipped"):
        listed = grouped_by_doctor(meta["analyzers_skipped"])
        parts.append(f'<div class="callout warn">Detectors that were not run: {listed}. '
                     "Their categories are ungraded rather than clean.</div>")
    if meta.get("analyzer_errors"):
        listed = grouped_by_doctor(meta["analyzer_errors"])
        # Partial, not an upper bound — the category is dropped and the weights
        # renormalized, so restoring it could move the score either way.
        parts.append(f'<div class="callout bad">Detectors that did not complete: {listed}. '
                     "Their categories were dropped, so this score is partial: the missing "
                     "ones could have moved it either way.</div>")
    if meta.get("findings_out_of_scope"):
        parts.append(f'<div class="callout">{meta["findings_out_of_scope"]} finding(s) about '
                     "code outside this unit are not counted here.</div>")
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
