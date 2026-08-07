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

import common
import rubric
from common import (HEALTH_SCHEMA, doc_path, esc, git_sha, json_block, listed_packages,
                    load_findings, load_map, measure, read_asset, read_meta, rel_href,
                    render, warn, within)

SEVERITY_ORDER = ("high", "medium", "low")
SEVERITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}

# Distinct from an empty set, which means "this analysis covered nothing".
GRADE_EVERYTHING = object()


def grade_class(grade: str) -> str:
    if not grade or grade == rubric.UNGRADED:
        return "g-none"
    return "g-" + grade[0].lower()


def score_categories(findings: list[dict], loc: int, covered) -> dict:
    """Bucket findings by rubric category and score each one.

    `covered` names the categories this analysis was capable of reporting;
    `GRADE_EVERYTHING` means all of them. A category outside it comes back
    ungraded — a doctor with no duplication detector must not hand out a free
    100 for duplication, and neither must a detector that was skipped, crashed,
    or never ran at all.
    """
    buckets: dict[str, list[dict]] = {key: [] for key in rubric.CATEGORY_KEYS}
    unmapped: set[str] = set()
    for finding in findings:
        category, matched = rubric.categorize(finding)
        finding["_rubric_category"] = category
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

        graded = covered is GRADE_EVERYTHING or key in covered
        # Round before grading, not after. A score of 92.9535 is published as
        # 93.0, and the bands say 93.0 is an A — so grading the unrounded value
        # would print "93.0" beside "A-" and contradict the documented scale.
        score = (round(rubric.score_from_density(density, half_life), 1)
                 if graded else None)
        scores[key] = score
        rows.append({
            "key": key, "label": label, "weight": weight, "half_life": half_life,
            "graded": graded,
            "score": score,
            "grade": rubric.grade_for(score),
            "density": round(density, 2),
            "findings": counts,
        })

    overall = rubric.weighted_overall(scores)
    overall = None if overall is None else round(overall, 1)
    return {
        "categories": rows,
        "score": overall,
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
            f'<td class="num">{package.get("findings_total", "—")}</td></tr>'
        )
    return ("<h2>Packages</h2>"
            '<div class="tbl-wrap"><table><thead><tr><th>Package</th><th>Language</th>'
            '<th class="num">Files</th><th class="num">Lines</th><th class="num">Score</th>'
            "<th>Grade</th><th class=\"num\">Findings</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def render_caveats(errors: dict[str, str], skipped: set[str], unmapped: list[str],
                   ungraded: list[str], notes: list[str], out_of_scope: int,
                   roots: list[str], duplicates: int = 0) -> str:
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
        listed = ", ".join(f"<code>{esc(k)}</code>" for k in sorted(errors))
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
        listed = ", ".join(f"<code>{esc(k)}</code>" for k in sorted(skipped))
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


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def scoring_roots(args, repo: Path) -> list[str]:
    """The code the grade is a claim about — repo-relative.

    Findings and the LOC they are divided by have to cover the same code. At the
    root that is the union of the mapped packages, not the whole checkout: when
    the user deliberately leaves a directory unassigned, measuring `.` puts its
    lines in the denominator while none of its findings are in the numerator,
    which quietly improves the repo's grade in proportion to how much code was
    left out.
    """
    if args.root_dir:
        return list(args.root_dir)
    if args.root and args.map:
        roots = [root for package in load_map(args.map).get("packages", [])
                 for root in package.get("roots", [])]
        if roots and "." not in roots:
            return sorted(set(roots))
    return ["."]


def is_repo_root_file(path: str, repo: Path) -> bool:
    """A file sitting directly in the repo root — repo-wide configuration."""
    if not path:
        return False
    try:
        candidate = Path(path)
        resolved = (candidate if candidate.is_absolute() else repo / candidate).resolve()
        return resolved.parent == repo and resolved.is_file()
    except (OSError, ValueError):
        return False


def resolve_coverage(args, reports: list[dict]):
    """Which rubric categories this analysis is entitled to grade.

    Coverage is **evidence**, not the doctor's advertised capability. A report
    from `analyze_all.py` names the analyzers that ran, so it can be believed
    per category; the other two shapes cannot, and are handled by their own
    rules rather than being credited with everything the doctor could have done.
    That distinction is what stops `--skip-duplicates`, a crashed detector, or a
    single-detector report from being read as a clean bill of health.
    """
    if args.assume_full_coverage:
        return GRADE_EVERYTHING
    if args.covers:
        named = {c.strip() for part in args.covers for c in part.split(",") if c.strip()}
        unknown = named - set(rubric.CATEGORY_KEYS)
        if unknown:
            raise SystemExit(f"error: --covers names unknown categories: {', '.join(sorted(unknown))}")
        return named

    evidenced = [report for report in reports if report["shape"] == common.SHAPE_FULL]
    if evidenced:
        # At least one report says what it ran. Believe it, and let the other
        # reports contribute findings without inflating what was examined.
        return {rubric.DETECTOR_CATEGORIES[name]
                for report in evidenced for name in report["ran"]
                if name in rubric.DETECTOR_CATEGORIES}

    if any(report["shape"] == common.SHAPE_PARTIAL for report in reports):
        warn("a findings file is a single detector's output, which says nothing about what "
             "else was examined — every category is ungraded. Pass --covers a,b,c to name "
             "what this analysis actually looked at, or --assume-full-coverage.")
        return set()

    covered = rubric.DOCTOR_COVERAGE.get(args.doctor)
    if covered is None:
        # No known doctor produced these findings, so nothing here can be
        # claimed as measured. An empty findings list from a doctor that could
        # not read the language would otherwise render as an A+, which is the
        # one output this skill must never produce.
        warn(f"no coverage profile for doctor {args.doctor!r} — every category is ungraded. "
             "Pass --assume-full-coverage if the findings really do cover the whole rubric, "
             "--covers a,b,c to name what was examined, or add the doctor to "
             "rubric.DOCTOR_COVERAGE.")
        return set()
    return set(covered)


