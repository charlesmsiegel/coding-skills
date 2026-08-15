#!/usr/bin/env python3
"""The health document's presentation layer: findings in, HTML fragments out.

Split out of build_health.py, which had grown past the point where the
arithmetic and the markup could be read separately. Everything here is
pure — it takes already-scored data and returns a string — so the grade
cannot be changed by anything in this file, and a rendering change cannot
silently move a number.

The severity vocabulary lives here with it. `SEVERITY_ORDER` ranks a
finding for both sorting and counting, and `line_number` coerces the line
field to something sortable; build_health.py imports both rather than
keeping a second copy, so the order findings are scored in and the order
they are displayed in cannot drift apart.

Imported by build_health.py and build_summary.py. It imports neither, and
must not: the dependency runs one way, page builders onto renderers.
"""

from __future__ import annotations

import rubric
from common import esc, grouped_by_doctor

SEVERITY_ORDER = ("high", "medium", "low")
SEVERITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def grade_class(grade: str) -> str:
    if not grade or grade == rubric.UNGRADED:
        return "g-none"
    return "g-" + grade[0].lower()


def line_number(finding: dict) -> int:
    """A finding's line as something sortable.

    `.get("line", 0)` is not enough: a producer that emits `"line": null` for a
    file-level finding returns None, and one None beside one int aborts the
    whole page with a TypeError deep in `sorted`. `--covers` exists precisely so
    tools this skill has never seen can be graded, so their fields cannot be
    assumed well-typed.
    """
    try:
        return int(finding.get("line") or 0)
    except (TypeError, ValueError):
        return 0


def render_category_rows(rows: list[dict]) -> str:
    out = []
    for row in rows:
        counts = row["findings"]
        cls = grade_class(row["grade"]) + ("" if row["graded"] else " ungraded")
        if row["graded"]:
            width = max(1.5, row["score"])
            bar = f'<div class="bar"><i style="width:{width:.1f}%"></i></div>'
            # The stored one-decimal value, not a re-rounded integer: 92.6 is
            # graded A- and must not be shown as "93", which reads as an A.
            score = f'{row["score"]:.1f}'
            tip = (f'{counts["total"]} finding(s) · {row["density"]:.2f} weighted per KLOC '
                   f'· scores 50 at {row["half_life"]:.0f}')
        else:
            bar = '<div class="bar"></div>'
            score = "—"
            tip = "not measured by this analysis"
        out.append(
            f'<div class="catrow {cls}" title="{esc(tip)}">'
            f'<div class="cname">{esc(row["label"])}'
            f'<small>weight {row["weight"]:.0f}% · {counts["total"]} finding(s)</small></div>'
            f"{bar}"
            f'<div class="cscore">{score}</div>'
            f'<div class="cgrade">{esc(row["grade"])}</div>'
            f"</div>"
        )
    return "\n".join(out)


def render_findings_summary(findings: list[dict]) -> str:
    counts = {sev: sum(1 for f in findings if f.get("severity") == sev) for sev in SEVERITY_ORDER}
    kpis = [f'<div class="kpi accent"><div class="n">{len(findings)}</div><div class="l">total</div></div>']
    for sev, cls in (("high", "bad"), ("medium", "warn"), ("low", "")):
        kpis.append(f'<div class="kpi {cls}"><div class="n">{counts[sev]}</div>'
                    f'<div class="l">{SEVERITY_ICONS[sev]} {sev}</div></div>')
    return '<div class="kpis">' + "".join(kpis) + "</div>"


