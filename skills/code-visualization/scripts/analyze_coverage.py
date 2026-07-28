#!/usr/bin/env python3
"""Coverage tab: per-file test coverage from an existing artifact, joined with
size/complexity so untested-AND-complex files stand out.

Usage: python analyze_coverage.py REPO_DIR --tabs-dir TABS_DIR [--coverage-file PATH]

Emits: TABS_DIR/09-coverage.html and a JSON summary on stdout.

This analyzer never RUNS the test suite — it renders an artifact the repo
already has (coverage.xml / lcov.info / coverage.out; auto-discovered unless
--coverage-file names one). No artifact → exit 0 with a JSON note listing what
was searched for and any conversion hints (e.g. a .coverage sqlite file that
`coverage xml` would make parseable); the workflow then asks the user rather
than guessing.
"""
import argparse
import json
import sys
from pathlib import Path

from common import (CODE_LANGS, bar_cell, detect_lang, esc, is_test_path,
                    json_block, loc_and_complexity, read_text, walk_source,
                    write_fragment)
from coverage_data import artifact_age_days, discover, parse, resolve_paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--tabs-dir", required=True)
    ap.add_argument("--coverage-file", default=None,
                    help="explicit artifact path (skips auto-discovery); kind inferred from name/content")
    ap.add_argument("--exclude", default="",
                    help="comma-separated extra directory names to skip")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    tabs = Path(args.tabs_dir)
    extra_exclude = {d.strip() for d in args.exclude.split(",") if d.strip()}

    if args.coverage_file:
        p = Path(args.coverage_file)
        kind = ("cobertura" if p.suffix == ".xml"
                else "lcov" if p.suffix in (".info", ".lcov")
                else "go" if p.suffix == ".out" else "cobertura")
        artifacts, hints = [(p, kind)], []
    else:
        artifacts, hints = discover(repo)

    if not artifacts:
        print(json.dumps({
            "note": "no parseable coverage artifact found (searched for coverage.xml, "
                    "cobertura*.xml, lcov.info, coverage.lcov, coverage.out)",
            "hints": hints,
            "ask_user": "Ask where coverage lives, or whether to skip the Coverage tab — do not fabricate one.",
        }, indent=2))
        return 0

    artifact, kind = artifacts[0]
    raw = parse(artifact, kind)
    repo_files = [rel for rel, _ in walk_source(repo, extra_exclude=extra_exclude)]
    cov = resolve_paths(raw, repo_files)
    unresolved = len(raw) - len(cov)
    age_days = artifact_age_days(artifact)

    if not cov:
        print(json.dumps({
            "note": f"artifact {artifact} parsed but none of its {len(raw)} paths matched this repo",
            "hints": hints,
            "ask_user": "The artifact may belong to another checkout; ask the user which one applies.",
        }, indent=2))
        return 0

    rows = []
    measured_covered = measured_total = 0
    unmeasured = []
    for rel, p in walk_source(repo, extra_exclude=extra_exclude):
        if detect_lang(rel) not in CODE_LANGS or is_test_path(rel):
            continue
        loc, branches, _ = loc_and_complexity(read_text(p))
        if loc == 0:
            continue
        if rel in cov:
            covered, total = cov[rel]
            pct = 100.0 * covered / total if total else 0.0
            measured_covered += covered
            measured_total += total
            rows.append({"path": rel, "loc": loc, "branches": branches,
                         "covered": covered, "total": total, "pct": round(pct, 1)})
        else:
            unmeasured.append({"path": rel, "loc": loc, "branches": branches})

    overall = 100.0 * measured_covered / measured_total if measured_total else 0.0
    # risk = how much untested complexity: branches weighted by uncovered share
    for r in rows:
        r["risk"] = round(r["branches"] * (1 - r["pct"] / 100.0), 1)
    rows.sort(key=lambda r: -r["risk"])
    unmeasured.sort(key=lambda r: -r["branches"])

    worst = rows[:25]
    max_risk = worst[0]["risk"] if worst and worst[0]["risk"] > 0 else 1
    tbl = "\n".join(
        f"<tr><td><code>{esc(r['path'])}</code></td>"
        f"<td class='num'>{r['pct']:.0f}%</td>"
        f"<td class='num'>{r['covered']}/{r['total']}</td>"
        f"<td class='num'>{r['branches']}</td>"
        f"<td class='num' data-sort='{r['risk']}'>{bar_cell(r['risk'], max_risk, 'bad')}</td></tr>"
        for r in worst)

    tm_items = [{"name": r["path"], "value": max(1, r["loc"]),
                 "metric": round(100 - r["pct"], 1),
                 "meta": f"{r['pct']:.0f}% covered · {r['branches']} branches"}
                for r in rows[:80]]
    treemap = {"items": tm_items, "valueLabel": "LOC", "metricLabel": "% uncovered", "metricMax": 100}

    unm_html = ""
    if unmeasured:
        li = "".join(f"<li><code>{esc(r['path'])}</code> <span class='dim'>({r['branches']} branches, {r['loc']} LOC)</span></li>"
                     for r in unmeasured[:10])
        unm_html = (f"<details><summary>{len(unmeasured)} source file(s) absent from the coverage report</summary>"
                    f"<div class='body'><p class='dim'>Never imported by any test, excluded from the run, or added "
                    f"after the artifact was generated — 0% is the safe assumption.</p><ul>{li}</ul></div></details>")

    age_note = ""
    if age_days > 7:
        age_note = (f"<div class='callout warn'><b>The coverage artifact is ~{age_days:.0f} days old.</b> "
                    f"Code merged since then is invisible to it; treat these numbers as a floor from that date, "
                    f"not the current state.</div>")

    body = f"""
<div class="kpis">
  <div class="kpi {'good' if overall >= 75 else 'warn' if overall >= 50 else 'bad'}"><div class="n">{overall:.0f}%</div><div class="l">line coverage (measured files)</div></div>
  <div class="kpi"><div class="n">{len(rows):,}</div><div class="l">files measured</div></div>
  <div class="kpi {'warn' if unmeasured else 'good'}"><div class="n">{len(unmeasured):,}</div><div class="l">source files not measured</div></div>
</div>
<div class="callout"><b>Source:</b> <code>{esc(str(artifact))}</code> ({esc(kind)}). This tab renders an existing artifact — it did not run the tests. Coverage says which lines executed under test, not that their behavior was asserted; pair it with the Invariants tab before trusting a green file.</div>
{age_note}
<h2>Uncovered complexity map</h2>
<p class="dim">Area = lines of code · color = % uncovered · the large, hot blocks are the untested complexity.</p>
<div class="viz" data-render="treemap" style="height:520px"></div>
<script type="application/json">{json_block(treemap)}</script>
<h2>Highest-risk files — complex and untested</h2>
<p class="dim">Risk = branch points × uncovered share. Tests excluded from the ranking.</p>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>File</th><th class="num">Coverage</th><th class="num">Lines</th><th class="num">Branches</th><th class="num">Untested complexity</th></tr></thead>
<tbody>{tbl}</tbody></table></div>
{unm_html}
"""
    write_fragment(tabs, "09-coverage.html", "Coverage", body)

    print(json.dumps({
        "artifact": str(artifact), "format": kind,
        "artifact_age_days": round(age_days, 1),
        "overall_line_coverage_pct": round(overall, 1),
        "files_measured": len(rows),
        "files_unmeasured": len(unmeasured),
        "paths_unresolved": unresolved,
        "highest_risk": [{k: r[k] for k in ("path", "pct", "branches", "risk")} for r in rows[:12]],
        "unmeasured_complex": [r["path"] for r in unmeasured[:10]],
        "hints": hints,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
