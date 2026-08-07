#!/usr/bin/env python3
"""Turn a code doctor's findings into a graded health document.

Consumes the JSON any of the doctors emit (`analyze_all.py --format json`,
`analyze_django.py --format json`, or a single detector's output), scores it
against the rubric in rubric.py, and writes an HTML page carrying a letter grade
plus a `code-health-meta` JSON block so the numbers can be extracted later
without parsing the page.

Two modes:

  package  one unit — findings for its roots, sized over its roots
  --root   the repo — the same arithmetic over the union of every package's
           findings plus any repo-wide ones, and a per-package grade table read
           back out of the package documents

The root grade is recomputed rather than averaged from package grades, so it is
the same kind of measurement as a package grade and comparable to one.

Usage:
  python build_health.py --out src/billing/docs/health.html \\
      --findings billing.json --name billing --root-dir src/billing \\
      --language python --doctor python-code-doctor --repo .

  python build_health.py --root --out docs/health.html --map docs/code-overview.json \\
      --findings billing.json --findings web.json --findings repo-wide.json --repo .
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import rubric
from common import (HEALTH_SCHEMA, doc_path, esc, git_sha, json_block, load_findings,
                    load_map, measure, read_asset, read_meta, rel_href, render, warn)

SEVERITY_ORDER = ("high", "medium", "low")
SEVERITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def grade_class(grade: str) -> str:
    if not grade or grade == rubric.UNGRADED:
        return "g-none"
    return "g-" + grade[0].lower()


def score_categories(findings: list[dict], loc: int, covered: set[str] | None) -> dict:
    """Bucket findings by rubric category and score each one.

    `covered` names the categories the doctor is actually capable of reporting.
    A category outside it comes back ungraded — a doctor that has no duplication
    detector must not hand out a free 100 for duplication.
    """
    buckets: dict[str, list[dict]] = {key: [] for key in rubric.CATEGORY_KEYS}
    unmapped: set[str] = set()
    for finding in findings:
        category, matched = rubric.categorize(finding)
        buckets[category].append(finding)
        if not matched:
            unmapped.add(rubric.finding_type(finding))

    rows = []
    scores: dict[str, float | None] = {}
    for key, label, weight, half_life in rubric.CATEGORIES:
        bucket = buckets[key]
        counts = {sev: sum(1 for f in bucket if f.get("severity") == sev) for sev in SEVERITY_ORDER}
        counts["total"] = len(bucket)
        weighted = sum(rubric.severity_weight(f.get("severity", "medium")) for f in bucket)
        density = rubric.density(weighted, loc)

        graded = covered is None or key in covered
        score = rubric.score_from_density(density, half_life) if graded else None
        scores[key] = score
        rows.append({
            "key": key, "label": label, "weight": weight, "half_life": half_life,
            "graded": graded,
            "score": None if score is None else round(score, 1),
            "grade": rubric.grade_for(score),
            "density": round(density, 2),
            "findings": counts,
        })

    overall = rubric.weighted_overall(scores)
    return {
        "categories": rows,
        "score": None if overall is None else round(overall, 1),
        "grade": rubric.grade_for(overall),
        "ungraded": [row["key"] for row in rows if not row["graded"]],
        "unmapped_types": sorted(unmapped),
    }


def relativize(value: str, repo: Path) -> str:
    """Cut the absolute repo prefix off a path.

    The doctors are run with an absolute path and echo it back, in the `file`
    field and inside description text alike. Left alone, the location column of
    every findings table is 80% sandbox path — and the documents are meant to be
    committed, where an absolute path from the machine that generated them is
    noise at best and misleading at worst.
    """
    prefix = str(repo)
    if not prefix.endswith("/"):
        prefix += "/"
    return str(value).replace(prefix, "").replace(str(repo), ".")


def top_findings(findings: list[dict], limit: int, repo: Path) -> list[dict]:
    ranked = sorted(
        findings,
        key=lambda f: (SEVERITY_ORDER.index(f.get("severity", "medium"))
                       if f.get("severity") in SEVERITY_ORDER else 1,
                       str(f.get("file", "")), f.get("line", 0)),
    )
    out = []
    for finding in ranked[:limit]:
        category, _ = rubric.categorize(finding)
        out.append({
            "severity": finding.get("severity", "medium"),
            "type": rubric.finding_type(finding),
            "category": category,
            "file": relativize(finding.get("file", ""), repo),
            "line": finding.get("line", 0),
            "description": relativize(str(finding.get("description", "")), repo)[:400],
            "suggestion": relativize(
                str(finding.get("suggestion") or finding.get("after") or ""), repo)[:300],
        })
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render_category_rows(rows: list[dict]) -> str:
    out = []
    for row in rows:
        counts = row["findings"]
        cls = grade_class(row["grade"]) + ("" if row["graded"] else " ungraded")
        if row["graded"]:
            width = max(1.5, row["score"])
            bar = f'<div class="bar"><i style="width:{width:.1f}%"></i></div>'
            score = f'{row["score"]:.0f}'
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
    counts: dict[str, int] = {}
    for finding in findings:
        counts[rubric.finding_type(finding)] = counts.get(rubric.finding_type(finding), 0) + 1
    if not counts:
        return ""
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    rows = "".join(
        f'<tr><td><code>{esc(name)}</code></td>'
        f'<td>{esc(rubric.CATEGORY_LABELS.get(rubric.categorize({"type": name})[0], ""))}</td>'
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
            f'<td class="num">{package.get("findings_total", "—")}</td></tr>'
        )
    return ("<h2>Packages</h2>"
            '<div class="tbl-wrap"><table><thead><tr><th>Package</th><th>Language</th>'
            '<th class="num">Files</th><th class="num">Lines</th><th class="num">Score</th>'
            "<th>Grade</th><th class=\"num\">Findings</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def render_caveats(errors: dict[str, str], unmapped: list[str], ungraded: list[str],
                   notes: list[str]) -> str:
    parts = []
    if errors:
        listed = ", ".join(f"<code>{esc(k)}</code>" for k in sorted(errors))
        parts.append('<div class="callout bad"><strong>Detectors that did not complete:</strong> '
                     f"{listed}. A zero count in those categories means <em>unknown</em>, "
                     "not clean — the grade is an upper bound.</div>")
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


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build(args, findings: list[dict], errors: dict[str, str]) -> tuple[str, dict]:
    repo = Path(args.repo).resolve()
    roots = [repo / r for r in (args.root_dir or ["."])]
    size = measure(roots) if args.loc is None else {"files": args.files or 0, "loc": args.loc}

    covered = None if args.assume_full_coverage else rubric.DOCTOR_COVERAGE.get(args.doctor)
    if args.doctor and covered is None and not args.assume_full_coverage:
        warn(f"unknown doctor {args.doctor!r} — every category is being graded; pass "
             "--assume-full-coverage to silence this, or add the doctor to rubric.DOCTOR_COVERAGE")
    # A detector that crashed reported nothing; that category cannot be graded.
    if errors and covered is not None:
        covered = set(covered) - {rubric.DETECTOR_CATEGORIES[name]
                                  for name in errors if name in rubric.DETECTOR_CATEGORIES}

    scored = score_categories(findings, size["loc"], covered)
    packages = []
    links: dict[str, str] = {}
    if args.root:
        packages, links = collect_packages(repo, args.map, Path(args.out))

    meta = {
        "schema": HEALTH_SCHEMA,
        "scope": "repository" if args.root else "package",
        "package": args.name,
        "roots": args.root_dir or ["."],
        "language": args.language,
        "doctor": args.doctor,
        "generated": args.date or dt.date.today().isoformat(),
        "commit": args.commit or git_sha(repo),
        "size": size,
        "score": scored["score"],
        "grade": scored["grade"],
        "categories": scored["categories"],
        "ungraded": scored["ungraded"],
        "unmapped_types": scored["unmapped_types"],
        "analyzer_errors": errors,
        "findings_total": len(findings),
        "findings_by_severity": {sev: sum(1 for f in findings if f.get("severity") == sev)
                                 for sev in SEVERITY_ORDER},
        "top_findings": top_findings(findings, args.top, repo),
    }
    if packages:
        meta["packages"] = packages

    body = render(read_asset("health-body.html"), {
        "GRADE": esc(scored["grade"]),
        "GRADE_CLASS": grade_class(scored["grade"]),
        "SCORE": "—" if scored["score"] is None else f'{scored["score"]:.1f}',
        "SUBJECT": esc(args.name),
        "SUBJECT_DETAIL": esc(", ".join(args.root_dir)
                              or ("the whole repository" if args.root else ".")),
        "HEADLINE_BADGES": headline_badges(meta),
        "UNGRADED_NOTE": ('<div class="callout warn">Nothing in this analysis could be graded — '
                          "the grade shown is a placeholder.</div>"
                          if scored["score"] is None else ""),
        "CATEGORY_ROWS": render_category_rows(scored["categories"]),
        "PACKAGE_TABLE": render_package_table(packages, links),
        "FINDINGS_SUMMARY": render_findings_summary(findings),
        "TOP_FINDINGS": render_top_findings(meta["top_findings"]),
        "BY_TYPE": render_by_type(findings),
        "CAVEATS": render_caveats(errors, scored["unmapped_types"], scored["ungraded"], args.note),
        "META_JSON": json_block(meta),
    })

    scope = "repository" if args.root else "package"
    page = render(read_asset("template.html"), {
        "DOC_TITLE": esc(f"{args.name} — Code Health"),
        "DOC_LABEL": "CODE HEALTH",
        "DOC_SUBTITLE": esc(args.subtitle or f"Graded health of the {args.name} {scope}."),
        "DOC_META": esc(" · ".join(part for part in (
            f"generated {meta['generated']}",
            meta["commit"],
            f"{size['files']} files, {size['loc']} lines",
            f"{len(findings)} findings",
            args.doctor,
        ) if part)),
        "DOC_BODY": body,
        "DOC_FOOTER": ("Generated by code-overview. Grades are computed from the code doctors' "
                       "deterministic detectors — they measure what a detector can see, not "
                       "whether the design is right. Read the code map alongside this."),
    })
    return page, meta


def collect_packages(repo: Path, map_path: str | None, out: Path) -> tuple[list[dict], dict[str, str]]:
    """Read each package's health document back for the roll-up table."""
    if not map_path:
        return [], {}
    data = load_map(map_path)
    packages, links = [], {}
    for package in data.get("packages", []):
        health = doc_path(repo, package, "health")
        meta = read_meta(health)
        if meta is None:
            warn(f"no code-health metadata at {health} — {package['name']} is missing from "
                 "the roll-up table (build its health page first)")
            continue
        packages.append({
            "package": meta.get("package", package["name"]),
            "language": meta.get("language", package.get("language", "")),
            "grade": meta.get("grade", rubric.UNGRADED),
            "score": meta.get("score"),
            "size": meta.get("size", {}),
            "findings_total": meta.get("findings_total", 0),
            "docs": package.get("docs", ""),
        })
        links[meta.get("package", package["name"])] = rel_href(out, health)
    return packages, links


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", required=True, help="path to write health.html to")
    parser.add_argument("--findings", action="append", default=[],
                        help="doctor JSON file; repeat to merge, '-' for stdin")
    parser.add_argument("--name", default="", help="package name (or repo name with --root)")
    parser.add_argument("--root-dir", action="append", default=[],
                        help="package root, repo-relative; repeat for a multi-root package")
    parser.add_argument("--repo", default=".", help="repository root (default: .)")
    parser.add_argument("--language", default="")
    parser.add_argument("--doctor", default="", help="which doctor produced the findings")
    parser.add_argument("--root", action="store_true",
                        help="repo-level document: adds the per-package grade table")
    parser.add_argument("--map", default="", help="package map, for the --root grade table")
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--note", action="append", default=[],
                        help="an extra caveat to record; repeatable")
    parser.add_argument("--top", type=int, default=25, help="findings to list (default: 25)")
    parser.add_argument("--loc", type=int, help="override the measured line count")
    parser.add_argument("--files", type=int, help="override the measured file count")
    parser.add_argument("--commit", default="", help="commit sha for the metadata")
    parser.add_argument("--date", default="", help="generation date (default: today)")
    parser.add_argument("--assume-full-coverage", action="store_true",
                        help="grade every category even for a doctor with known blind spots")
    args = parser.parse_args(argv)

    if not args.findings:
        parser.error("--findings is required (pass '-' to read a doctor's JSON from stdin)")
    if not args.name:
        args.name = Path(args.repo).resolve().name if args.root else "package"

    findings, errors = load_findings(args.findings)
    page, meta = build(args, findings, errors)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"{out}: {meta['grade']} ({meta['score']}) — {meta['findings_total']} finding(s)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
