#!/usr/bin/env python3
"""Render a panel's verdicts into theory.html.

Every tab comes from the verdict files, so the letter and the rows under it
cannot disagree — there is no path by which a grade is typed onto the page by
hand.

The Theory tab is the one that matters most. The grade is a byproduct of having
tried to state the theory; the statement itself is what a next reader actually
needs, and it is precisely the artifact that gets lost when only the code is
handed over. A reader who ignores the letter and reads that tab has still got
the value of this document.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import theory_rubric as tr
from common import CODE_EXTENSIONS, esc, json_block, measure, render, warn

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def read_verdicts(paths: list[Path]) -> list[dict]:
    verdicts = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise tr.VerdictError(f"{path}: a verdict must be a JSON object")
        tr.validate_verdict(data)
        verdicts.append(data)
    return verdicts


def grade_class(grade: str) -> str:
    letter = (grade or "").strip()[:1].lower()
    return f"g-{letter}" if letter in "abcdf" else "g-none"


def panels(fragments: list[str]) -> tuple[str, str]:
    """Turn `<!-- tab: Title -->` fragments into code-visualization's markup."""
    nav, sections = [], []
    for fragment in fragments:
        header, _, body = fragment.partition("\n")
        title = header.removeprefix("<!-- tab:").removesuffix("-->").strip()
        tab_id = "tab-" + title.lower().replace(" ", "-")
        selected = "true" if not nav else "false"
        active = " active" if not sections else ""
        nav.append(f'<button role="tab" data-tab="{tab_id}" aria-selected="{selected}" '
                   f'aria-controls="{tab_id}">{esc(title)}</button>')
        sections.append(f'<section class="panel{active}" id="{tab_id}" role="tabpanel">\n'
                        f"{body}\n</section>")
    return "\n".join(nav), "\n".join(sections)


# --------------------------------------------------------------------------
# tabs
# --------------------------------------------------------------------------

def read_package_grade(name: str, path: Path) -> dict:
    """A package row for the root table, read back out of its own document."""
    blank = {"name": name, "score": None, "grade": tr.UNGRADED,
             "exempt": False, "disputed": [], "generated": False}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return blank
    match = re.search(r'id="theory-meta">(.*?)</script>', text, re.S)
    if not match:
        return blank
    try:
        meta = json.loads(match.group(1).replace("<\\/", "</"))
    except json.JSONDecodeError:
        return blank
    return {"name": name, "score": meta.get("score"),
            "grade": meta.get("grade", tr.UNGRADED),
            "exempt": bool(meta.get("exempt")),
            "disputed": meta.get("disputed") or [], "generated": True}


