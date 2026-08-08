#!/usr/bin/env python3
"""Render an audited inventory into measurement.html.

Every tab comes from the inventory file, so the score in the grade card and the
table it was computed from cannot disagree — there is no path by which a number
is typed onto the page by hand.

The Inventory tab is the load-bearing one. This score's denominator is an
authored judgment: somebody decided which things are measurable and how much
each matters. A reader who cannot see those rows can only accept or reject the
letter, and a letter nobody can dispute is a letter nobody should trust. So the
page ships the whole table — every row with its weight, its credit, the finding
that set that credit, the N it was computed over, and the file:line it came
from.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date
from pathlib import Path

import rubric

ASSETS = Path(__file__).resolve().parent.parent / "assets"

SEVERITY_CLASS = {"high": "bad", "medium": "warn", "low": "neutral"}


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def opt(value, fallback: str = "—") -> str:
    """An optional string, escaped, with `None` never reaching the page.

    `esc(entry.get("credit_reason"))` on a row that omits the key renders the
    literal word "None" in the Why column, which reads as an authored reason
    rather than a missing one. The validator now requires the fields that
    matter, but a renderer that turns a hole into confident-looking text is the
    wrong failure mode for a document whose whole job is not overstating its
    evidence, so every optional string goes through here.
    """
    text = "" if value is None else str(value).strip()
    return esc(text if text else fallback)


def json_block(data) -> str:
    """Serialize for embedding in a <script> block.

    `</` is escaped because a `</script>` inside a JSON string would end the
    block early and spill the rest of the payload into the document body.
    """
    return json.dumps(data, separators=(",", ":"), sort_keys=False).replace("</", "<\\/")


def fill(template: str, slots: dict[str, str]) -> str:
    for key, value in slots.items():
        template = template.replace(f"<!--{key}-->", value)
    return template


def grade_class(grade: str) -> str:
    letter = (grade or "").strip()[:1].lower()
    return f"g-{letter}" if letter in "abcdf" else "g-none"


def read_inventory(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise rubric.InventoryError("inventory must be a JSON object")
    # The schema key is not decoration. Every rule the validator enforces —
    # credit is a ladder, unmeasurable rows stay in the denominator, evidence
    # is file:line you opened — is a contract the author accepted by naming
    # this version. A file naming another version, or naming none, was written
    # against rules this builder does not know, so the score it would produce
    # is a claim about nothing. INVENTORY_SCHEMA existed and was never checked.
    schema = data.get("schema")
    if schema != rubric.INVENTORY_SCHEMA:
        raise rubric.InventoryError(
            f"inventory must declare a 'schema' of {rubric.INVENTORY_SCHEMA!r}, "
            f"got {schema!r}"
        )
    rows = data.get("rows") or []
    findings = data.get("findings") or []
    if not isinstance(rows, list) or not isinstance(findings, list):
        raise rubric.InventoryError("`rows` and `findings` must both be lists")
    rubric.validate(rows, findings)
    return {"subject": str(data.get("subject") or ""), "rows": rows, "findings": findings,
            "not_audited": data.get("not_audited") or []}


# --------------------------------------------------------------------------
# tabs
# --------------------------------------------------------------------------

def render_score_tab(scored: dict, rows: list[dict], intro: str,
                     not_audited: list, package_table: str = "",
                     out_of_scope: int = 0, scoped: bool = False) -> str:
    score = scored["score"]
    shown = "—" if score is None else f"{score:.1f}"
    gates = scored["by_importance"].get("3") or {}
    gate_share = gates.get("share")

    if score is None:
        headline = ('<div class="callout warn"><strong>No measurement content here.</strong> '
                    "Nothing in this unit produces a quality, accuracy or score number, so "
                    "there is nothing to grade. That is an honest null, not a pass.</div>")
    else:
        gate_line = ("no ship-gating numbers were inventoried"
                     if gate_share is None
                     else f"{gate_share:.0f}% of the weight that gates a ship decision is "
                          "soundly measured")
        headline = (f'<div class="callout"><strong>{esc(gate_line)}.</strong> '
                    "The headline score averages every importance level; this line is the "
                    "cut that decides releases.</div>")

    kpis = "".join([
        f'<div class="kpi accent"><div class="n">{len(rows)}</div>'
        '<div class="l">measurable things</div></div>',
        f'<div class="kpi"><div class="n">{scored["weight_measured"]:.2f}</div>'
        '<div class="l">weight measured</div></div>',
        f'<div class="kpi"><div class="n">{scored["weight_total"]:.0f}</div>'
        '<div class="l">weight total</div></div>',
    ])

    # Dropped rows used to exist only on stderr and in the hidden metadata, so
    # a three-row inventory with two unmeasured things could render A+ / 100
    # here with nothing on the page saying what the 100 was over. The count
    # sits beside the "measurable things" KPI it qualifies.
    scope_note = ""
    if out_of_scope:
        plural = "" if out_of_scope == 1 else "s"
        verb = "was" if out_of_scope == 1 else "were"
        scope_note = (
            f'<p class="dim"><strong>{out_of_scope} further row{plural}</strong> in this '
            f"audit {verb} defined outside this unit and {verb} left out of the score "
            "above. They are not unmeasured — they are scored on the page of the "
            "package that defines them, not here.</p>"
        )

    breakdown_parts = []
    for level, bucket in scored["by_importance"].items():
        # Precomputed rather than nested inside the f-string: nested same-type
        # quotes in an f-string expression are a syntax error before 3.12, and
        # this repo's floor is 3.11.
        share = "—" if bucket["share"] is None else f"{bucket['share']:.0f}%"
        breakdown_parts.append(
            f"<tr><td>{esc(rubric.IMPORTANCE_LABELS[int(level)])}</td>"
            f'<td class="num">{bucket["rows"]}</td>'
            f'<td class="num">{bucket["total"]:.0f}</td>'
            f'<td class="num">{bucket["measured"]:.2f}</td>'
            f'<td class="num">{share}</td></tr>'
        )
    breakdown_rows = "".join(breakdown_parts)

    gaps = ""
    if not_audited:
        items = "".join(f"<li>{esc(item)}</li>" for item in not_audited)
        # Labelled, not filtered. These entries are repository-wide by nature —
        # a dashboard nobody had access to is not any one package's gap — and
        # dropping them from a package page would hide a real gap. So every
        # page prints them and a scoped page says whose gap they are.
        if scoped:
            heading = "Not audited (whole repository)"
            lead = ("A silent gap reads as a clean bill of health, so it is listed "
                    "instead. This list covers the whole repository, not just this "
                    "unit: these are things the audit never reached anywhere.")
        else:
            heading = "Not audited"
            lead = ("A silent gap reads as a clean bill of health, so it is listed "
                    "instead.")
        gaps = (f"<h3>{esc(heading)}</h3><p class=\"dim\">{lead}</p><ul>{items}</ul>")

    return (
        '<!-- tab: Score -->\n'
        f'<section class="gradecard {grade_class(scored["grade"])}">'
        f'<div><div class="letter">{esc(scored["grade"])}</div>'
        f'<div class="score">{shown} / 100</div></div>'
        '<div class="what"><h2>Measurement coverage</h2>'
        '<p class="dim">Importance-weighted measured things over measurable things. '
        'It says how much of what matters is actually measured — not whether the code '
        'is correct.</p></div></section>'
        f"{headline}{intro}"
        f'<div class="kpis">{kpis}</div>{scope_note}'
        "<h3>By importance</h3>"
        '<div class="tbl-wrap"><table><thead><tr><th>Importance</th>'
        '<th class="num">Things</th><th class="num">Weight</th>'
        '<th class="num">Measured</th><th class="num">Share</th></tr></thead>'
        f"<tbody>{breakdown_rows}</tbody></table></div>{gaps}{package_table}"
    )


def render_inventory_tab(rows: list[dict]) -> str:
    if not rows:
        return ('<!-- tab: Inventory -->\n<p class="dim">Nothing measurable was '
                "inventoried in this unit.</p>")

    parts = []
    for entry in rows:
        # Every branch is computed before the f-string. Nested same-type quotes
        # and backslashes inside f-string expressions are both 3.12+ syntax,
        # and this repo runs on 3.11.
        formula = (f'<br><code class="mono">{esc(entry.get("formula"))}</code>'
                   if entry.get("formula") else "")
        finding = (f'<br><span class="badge bad">{esc(entry["finding"])}</span>'
                   if entry.get("finding") else "")
        n_shown = "—" if entry.get("n") is None else str(entry["n"])
        if entry.get("n_total") not in (None, ""):
            n_shown += f" / {esc(entry['n_total'])}"
        citations = "".join(
            f'<code class="floc">{esc(item)}</code><br>'
            for item in entry.get("evidence") or []
        )
        parts.append(
            "<tr>"
            f"<td><strong>{esc(entry['name'])}</strong>{formula}</td>"
            f'<td>{esc(rubric.IMPORTANCE_LABELS[int(entry["importance"])])}'
            f'<br><span class="faint">{opt(entry.get("importance_reason"))}</span></td>'
            f'<td class="num">{float(entry["credit"]):.2f}</td>'
            f'<td>{opt(entry.get("credit_reason"))}{finding}</td>'
            f'<td class="num">{n_shown}</td>'
            f'<td>{opt(entry.get("consumer"), "nobody reads it")}</td>'
            f"<td>{citations}</td>"
            "</tr>"
        )
    body = "".join(parts)
    # The credit column is a bare number to anyone who has not memorised the
    # ladder, and a reader who cannot tell 0.25 from 0.0 cannot dispute a row —
    # which is the only thing that makes the score arguable.
    legend = "".join(
        f'<li><code class="mono">{step:.2f}</code> — {esc(rubric.CREDIT_LABELS[step])}</li>'
        for step in rubric.CREDIT_STEPS
    )
    return (
        '<!-- tab: Inventory -->\n'
        "<p>Every measurable thing this audit found, with the weight and credit that "
        "produced the score. The denominator is a judgment — dispute the rows, not the "
        "letter.</p>"
        '<p class="dim">Credit is set by the worst confirmed finding against the thing:'
        f"</p><ul class=\"dim\">{legend}</ul>"
        '<div class="tbl-wrap"><table><thead><tr><th>Thing</th><th>Importance</th>'
        '<th class="num">Credit</th><th>Why</th><th class="num">N</th><th>Consumer</th>'
        f"<th>Evidence</th></tr></thead><tbody>{body}</tbody></table></div>"
    )


def render_findings_tab(findings: list[dict]) -> str:
    if not findings:
        return ('<!-- tab: Findings -->\n<p class="dim">No confirmed findings against the '
                "measurement itself. That is not the same as the numbers being right — see "
                "the Unmeasurable tab for what nothing here could check.</p>")

    order = {"high": 0, "medium": 1, "low": 2}
    ranked = sorted(findings, key=lambda f: order.get(str(f.get("severity")), 3))
    cards = "".join(
        f'<div class="card"><div><span class="badge '
        f'{SEVERITY_CLASS.get(str(item.get("severity")), "neutral")}">'
        f'{opt(item.get("severity"), "unrated")}</span> '
        f"<code>{opt(item.get('id'), 'unnamed')}</code></div>"
        f"<h3>{opt(item.get('title'), 'untitled finding')}</h3>"
        f"<p>{opt(item.get('detail'), 'no detail was written for this finding')}</p>"
        f'<p class="dim"><strong>Blast radius:</strong> '
        f'{opt(item.get("blast_radius"), "not stated")}</p>'
        + "".join(f'<code class="floc">{esc(cite)}</code> ' for cite in item.get("evidence") or [])
        + "</div>"
        for item in ranked
    )
    return ('<!-- tab: Findings -->\n<p>Ranked by likelihood × blast radius on the decisions '
            f"the number drives, not by how clever the finding is.</p>{cards}")


def render_unmeasurable_tab(rows: list[dict]) -> str:
    gaps = [entry for entry in rows if entry.get("status") == "unmeasurable"]
    if not gaps:
        return ('<!-- tab: Unmeasurable -->\n<p class="dim">Nothing was found that today\'s '
                "data structurally cannot measure.</p>")
    body = "".join(
        f"<tr><td><strong>{esc(entry['name'])}</strong></td>"
        f'<td>{esc(rubric.IMPORTANCE_LABELS[int(entry["importance"])])}</td>'
        f"<td>{opt(entry.get('unmeasurable_reason'))}</td></tr>"
        for entry in gaps
    )
    return (
        '<!-- tab: Unmeasurable -->\n'
        "<p>Structurally unmeasurable with today's data — recall with no gold set, "
        "calibration with no outcomes, causal effect with no control arm. These are "
        "<strong>not defects</strong>, and they stay in the denominator on purpose: "
        "dropping them is how silence gets read as success.</p>"
        '<div class="tbl-wrap"><table><thead><tr><th>Thing</th><th>Importance</th>'
        f"<th>What today's data cannot supply</th></tr></thead><tbody>{body}</tbody>"
        "</table></div>"
    )


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


def build_metadata(inventory: dict, scored: dict, name: str, args,
                   out_of_scope: int, packages: list[dict]) -> dict:
    meta = {
        "schema": rubric.DOCUMENT_SCHEMA,
        "scope": "repository" if args.root else "package",
        "package": name,
        "generated": args.generated or date.today().isoformat(),
        "commit": args.commit or "",
        "score": scored["score"],
        "grade": scored["grade"],
        "weight_total": scored["weight_total"],
        "weight_measured": scored["weight_measured"],
        "by_importance": scored["by_importance"],
        "rows": inventory["rows"],
        "findings": inventory["findings"],
        "not_audited": inventory["not_audited"],
        "rows_out_of_scope": out_of_scope,
    }
    if args.root:
        meta["packages"] = packages
    return meta


TRAILING_LINE_NUMBER = re.compile(r":\d+$")


def defining_path(entry: dict, repo: Path) -> str:
    """Where the measurable thing is *defined* — the first evidence citation.

    A metric defined in evals/ that scores a service's output cites both. It
    belongs to whoever defines it: assigning it to the service would give two
    packages the same row and double-count it in the repository denominator.

    The trailing line number is stripped *before* backslash normalization,
    because a raw split on the first colon truncates a Windows absolute path
    at its drive letter (`C:\\repo\\src\\billing\\metrics.py:10` -> `"C"`) and
    the row silently vanishes from every scope. An absolute citation that
    falls under `repo` is made repo-relative so it can still match; one
    outside `repo` is left absolute, so it matches no scope instead of being
    silently mangled into one.
    """
    for citation in entry.get("evidence") or []:
        text = str(citation).strip()
        if not text:
            continue
        text = TRAILING_LINE_NUMBER.sub("", text)
        text = text.replace("\\", "/").removeprefix("./")
        try:
            candidate = Path(text)
            if candidate.is_absolute():
                text = str(candidate.resolve().relative_to(repo.resolve())).replace("\\", "/")
        except (OSError, ValueError):
            pass  # a malformed or out-of-repo citation is left as-is, not fatal
        return text
    return ""


def in_scope(entry: dict, scopes: list[str], repo: Path) -> bool:
    if not scopes:
        return True
    path = defining_path(entry, repo)
    return any(path == scope or path.startswith(scope.rstrip("/") + "/")
               for scope in scopes)


def read_package_grade(name: str, path: Path) -> dict:
    """A package row for the root table, read back out of its own document.

    DUPLICATED READER, ON PURPOSE. The block this parses — a
    `<script type="application/json" id="measurement-meta">` with `</` written
    as `<\\/` — is a cross-skill contract: code-overview's build_summary.py
    reads the same block out of the same file through
    `skills/code-overview/scripts/common.py:read_meta`. The two skills are
    zipped and installed separately and cannot import one another, so the
    reader is copied rather than shared, exactly like the four other deliberate
    copies this repo pins in tests.

    What must not diverge is the *behaviour*, and it had: `read_meta` warns
    when the block is present but unparseable, this one returned a blank row in
    silence — so a corrupted document was tabled as "not generated", which is
    what a package nobody ever built also looks like. Same input, same
    complaint, now. `tests/science_investigation/test_meta_block_contract.py`
    is the pin.
    """
    blank = {"name": name, "score": None, "grade": rubric.UNGRADED,
             "rows": 0, "generated": False}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return blank
    match = re.search(r'id="measurement-meta">(.*?)</script>', text, re.S)
    if not match:
        return blank
    try:
        meta = json.loads(match.group(1).replace("<\\/", "</"))
    except json.JSONDecodeError as exc:
        print(f"warning: {path} has a measurement-meta block that is not valid JSON: {exc}",
              file=sys.stderr)
        return blank
    return {"name": name, "score": meta.get("score"),
            "grade": meta.get("grade", rubric.UNGRADED),
            "rows": len(meta.get("rows") or []), "generated": True}


def render_package_table(packages: list[dict]) -> str:
    if not packages:
        return ""
    parts = []
    for item in packages:
        score = "—" if item["score"] is None else f"{item['score']:.1f}"
        if not item["generated"]:
            state = "not generated"
        elif item["score"] is None:
            state = "no measurement content"
        else:
            state = "graded"
        parts.append(
            f"<tr><td>{esc(item['name'])}</td>"
            f'<td class="num">{score}</td>'
            f'<td class="num">{esc(item["grade"])}</td>'
            f'<td class="num">{item["rows"]}</td>'
            f"<td>{state}</td></tr>"
        )
    body = "".join(parts)
    return ("<h3>Packages</h3><p class=\"dim\">A package with nothing measurable is listed "
            "as having no measurement content, not as passing.</p>"
            '<div class="tbl-wrap"><table><thead><tr><th>Package</th><th class="num">Score</th>'
            '<th class="num">Grade</th><th class="num">Things</th><th>State</th></tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render an audited measurement inventory.")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--intro-file", type=Path, default=None)
    parser.add_argument("--commit", default="")
    parser.add_argument("--generated", default="")
    parser.add_argument("--template", type=Path, default=ASSETS / "template.html")
    parser.add_argument("--body", type=Path, default=ASSETS / "measurement-body.html")
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--root-dir", action="append", default=[], dest="root_dirs")
    parser.add_argument("--scope", action="append", default=[], dest="scopes")
    parser.add_argument("--root", action="store_true")
    parser.add_argument("--package", action="append", default=[], dest="packages",
                        metavar="NAME:PATH")
    args = parser.parse_args(argv)

    if args.packages and not args.root:
        parser.error("--package builds the repository roll-up table and needs --root")

    try:
        inventory = read_inventory(args.inventory)
    except rubric.InventoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {args.inventory}: {exc}", file=sys.stderr)
        return 2

    all_rows = inventory["rows"]
    total_rows = len(all_rows)
    if not args.root:
        scopes = [str(s).replace("\\", "/").strip("/")
                  for s in (args.scopes or args.root_dirs)]
        # `in_scope` treats an empty scope list as "everything", which is right
        # for --root and wrong here: a package page built with neither flag
        # scores the whole repository's inventory as this one package's, and
        # the roll-up then adds the same rows in again under every package.
        # The audit is deliberately repo-wide (a script pointed at src/billing
        # cannot see evals/), so this is the easy mistake, and it produces a
        # plausible number rather than an obvious failure.
        if not scopes:
            print(f"warning: --name {args.name} is a package build but neither --scope nor "
                  "--root-dir was given, so every row in the audit is being scored as this "
                  "package's. Pass --root-dir/--scope to say which paths are its, or "
                  "--root if this is the repository roll-up.", file=sys.stderr)
        inventory["rows"] = [entry for entry in all_rows
                             if in_scope(entry, scopes, args.repo)]
        kept_findings = {str(entry.get("finding")) for entry in inventory["rows"]
                         if str(entry.get("finding") or "").strip()}
        # A finding no row anywhere names is about the measurement setup, not
        # about one row, so no scope can be out of scope for it. Only a finding
        # attached exclusively to rows that this page dropped goes with them —
        # otherwise a high finding nobody wired to a row vanishes and the page
        # then reports "no confirmed findings", which is the exact lie this
        # skill exists to prevent.
        attached_anywhere = {str(entry.get("finding")) for entry in all_rows
                             if str(entry.get("finding") or "").strip()}
        inventory["findings"] = [
            f for f in inventory["findings"]
            if str(f.get("id")) in kept_findings
            or str(f.get("id")) not in attached_anywhere
        ]
    out_of_scope = total_rows - len(inventory["rows"])
    if out_of_scope:
        print(f"note: {out_of_scope} row(s) dropped as defined outside this unit",
              file=sys.stderr)

    scored = rubric.score_inventory(inventory["rows"])

    intro = ""
    if args.intro_file:
        try:
            intro = args.intro_file.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"warning: {args.intro_file}: {exc}", file=sys.stderr)
    if not intro.strip():
        intro = ('<div class="callout warn">No written summary was supplied. The prose that '
                 "says what this unit measures and why is the one part no script can "
                 "produce, and this page is weaker without it.</div>")

    packages = []
    for spec in args.packages:
        label, _, location = spec.partition(":")
        packages.append(read_package_grade(label.strip(), Path(location.strip())))

    body = fill(args.body.read_text(encoding="utf-8"), {
        "TAB_SCORE": render_score_tab(scored, inventory["rows"], intro,
                                      inventory["not_audited"],
                                      render_package_table(packages),
                                      out_of_scope=out_of_scope,
                                      scoped=not args.root),
        "TAB_INVENTORY": render_inventory_tab(inventory["rows"]),
        "TAB_FINDINGS": render_findings_tab(inventory["findings"]),
        "TAB_UNMEASURABLE": render_unmeasurable_tab(inventory["rows"]),
    })
    # Element 0 of the split is everything before the first "<!-- tab:"
    # marker — the scaffold's leading explanatory comment — and is by
    # definition not a tab. Without dropping it, it survives the `.strip()`
    # filter and panels() renders it as a bogus, wrongly-active first tab.
    fragments = [part for part in body.split("<!-- tab:")[1:] if part.strip()]
    nav, sections = panels([f"<!-- tab:{part}" for part in fragments])

    meta = build_metadata(inventory, scored, args.name, args, out_of_scope, packages)
    # Appended AFTER the last panel's closing tag, so it is inside no
    # `<section class="panel">` and is the last thing in TABS_PANELS. That
    # placement is load-bearing for tests as well as for readers: the block
    # carries the entire inventory — every row, finding, reason and citation —
    # so any assertion made against the whole document text matches this JSON
    # rather than the rendering, and passes with the tabs deleted. Panel-scoped
    # assertions are the fix, and they only work because nothing here leaks
    # into a panel. The extraction contract is unchanged: one
    # `id="measurement-meta"` script, `</` escaped as `<\/`, one regex read.
    sections += (f'\n<script type="application/json" id="measurement-meta">'
                 f"{json_block(meta)}</script>")

    page = fill(args.template.read_text(encoding="utf-8"), {
        "DOC_TITLE": esc(f"{args.name} — Measurement"),
        "DOC_LABEL": "MEASUREMENT AUDIT",
        "DOC_SUBTITLE": esc(args.subtitle or
                            f"Can the numbers {args.name} reports be believed?"),
        "DOC_META": esc(" · ".join(part for part in (
            f"generated {meta['generated']}", meta["commit"],
            f"{len(inventory['rows'])} measurable thing(s)") if part)),
        "TABS_NAV": nav,
        "TABS_PANELS": sections,
        "DOC_BODY": "",
        "DOC_FOOTER": ("Generated by science-investigation. The score is measurement "
                       "coverage, not code quality: it says how much of what matters is "
                       "measured, never whether the code is correct."),
    })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    shown = "—" if scored["score"] is None else f"{scored['score']:.1f}"
    print(f"wrote {args.out} — {scored['grade']} ({shown})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
