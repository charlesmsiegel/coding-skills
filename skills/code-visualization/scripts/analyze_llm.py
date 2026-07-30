#!/usr/bin/env python3
"""LLM Ops tab: model call sites, models in use, prompt lineage, mechanical gaps.

Usage: python3 analyze_llm.py REPO_DIR --tabs-dir TABS_DIR
Emits: TABS_DIR/10-llm-ops.html and a JSON summary on stdout.

Writes no fragment at all when the repo calls no model — the assembler drops
absent tabs, so a codebase with no LLM in it simply has no LLM tab.

Everything here is extracted, not judged: a call site with a file:line, the model
named at it, which parameters are passed, which prompt file feeds it. The gaps
are mechanical absences, each cited so a reader can confirm or dismiss one.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import llmops
import resources
from common import esc, walk_source, write_fragment

GAP_LABELS = {
    "no-max-tokens": ("bad", "no max_tokens"),
    "unbounded-output": ("warn", "unbounded output"),
    "no-timeout-or-retry": ("warn", "no timeout or retry"),
    "hardcoded-model": ("warn", "model fixed at call site"),
    "interpolated-system-prompt": ("warn", "interpolated system prompt"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--tabs-dir", required=True)
    ap.add_argument("--exclude", default="",
                    help="comma-separated extra directory names to skip")
    args = ap.parse_args()
    extra_exclude = {d.strip() for d in args.exclude.split(",") if d.strip()}
    repo = Path(args.repo).resolve()

    files = dict(walk_source(repo, extra_exclude=extra_exclude))
    refs = resources.scan(repo, files).refs
    scan = llmops.scan(files, refs=refs)

    if not scan.sites and not scan.models:
        print(json.dumps({"note": "no LLM usage detected",
                          "caveat": llmops.CAVEAT}, indent=2))
        return 0

    providers = sorted({s.provider for s in scan.sites if s.provider})
    by_kind = defaultdict(list)
    for gap in scan.gaps:
        by_kind[gap["kind"]].append(gap)

    site_rows = "\n".join(
        f"<tr><td><code>{esc(s.path)}:{s.line}</code></td><td>{esc(s.provider or '—')}</td>"
        f"<td><code>{esc(s.api)}</code></td><td>{esc(s.model or '—')}</td>"
        f"<td>{esc(', '.join(k for k, v in s.params.items() if v) or '—')}</td></tr>"
        for s in sorted(scan.sites, key=lambda s: (s.path, s.line)))

    model_rows = "\n".join(
        f"<tr><td><code>{esc(model)}</code></td><td class='num'>{len(cites)}</td>"
        f"<td>{' '.join(f'<code>{esc(c)}</code>' for c in cites[:5])}"
        + (f" <span class='dim'>… {len(cites)-5} more</span>" if len(cites) > 5 else "")
        + "</td></tr>"
        for model, cites in scan.models.items())

    prompt_rows = "\n".join(
        f"<tr><td><code>{esc(asset)}</code></td>"
        f"<td>{' '.join(f'<code>{esc(c)}</code>' for c in callers)}</td></tr>"
        for asset, callers in sorted(scan.prompt_assets.items()))
    prompt_html = f"""
<h2>Prompt lineage</h2>
<p class="dim">Prompt files loaded by the same file that calls the model. Changing one of these changes
behavior exactly as editing code does — and no type checker will tell you.</p>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>Prompt asset</th><th>Loaded by (calls the model)</th></tr></thead>
<tbody>{prompt_rows}</tbody></table></div>
""" if prompt_rows else ""

    inline_html = ""
    if scan.inline_prompts:
        items = " ".join(f"<code>{esc(p['path'])}:{p['line']}</code> "
                         f"<span class='dim'>({p['chars']:,} chars)</span>"
                         for p in scan.inline_prompts[:15])
        inline_html = (f"""<div class="callout"><b>{len(scan.inline_prompts)} inline prompt"""
                       f"""{'s' if len(scan.inline_prompts) != 1 else ''}</b> — long strings living in code """
                       f"""rather than in a prompt file, so they version with the module and are invisible """
                       f"""to anyone reviewing prompts.<p style="margin-top:8px">{items}</p></div>""")

    gap_html = ""
    if scan.gaps:
        gap_rows = []
        for k, gaps in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
            badge_cls, badge_text = GAP_LABELS.get(k, ("warn", k))
            cites = " ".join(f"<code>{esc(g['cite'])}</code>" for g in gaps[:8])
            if len(gaps) > 8:
                cites += f" <span class='dim'>… {len(gaps) - 8} more</span>"
            gap_rows.append(
                f"<tr><td><span class='badge {badge_cls}'>{esc(badge_text)}</span></td>"
                f"<td class='num'>{len(gaps)}</td>"
                f"<td>{cites}</td>"
                f"<td class='dim'>{esc(gaps[0]['detail'])}</td></tr>")
        rows = "\n".join(gap_rows)
        gap_html = f"""
<h2>Mechanical gaps</h2>
<p class="dim">Absences a machine can see, not judgments about the prompts. Each is a question for the
reader: is this deliberate here?</p>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>Gap</th><th class="num">Sites</th><th>Where</th><th>What it means</th></tr></thead>
<tbody>{rows}</tbody></table></div>
"""

    body = f"""
<div class="kpis">
  <div class="kpi accent"><div class="n">{len(scan.sites)}</div><div class="l">call sites</div></div>
  <div class="kpi"><div class="n">{len(providers)}</div><div class="l">{'provider' if len(providers)==1 else 'providers'}</div></div>
  <div class="kpi"><div class="n">{len(scan.models)}</div><div class="l">distinct models</div></div>
  <div class="kpi"><div class="n">{len(scan.prompt_assets)}</div><div class="l">prompt files</div></div>
  <div class="kpi {'warn' if scan.gaps else 'good'}"><div class="n">{len(scan.gaps)}</div><div class="l">gaps</div></div>
</div>
<div class="callout"><b>How this was found.</b> {esc(llmops.CAVEAT)}</div>
{inline_html}
<h2>Call sites</h2>
<p class="dim">Where this codebase talks to a model, with the parameters each call passes.</p>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>Site</th><th>Provider</th><th>API</th><th>Model</th><th>Parameters passed</th></tr></thead>
<tbody>{site_rows}</tbody></table></div>

<h2>Models in use</h2>
<p class="dim">Every string in the codebase that names a deployed model, and where it is written.</p>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>Model</th><th class="num">Mentions</th><th>Written at</th></tr></thead>
<tbody>{model_rows}</tbody></table></div>
{prompt_html}
{gap_html}
"""
    write_fragment(Path(args.tabs_dir), "10-llm-ops.html", "LLM Ops", body)

    summary = {
        "call_sites": len(scan.sites),
        "providers": providers,
        "models": scan.models,
        "sites": [{"cite": f"{s.path}:{s.line}", "provider": s.provider, "api": s.api,
                   "model": s.model,
                   "params": sorted(k for k, v in s.params.items() if v)}
                  for s in sorted(scan.sites, key=lambda s: (s.path, s.line))],
        "prompt_assets": scan.prompt_assets,
        "inline_prompts": scan.inline_prompts[:15],
        # Gaps are input for the Invariants & Risks tab: each one is a question
        # ("is the missing timeout deliberate here?"), not a verdict.
        "gaps": scan.gaps,
        "caveat": llmops.CAVEAT,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
