#!/usr/bin/env python3
"""Footprint + Contracts & Tests tabs for a PR/branch/commit-range.

Usage:
  python3 analyze_diff.py REPO --tabs-dir TABS [--base REF] [--head REF] [--worktree]

Emits: TABS/02-footprint.html, TABS/03-contracts-tests.html
Prints a JSON summary on stdout (for the agent's judgment tabs).
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from common import bar_cell, esc, git, is_test_path, json_block, write_fragment
from diffutil import def_patterns_for, is_generated_doc, parse_diff, resolve_base

CONFIG_EXT = {".yaml", ".yml", ".toml", ".ini", ".env", ".cfg", ".conf", ".properties", ".json"}
DOC_EXT = {".md", ".rst", ".txt", ".adoc"}
MANIFESTS = {"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
             "requirements.txt", "pyproject.toml", "poetry.lock", "uv.lock", "setup.py", "setup.cfg",
             "go.mod", "go.sum", "cargo.toml", "cargo.lock", "gemfile", "gemfile.lock",
             "pom.xml", "build.gradle", "build.gradle.kts", "composer.json"}

RISK_PATTERNS = [
    ("security", 3, re.compile(r"\b(auth|password|passwd|token|secret|credential|permission|privilege|jwt|oauth|csrf|cors|sanitiz|escape|crypt)\w*", re.I)),
    ("injection-surface", 3, re.compile(r"\b(eval|exec)\s*\(|pickle\.loads|yaml\.load\s*\(|subprocess|shell\s*=\s*True|innerHTML|dangerouslySetInnerHTML|f(?:ormat)?[\"']*\s*%\s*|execute\s*\(\s*f?[\"'].*(SELECT|INSERT|UPDATE|DELETE)", re.I)),
    ("concurrency", 2, re.compile(r"\b(lock|mutex|rlock|semaphore|atomic|threading|thread\b|goroutine|channel|asyncio|await|async |volatile|synchronized)", re.I)),
    ("transactions", 2, re.compile(r"\b(transaction|commit|rollback|savepoint|isolation)", re.I)),
    ("error-swallowing", 3, re.compile(r"except\s*(\w+\s*)?:\s*(pass|\.\.\.)\s*$|catch\s*\([^)]*\)\s*\{\s*\}|\.catch\(\s*\(\)\s*=>\s*\{?\s*\}?\s*\)")),
    ("retries/timeouts", 1, re.compile(r"\b(retry|retries|backoff|timeout|deadline)", re.I)),
    ("caching", 1, re.compile(r"\b(cache|memoiz|invalidat|ttl)\w*", re.I)),
    ("feature-flag", 1, re.compile(r"\b(feature.?flag|flag.?enabled|toggle|rollout)", re.I)),
    ("todo-markers", 1, re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")),
]


def categorize(path: str) -> str:
    p = path.lower()
    name = Path(p).name
    ext = Path(p).suffix
    if is_test_path(path):
        return "test"
    if re.search(r"(^|/)(migrations?|schema|db/migrate)(/|$)", p) or ext == ".sql":
        return "schema/migration"
    if name in MANIFESTS:
        return "dependencies"
    if re.search(r"(openapi|swagger)\.", name) or ext in (".proto", ".graphql", ".graphqls", ".avsc", ".thrift"):
        return "api-contract"
    if re.search(r"(^|/)(\.github|ci|\.circleci|\.gitlab)(/|$)", p) or name.startswith((".gitlab-ci", "jenkinsfile", "dockerfile")) or name == "makefile":
        return "build/ci"
    if ext in DOC_EXT:
        return "docs"
    if ext in CONFIG_EXT:
        return "config"
    return "source"


CAT_WEIGHT = {"schema/migration": 3, "api-contract": 3, "dependencies": 2, "config": 2,
              "build/ci": 1, "source": 1, "test": 0, "docs": 0}
CAT_BADGE = {"schema/migration": "bad", "api-contract": "bad", "dependencies": "warn",
             "config": "warn", "build/ci": "neutral", "source": "accent", "test": "good", "docs": "neutral"}


def signature_changes(fd):
    """Pair removed vs added definition lines by symbol name -> (name, old, new)."""
    pats = def_patterns_for(fd.path)
    if not pats:
        return [], [], []
    removed, added = {}, {}
    for text in fd.removed_lines():
        for pat in pats:
            m = pat.match(text)
            if m:
                removed.setdefault(m.group("name"), text.strip())
    for _, text in fd.added_lines():
        for pat in pats:
            m = pat.match(text)
            if m:
                added.setdefault(m.group("name"), text.strip())
    changed = [(n, removed[n], added[n]) for n in removed if n in added and removed[n] != added[n]]
    deleted = [(n, removed[n]) for n in removed if n not in added]
    new = [(n, added[n]) for n in added if n not in removed]
    return changed, deleted, new


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--tabs-dir", required=True)
    ap.add_argument("--base", default=None)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--worktree", action="store_true",
                    help="diff base against working tree (uncommitted changes included)")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    tabs = Path(args.tabs_dir)

    base, merge_base, note = resolve_base(repo, args.base, args.head)
    if args.worktree:
        raw = git(repo, "diff", "--no-color", merge_base)
        head_desc = "working tree"
        commits = []
    else:
        raw = git(repo, "diff", "--no-color", f"{merge_base}..{args.head}")
        head_desc = args.head
        commits = [l for l in git(repo, "log", "--oneline", f"{merge_base}..{args.head}").splitlines() if l.strip()]

    fds = parse_diff(raw)
    excluded_docs = [f.path for f in fds if is_generated_doc(f.path)]
    fds = [f for f in fds if not is_generated_doc(f.path)]
    if not fds:
        msg = ("diff contains only generated report docs (excluded): " + ", ".join(excluded_docs)
               if excluded_docs else "empty diff")
        print(json.dumps({"error": msg, "base": base, "head": head_desc}))
        return 1

    total_add = sum(f.adds for f in fds)
    total_del = sum(f.dels for f in fds)
    by_cat = defaultdict(list)

    file_rows = []
    all_sig_changed, all_sig_deleted, all_sig_new = [], [], []
    risk_hits_global = []
    for fd in fds:
        cat = categorize(fd.path)
        by_cat[cat].append(fd)
        reasons = []
        score = CAT_WEIGHT.get(cat, 1)
        if score >= 2:
            reasons.append(cat)
        hits = defaultdict(int)
        examples = {}
        if cat not in ("test", "docs"):  # risky-pattern scoring targets shipped behavior
            for ln, text in fd.added_lines():
                for label, w, pat in RISK_PATTERNS:
                    if pat.search(text):
                        hits[(label, w)] += 1
                        if label not in examples:
                            examples[label] = (ln, text.strip()[:160])
        for (label, w), n in hits.items():
            score += min(w * n, w * 2)  # cap repeats
            reasons.append(f"{label}×{n}")
            risk_hits_global.append({"file": fd.path, "label": label, "line": examples[label][0], "text": examples[label][1], "count": n})
        chg, dele, new = signature_changes(fd)
        if cat == "source":
            if chg:
                score += 3
                reasons.append(f"signature-change×{len(chg)}")
            if dele:
                score += 2
                reasons.append(f"removed-symbol×{len(dele)}")
            all_sig_changed += [{"file": fd.path, "name": n, "old": o, "new": w} for n, o, w in chg]
            all_sig_deleted += [{"file": fd.path, "name": n, "old": o} for n, o in dele]
        all_sig_new += [{"file": fd.path, "name": n, "new": w} for n, w in new]
        if fd.dels > 50 and fd.dels > 2 * fd.adds:
            score += 1
            reasons.append("large-removal")
        file_rows.append({"fd": fd, "cat": cat, "score": score, "reasons": reasons})

    file_rows.sort(key=lambda r: (-r["score"], -r["fd"].changed))

    # ---- test delta ----
    changed_src = [r for r in file_rows if r["cat"] == "source" and r["fd"].status != "D" and not r["fd"].binary]
    changed_tests = [r["fd"].path for r in file_rows if r["cat"] == "test"]
    test_text = ""
    for p in changed_tests:
        try:
            test_text += git(repo, "show", f"{args.head}:{p}") if not args.worktree else (repo / p).read_text(errors="replace")
        except Exception:
            pass
    uncovered, covered = [], []
    for r in changed_src:
        stem = Path(r["fd"].path).stem.lower()
        name_hit = any(stem in Path(t).name.lower() for t in changed_tests)
        content_hit = len(stem) > 3 and re.search(re.escape(stem), test_text, re.I)
        sym_hit = any(s["file"] == r["fd"].path and s["name"].lower() in test_text.lower()
                      for s in all_sig_changed + all_sig_new) if test_text else False
        (covered if (name_hit or content_hit or sym_hit) else uncovered).append(r)

    # ================= Footprint fragment =================
    cat_order = sorted(by_cat, key=lambda c: -sum(f.changed for f in by_cat[c]))
    max_cat = max(sum(f.changed for f in by_cat[c]) for c in cat_order)
    cat_rows = "\n".join(
        f"<tr><td><span class='badge {CAT_BADGE.get(c,'neutral')}'>{esc(c)}</span></td>"
        f"<td class='num'>{len(by_cat[c])}</td>"
        f"<td class='num'><span class='plusminus'><span class='p'>+{sum(f.adds for f in by_cat[c]):,}</span> <span class='m'>−{sum(f.dels for f in by_cat[c]):,}</span></span></td>"
        f"<td>{bar_cell(sum(f.changed for f in by_cat[c]), max_cat)}</td></tr>"
        for c in cat_order)

    dirs = defaultdict(int)
    for fd in fds:
        d = str(Path(fd.path).parent)
        dirs["(root)" if d == "." else d] += fd.changed
    spread = len(dirs)
    shape = ("tightly clustered" if spread <= 2 else
             "moderately spread" if spread <= 5 else "shotgun — spread across many directories")

    max_ch = max(f.changed for f in fds) or 1
    ftbl = []
    for r in file_rows[:40]:
        fd = r["fd"]
        aw = 100.0 * fd.adds / max_ch
        dw = 100.0 * fd.dels / max_ch
        status = {"A": "<span class='badge good'>new</span>", "D": "<span class='badge bad'>deleted</span>",
                  "R": "<span class='badge neutral'>renamed</span>"}.get(fd.status, "")
        ftbl.append(
            f"<tr><td><code>{esc(fd.path)}</code> {status}</td>"
            f"<td><span class='badge {CAT_BADGE.get(r['cat'],'neutral')}'>{esc(r['cat'])}</span></td>"
            f"<td class='num'><span class='plusminus'><span class='p'>+{fd.adds}</span> <span class='m'>−{fd.dels}</span></span></td>"
            f"<td data-sort='{fd.changed}'><div class='dbar'><i class='a' style='width:{aw:.1f}%'></i><i class='d' style='width:{dw:.1f}%'></i></div></td>"
            f"<td class='num'>{r['score']}</td>"
            f"<td class='dim' style='font-size:12px'>{esc(', '.join(r['reasons'][:4]))}</td></tr>")

    tm = {"items": [{"name": r["fd"].path, "value": max(1, r["fd"].changed), "metric": r["score"],
                     "meta": f"+{r['fd'].adds}/−{r['fd'].dels} · {r['cat']}"}
                    for r in file_rows if not r["fd"].binary][:80],
          "valueLabel": "lines changed", "metricLabel": "risk score"}

    commits_html = ""
    if commits:
        li = "".join(f"<li><code>{esc(c.split(' ',1)[0])}</code> {esc(c.split(' ',1)[1] if ' ' in c else '')}</li>" for c in commits[:30])
        commits_html = f"<details><summary>{len(commits)} commit{'s' if len(commits)!=1 else ''} in range</summary><div class='body'><ul>{li}</ul></div></details>"

    body = f"""
