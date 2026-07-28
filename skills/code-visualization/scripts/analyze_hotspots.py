#!/usr/bin/env python3
"""Hotspots tab: churn (git commit frequency) x complexity. Where bugs live.

Usage: python3 analyze_hotspots.py REPO_DIR --tabs-dir TABS_DIR [--since "18 months ago"]
Emits: TABS_DIR/04-hotspots.html and a JSON summary on stdout.
Degrades gracefully (empty fragment skipped by assembler) if not a git repo.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from common import (CODE_LANGS, bar_cell, detect_lang, esc, git, is_test_path,
                    json_block, loc_and_complexity, read_text, walk_source,
                    write_fragment)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--tabs-dir", required=True)
    ap.add_argument("--since", default="24 months ago")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    tabs = Path(args.tabs_dir)

    try:
        git(repo, "rev-parse", "--git-dir")
    except Exception:
        write_fragment(tabs, "04-hotspots.html", "Hotspots", "")
        print(json.dumps({"error": "not a git repository; hotspots skipped"}))
        return 0

    log = git(repo, "log", f"--since={args.since}", "--name-only",
              "--no-merges", "--pretty=format:@@%H|%an")
    churn = defaultdict(int)
    authors = defaultdict(set)
    commits = 0
    cur_author = None
    for line in log.splitlines():
        if line.startswith("@@"):
            commits += 1
            cur_author = line.split("|", 1)[1] if "|" in line else "?"
        elif line.strip():
            churn[line.strip()] += 1
            if cur_author:
                authors[line.strip()].add(cur_author)

    rows = []
    for rel, p in walk_source(repo):
        lang = detect_lang(rel)
        if lang not in CODE_LANGS:
            continue
        c = churn.get(rel, 0)
        loc, branches, depth = loc_and_complexity(read_text(p))
        if loc == 0:
            continue
        rows.append({
            "path": rel, "churn": c, "loc": loc, "branches": branches,
            "authors": len(authors.get(rel, ())), "test": is_test_path(rel),
            "score": c * (branches + 1),
        })
    if not rows or commits == 0:
        write_fragment(tabs, "04-hotspots.html", "Hotspots", "")
        print(json.dumps({"error": f"no commit history since '{args.since}'; hotspots skipped"}))
        return 0

    rows.sort(key=lambda r: -r["score"])
    src_rows = [r for r in rows if not r["test"]]
    top = src_rows[:60]
    max_churn = max((r["churn"] for r in rows), default=1) or 1

    tm_items = [{"name": r["path"], "value": r["loc"], "metric": r["churn"],
                 "meta": f"{r['branches']} branches · {r['authors']} authors"}
                for r in src_rows[:80] if r["churn"] > 0 or r["loc"] > 0]
    treemap = {"items": tm_items, "valueLabel": "LOC", "metricLabel": "commits", "metricMax": max_churn}

    max_score = top[0]["score"] if top else 1
    tbl = "\n".join(
        f"<tr><td><code>{esc(r['path'])}</code></td>"
        f"<td class='num'>{r['churn']}</td><td class='num'>{r['branches']}</td>"
        f"<td class='num'>{r['loc']:,}</td><td class='num'>{r['authors']}</td>"
        f"<td class='num' data-sort='{r['score']}'>{bar_cell(r['score'], max_score, 'bad')}</td></tr>"
        for r in top[:25])

    stable = sorted((r for r in src_rows if r["churn"] == 0 and r["branches"] > 30),
                    key=lambda r: -r["branches"])[:8]
    stable_html = ""
    if stable:
        li = "".join(f"<li><code>{esc(r['path'])}</code> <span class='dim'>({r['branches']} branches, untouched)</span></li>" for r in stable)
        stable_html = f"""<details><summary>Complex but dormant files ({len(stable)}) — high complexity, zero recent commits</summary><div class="body"><p class="dim">Not urgent, but risky to modify: nobody has touched these recently, so working knowledge may have evaporated.</p><ul>{li}</ul></div></details>"""

    body = f"""
<div class="kpis">
  <div class="kpi accent"><div class="n">{commits:,}</div><div class="l">commits ({esc(args.since)})</div></div>
  <div class="kpi"><div class="n">{len([r for r in rows if r['churn']>0]):,}</div><div class="l">files touched</div></div>
  <div class="kpi warn"><div class="n">{esc(top[0]['path'].split('/')[-1]) if top else '–'}</div><div class="l">top hotspot</div></div>
</div>
<div class="callout"><b>How to read this:</b> files that are both <i>complex</i> and <i>frequently changed</i> are where defects concentrate — complexity makes mistakes likely, churn gives them opportunities. Prioritize refactoring and review attention here, not on complex-but-stable code.</div>

<h2>Churn × size map</h2>
<p class="dim">Area = lines of code · color = commit count since {esc(args.since)} · hover for details.</p>
<div class="viz" data-render="treemap" style="height:540px"></div>
<script type="application/json">{json_block(treemap)}</script>

<h2>Hotspot ranking</h2>
<p class="dim">Score = commits × (branch points + 1). Tests excluded.</p>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>File</th><th class="num">Commits</th><th class="num">Branches</th><th class="num">LOC</th><th class="num">Authors</th><th class="num">Hotspot score</th></tr></thead>
<tbody>{tbl}</tbody></table></div>
{stable_html}
"""
    write_fragment(tabs, "04-hotspots.html", "Hotspots", body)

    print(json.dumps({
        "commits_analyzed": commits, "since": args.since,
        "top_hotspots": [{k: r[k] for k in ("path", "churn", "branches", "loc", "authors", "score")} for r in top[:12]],
        "complex_dormant": [r["path"] for r in stable],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
