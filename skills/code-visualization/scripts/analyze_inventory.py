#!/usr/bin/env python3
"""Inventory tab: language breakdown, size, largest/most-complex files, directory map.

Usage: python3 analyze_inventory.py REPO_DIR --tabs-dir TABS_DIR
Emits: TABS_DIR/02-inventory.html and a JSON summary on stdout (for the agent).
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from common import (CODE_LANGS, bar_cell, detect_lang, esc, is_test_path,
                    loc_and_complexity, read_text, walk_source, write_fragment)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--tabs-dir", required=True)
    ap.add_argument("--exclude", default="",
                    help="comma-separated extra directory names to skip (e.g. generated,migrations)")
    args = ap.parse_args()
    extra_exclude = {d.strip() for d in args.exclude.split(",") if d.strip()}
    repo = Path(args.repo).resolve()

    files = []  # (rel, lang, loc, branches, depth, is_test)
    lang_loc = defaultdict(int)
    lang_files = defaultdict(int)
    dir_loc = defaultdict(int)
    test_loc = src_loc = 0
    skipped_large = []
    other_files = 0
    other_exts = defaultdict(int)

    for rel, p in walk_source(repo, skipped_large=skipped_large, extra_exclude=extra_exclude):
        lang = detect_lang(rel)
        if lang == "Other":
            # Unrecognized language: counted and reported, never silently
            # dropped — a Dart or Haskell repo must not read as "empty".
            other_files += 1
            other_exts[Path(rel).suffix.lower() or "(no ext)"] += 1
            continue
        text = read_text(p)
        loc, branches, depth = loc_and_complexity(text)
        if loc == 0:
            continue
        test = is_test_path(rel)
        files.append((rel, lang, loc, branches, depth, test))
        lang_loc[lang] += loc
        lang_files[lang] += 1
        top = rel.split("/")[0] if "/" in rel else "(root)"
        dir_loc[top] += loc
        if lang in CODE_LANGS:
            if test:
                test_loc += loc
            else:
                src_loc += loc

    total_loc = sum(lang_loc.values())
    code_files = [f for f in files if f[1] in CODE_LANGS]
    top_langs = sorted(lang_loc.items(), key=lambda kv: -kv[1])
    max_lang = top_langs[0][1] if top_langs else 1

    biggest = sorted(code_files, key=lambda f: -f[2])[:20]
    most_complex = sorted(code_files, key=lambda f: -f[3])[:20]
    max_loc = biggest[0][2] if biggest else 1
    max_br = most_complex[0][3] if most_complex else 1
    top_dirs = sorted(dir_loc.items(), key=lambda kv: -kv[1])[:14]
    max_dir = top_dirs[0][1] if top_dirs else 1
    test_ratio = (100.0 * test_loc / (test_loc + src_loc)) if (test_loc + src_loc) else 0.0

    coverage_notes = []
    if other_files:
        top_ext = ", ".join(f"{e} ({n})" for e, n in
                            sorted(other_exts.items(), key=lambda kv: -kv[1])[:5])
        coverage_notes.append(
            f"{other_files:,} file(s) in unrecognized languages are not counted above ({esc(top_ext)})")
    if skipped_large:
        biggest_skipped = max(skipped_large, key=lambda s: s["bytes"])
        coverage_notes.append(
            f"{len(skipped_large)} file(s) over 2 MB skipped entirely, largest "
            f"<code>{esc(biggest_skipped['path'])}</code> ({biggest_skipped['bytes']//1_000_000} MB)")
    coverage_note = (
        f"<p class=\"dim\">Coverage: {'; '.join(coverage_notes)}.</p>" if coverage_notes else "")

    lang_rows = "\n".join(
        f"<tr><td>{esc(lang)}</td><td class='num'>{n:,}</td>"
        f"<td class='num'>{lang_files[lang]:,}</td>"
        f"<td>{bar_cell(n, max_lang)}</td>"
        f"<td class='num'>{100.0*n/total_loc:.1f}%</td></tr>"
        for lang, n in top_langs[:12]
    )
    dir_rows = "\n".join(
        f"<tr><td><code>{esc(d)}/</code></td><td class='num'>{n:,}</td>"
        f"<td>{bar_cell(n, max_dir)}</td></tr>"
        for d, n in top_dirs
    )
    big_rows = "\n".join(
        f"<tr><td><code>{esc(rel)}</code>{' <span class=\"badge neutral\">test</span>' if t else ''}</td>"
        f"<td class='num'>{loc:,}</td><td>{bar_cell(loc, max_loc)}</td>"
        f"<td class='num'>{br}</td></tr>"
        for rel, lang, loc, br, dep, t in biggest
    )
    cx_rows = "\n".join(
        f"<tr><td><code>{esc(rel)}</code>{' <span class=\"badge neutral\">test</span>' if t else ''}</td>"
        f"<td class='num'>{br}</td><td>{bar_cell(br, max_br, 'warn')}</td>"
        f"<td class='num'>{loc:,}</td><td class='num'>{dep}</td></tr>"
        for rel, lang, loc, br, dep, t in most_complex
    )

    body = f"""
<div class="kpis">
  <div class="kpi accent"><div class="n">{len(files):,}</div><div class="l">source files</div></div>
  <div class="kpi"><div class="n">{total_loc:,}</div><div class="l">lines of code</div></div>
  <div class="kpi"><div class="n">{len([lang for lang in top_langs if lang[0] in CODE_LANGS]):,}</div><div class="l">languages</div></div>
  <div class="kpi {'good' if test_ratio >= 25 else 'warn'}"><div class="n">{test_ratio:.0f}%</div><div class="l">test code share</div></div>
</div>

<h2>Languages</h2>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>Language</th><th class="num">LOC</th><th class="num">Files</th><th>Share</th><th class="num">%</th></tr></thead>
<tbody>{lang_rows}</tbody></table></div>
{coverage_note}

<h2>Top-level directories</h2>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>Directory</th><th class="num">LOC</th><th>Share</th></tr></thead>
<tbody>{dir_rows}</tbody></table></div>

<div class="grid cols-2">
<div>
<h2>Largest files</h2>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>File</th><th class="num">LOC</th><th>Size</th><th class="num">Branches</th></tr></thead>
<tbody>{big_rows}</tbody></table></div>
</div>
<div>
<h2>Most branch-heavy files</h2>
<p class="dim">Branch count ≈ decision points (if/for/while/case/catch/&amp;&amp;/||). A rough cyclomatic-complexity proxy.</p>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>File</th><th class="num">Branches</th><th>Heat</th><th class="num">LOC</th><th class="num">Max depth</th></tr></thead>
<tbody>{cx_rows}</tbody></table></div>
</div>
</div>
"""
    write_fragment(Path(args.tabs_dir), "02-inventory.html", "Inventory", body)

    summary = {
        "total_loc": total_loc,
        "source_files": len(files),
        "test_loc_share_pct": round(test_ratio, 1),
        "languages": {lang: n for lang, n in top_langs[:8]},
        # What the numbers above do NOT include — so a sparse inventory reads
        # as a coverage gap, not as a small codebase.
        "excluded": {
            "unrecognized_language_files": other_files,
            "unrecognized_extensions": dict(sorted(other_exts.items(), key=lambda kv: -kv[1])[:8]),
            "files_over_2mb": skipped_large[:10],
        },
        "top_dirs": dict(top_dirs[:10]),
        "largest_files": [{"path": r, "loc": n} for r, _, n, _, _, _ in biggest[:10]],
        "most_branch_heavy": [{"path": r, "branches": b, "loc": n} for r, _, n, b, _, _ in most_complex[:10]],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