<div class="kpis">
  <div class="kpi accent"><div class="n">{len(fds)}</div><div class="l">files changed</div></div>
  <div class="kpi good"><div class="n">+{total_add:,}</div><div class="l">additions</div></div>
  <div class="kpi bad"><div class="n">−{total_del:,}</div><div class="l">deletions</div></div>
  <div class="kpi"><div class="n">{len(commits) if commits else '–'}</div><div class="l">commits</div></div>
  <div class="kpi {'good' if spread<=2 else 'warn' if spread<=5 else 'bad'}"><div class="n">{spread}</div><div class="l">directories touched</div></div>
</div>
<div class="callout"><b>Footprint shape: {esc(shape)}.</b> A tightly clustered change usually means one concern; a diff smeared across many directories can indicate shotgun surgery — one logical change forced through poorly separated modules — or several unrelated changes bundled into one PR.</div>

<h2>Change map</h2>
<p class="dim">Area = lines changed · color = heuristic risk score · hover for details.</p>
<div class="viz" data-render="treemap" style="height:480px"></div>
<script type="application/json">{json_block(tm)}</script>

<h2>By category</h2>
<div class="tbl-wrap"><table>
<thead><tr><th>Category</th><th class="num">Files</th><th class="num">+/−</th><th>Volume</th></tr></thead>
<tbody>{cat_rows}</tbody></table></div>

