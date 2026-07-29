#!/usr/bin/env python3
"""Blast Radius tab: who calls the symbols this PR changed — especially callers
the PR did NOT touch (they inherit new behavior silently).

Usage:
  python3 analyze_blast_radius.py REPO --tabs-dir TABS [--base REF] [--head REF] [--worktree]

Emits: TABS/04-blast-radius.html and a JSON summary on stdout.
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import resources
from common import detect_lang, esc, git, is_test_path, json_block, read_text, walk_source, write_fragment
from diffutil import (def_patterns_for, is_generated_doc, parse_diff, resolve_base,
                      untracked_file_diffs)

CAVEAT = ("Caller matching is textual (name( / .name) with no import resolution: "
          "a same-named attribute or symbol in an unrelated module counts as a "
          "caller, and dynamic dispatch/reflection are invisible. Use it to aim "
          "review attention, never as proof of safety or of impact.")

GENERIC_NAMES = {"main", "init", "new", "get", "set", "run", "call", "update", "create",
                 "delete", "add", "remove", "start", "stop", "read", "write", "close",
                 "open", "next", "name", "value", "data", "test", "setup", "handle",
                 "process", "load", "save", "render", "parse", "format", "build", "to",
                 "from", "str", "repr", "len", "hash", "eq", "copy", "clone", "default"}


def changed_symbols(fd):
    """Symbols whose definitions were touched: defined on added lines, or whose
    definition line was removed (signature change / deletion)."""
    pats = def_patterns_for(fd.path)
    syms = {}
    for ln, text in fd.added_lines():
        for pat in pats:
            m = pat.match(text)
            if m:
                syms.setdefault(m.group("name"), "modified/added")
    removed = set()
    for text in fd.removed_lines():
        for pat in pats:
            m = pat.match(text)
            if m:
                removed.add(m.group("name"))
    for n in removed:
        if n not in syms:
            syms[n] = "removed"
    return syms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--tabs-dir", required=True)
    ap.add_argument("--base", default=None)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--worktree", action="store_true")
    ap.add_argument("--max-symbols", type=int, default=40)
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    tabs = Path(args.tabs_dir)

    if args.worktree and args.head != "HEAD":
        ap.error("--head cannot be combined with --worktree: the working tree IS the head side")

    base, merge_base, _ = resolve_base(repo, args.base, args.head, worktree=args.worktree)
    raw = git(repo, "-c", "core.quotepath=false", "diff", "--no-color",
              merge_base if args.worktree else f"{merge_base}..{args.head}")
    fds = parse_diff(raw)
    if args.worktree:
        fds += untracked_file_diffs(repo)[0]
    excluded_docs = [f.path for f in fds if is_generated_doc(f.path)]
    fds = [f for f in fds if not is_generated_doc(f.path)]
    changed_files = {fd.path for fd in fds}

    # symbol -> (defining file, kind)
    symdefs = {}
    for fd in fds:
        if fd.binary or is_test_path(fd.path):
            continue
        for name, kind in changed_symbols(fd).items():
            if len(name) <= 2 or name.lower() in GENERIC_NAMES or name.startswith("__"):
                continue
            symdefs.setdefault(name, (fd.path, kind))
    total_symbols = len(symdefs)
    symdefs = dict(list(symdefs.items())[: args.max_symbols])
    truncated = total_symbols - len(symdefs)

    # ---- runtime resources: who loads the templates/prompts this diff edited?
    # Computed before the symbol analysis returns, because a prompt-only diff
    # changes no symbol at all and would otherwise leave the tab empty.
    scan = resources.scan(repo, dict(walk_source(repo)))
    loaders_of = defaultdict(list)
    for ref in sorted(scan.refs):
        loaders_of[ref.dst].append((ref.src, f"{ref.src}:{ref.line}"))
    changed_resources = []
    for path in sorted(changed_files & set(loaders_of)):
        untouched = [cite for src, cite in loaders_of[path] if src not in changed_files]
        touched = [cite for src, cite in loaders_of[path] if src in changed_files]
        changed_resources.append({"path": path, "loaders": len(loaders_of[path]),
                                  "untouched_loaders": untouched, "touched_loaders": touched})
    res_html = ""
    if changed_resources:
        rows = "".join(
            f"<tr><td><code>{esc(c['path'])}</code></td><td class='num'>{c['loaders']}</td>"
            f"<td class='num'{' style=color:var(--bad);font-weight:600' if c['untouched_loaders'] else ''}>"
            f"{len(c['untouched_loaders'])}</td>"
            f"<td>{' '.join(f'<code>{esc(x)}</code>' for x in c['untouched_loaders'][:10])}"
            + (f" <span class='dim'>… {len(c['untouched_loaders'])-10} more</span>"
               if len(c["untouched_loaders"]) > 10 else "") + "</td></tr>"
            for c in changed_resources)
        loose = sum(len(c["untouched_loaders"]) for c in changed_resources)
        res_html = f"""