def build(args, findings: list[dict], errors: dict[str, str], skipped: set[str],
          reports: list[dict], duplicates: int) -> tuple[str, dict]:
    repo = Path(args.repo).resolve()
    relative_roots = scoring_roots(args, repo)
    roots = [repo / r for r in relative_roots]
    size = measure(roots) if args.loc is None else {"files": args.files or 0, "loc": args.loc}

    # Keep only findings about the code this document is a claim about. The
    # doctors are run from the repo root so they can see manifests, tests and
    # settings; without that context they invent findings (a package with no
    # manifest of its own reports a missing one) and miss real ones.
    scope = [repo / s for s in (args.scope or relative_roots)]
    out_of_scope = 0
    if scope and [Path(s).resolve() for s in scope] != [repo]:
        def in_scope(finding: dict) -> bool:
            path = str(finding.get("file", ""))
            if within(path, scope, repo):
                return True
            # The repo grade has to keep findings about repo-level configuration
            # — tsconfig.json, the root manifest, settings — which belong to no
            # package but describe the whole tree. Only files sitting directly
            # in the repo root qualify; an unmapped *directory* is still out.
            return args.root and is_repo_root_file(path, repo)

        kept = [f for f in findings if in_scope(f)]
        out_of_scope = len(findings) - len(kept)
        findings = kept

    covered = resolve_coverage(args, reports)
    scored = score_categories(findings, size["loc"], covered)
    packages = []
    links: dict[str, str] = {}
    if args.root:
        packages, links = collect_packages(repo, args.map, Path(args.out))

    meta = {
        "schema": HEALTH_SCHEMA,
        "scope": "repository" if args.root else "package",
        "package": args.name,
        "roots": relative_roots,
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
        "analyzers_skipped": sorted(skipped),
        "findings_out_of_scope": out_of_scope,
        "duplicates_merged": duplicates,
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
        "SUBJECT_DETAIL": esc("the whole repository" if relative_roots == ["."]
                              else ", ".join(relative_roots)),
        "HEADLINE_BADGES": headline_badges(meta),
        "UNGRADED_NOTE": ('<div class="callout warn">Nothing in this analysis could be graded — '
                          "the grade shown is a placeholder.</div>"
                          if scored["score"] is None else ""),
        "CATEGORY_ROWS": render_category_rows(scored["categories"]),
        "PACKAGE_TABLE": render_package_table(packages, links),
        "FINDINGS_SUMMARY": render_findings_summary(findings),
        "TOP_FINDINGS": render_top_findings(meta["top_findings"]),
        "BY_TYPE": render_by_type(findings),
        "CAVEATS": render_caveats(errors, skipped, scored["unmapped_types"],
                                  scored["ungraded"], args.note, out_of_scope,
                                  relative_roots, duplicates),
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
    for package in listed_packages(repo, data.get("packages", [])):
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
                        help="package root, repo-relative; repeat for a multi-root package. "
                             "With --root and --map, defaults to the union of the mapped "
                             "packages' roots so findings and LOC cover the same code")
    parser.add_argument("--scope", action="append", default=[],
                        help="keep only findings under this repo-relative path (default: the "
                             "root dirs). Lets the doctor run from the repo root — where it can "
                             "see manifests, tests and settings — while this document stays "
                             "about one package")
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
    parser.add_argument("--covers", action="append", default=[],
                        help="comma-separated rubric categories this analysis actually examined "
                             f"({', '.join(rubric.CATEGORY_KEYS)}). Use when the findings come "
                             "from a single detector or an unrecognized tool, where nothing in "
                             "the file says what was looked at")
    parser.add_argument("--assume-full-coverage", action="store_true",
                        help="grade every category regardless of which doctor produced the "
                             "findings; without it, an unrecognized --doctor leaves everything "
                             "ungraded rather than scoring an unread language A+")
    args = parser.parse_args(argv)

    if not args.findings:
        parser.error("--findings is required (pass '-' to read a doctor's JSON from stdin)")
    if not args.name:
        args.name = Path(args.repo).resolve().name if args.root else "package"

    findings, errors, skipped, reports, duplicates = load_findings(args.findings)
    page, meta = build(args, findings, errors, skipped, reports, duplicates)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"{out}: {meta['grade']} ({meta['score']}) — {meta['findings_total']} finding(s)"
          + (f", {meta['findings_out_of_scope']} outside {', '.join(meta['roots'])}"
             if meta["findings_out_of_scope"] else ""),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