def render_package_table(packages: list[dict]) -> str:
    if not packages:
        return ""
    rows = []
    for item in packages:
        score = "—" if item["score"] is None else f"{item['score']:.1f}"
        if not item["generated"]:
            state = "not generated"
        elif item["exempt"]:
            state = "too small to warrant a theory"
        elif item["disputed"]:
            state = "panel disagreed on " + ", ".join(str(k) for k in item["disputed"])
        else:
            state = "graded"
        rows.append(f"<tr><td>{esc(item['name'])}</td>"
                    f'<td class="num">{score}</td>'
                    f'<td class="num">{esc(item["grade"])}</td>'
                    f"<td>{esc(state)}</td></tr>")
    return ("<h3>Packages</h3><p class=\"dim\">A package too small to warrant a theory is "
            "listed as such, not as passing.</p>"
            '<div class="tbl-wrap"><table><thead><tr><th>Package</th>'
            '<th class="num">Score</th><th class="num">Grade</th><th>State</th>'
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>")


def render_grade_tab(scored: dict, intro: str, size: dict, package_table: str = "") -> str:
    score = scored["score"]
    shown = "—" if score is None else f"{score:.1f}"

    if scored["exempt"]:
        headline = ('<div class="callout"><strong>Too small to warrant a theory.</strong> '
                    f"{esc(scored['exempt_reason'])}. Scored null — not zero, and not a "
                    "pass. Applying these gates to a handful of helpers is its own kind "
                    "of failure.</div>")
    else:
        headline = ""

    disputed = scored["disputed"]
    if disputed:
        names = ", ".join(esc(tr.DIMENSION_LABELS[key]) for key in disputed)
        dispute_note = ('<div class="callout warn"><strong>The panel disagreed on: </strong>'
                        f"{names}. Three careful readers reached different conclusions about "
                        "what this code models, which is a fact about the code rather than "
                        "about the readers. See the Disagreement tab.</div>")
    else:
        dispute_note = ""

    card = (
        f'<section class="gradecard {grade_class(scored["grade"])}">'
        f'<div><div class="letter">{esc(scored["grade"])}</div>'
        f'<div class="score">{shown} / 100</div></div>'
        '<div class="what"><h2>Theory</h2>'
        '<p class="dim">How well this unit&#39;s code expresses a coherent theory of the '
        "problem it solves, in Peter Naur&#39;s sense: can the model be mapped to the "
        "world, justified, and can it absorb the next requirement.</p>"
        "</div></section>"
    )

    return (
        '<!-- tab: Grade -->\n'
        + card + headline + dispute_note + intro +
        '<div class="callout warn"><strong>This is a reading, not a measurement.</strong> '
        "The other grades in this document set rest on something outside the grader — a "
        "detector's findings, an inventory with citations. This one is a judgment, and a "
        "model auditing abstractions is partly <em>circular</em>: the weakness that "
        "produces repetition-with-variants also evaluates whether the repetition was "
        "warranted. Three independent judges narrow that; they do not close it. Compare "
        "the rows and the evidence, not this letter — and letters from different models "
        "are not comparable at all.</div>"
        f'<div class="kpis"><div class="kpi accent"><div class="n">{scored["panel_size"]}</div>'
        '<div class="l">independent judges</div></div>'
        f'<div class="kpi"><div class="n">{size.get("files", 0)}</div>'
        '<div class="l">files</div></div>'
        f'<div class="kpi"><div class="n">{size.get("loc", 0)}</div>'
        '<div class="l">lines</div></div></div>' + package_table
    )


def render_theory_tab(verdicts: list[dict]) -> str:
    parts = ["<!-- tab: Theory -->\n",
             "<p>The theory is the deliverable; the grade is a byproduct of having tried "
             "to state it. Each judge wrote independently — where they describe the same "
             "system differently, that difference is the finding.</p>"]

    for index, verdict in enumerate(verdicts, start=1):
        rehearsals = []
        for item in verdict.get("rehearsals") or []:
            badge = "good" if item.get("verdict") == "extension" else "warn"
            citations = " ".join(f'<code class="floc">{esc(c)}</code>'
                                 for c in item.get("evidence") or [])
            rehearsals.append(
                f'<tr><td>{esc(item.get("requirement"))}</td>'
                f'<td><span class="badge {badge}">{esc(item.get("verdict"))}</span></td>'
                f'<td>{esc(item.get("why"))}{"<br>" + citations if citations else ""}</td></tr>'
            )
        parts.append(
            f'<div class="card"><h3>Judge {index}</h3>'
            f'<p><strong>Theory: </strong>{esc(verdict.get("theory"))}</p>'
            f'<p class="dim"><strong>Instead of: </strong>{esc(verdict.get("instead_of"))}</p>'
            '<div class="tbl-wrap"><table><thead><tr><th>Plausible next requirement</th>'
            "<th>Lands as</th><th>Why</th></tr></thead><tbody>"
            + "".join(rehearsals) + "</tbody></table></div></div>"
        )
    return "".join(parts)


def render_dimensions_tab(scored: dict) -> str:
    rows = []
    for row in scored["dimensions"]:
        spread = (f'<span class="badge bad">{row["spread"]} rungs apart</span>'
                  if row["disputed"] else f'{row["spread"]} rung(s)')
        judges = ", ".join(tr.LADDER_LABELS[step] for step in row["steps"])
        citations = "".join(f'<code class="floc">{esc(c)}</code><br>'
                            for c in row["evidence"])
        why = "<br>".join(esc(text) for text in row["rationales"] if text)
        rows.append(
            f'<tr><td><strong>{esc(row["label"])}</strong>'
            f'<br><span class="faint">{esc(row["question"])}</span></td>'
            f'<td class="num">{row["weight"]:.0f}</td>'
            f'<td>{esc(row["step_label"])}<br><span class="faint">{esc(judges)}</span></td>'
            f"<td>{spread}</td>"
            f"<td>{why}</td>"
            f"<td>{citations}</td></tr>"
        )
    return (
        '<!-- tab: Dimensions -->\n'
        "<p>The rows the letter was computed from. A step below <em>holds</em> must cite "
        "the evidence that lowered it — dispute the rows, not the grade.</p>"
        '<div class="tbl-wrap"><table><thead><tr><th>Dimension</th>'
        '<th class="num">Weight</th><th>Median step</th><th>Spread</th>'
        "<th>Why</th><th>Evidence</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table></div>"
    )


def render_disagreement_tab(scored: dict) -> str:
    disputed = [row for row in scored["dimensions"] if row["disputed"]]
    if not disputed:
        return ('<!-- tab: Disagreement -->\n<p class="dim">The three judges agreed within '
                "one rung on every dimension. That is weak evidence the code reads "
                "consistently — it is not evidence the grade is right.</p>")

    cards = []
    for row in disputed:
        readings = "".join(
            f"<li><strong>{esc(tr.LADDER_LABELS[step])}</strong> — {esc(why)}</li>"
            for step, why in zip(row["steps"], row["rationales"])
        )
        cards.append(
            f'<div class="card"><h3>{esc(row["label"])}</h3>'
            f'<p class="dim">{row["spread"]} rungs apart. The median '
            f"({esc(row['step_label'])}) is what scored; these are the readings behind "
            f"it.</p><ul>{readings}</ul></div>"
        )
    return (
        '<!-- tab: Disagreement -->\n'
        "<p>Three careful readers <strong>could not agree</strong> on these dimensions. "
        "Taking the median alone would hide the most interesting thing the panel learned: "
        "ambiguity about what the code models is a property of the code.</p>"
        + "".join(cards)
    )


def build_metadata(scored: dict, verdicts: list[dict], size: dict, args,
                   packages: list[dict] | None = None) -> dict:
    meta = {
        "schema": tr.DOCUMENT_SCHEMA,
        "scope": "repository" if args.root else "package",
        "package": args.name,
        "generated": args.generated or date.today().isoformat(),
        "commit": args.commit or "",
        "model": args.model or "",
        "panel_size": scored["panel_size"],
        "score": scored["score"],
        "grade": scored["grade"],
        "exempt": scored["exempt"],
        "exempt_reason": scored["exempt_reason"],
        "size": size,
        "theory": verdicts[0].get("theory", ""),
        "dimensions": scored["dimensions"],
        "disputed": scored["disputed"],
        "verdicts": verdicts,
    }
    if args.root:
        meta["packages"] = packages or []
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a panel of theory verdicts into theory.html.")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--verdict", action="append", default=[], dest="verdicts",
                        type=Path, metavar="FILE")
    parser.add_argument("--name", required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--root-dir", action="append", default=[], dest="root_dirs")
    parser.add_argument("--root", action="store_true")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--intro-file", type=Path, default=None)
    parser.add_argument("--commit", default="")
    parser.add_argument("--generated", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--template", type=Path, default=ASSETS / "template.html")
    parser.add_argument("--body", type=Path, default=ASSETS / "theory-body.html")
    parser.add_argument("--package", action="append", default=[], dest="packages",
                        metavar="NAME:PATH")
    args = parser.parse_args(argv)

    if args.packages and not args.root:
        parser.error("--package builds the repository roll-up table and needs --root")

    try:
        verdicts = read_verdicts(args.verdicts)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except tr.VerdictError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    relative_roots = list(args.root_dirs or ["."])
    roots = [args.repo / part for part in relative_roots]
    missing_roots = [r for r, path in zip(relative_roots, roots) if not path.exists()]
    size = measure(roots, CODE_EXTENSIONS)

    # `measure` returns {files: 0, loc: 0} for a path that is not there, and
    # says nothing. build_health.py:944 guards the same drift — "a clean report
    # over a root that no longer exists scored a confident A+" — and here it is
    # worse in two ways. The grade comes from the verdicts, not from the size,
    # so nothing about measuring nothing lowers the letter: a typo'd --root-dir
    # renders A+ (100.0) over "0 files · 0 lines". And the size test is the only
    # non-judgment half of the exemption, so an unmeasurable unit with two
    # trivial votes is exempted on a pure vote — collapsing the two gates that
    # exist because either alone is gameable.
    #
    # Refusing, rather than ungrading as build_health does. Its ungraded shape
    # is score null / grade "—", and here that shape is already spoken for: it
    # is what "too small to warrant a theory" looks like, so an ungraded theory
    # page would read as an exemption for a package that may be enormous. There
    # is no honest page to write, so none is written — the same answer this
    # script already gives a verdict whose grade cannot be argued with. Any
    # missing root is enough, not only all of them: a partially-measured unit
    # sizes part of the code while the panel judged all of it.
    if missing_roots or size["files"] == 0:
        reason = (f"these roots do not exist: {', '.join(missing_roots)}" if missing_roots
                  else f"no source files under {', '.join(relative_roots)}")
        print(f"error: {reason} — the panel judged code this run cannot see, so the size "
              "gate that is half the exemption test, and the file and line counts under "
              "the letter, would be computed over nothing. Refusing to write a page: an "
              "empty unit would render either a confident grade or a null that reads as "
              "'too small to warrant a theory'. Check --root-dir against the package map.",
              file=sys.stderr)
        return 2

    try:
        # The floor is never offered at repo scope: a repository of individually
        # trivial packages still has a system-level question worth asking.
        scored = tr.score_panel(verdicts, size, allow_exemption=not args.root)
    except tr.VerdictError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    intro = ""
    if args.intro_file:
        try:
            intro = args.intro_file.read_text(encoding="utf-8")
        except OSError as exc:
            warn(f"{args.intro_file}: {exc}")

    packages = []
    for spec in args.packages:
        label, _, location = spec.partition(":")
        packages.append(read_package_grade(label.strip(), Path(location.strip())))

    body = render(args.body.read_text(encoding="utf-8"), {
        "TAB_GRADE": render_grade_tab(scored, intro, size, render_package_table(packages)),
        "TAB_THEORY": render_theory_tab(verdicts),
        "TAB_DIMENSIONS": render_dimensions_tab(scored),
        "TAB_DISAGREEMENT": render_disagreement_tab(scored),
    })
    # [1:] drops everything before the first marker — the scaffold's leading
    # comment is not a tab, and treating it as one makes it a bogus first panel
    # that renders active and hides the grade card.
    fragments = [f"<!-- tab:{part}" for part in body.split("<!-- tab:")[1:] if part.strip()]
    nav, sections = panels(fragments)

    meta = build_metadata(scored, verdicts, size, args, packages)
    sections += (f'\n<script type="application/json" id="theory-meta">'
                 f"{json_block(meta)}</script>")

    scope = "repository" if args.root else "package"
    page = render(args.template.read_text(encoding="utf-8"), {
        "DOC_TITLE": esc(f"{args.name} — Code Theory"),
        "DOC_LABEL": "CODE THEORY",
        "DOC_SUBTITLE": esc(args.subtitle or
                            f"Does the {args.name} {scope} express a coherent theory?"),
        "DOC_META": esc(" · ".join(part for part in (
            f"generated {meta['generated']}", meta["commit"],
            f"{scored['panel_size']} judges", meta["model"]) if part)),
        "TABS_NAV": nav,
        "TABS_PANELS": sections,
        "DOC_BODY": "",
        "DOC_FOOTER": ("Generated by code-overview. This grade is a judgment, not a "
                       "measurement — read the Dimensions tab's evidence rather than the "
                       "letter, and do not compare letters across models."),
    })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    shown = "—" if scored["score"] is None else f"{scored['score']:.1f}"
    print(f"wrote {args.out} — {scored['grade']} ({shown})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