<h2>Runtime resources changed</h2>
<div class="callout {'warn' if loose else 'good'}"><b>{len(changed_resources)} loaded file{'s' if len(changed_resources)!=1 else ''} edited</b> —
templates, prompts or data this codebase reads at runtime. {loose} loading site{'s' if loose!=1 else ''} outside this diff
inherit{'' if loose!=1 else 's'} the new content with nothing in the diff to review. {esc(resources.CAVEAT)}</div>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>Changed file</th><th class="num">Loaders</th><th class="num">Untouched</th><th>Loaded at</th></tr></thead>
<tbody>{rows}</tbody></table></div>
"""

    renames = [{"from": f.old_path, "to": f.path} for f in fds if f.status == "R"]
    if not symdefs:
        rename_note = ""
        if renames:
            rename_note = ("<div class='callout warn'><b>Renamed files:</b> " +
                           ", ".join(f"<code>{esc(r['from'])}</code> → <code>{esc(r['to'])}</code>" for r in renames[:8]) +
                           ". A pure rename changes no symbol names, so it is invisible to this tab — but every "
                           "path-based reference (imports, configs, docs) to the old path is now stale and is not traced here.</div>")
        write_fragment(tabs, "04-blast-radius.html", "Blast Radius",
                       "<div class='callout good'><b>No named function/class definitions were changed in supported languages</b>, so there is no symbol-level blast radius to trace. Behavioral impact, if any, flows through data/config/edited call sites — see the Footprint and Flow Impact tabs.</div>"
                       + rename_note + res_html)
        print(json.dumps({"symbols": {}, "note": "no changed symbols detected",
                          "renames": renames, "excluded_generated_docs": excluded_docs,
                          "changed_resources": changed_resources,
                          "resource_caveat": resources.CAVEAT}))
        return 0

    # scan the head worktree for call sites
    call_res = {n: re.compile(r"(?<![\w.])" + re.escape(n) + r"\s*\(|\." + re.escape(n) + r"\b") for n in symdefs}
    callers = defaultdict(lambda: defaultdict(int))   # symbol -> caller file -> count
    def_pats_cache = {}
    for rel, p in walk_source(repo):
        if detect_lang(rel) not in ("Python", "JavaScript", "TypeScript", "Go", "Rust", "Ruby", "Java", "Kotlin", "C#", "Scala", "Vue", "Svelte"):
            continue
        text = read_text(p)
        if not text:
            continue
        pats = def_pats_cache.setdefault(Path(rel).suffix, def_patterns_for(rel))
        for line in text.splitlines():
            # Names DEFINED on this line: a definition of the same name in any
            # file (override, reimplementation) is not a call of the changed one.
            def_names = {m.group("name") for pt in pats if (m := pt.match(line))}
            for name, cre in call_res.items():
                if name in line and cre.search(line):
                    if name in def_names:
                        continue
                    callers[name][rel] += 1

    rows = []
    graph_nodes, graph_links = {}, defaultdict(int)
    for name, (deffile, kind) in symdefs.items():
        cfiles = callers.get(name, {})
        # "Untouched" splits into production callers (where regressions hide)
        # and test callers (which will at least run in CI) — conflating them
        # inflates the headline.
        outside = {f: c for f, c in cfiles.items() if f not in changed_files and not is_test_path(f)}
        outside_tests = {f: c for f, c in cfiles.items() if f not in changed_files and is_test_path(f)}
        inside = {f: c for f, c in cfiles.items() if f in changed_files and f != deffile}
        rows.append({"name": name, "file": deffile, "kind": kind,
                     "callers": len(cfiles), "outside": len(outside),
                     "inside": len(inside), "test_callers": len(outside_tests),
                     "outside_files": sorted(outside, key=lambda f: -outside[f]),
                     })
        graph_nodes.setdefault(deffile, {"id": deffile, "label": Path(deffile).name,
                                         "group": "changed", "size": 1})
        for f in cfiles:
            g = "changed" if f in changed_files else ("test (untouched)" if is_test_path(f) else "untouched caller")
            graph_nodes.setdefault(f, {"id": f, "label": Path(f).name, "group": g, "size": 1})
            graph_nodes[f]["size"] += cfiles[f]
            if f != deffile:
                graph_links[(f, deffile)] += cfiles[f]

    rows.sort(key=lambda r: (-r["outside"], -r["callers"]))
    risky = [r for r in rows if r["outside"] > 0 and r["kind"] in ("modified/added", "removed")]

    graph = {"nodes": list(graph_nodes.values()),
             "links": [{"source": a, "target": b, "weight": w} for (a, b), w in graph_links.items()]}
    for n in graph["nodes"]:
        n["meta"] = "in this diff" if n["group"] == "changed" else "NOT in this diff"

    def caller_list(r):
        if not r["outside_files"]:
            return ""
        li = "".join(f"<li><code>{esc(f)}</code></li>" for f in r["outside_files"][:15])
        more = f"<li class='dim'>… {len(r['outside_files'])-15} more</li>" if len(r["outside_files"]) > 15 else ""
        return f"<details><summary>{r['outside']} untouched caller file{'s' if r['outside']!=1 else ''}</summary><div class='body'><ul>{li}{more}</ul></div></details>"

    tbl = "".join(
        f"<tr><td><code>{esc(r['name'])}</code>"
        + (" <span class='badge bad'>removed</span>" if r["kind"] == "removed" else "")
        + f"</td><td><code>{esc(r['file'])}</code></td>"
        f"<td class='num'>{r['callers']}</td>"
        f"<td class='num'{' style=color:var(--bad);font-weight:600' if r['outside'] else ''}>{r['outside']}</td>"
        f"<td class='num'>{r['test_callers']}</td>"
        f"<td>{caller_list(r)}</td></tr>"
        for r in rows[:30])

    headline = (f"<div class='callout warn'><b>{len(risky)} changed symbol{'s reach' if len(risky)!=1 else ' reaches'} non-test call sites this PR never touched.</b> Those callers silently inherit the new behavior — they are where regressions hide, and their absence from the diff means no reviewer will look at them unless prompted. Verify each untouched caller still holds its assumptions. The list is name-matched, so same-named symbols from unrelated modules may appear in it; confirm each caller actually uses the changed definition before acting.</div>"
                if risky else
                "<div class='callout good'><b>All detected call sites of changed symbols are inside this diff.</b> The change is self-contained at the symbol level (name-match analysis; dynamic dispatch and reflection are invisible to it).</div>")
    truncation_note = (f"<p class='dim'>{truncated} further changed symbol(s) beyond the "
                       f"--max-symbols={args.max_symbols} cap were NOT traced.</p>" if truncated else "")

    body = f"""