<h2>Files, risk-ordered</h2>
<p class="dim">Reviewer attention is a budget — spend it top-down. Score combines category weight, risky patterns in added lines, signature changes, and large removals. Heuristic, not verdict.</p>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>File</th><th>Category</th><th class="num">+/−</th><th>Change</th><th class="num">Risk</th><th>Why</th></tr></thead>
<tbody>{''.join(ftbl)}</tbody></table></div>
{commits_html}
"""
    write_fragment(tabs, "02-footprint.html", "Footprint", body)

    # ================= Contracts & Tests fragment =================
    def sig_rows(items, kind):
        out = []
        for s in items[:30]:
            pub = not s["name"].startswith("_")
            badge = "<span class='badge warn'>public?</span>" if pub else "<span class='badge neutral'>internal</span>"
            if kind == "changed":
                out.append(f"<tr><td><code>{esc(s['file'])}</code></td><td><code>{esc(s['name'])}</code> {badge}</td>"
                           f"<td><div class='diff-snippet' style='margin:0'><span class='ln del'>- {esc(s['old'])}</span><span class='ln add'>+ {esc(s['new'])}</span></div></td></tr>")
            elif kind == "deleted":
                out.append(f"<tr><td><code>{esc(s['file'])}</code></td><td><code>{esc(s['name'])}</code> {badge}</td>"
                           f"<td><div class='diff-snippet' style='margin:0'><span class='ln del'>- {esc(s['old'])}</span></div></td></tr>")
        return "".join(out)

    contract_files = [r for r in file_rows if r["cat"] in ("schema/migration", "api-contract", "dependencies")]
    cf_rows = "".join(
        f"<tr><td><code>{esc(r['fd'].path)}</code></td>"
        f"<td><span class='badge {CAT_BADGE[r['cat']]}'>{esc(r['cat'])}</span></td>"
        f"<td class='num'><span class='plusminus'><span class='p'>+{r['fd'].adds}</span> <span class='m'>−{r['fd'].dels}</span></span></td></tr>"
        for r in contract_files)

    unc_rows = "".join(
        f"<tr><td><code>{esc(r['fd'].path)}</code></td>"
        f"<td class='num'><span class='plusminus'><span class='p'>+{r['fd'].adds}</span> <span class='m'>−{r['fd'].dels}</span></span></td>"
        f"<td class='dim'>{esc(', '.join(x for x in r['reasons'] if 'signature' in x or '×' in x)[:80]) or '—'}</td></tr>"
        for r in uncovered)
    cov_note = (f"{len(covered)} of {len(changed_src)} changed source files have matching test changes."
                if changed_src else "No source files changed.")

    sig_section = ""
    if all_sig_changed:
        sig_section += f"""<h2>Changed signatures</h2>
