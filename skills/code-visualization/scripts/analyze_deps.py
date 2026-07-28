#!/usr/bin/env python3
"""Dependencies tab: module import graph, cycles, fan-in/fan-out.

Usage: python3 analyze_deps.py REPO_DIR --tabs-dir TABS_DIR [--depth N]
Emits: TABS_DIR/03-dependencies.html and a JSON summary on stdout.

Supported import extraction: Python (ast), JS/TS (import/require/export-from),
Go (module-path imports), Rust (use crate::), Java/Kotlin (import pkg.Class),
Ruby (require_relative). Other languages appear as nodes without edges.
"""
import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from common import (bar_cell, detect_lang, esc, json_block,
                    loc_and_complexity, read_text, walk_source, write_fragment)

JS_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"]
JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[\w*{}\s,$]+\s+from\s+)?|export\s+(?:[\w*{}\s,$]+\s+from\s+)|require\s*\(\s*|import\s*\(\s*)['"]([^'"]+)['"]"""
)
GO_IMPORT_RE = re.compile(r'^\s*(?:[\w.]+\s+)?"([^"]+)"', re.M)
RUST_USE_RE = re.compile(r"^\s*(?:pub\s+)?use\s+crate::([\w:]+)", re.M)
JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", re.M)
RUBY_REQ_RE = re.compile(r"""require_relative\s+['"]([^'"]+)['"]""")


def build_python_index(files):
    idx = {}
    for rel in files:
        if not rel.endswith((".py", ".pyi")):
            continue
        parts = rel.rsplit(".", 1)[0].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        for skip in range(0, min(3, len(parts))):  # tolerate src/, lib/ prefixes
            key = ".".join(parts[skip:])
            if key and key not in idx:
                idx[key] = rel
    return idx


def python_edges(rel, text, py_idx):
    edges = set()
    try:
        tree = ast.parse(text)
    # ValueError: NUL bytes (e.g. a mis-detected binary); RecursionError:
    # pathologically nested literals. Either must cost one file, not the run.
    except (SyntaxError, ValueError, RecursionError):
        return edges
    pkg_parts = rel.rsplit(".", 1)[0].split("/")
    if pkg_parts[-1] == "__init__":
        pkg_parts = pkg_parts[:-1]
    else:
        pkg_parts = pkg_parts[:-1]

    def resolve(name):
        for cand in (name, name.rsplit(".", 1)[0] if "." in name else None):
            if cand and cand in py_idx:
                return py_idx[cand]
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                t = resolve(a.name)
                if t:
                    edges.add(t)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                mod = ".".join(base + (node.module.split(".") if node.module else []))
            else:
                mod = node.module or ""
            if not mod:
                continue
            t = resolve(mod)
            if t:
                edges.add(t)
            for a in node.names:
                t2 = resolve(f"{mod}.{a.name}")
                if t2:
                    edges.add(t2)
    edges.discard(rel)
    return edges


def resolve_relative_js(rel, spec, file_set):
    base = Path(rel).parent
    target = (base / spec)
    cands = []
    norm = str(target).replace("\\", "/")
    norm = re.sub(r"/\./", "/", "/" + norm).lstrip("/")
    parts = []
    for part in norm.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    norm = "/".join(parts)
    if re.search(r"\.[a-z]+$", norm):
        cands.append(norm)
    for e in JS_EXTS:
        cands.append(norm + e)
        cands.append(norm + "/index" + e)
    for c in cands:
        if c in file_set:
            return c
    return None


def go_module_path(repo: Path):
    gomod = repo / "go.mod"
    if gomod.exists():
        m = re.search(r"^module\s+(\S+)", read_text(gomod), re.M)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------- module tree
# Directory names that are packaging structure rather than a unit of design:
# nobody calls "src" a module. The repo's own name is added at runtime, so
# src/<reponame>/core reads as "core". These are hints, not a verdict — a
# directory only gets peeled if it is ALSO a pass-through (see peelable).
STRUCTURAL_HINTS = {
    "src", "lib", "libs", "source", "sources", "app", "apps",
    "pkg", "packages", "internal", "cmd", "main",
    "java", "kotlin", "scala",
}
# Grouping targets. Below MIN the graph is too coarse to show design (a
# frontend/backend monorepo rendering as two boxes); above MAX it is unreadable.
TARGET_MIN, TARGET_MAX = 12, 30
OWN_CODE_FLOOR = 0.10  # a dir holding this share of its subtree's LOC is real


def normalize_name(name: str) -> str:
    return re.sub(r"[-_. ]", "", name).lower()


def build_dir_tree(locs):
    """Map every directory to its own LOC, subtree LOC, and child directories.

    "own" counts only files sitting directly in the directory; "sub" counts the
    whole subtree. The ratio between them is what separates a pass-through
    wrapper from a directory that actually holds code.
    """
    nodes = defaultdict(lambda: {"own": 0, "sub": 0, "kids": set()})
    nodes[""]  # the repo root always exists, even for an empty repo
    for rel, n in locs.items():
        parts = rel.split("/")
        cur = ""
        nodes[cur]["sub"] += n
        for seg in parts[:-1]:
            parent = cur
            cur = f"{cur}/{seg}" if cur else seg
            nodes[parent]["kids"].add(cur)
            nodes[cur]["sub"] += n
        nodes[cur]["own"] += n
    return nodes


def peelable(d, nodes, hints, chains=True):
    """True if d is structure to descend past rather than a module in its own right.

    Three conditions, all required. It must have somewhere to descend to; it
    must be nearly empty of its own code (this is what keeps a directory named
    "services" full of .py files from being mistaken for a wrapper); and it must
    either carry a structural name or be a single-child link in a chain like
    frontend/ -> src/ or Java's com/example/.

    chains=False drops that last clause, keeping single-child directories as
    modules. --depth uses it so that asking for depth 1 on a frontend/backend
    repo yields the two arms rather than diving straight through them.
    """
    if d == "":
        return True  # the repo root is never a module
    info = nodes[d]
    if not info["kids"]:
        return False
    if info["own"] / max(info["sub"], 1) >= OWN_CODE_FLOOR:
        return False
    name = normalize_name(d.rsplit("/", 1)[-1])
    return name in hints or (chains and len(info["kids"]) == 1)


def expand(d, nodes, hints, chains=True):
    """Resolve d to the frontier nodes it stands for, peeling structural dirs.

    A peeled directory is dropped unless it holds loose files of its own, in
    which case it stays behind to catch them — no file is ever orphaned.
    """
    out, stack = [], [d]
    while stack:
        cur = stack.pop()
        if peelable(cur, nodes, hints, chains):
            stack.extend(nodes[cur]["kids"])
            if nodes[cur]["own"] > 0:
                out.append(cur)
        else:
            out.append(cur)
    return out


def split_children(d, nodes, hints, chains=True):
    return {n for k in nodes[d]["kids"] for n in expand(k, nodes, hints, chains)}


def partition_modules(nodes, hints):
    """Choose the module set: peel to the top-level arms, then split the biggest.

    Splitting the largest node repeatedly (rather than applying one uniform
    depth) lets a deep arm decompose while a shallow one stays whole, which is
    what a mixed monorepo needs.
    """
    frontier = set(expand("", nodes, hints))
    while len(frontier) < TARGET_MIN:
        for n in sorted((m for m in frontier if nodes[m]["kids"]),
                        key=lambda m: -nodes[m]["sub"]):
            repl = split_children(n, nodes, hints)
            if nodes[n]["own"] > 0:
                repl.add(n)
            if not repl - frontier:
                continue  # already split; splitting again adds nothing
            merged = (frontier - {n}) | repl
            if len(merged) <= TARGET_MAX:
                frontier = merged
                break
        else:
            break  # nothing left that fits the budget
    return frontier


def partition_at_depth(nodes, hints, depth):
    """--depth override: fixed nesting depth, ignoring structurally-named dirs.

    Only hint-named wrappers are free here; every other directory costs a level.
    That keeps the knob usable in the coarsening direction, which is the reason
    anyone reaches for it.
    """
    out = set()

    def collect(d, remaining):
        children = split_children(d, nodes, hints, chains=False) if remaining > 0 else set()
        if not children:
            out.add(d)
            return
        for child in children:
            collect(child, remaining - 1)
        if nodes[d]["own"] > 0:
            out.add(d)

    for seed in expand("", nodes, hints, chains=False):
        if seed == "":
            out.add(seed)  # loose root files; never recurse from the root twice
        else:
            collect(seed, depth - 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--tabs-dir", required=True)
    ap.add_argument("--exclude", default="",
                    help="comma-separated extra directory names to skip (e.g. generated,migrations)")
    ap.add_argument("--depth", type=int, default=0,
                    help="fixed module nesting depth, counting only non-structural "
                         "directories (0=auto: split the largest module until the "
                         "graph holds 12-30 nodes)")
    ap.add_argument("--max-nodes", type=int, default=55)
    args = ap.parse_args()
    extra_exclude = {d.strip() for d in args.exclude.split(",") if d.strip()}
    repo = Path(args.repo).resolve()

    paths, locs = {}, {}
    for rel, p in walk_source(repo, extra_exclude=extra_exclude):
        lang = detect_lang(rel)
        if lang in ("Other", "Markdown", "reStructuredText", "JSON", "YAML", "TOML", "HTML", "CSS"):
            continue
        paths[rel] = p
        locs[rel] = loc_and_complexity(read_text(p))[0]
    file_set = set(paths)
    py_idx = build_python_index(file_set)
    go_mod = go_module_path(repo)
    # dir -> .go files, so Go import resolution is a lookup instead of a scan
    # over every file for every import (quadratic on large Go monorepos).
    go_files_by_dir = defaultdict(list)
    for f in file_set:
        if f.endswith(".go"):
            go_files_by_dir[str(Path(f).parent).replace("\\", "/")].append(f)

    edges = defaultdict(set)  # src file -> {dst files}
    # Files are re-read here rather than kept from the first pass: holding
    # every source text at once is multi-GB on big repos.
    for rel, text in ((rel, read_text(p)) for rel, p in paths.items()):
        ext = "." + rel.rsplit(".", 1)[-1] if "." in rel else ""
        if ext in (".py", ".pyi"):
            edges[rel] |= python_edges(rel, text, py_idx)
        elif ext in JS_EXTS:
            for spec in JS_IMPORT_RE.findall(text):
                if spec.startswith("."):
                    t = resolve_relative_js(rel, spec, file_set)
                    if t and t != rel:
                        edges[rel].add(t)
        elif ext == ".go" and go_mod:
            in_block = False
            for line in text.splitlines():
                if re.match(r"^\s*import\s*\(", line):
                    in_block = True
                    continue
                if in_block and line.strip() == ")":
                    in_block = False
                    continue
                m = None
                if in_block:
                    m = GO_IMPORT_RE.match(line)
                elif re.match(r"^\s*import\s", line):
                    m = re.search(r'"([^"]+)"', line)
                if m and m.group(1).startswith(go_mod):
                    sub = m.group(1)[len(go_mod):].strip("/")
                    src_dir = str(Path(rel).parent).replace("\\", "/")
                    for cand_dir in (sub, f"src/{sub}"):
                        if cand_dir == src_dir:
                            continue
                        for f in go_files_by_dir.get(cand_dir, ()):
                            if f != rel:
                                edges[rel].add(f)
        elif ext == ".rs":
            for use in RUST_USE_RE.findall(text):
                segs = use.split("::")
                for n in range(len(segs), 0, -1):
                    path = "/".join(segs[:n])
                    for cand in (f"src/{path}.rs", f"src/{path}/mod.rs", f"{path}.rs"):
                        if cand in file_set and cand != rel:
                            edges[rel].add(cand)
                            break
                    else:
                        continue
                    break
        elif ext in (".java", ".kt"):
            for imp in JAVA_IMPORT_RE.findall(text):
                path = imp.replace(".", "/")
                for cand in (f"{path}.java", f"{path}.kt",
                             f"src/main/java/{path}.java", f"src/main/kotlin/{path}.kt",
                             f"src/{path}.java"):
                    if cand in file_set and cand != rel:
                        edges[rel].add(cand)
                        break
        elif ext == ".rb":
            for spec in RUBY_REQ_RE.findall(text):
                t = resolve_relative_js(rel, spec if spec.endswith(".rb") else spec + ".rb", file_set)
                if t and t != rel:
                    edges[rel].add(t)

    # ---- module grouping ----
    hints = set(STRUCTURAL_HINTS) | {normalize_name(repo.name)}
    tree = build_dir_tree(locs)
    frontier = (partition_at_depth(tree, hints, args.depth) if args.depth > 0
                else partition_modules(tree, hints))
    # Longest prefix wins, so a file in frontend/api lands in frontend/api even
    # when its parent frontend/ is also a module (holding loose files).
    by_depth = sorted(frontier, key=len, reverse=True)
    dir_cache = {}

    def module_of(rel):
        d = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if d not in dir_cache:
            dir_cache[d] = next(
                (m for m in by_depth if m == "" or d == m or d.startswith(m + "/")), "")
        return dir_cache[d]

    def display_of(m):
        """Module path with the structural segments dropped, for reading."""
        if m == "":
            return "(root)"
        segs = [s for s in m.split("/") if normalize_name(s) not in hints]
        return "/".join(segs) or m

    def group_of(m):
        """Colour key: the arm a module belongs to (frontend/, backend/, ...)."""
        if m == "":
            return "(root)"
        return display_of(m).split("/")[0]

    mod_loc, mod_files = defaultdict(int), defaultdict(int)
    for rel, n in locs.items():
        m = module_of(rel)
        mod_loc[m] += n
        mod_files[m] += 1
    frontier = {m for m in frontier if mod_files.get(m)}  # drop emptied nodes
    peeled = sorted({s for m in frontier for s in m.split("/")
                     if normalize_name(s) in hints})
    keep = {m for m, _ in sorted(mod_loc.items(), key=lambda kv: -kv[1])[: args.max_nodes - 1]}
    lump = len(mod_loc) > len(keep)

    def mod_key(rel):
        m = module_of(rel)
        return m if m in keep else "(other)"

    mod_edges = defaultdict(int)
    for src, dsts in edges.items():
        ms = mod_key(src)
        for dst in dsts:
            md = mod_key(dst)
            if ms != md:
                mod_edges[(ms, md)] += 1

    # ---- Tarjan SCC (iterative) ----
    def find_sccs(vertices, adj):
        index, low, onstack, stack, sccs = {}, {}, set(), [], []
        counter = [0]

        def strongconnect(v):
            work = [(v, iter(sorted(adj[v])))]
            index[v] = low[v] = counter[0]
            counter[0] += 1
            stack.append(v)
            onstack.add(v)
            while work:
                node, it = work[-1]
                advanced = False
                for w in it:
                    if w not in index:
                        index[w] = low[w] = counter[0]
                        counter[0] += 1
                        stack.append(w)
                        onstack.add(w)
                        work.append((w, iter(sorted(adj[w]))))
                        advanced = True
                        break
                    elif w in onstack:
                        low[node] = min(low[node], index[w])
                if advanced:
                    continue
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    comp = []
                    while True:
                        w = stack.pop()
                        onstack.discard(w)
                        comp.append(w)
                        if w == node:
                            break
                    if len(comp) > 1:
                        sccs.append(sorted(comp))
        for v in vertices:
            if v not in index:
                strongconnect(v)
        return sccs

    adj = defaultdict(set)
    for (a, b) in mod_edges:
        adj[a].add(b)
    all_mods = sorted(set(mod_loc if not lump else list(keep) + ["(other)"]))
    sccs = find_sccs(all_mods, adj)

    file_adj = defaultdict(set)
    for src, dsts in edges.items():
        file_adj[src] |= dsts
    file_sccs = find_sccs(sorted(paths), file_adj)
    cyc_members = {m for scc in sccs for m in scc}
    scc_of = {}
    for i, scc in enumerate(sccs):
        for m in scc:
            scc_of[m] = i

    # ---- fan-in/out at file level ----
    fan_out = {f: len(d) for f, d in edges.items()}
    fan_in = defaultdict(int)
    for d in edges.values():
        for t in d:
            fan_in[t] += 1
    mod_fan_in, mod_fan_out = defaultdict(int), defaultdict(int)
    for (a, b), w in mod_edges.items():
        mod_fan_out[a] += 1
        mod_fan_in[b] += 1

    nodes = []
    for m in all_mods:
        if m == "(other)" and not lump:
            continue
        disp = "(other)" if m == "(other)" else display_of(m)
        # The node id stays the real directory path so citations elsewhere in
        # the atlas resolve; only the label and tooltip drop the structure.
        nodes.append({
            "id": m, "label": disp.split("/")[-1],
            "group": ("cycle" if m in cyc_members else
                      ("(other)" if m == "(other)" else group_of(m))),
            "size": mod_loc.get(m, 1), "fanIn": mod_fan_in.get(m, 0), "fanOut": mod_fan_out.get(m, 0),
            "meta": (f"{m or '(repo root)'} · {mod_files.get(m,0)} files"
                     + (f" · in dependency cycle #{scc_of[m]+1}" if m in cyc_members else "")),
        })
    links = [
        {"source": a, "target": b, "weight": w,
         "cycle": bool(a in cyc_members and b in cyc_members and scc_of.get(a) == scc_of.get(b))}
        for (a, b), w in mod_edges.items()
    ]
    graph = {"nodes": nodes, "links": links,
             "legendExtra": '<span><i class="sw" style="background:transparent;border:2px solid var(--bad);box-sizing:border-box"></i>red edges = cycle</span>'}

    hub_in = sorted(fan_in.items(), key=lambda kv: -kv[1])[:15]
    hub_out = sorted(fan_out.items(), key=lambda kv: -kv[1])[:15]
    max_in = hub_in[0][1] if hub_in else 1
    max_out = hub_out[0][1] if hub_out else 1

    cyc_html = ""
    if sccs:
        rows = "".join(
            f"<li><span class='badge bad'>cycle {i+1}</span> " +
            " ⇄ ".join(f"<code>{esc(m)}</code>" for m in scc) + "</li>"
            for i, scc in enumerate(sccs)
        )
        cyc_html = f"""<div class="callout bad"><b>{len(sccs)} module-level dependency cycle{'s' if len(sccs)!=1 else ''} detected</b> — modules that import each other directly or transitively. Cycles make modules impossible to understand or test in isolation.<ul style="margin-top:8px">{rows}</ul></div>"""
    else:
        cyc_html = """<div class="callout good"><b>No module-level dependency cycles detected.</b> The import graph is a DAG at this grouping level.</div>"""
    if file_sccs:
        frows = "".join(
            f"<li><span class='badge warn'>group {i+1}</span> " +
            " ⇄ ".join(f"<code>{esc(f)}</code>" for f in scc[:8]) +
            (f" <span class='dim'>… {len(scc)-8} more</span>" if len(scc) > 8 else "") + "</li>"
            for i, scc in enumerate(sorted(file_sccs, key=len, reverse=True)[:6])
        )
        cyc_html += f"""<details{' open' if not sccs else ''}><summary>File-level import cycles: {len(file_sccs)} strongly-connected group{'s' if len(file_sccs)!=1 else ''}</summary><div class="body"><p class="dim">Files that (transitively) import each other. In Python these often include type-annotation or lazy in-function imports — less harmful than top-level cycles, but each one still couples the files' lifecycles. Worth explaining case-by-case in the Boundaries tab.</p><ul>{frows}</ul></div></details>"""

    in_rows = "\n".join(
        f"<tr><td><code>{esc(f)}</code></td><td class='num'>{n}</td><td>{bar_cell(n, max_in)}</td><td class='num'>{fan_out.get(f,0)}</td></tr>"
        for f, n in hub_in)
    out_rows = "\n".join(
        f"<tr><td><code>{esc(f)}</code></td><td class='num'>{n}</td><td>{bar_cell(n, max_out, 'warn')}</td><td class='num'>{fan_in.get(f,0)}</td></tr>"
        for f, n in hub_out)

    edge_count = sum(1 for _ in ((s, t) for s, d in edges.items() for t in d))
    arms = sorted({n["group"] for n in nodes if n["group"] not in ("cycle", "(other)")}
                  | {group_of(m) for m in cyc_members if m != "(other)"})
    body = f"""
<div class="kpis">
  <div class="kpi accent"><div class="n">{len(nodes)}</div><div class="l">modules</div></div>
  <div class="kpi"><div class="n">{edge_count:,}</div><div class="l">file-level imports</div></div>
  <div class="kpi {'bad' if sccs else 'good'}"><div class="n">{len(sccs)}</div><div class="l">cycles</div></div>
  <div class="kpi"><div class="n">{len(arms)}</div><div class="l">top-level {'arm' if len(arms)==1 else 'arms'}</div></div>
</div>
{cyc_html}
<h2>Module import graph</h2>
<p class="dim">Node area = lines of code · arrows point from importer to imported · drag nodes, Ctrl/⌘-scroll to zoom, hover to isolate a neighborhood.</p>
<div class="viz" data-render="forcegraph" style="height:620px"></div>
<script type="application/json">{json_block(graph)}</script>

<div class="grid cols-2">
<div>
<h2>Highest fan-in (most depended-on)</h2>
<p class="dim">Changes here ripple widely — review with extra care.</p>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>File</th><th class="num">Fan-in</th><th></th><th class="num">Fan-out</th></tr></thead>
<tbody>{in_rows}</tbody></table></div>
</div>
<div>
<h2>Highest fan-out (most dependencies)</h2>
<p class="dim">High fan-out can indicate orchestrators — or god modules doing too much.</p>
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>File</th><th class="num">Fan-out</th><th></th><th class="num">Fan-in</th></tr></thead>
<tbody>{out_rows}</tbody></table></div>
</div>
</div>
"""
    write_fragment(Path(args.tabs_dir), "03-dependencies.html", "Dependencies", body)

    summary = {
        "top_level_arms": arms,
        "structural_dirs_peeled": peeled,
        "modules": len(nodes),
        # Listed so you can judge the grouping: if these read as structure
        # rather than units of design, re-run with --depth N.
        "module_list": sorted(
            ({"id": m or "(repo root)", "display": display_of(m),
              "files": mod_files.get(m, 0), "loc": mod_loc.get(m, 0)}
             for m in frontier),
            key=lambda d: -d["loc"]),
        "import_edges": edge_count,
        "module_cycles": sccs,
        "file_cycles": sorted(file_sccs, key=len, reverse=True)[:6],
        "top_fan_in_files": [{"path": f, "fan_in": n, "fan_out": fan_out.get(f, 0)} for f, n in hub_in[:10]],
        "top_fan_out_files": [{"path": f, "fan_out": n, "fan_in": fan_in.get(f, 0)} for f, n in hub_out[:10]],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