<div class="kpis">
  <div class="kpi accent"><div class="n">{len(symdefs)}{f' of {total_symbols}' if truncated else ''}</div><div class="l">changed symbols traced</div></div>
  <div class="kpi {'bad' if risky else 'good'}"><div class="n">{sum(r['outside'] for r in rows)}</div><div class="l">untouched caller files (non-test)</div></div>
  <div class="kpi"><div class="n">{sum(r['test_callers'] for r in rows)}</div><div class="l">untouched test caller files</div></div>
</div>
{headline}
{truncation_note}
<h2>Caller graph</h2>
<p class="dim">Arrows point from caller to changed definition. Node color: changed in this diff vs untouched. Matching is textual (<code>name(</code> / <code>.name</code>) — precise enough to point a reviewer, not a proof.</p>
<div class="viz" data-render="forcegraph" style="height:560px"></div>
<script type="application/json">{json_block(graph)}</script>
<h2>Symbols, ordered by untouched callers</h2>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>Symbol</th><th>Defined in</th><th class="num">Caller files</th><th class="num">Untouched (non-test)</th><th class="num">In tests</th><th>Untouched callers</th></tr></thead>
<tbody>{tbl}</tbody></table></div>
{res_html}
"""
    write_fragment(tabs, "04-blast-radius.html", "Blast Radius", body)

    print(json.dumps({
        "base": base,
        "symbols_traced": len(symdefs),
        "symbols_total": total_symbols,
        "symbols_not_traced": truncated,
        "caveat": CAVEAT,
        "excluded_generated_docs": excluded_docs,
        "renames": renames,
        # A prompt or template this diff edited is inherited by every file that
        # loads it, exactly as a changed signature is inherited by its callers.
        "changed_resources": changed_resources,
        "resource_caveat": resources.CAVEAT,
        "symbols": [{k: r[k] for k in ("name", "file", "kind", "callers", "outside", "test_callers")} | {"untouched_callers": r["outside_files"][:10]}
                    for r in rows[:25]],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