<p class="dim">Every caller of these symbols inherits the new contract — the Blast Radius tab lists them.</p>
<div class="tbl-wrap"><table><thead><tr><th>File</th><th>Symbol</th><th>Old → new</th></tr></thead><tbody>{sig_rows(all_sig_changed,'changed')}</tbody></table></div>"""
    if all_sig_deleted:
        sig_section += f"""<h3>Removed symbols</h3>
<div class="tbl-wrap"><table><thead><tr><th>File</th><th>Symbol</th><th>Removed definition</th></tr></thead><tbody>{sig_rows(all_sig_deleted,'deleted')}</tbody></table></div>"""
    if not sig_section:
        sig_section = "<div class='callout good'><b>No function/class signature changes detected</b> in supported languages — the change appears additive or internal to function bodies.</div>"

    contract_callout = (f"<div class='callout bad'><b>{len(contract_files)} contract-surface file{'s' if len(contract_files)!=1 else ''} touched</b> (schema, API definitions, dependency manifests). These outlive the code that reads them and deserve several times the scrutiny of internal changes.</div>"
                        if contract_files else
                        "<div class='callout good'><b>No schema, API-contract, or dependency-manifest files touched.</b></div>")

    test_callout = (f"<div class='callout warn'><b>{len(uncovered)} changed source file{'s have' if len(uncovered)!=1 else ' has'} no corresponding test change.</b> A modified conditional with untouched tests means the tests never pinned that behavior down — the change is effectively unverified.</div>"
                    if uncovered else
                    f"<div class='callout good'><b>{esc(cov_note)}</b></div>")

    body2 = f"""
{contract_callout}
{f'<div class="tbl-wrap"><table><thead><tr><th>File</th><th>Kind</th><th class="num">+/−</th></tr></thead><tbody>{cf_rows}</tbody></table></div>' if contract_files else ''}
{sig_section}
<h2>Test delta</h2>
<p class="dim">Match is heuristic: a changed test counts as covering a source file if it shares a name stem or mentions the file's changed symbols. {esc(cov_note)}</p>
{test_callout}
{f'<div class="tbl-wrap"><table class="sortable"><thead><tr><th>Changed file — no test change found</th><th class="num">+/−</th><th>Risk signals</th></tr></thead><tbody>{unc_rows}</tbody></table></div>' if uncovered else ''}
"""
    write_fragment(tabs, "03-contracts-tests.html", "Contracts & Tests", body2)

    # ================= stdout summary =================
    summary = {
        "base": base, "merge_base": merge_base[:12], "head": head_desc, "note": note,
        "excluded_generated_docs": excluded_docs,
        "commits": len(commits) if commits else None,
        "totals": {"files": len(fds), "additions": total_add, "deletions": total_del,
                   "directories": spread, "shape": shape},
        "by_category": {c: len(by_cat[c]) for c in cat_order},
        "risk_ordered_files": [{"path": r["fd"].path, "cat": r["cat"], "score": r["score"],
                                "adds": r["fd"].adds, "dels": r["fd"].dels,
                                "reasons": r["reasons"]} for r in file_rows[:25]],
        "signature_changes": all_sig_changed[:25],
        "removed_symbols": all_sig_deleted[:25],
        "new_symbols": [s["name"] for s in all_sig_new][:40],
        "contract_files": [r["fd"].path for r in contract_files],
        "source_files_without_test_changes": [r["fd"].path for r in uncovered],
        "risky_added_lines": sorted(risk_hits_global, key=lambda h: -h["count"])[:30],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