def render_top_findings(items: list[dict]) -> str:
    if not items:
        return '<p class="dim">No findings to list.</p>'
    rows = []
    for item in items:
        location = f'{item["file"]}:{item["line"]}' if item["file"] else "—"
        suggestion = (f'<br><span class="faint">→ {esc(item["suggestion"])}</span>'
                      if item["suggestion"] else "")
        rows.append(
            f'<tr><td class="sev-{esc(item["severity"])}">{SEVERITY_ICONS.get(item["severity"], "")} '
            f'{esc(item["severity"])}</td>'
            f'<td><code class="ftype">{esc(item["type"])}</code></td>'
            f'<td class="cat">{esc(rubric.CATEGORY_LABELS.get(item["category"], item["category"]))}</td>'
            f'<td><code class="floc">{esc(location)}</code></td>'
            f'<td>{esc(item["description"])}{suggestion}</td></tr>'
        )
    return ('<div class="tbl-wrap"><table><thead><tr><th>Severity</th><th>Type</th>'
            "<th>Category</th><th>Location</th><th>Finding</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def render_by_type(findings: list[dict], limit: int = 25) -> str:
    """Counts per finding type, carrying the category each was actually scored under.

    The category has to come from the finding, not be re-derived from the type
    token: `bare_except` arrives from the `code_smells` detector and is scored
    under Complexity, but the token alone matches no rubric keyword and would
    re-derive as Hygiene — so a rebuilt category would make this table
    contradict the scores directly above it.
    """
    counts: dict[str, int] = {}
    categories: dict[str, str] = {}
    for finding in findings:
        name = rubric.finding_type(finding)
        counts[name] = counts.get(name, 0) + 1
        categories.setdefault(
            name, finding.get("_rubric_category") or rubric.categorize(finding)[0])
    if not counts:
        return ""
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    rows = "".join(
        f'<tr><td><code class="ftype">{esc(name)}</code></td>'
        f'<td class="cat">{esc(rubric.CATEGORY_LABELS.get(categories[name], ""))}</td>'
        f'<td class="num">{count}</td></tr>'
        for name, count in ordered
    )
    more = ("" if len(counts) <= limit
            else f'<p class="faint">{len(counts) - limit} further type(s) not shown.</p>')
    return ('<details><summary>Findings by type</summary><div class="body">'
            '<div class="tbl-wrap"><table><thead><tr><th>Type</th><th>Category</th>'
            '<th class="num">Count</th></tr></thead><tbody>'
            + rows + "</tbody></table></div>" + more + "</div></details>")


def render_package_table(packages: list[dict], links: dict[str, str]) -> str:
    if not packages:
        return ""
    rows = []
    for package in packages:
        name = package.get("package") or package.get("name", "?")
        href = links.get(name, "")
        label = f'<a href="{esc(href)}">{esc(name)}</a>' if href else esc(name)
        score = package.get("score")
        grade = package.get("grade", rubric.UNGRADED)
        size = package.get("size", {})
        rows.append(
            f"<tr><td>{label}</td>"
            f'<td><span class="badge neutral">{esc(package.get("language") or "?")}</span></td>'
            f'<td class="num">{size.get("files", "—")}</td>'
            f'<td class="num">{size.get("loc", "—")}</td>'
            f'<td class="num">{"—" if score is None else f"{score:.1f}"}</td>'
            f'<td class="cgrade {grade_class(grade)}" style="color:var(--grade);'
            f'font-family:var(--mono);font-weight:600">{esc(grade)}</td>'
            f'<td class="num">{"not generated" if package.get("generated") is False else package.get("findings_total", "—")}</td></tr>'
        )
    return ("<h2>Packages</h2>"
            '<div class="tbl-wrap"><table><thead><tr><th>Package</th><th>Language</th>'
            '<th class="num">Files</th><th class="num">Lines</th><th class="num">Score</th>'
            "<th>Grade</th><th class=\"num\">Findings</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def render_candidates(candidates: list[dict]) -> str:
    """Leads, rendered so nobody mistakes one for a defect.

    A candidate carries no fix and asserts nothing. The two things that keep it
    honest on a page whose headline is a grade are the statement that it did not
    affect that grade, and `also_caused_by` — the specific ways a healthy
    codebase produces the same observation, so the reader can rule them out
    instead of taking the tool's word for it.
    """
    note = ('<div class="callout warn"><strong>These did not affect the grade.</strong> '
            "A candidate is an unverified lead, not a defect: it names what was observed "
            "and the ways healthy code produces the same observation, and it deliberately "
            "carries no fix. Confirm one before acting on it.</div>")
    if not candidates:
        return (note + '<p class="dim">No candidates were reported for this unit.</p>')

    rows = []
    for item in sorted(candidates, key=lambda c: (SEVERITY_ORDER.index(c.get("severity", "medium"))
                                                  if c.get("severity") in SEVERITY_ORDER else 1,
                                                  str(c.get("file")), line_number(c))):
        benign = "".join(f"<li>{esc(reason)}</li>"
                         for reason in item.get("also_caused_by") or [])
        location = f'{item.get("file", "")}:{line_number(item)}'
        rows.append(
            f'<tr><td><code class="ftype">{esc(item.get("smell_type", "candidate"))}</code></td>'
            f'<td><code class="floc">{esc(location)}</code></td>'
            f'<td>{esc(item.get("description", ""))}'
            f'<div class="faint">Also caused by:<ul>{benign}</ul></div></td>'
            f'<td>{esc(item.get("doctor", ""))}</td></tr>'
        )
    return (note + '<div class="tbl-wrap"><table><thead><tr><th>Type</th><th>Location</th>'
            "<th>Observed · and what else produces it</th><th>Reported by</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def render_coverage(meta: dict) -> str:
    """What was measured, what was not, and why — beside the grade that used it."""
    parts = []

    doctors = meta.get("doctors") or []
    if doctors:
        chips = "".join(f'<span class="badge accent">{esc(name)}</span> ' for name in doctors)
        parts.append(f"<h3>Doctors that ran</h3><p>{chips}</p>")
    else:
        parts.append('<div class="callout bad">No doctor contributed to this page, so '
                     "nothing on it was measured. The grade is a placeholder.</div>")

    failures = meta.get("doctor_errors") or {}
    if failures:
        items = "".join(f"<li><strong>{esc(name)}</strong>: {esc(reason)}</li>"
                        for name, reason in failures.items())
        parts.append('<div class="callout bad"><strong>A doctor failed.</strong> Whatever it '
                     "alone covered is unknown, not clean — those categories are ungraded "
                     f"rather than scored from the surviving report.<ul>{items}</ul></div>")

    for doctor, block in (meta.get("completeness") or {}).items():
        rows = []
        for key, detail in (block or {}).items():
            if isinstance(detail, dict):
                verdict = detail.get("adequate")
                state = "adequate" if verdict is True else ("incomplete" if verdict is False
                                                            else "not stated")
                numbers = ", ".join(f"{k}: {v}" for k, v in detail.items() if k != "adequate")
            else:
                # Everything code-doctor puts in a completeness block is a
                # plain *string* — `detectors_failed`, `merge_state`
                # ("git unavailable — conflict markers reported as
                # candidates"), the files_unreadable accounting. Skipping
                # every non-dict value dropped exactly the class of caveat
                # this tab exists to name: it reached the envelope, survived
                # into the hidden JSON metadata, and appeared nowhere a reader
                # looks. A note carries no adequate/inadequate verdict of its
                # own, so it is labelled as the note it is rather than given
                # one it never claimed.
                state = "note"
                numbers = (", ".join(str(item) for item in detail)
                           if isinstance(detail, (list, tuple, set))
                           else ("" if detail is None else str(detail)))
            rows.append(f"<tr><td>{esc(key)}</td><td>{esc(state)}</td>"
                        f"<td>{esc(numbers)}</td></tr>")
        if rows:
            parts.append(f"<h3>Evidence completeness · {esc(doctor)}</h3>"
                         '<div class="tbl-wrap"><table><thead><tr><th>Evidence</th>'
                         "<th>Verdict</th><th>Detail</th></tr></thead>"
                         f"<tbody>{''.join(rows)}</tbody></table></div>")

    ungraded = meta.get("ungraded") or []
    if ungraded:
        names = ", ".join(esc(rubric.CATEGORY_LABELS.get(key, key)) for key in ungraded)
        parts.append('<div class="callout warn"><strong>Ungraded: </strong>'
                     f"{names}. Nothing measured these, so they are dropped from the mean "
                     "rather than counted as zero or as a hundred.</div>")

    return "".join(parts)


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


def render_caveats(errors: dict[str, dict[str, str]], skipped: dict[str, list[str]],
                   unmapped: list[str], ungraded: list[str], notes: list[str],
                   out_of_scope: int, roots: list[str], duplicates: int = 0) -> str:
    parts = []
    if duplicates:
        parts.append(f'<div class="callout">{duplicates} finding(s) were reported by more than '
                     'one doctor at the same file, line and type, and were merged — keeping the '
                     'higher severity — so one defect is charged once.</div>')
    if out_of_scope:
        listed = ", ".join(f"<code>{esc(r)}</code>" for r in roots)
        parts.append(f'<div class="callout">{out_of_scope} finding(s) in the report were about '
                     f"code outside {listed} and are not counted here. That keeps the findings "
                     "and the lines they are divided by describing the same code; if some of "
                     "that code should be graded, add it to the package map.</div>")
    if errors:
        listed = grouped_by_doctor(errors)
        # Not "an upper bound": the affected category is dropped and the weights
        # renormalized, so restoring it could move the overall either way —
        # up if it would have scored well, down if badly. Saying "upper bound"
        # would give the reader a direction of error that is simply wrong.
        parts.append('<div class="callout bad"><strong>Detectors that did not complete:</strong> '
                     f"{listed}. A zero count in those categories means <em>unknown</em>, "
                     "not clean, so they were dropped from the grade — which makes this score "
                     "partial rather than high or low: the missing categories could have moved "
                     "it either way.</div>")
    if skipped:
        listed = grouped_by_doctor(skipped)
        parts.append('<div class="callout warn"><strong>Detectors that were not run:</strong> '
                     f"{listed}. Their rubric categories are ungraded rather than clean.</div>")
    if ungraded:
        listed = ", ".join(esc(rubric.CATEGORY_LABELS.get(k, k)) for k in ungraded)
        parts.append('<div class="callout warn"><strong>Ungraded categories:</strong> '
                     f"{listed}. Nothing measured them, so they were dropped from the weighted "
                     "mean and the remaining weights renormalized.</div>")
    if unmapped:
        listed = ", ".join(f"<code>{esc(t)}</code>" for t in unmapped[:20])
        parts.append('<div class="callout"><strong>Finding types outside the rubric:</strong> '
                     f"{listed}. They were counted under Dependencies &amp; Hygiene; extend the "
                     "rubric if any of them belongs elsewhere.</div>")
    for note in notes:
        parts.append(f'<div class="callout">{esc(note)}</div>')
    return ("<h2>Caveats</h2>" + "".join(parts)) if parts else ""


def headline_badges(meta: dict) -> str:
    badges = []
    if meta.get("language"):
        badges.append(f'<span class="badge neutral">{esc(meta["language"])}</span>')
    if meta.get("doctor"):
        badges.append(f'<span class="badge accent">{esc(meta["doctor"])}</span>')
    size = meta.get("size", {})
    badges.append(f'<span class="badge neutral">{size.get("files", 0)} files · '
                  f'{size.get("loc", 0)} lines</span>')
    total = meta.get("findings_total", 0)
    cls = "good" if total == 0 else ("bad" if total > 200 else "warn")
    badges.append(f'<span class="badge {cls}">{total} finding(s)</span>')
    return " ".join(badges)

