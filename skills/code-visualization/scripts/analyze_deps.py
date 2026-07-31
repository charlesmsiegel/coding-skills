#!/usr/bin/env python3
"""Dependencies tab: module dependency graph, cycles, fan-in/fan-out.

Usage: python3 analyze_deps.py REPO_DIR --tabs-dir TABS_DIR [--depth N]
Emits: TABS_DIR/03-dependencies.html and a JSON summary on stdout.

Two kinds of dependency, both drawn:

  imports   Python (ast), JS/TS (import/require/export-from, plus @/-style root
            aliases), Go (module-path imports against every go.mod in the repo),
            Rust (use crate::/workspace-crate paths and mod declarations),
            Java/Kotlin/Scala (import resolved against declared packages),
            C# (using resolved against declared namespaces), Ruby
            (require_relative). Other languages appear as nodes without edges.

            Resolution keys on what files DECLARE (package, namespace, crate,
            module path), never on where a build layout happens to put them —
            a Gradle module/src/main/kotlin tree resolves exactly like a flat
            one. Every language reports imports seen vs resolved in the
            summary's "resolution" block; a graph that could not be resolved
            says so instead of rendering as "no coupling".
  loads     runtime resource references (resources.py) — a rendered template, a
            prompt read off disk, an embedded schema. A referenced asset becomes
            a node; an unreferenced one does not, so a docs directory does not
            swamp the graph.
"""
import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import resources
from common import (bar_cell, detect_lang, esc, json_block,
                    loc_and_complexity, read_text, walk_source, write_fragment)

# Languages that carry no import syntax we extract; they are still eligible to
# be *targets* of a load, which is how a template or prompt enters the graph.
NON_CODE_LANGS = ("Other", "Markdown", "reStructuredText", "JSON", "YAML",
                  "TOML", "HTML", "CSS")

JS_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"]
JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[\w*{}\s,$]+\s+from\s+)?|export\s+(?:[\w*{}\s,$]+\s+from\s+)|require\s*\(\s*|import\s*\(\s*)['"]([^'"]+)['"]"""
)
GO_IMPORT_RE = re.compile(r'^\s*(?:[\w.]+\s+)?"([^"]+)"', re.M)
# No trailing ';' required: Kotlin and Scala imports carry none, and the old
# semicolon-anchored pattern silently matched zero Kotlin imports. '*' rides
# along so star imports arrive intact ('.' and '_' are handled by the caller).
JVM_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.*]+)", re.M)
JVM_PKG_RE = re.compile(r"^\s*package\s+([\w.]+)", re.M)
# Matches namespace-usings and aliases; 'using var x = ...' and
# 'using (var x = ...)' fail the required trailing ';' after the dotted name.
CS_USING_RE = re.compile(
    r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?:\w+\s*=\s*)?([\w.]+)\s*;", re.M)
CS_NS_RE = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
RUST_USE_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+([\w:]+)", re.M)
RUST_MOD_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;", re.M)
RUBY_REQ_RE = re.compile(r"""require_relative\s+['"]([^'"]+)['"]""")

JVM_EXTS = (".java", ".kt", ".scala")
# A star import (or a C# namespace-using) is a real dependency on every file of
# the package, but a package that large is a namespace, not a unit — linking
# hundreds of files off one line would drown the graph in noise.
MAX_STAR_TARGETS = 25


class ResolutionStats:
    """Per-language accounting of import statements, so a resolver gap surfaces
    as a number instead of a silently sparse graph.

    first_party: statements that name something this repo plausibly owns (a
    declared package root, a workspace crate, a module path, a relative spec).
    resolved is a subset of first_party; everything else is external. Samples
    keep the first few unresolved first-party specs for the summary.
    """

    def __init__(self):
        self.by_lang = defaultdict(
            lambda: {"first_party": 0, "resolved": 0, "external": 0, "samples": []})

    def count(self, lang, spec, rel, first_party, resolved):
        s = self.by_lang[lang]
        if not first_party:
            s["external"] += 1
            return
        s["first_party"] += 1
        if resolved:
            s["resolved"] += 1
        elif len(s["samples"]) < 8:
            s["samples"].append(f"{spec}  ({rel})")

    def summary(self):
        return {lang: dict(s) for lang, s in sorted(self.by_lang.items())}

    def under_resolved(self):
        """Languages where imports the repo appears to own mostly failed to
        resolve — the signal that the graph under-states coupling."""
        return [(lang, s) for lang, s in sorted(self.by_lang.items())
                if s["first_party"] >= 10 and s["resolved"] < 0.5 * s["first_party"]]


def build_python_index(files):
    """Map every importable suffix of a module path to its file.

    sys.path can be rooted anywhere — a package root, a src/ directory, or the
    script's own directory — so a module is importable under *any* suffix of its
    path, not just the first few. Every suffix is indexed; a suffix two different
    files both claim is dropped rather than guessed, because inventing an edge to
    one of two same-named modules is worse than drawing none. Where the answer is
    genuinely local (a script importing its neighbour), python_edges resolves
    against the importing file's own directory first and never consults this.
    """
    claims = defaultdict(set)
    for rel in files:
        if not rel.endswith((".py", ".pyi")):
            continue
        parts = rel.rsplit(".", 1)[0].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        for skip in range(len(parts)):
            key = ".".join(parts[skip:])
            if key:
                claims[key].add(rel)
    return {key: next(iter(hits)) for key, hits in claims.items() if len(hits) == 1}


def python_edges(rel, text, py_idx, file_set=frozenset(), py_roots=frozenset(), stats=None):
    edges = set()
    try:
        tree = ast.parse(text)
    # ValueError: NUL bytes (e.g. a mis-detected binary); RecursionError:
    # pathologically nested literals. Either must cost one file, not the run.
    except (SyntaxError, ValueError, RecursionError):
        return edges

    def count(spec, first_party, resolved):
        if stats is not None:
            stats.count("Python", spec, rel, first_party, resolved)
    pkg_parts = rel.rsplit(".", 1)[0].split("/")
    if pkg_parts[-1] == "__init__":
        pkg_parts = pkg_parts[:-1]
    else:
        pkg_parts = pkg_parts[:-1]

    src_dir = "/".join(rel.split("/")[:-1])

    def sibling(cand):
        """The module a script would import with sys.path[0] = its own dir."""
        stem = cand.replace(".", "/")
        for suffix in (".py", ".pyi", "/__init__.py"):
            hit = f"{src_dir}/{stem}{suffix}" if src_dir else f"{stem}{suffix}"
            if hit in file_set:
                return hit
        return None

    def resolve(name):
        for cand in (name, name.rsplit(".", 1)[0] if "." in name else None):
            if not cand:
                continue
            # Neighbour first: two directories each shipping their own common.py
            # must resolve to the one beside the importer, never to its twin.
            hit = sibling(cand) or py_idx.get(cand)
            if hit:
                return hit
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                t = resolve(a.name)
                if t:
                    edges.add(t)
                count(a.name, bool(t) or a.name.split(".")[0] in py_roots, bool(t))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                mod = ".".join(base + (node.module.split(".") if node.module else []))
            else:
                mod = node.module or ""
            if not mod:
                continue
            hit = False
            t = resolve(mod)
            if t:
                edges.add(t)
                hit = True
            for a in node.names:
                t2 = resolve(f"{mod}.{a.name}")
                if t2:
                    edges.add(t2)
                    hit = True
            spec = ("." * node.level) + (node.module or "")
            # A relative import is first-party by construction.
            count(spec, node.level > 0 or hit or mod.split(".")[0] in py_roots, hit)
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


def build_go_modules(all_paths):
    """Every go.mod in the repo: [(module_path, dir)], longest module first.

    Multi-module repos (and repos whose go.mod is not at the root) resolve per
    module; the longest-prefix match sends nested modules to the right one.
    """
    mods = []
    for rel, p in all_paths.items():
        if rel == "go.mod" or rel.endswith("/go.mod"):
            m = re.search(r"^module\s+(\S+)", read_text(p), re.M)
            if m:
                mods.append((m.group(1), rel[: -len("go.mod")].rstrip("/")))
    return sorted(mods, key=lambda t: -len(t[0]))


def build_jvm_index(paths):
    """What each Java/Kotlin/Scala/C# file declares, keyed for import lookup.

    pkg_files: package/namespace -> files declaring it (a star import or a C#
    using is a dependency on all of them). stem_files: (package, FileStem) ->
    files, the layout-independent answer to `import a.b.C` — wherever a build
    tool put the file, its declaration says what it is. pkg_roots holds the
    first two segments of every declared package, the "does this repo plausibly
    own dev.rpg.*?" test for classifying unresolved imports.
    """
    pkg_files, stem_files, pkg_roots = defaultdict(set), defaultdict(set), set()
    for rel, p in paths.items():
        ext = "." + rel.rsplit(".", 1)[-1] if "." in rel else ""
        if ext not in JVM_EXTS and ext != ".cs":
            continue
        text = read_text(p)
        if ext == ".cs":
            pkgs = CS_NS_RE.findall(text) or [""]
        elif ext == ".scala":
            # Chained package clauses compose: `package a.b` + `package c` = a.b.c
            found = JVM_PKG_RE.findall(text)
            pkgs = [".".join(found)] if found else [""]
        else:
            m = JVM_PKG_RE.search(text)
            pkgs = [m.group(1)] if m else [""]
        stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        for pkg in pkgs:
            pkg_files[pkg].add(rel)
            stem_files[(pkg, stem)].add(rel)
            if pkg:
                pkg_roots.add(tuple(pkg.split(".")[:2]))
    return pkg_files, stem_files, pkg_roots


def resolve_jvm(imp, pkg_files, stem_files):
    """Resolve a dotted import against declarations. Returns (files, first_party).

    A `.*`/`._` suffix (or a bare package name — the C# using case) links every
    file of the package. Otherwise the boundary between package and symbol is
    unknown (a.b.C, a.b.C.Inner, a.b.topLevelFn all exist), so the split is
    searched right-to-left against declared (package, stem) pairs. Ambiguity —
    two files declaring the same package+stem, e.g. Kotlin expect/actual pairs —
    resolves to nothing: a wrong edge is worse than a missing one.
    """
    imp = imp.rstrip(".")
    star = imp.endswith((".*", "._"))
    if star:
        imp = imp[:-2]
    if star or imp in pkg_files:
        files = pkg_files.get(imp, set())
        first_party = bool(files) or tuple(imp.split(".")[:2]) in {
            tuple(p.split(".")[:2]) for p in pkg_files if p}
        return (set(sorted(files)[:MAX_STAR_TARGETS]), first_party)
    segs = imp.split(".")
    for i in range(len(segs) - 1, 0, -1):
        hits = stem_files.get((".".join(segs[:i]), segs[i]), set())
        if len(hits) == 1:
            return (set(hits), True)
        if hits:
            return (set(), True)  # ambiguous: first-party, deliberately unresolved
    pkg = ".".join(segs[:-1])
    files = pkg_files.get(pkg, set())
    if len(files) == 1:
        # `import a.b.topLevelFn` in a single-file package: the file is certain.
        return (set(files), True)
    return (set(), bool(files))


def build_rust_crates(all_paths):
    """Cargo workspace map: crate name (underscored, as `use` spells it) ->
    src root, plus dir -> nearest enclosing crate src root for crate:: paths."""
    crates, crate_dirs = {}, []
    for rel, p in all_paths.items():
        if rel == "Cargo.toml" or rel.endswith("/Cargo.toml"):
            m = re.search(r'^\s*name\s*=\s*"([^"]+)"', read_text(p), re.M)
            if m:
                d = rel[: -len("Cargo.toml")].rstrip("/")
                crates[m.group(1).replace("-", "_")] = f"{d}/src" if d else "src"
                crate_dirs.append(d)
    crate_dirs.sort(key=len, reverse=True)

    def own_src(rel):
        for d in crate_dirs:
            if not d or rel.startswith(d + "/"):
                return f"{d}/src" if d else "src"
        return "src"

    return crates, own_src


def resolve_rust_path(src_root, segs, file_set):
    """a::b::c against a crate src tree: the tail segments are items, not
    modules, so back off until a module file exists; a bare crate reference
    lands on its lib.rs/main.rs (where the pub use re-exports live)."""
    for n in range(len(segs), 0, -1):
        base = f"{src_root}/{'/'.join(segs[:n])}"
        for cand in (f"{base}.rs", f"{base}/mod.rs"):
            if cand in file_set:
                return cand
    for entry in ("lib.rs", "main.rs"):
        if f"{src_root}/{entry}" in file_set:
            return f"{src_root}/{entry}"
    return None


def build_basename_index(file_set):
    idx = defaultdict(list)
    for f in file_set:
        idx[f.rsplit("/", 1)[-1]].append(f)
    return idx


def resolve_unique_suffix(spec, by_base, exts=("",)):
    """The path-suffix fallback for root-relative specs ('@/lib/util',
    'lib/foo'): an edge only when exactly one file ends with the spec."""
    cands = [spec] if re.search(r"\.[A-Za-z0-9]+$", spec) else []
    for e in exts:
        if e:
            cands += [spec + e, f"{spec}/index{e}"]
    for c in cands:
        hits = [f for f in by_base.get(c.rsplit("/", 1)[-1], ())
                if f == c or f.endswith("/" + c)]
        if len(hits) == 1:
            return hits[0]
        if hits:
            return None
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
    if not repo.is_dir():
        # A failed clone or mistyped path must not become a confident
        # "0 modules, 0 edges" graph.
        print(f"error: repo directory does not exist: {repo}", file=sys.stderr)
        return 2

    paths, locs, all_paths = {}, {}, {}
    for rel, p in walk_source(repo, extra_exclude=extra_exclude):
        all_paths[rel] = p
        if detect_lang(rel) in NON_CODE_LANGS:
            continue
        paths[rel] = p
        locs[rel] = loc_and_complexity(read_text(p))[0]
    file_set = set(paths)
    py_idx = build_python_index(file_set)
    # Path segments that hold Python — the "does the repo plausibly own this
    # import?" test. Broad on purpose: an ambiguous module the index dropped is
    # still first-party, and should be counted as unresolved rather than external.
    py_roots = {seg for rel in file_set if rel.endswith((".py", ".pyi"))
                for seg in rel.rsplit(".", 1)[0].split("/")}
    go_mods = build_go_modules(all_paths)
    jvm_pkg_files, jvm_stem_files, jvm_pkg_roots = build_jvm_index(paths)
    rust_crates, rust_own_src = build_rust_crates(all_paths)
    by_base = build_basename_index(file_set)
    # dir -> .go files, so Go import resolution is a lookup instead of a scan
    # over every file for every import (quadratic on large Go monorepos).
    go_files_by_dir = defaultdict(list)
    for f in file_set:
        if f.endswith(".go"):
            go_files_by_dir[str(Path(f).parent).replace("\\", "/")].append(f)

    stats = ResolutionStats()
    edges = defaultdict(set)  # src file -> {dst files}
    # Files are re-read here rather than kept from the first pass: holding
    # every source text at once is multi-GB on big repos.
    for rel, text in ((rel, read_text(p)) for rel, p in paths.items()):
        ext = "." + rel.rsplit(".", 1)[-1] if "." in rel else ""
        lang = detect_lang(rel)
        if ext in (".py", ".pyi"):
            edges[rel] |= python_edges(rel, text, py_idx, file_set, py_roots, stats)
        elif ext in JS_EXTS:
            for spec in JS_IMPORT_RE.findall(text):
                if spec.startswith("."):
                    t = resolve_relative_js(rel, spec, file_set)
                    if t and t != rel:
                        edges[rel].add(t)
                    stats.count(lang, spec, rel, True, bool(t))
                elif spec.startswith(("@/", "~/")):
                    # The src-root alias (tsconfig paths / Vite / Next): resolve
                    # by unique path suffix, no tsconfig parsing required.
                    t = resolve_unique_suffix(spec[2:], by_base, JS_EXTS)
                    if t and t != rel:
                        edges[rel].add(t)
                    stats.count(lang, spec, rel, True, bool(t))
                else:
                    # Bare specifiers are packages; only a spec that uniquely
                    # names a repo file (baseUrl-style import) is first-party.
                    t = None
                    if "/" in spec and not spec.startswith("@"):
                        t = resolve_unique_suffix(spec, by_base, JS_EXTS)
                    if t and t != rel:
                        edges[rel].add(t)
                    stats.count(lang, spec, rel, bool(t), bool(t))
        elif ext == ".go":
            src_dir = str(Path(rel).parent).replace("\\", "/")
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
                if not m:
                    continue
                imp = m.group(1)
                owner = next(((mp, d) for mp, d in go_mods
                              if imp == mp or imp.startswith(mp + "/")), None)
                if owner is None:
                    stats.count(lang, imp, rel, False, False)
                    continue
                mod_path, mod_dir = owner
                sub = imp[len(mod_path):].strip("/")
                cand_dir = f"{mod_dir}/{sub}".strip("/") if sub else mod_dir
                hit = False
                if cand_dir != src_dir:
                    for f in go_files_by_dir.get(cand_dir, ()):
                        if f != rel:
                            edges[rel].add(f)
                            hit = True
                stats.count(lang, imp, rel, True, hit or cand_dir == src_dir)
        elif ext == ".rs":
            own = rust_own_src(rel)
            own_dir = str(Path(rel).parent).replace("\\", "/")
            for name in RUST_MOD_RE.findall(text):
                # `mod x;` in lib/main/mod.rs looks beside itself; in foo.rs it
                # looks inside foo/ (with the pre-2018 sibling as fallback).
                stem = rel.rsplit("/", 1)[-1]
                bases = ([own_dir] if stem in ("lib.rs", "main.rs", "mod.rs")
                         else [f"{own_dir}/{stem[:-3]}", own_dir])
                t = next((c for b in bases
                          for c in (f"{b}/{name}.rs", f"{b}/{name}/mod.rs")
                          if c in file_set and c != rel), None)
                if t:
                    edges[rel].add(t)
                stats.count(lang, f"mod {name}", rel, True, bool(t))
            for use in RUST_USE_RE.findall(text):
                segs = [s for s in use.strip(":").split("::") if s]
                if not segs:
                    continue
                head = segs[0]
                if head == "crate":
                    root, segs = own, segs[1:]
                elif head == "self":
                    root, segs = own_dir, segs[1:]
                elif head == "super":
                    root, segs = own_dir, segs
                    while segs and segs[0] == "super":
                        root, segs = root.rsplit("/", 1)[0], segs[1:]
                elif head in rust_crates:
                    root, segs = rust_crates[head], segs[1:]
                else:
                    stats.count(lang, use, rel, False, False)
                    continue
                t = resolve_rust_path(root, segs, file_set)
                if t and t != rel:
                    edges[rel].add(t)
                stats.count(lang, use, rel, True, bool(t))
        elif ext in JVM_EXTS:
            for imp in JVM_IMPORT_RE.findall(text):
                targets, first_party = resolve_jvm(imp, jvm_pkg_files, jvm_stem_files)
                targets.discard(rel)
                edges[rel] |= targets
                if not first_party:
                    first_party = tuple(imp.split(".")[:2]) in jvm_pkg_roots
                stats.count(lang, imp, rel, first_party, bool(targets))
        elif ext == ".cs":
            for imp in CS_USING_RE.findall(text):
                targets, first_party = resolve_jvm(imp, jvm_pkg_files, jvm_stem_files)
                targets.discard(rel)
                edges[rel] |= targets
                if not first_party:
                    first_party = tuple(imp.split(".")[:2]) in jvm_pkg_roots
                stats.count(lang, imp, rel, first_party, bool(targets))
        elif ext == ".rb":
            for spec in RUBY_REQ_RE.findall(text):
                t = resolve_relative_js(rel, spec if spec.endswith(".rb") else spec + ".rb", file_set)
                if t and t != rel:
                    edges[rel].add(t)
                stats.count(lang, spec, rel, True, bool(t))

    # ---- runtime resource references ----
    # A referenced asset joins the graph as a node (with its own LOC, so a large
    # prompts/ directory reads as the mass it is); an unreferenced one stays out.
    scan = resources.scan(repo, all_paths)
    res_edges = defaultdict(set)
    for ref in scan.refs:
        if ref.src != ref.dst:
            res_edges[ref.src].add(ref.dst)
    loaders_of = defaultdict(list)  # asset -> ["file:line", ...]
    for ref in sorted(scan.refs):
        loaders_of[ref.dst].append(f"{ref.src}:{ref.line}")

    asset_nodes = sorted({d for dsts in res_edges.values() for d in dsts} - file_set)
    asset_loc = 0
    for rel in asset_nodes:
        text = read_text(all_paths[rel])
        locs[rel] = 0 if "\x00" in text[:1024] else loc_and_complexity(text)[0]
        asset_loc += locs[rel]
    node_files = file_set | set(asset_nodes)

    # A prompt or template nothing loads is a finding — but only where we know
    # what a loaded asset in that directory looks like, so a docs tree full of
    # never-referenced Markdown is not paraded as dead code.
    referenced = set(loaders_of)
    asset_dirs = {rel.rsplit("/", 1)[0] if "/" in rel else "" for rel in asset_nodes}
    asset_exts = {rel.rsplit(".", 1)[-1] for rel in asset_nodes if "." in rel}
    orphans = sorted(
        rel for rel in all_paths
        if rel not in referenced and rel not in file_set
        and not rel.startswith(".")  # .github & co are loaded by outside tooling
        and (rel.rsplit("/", 1)[0] if "/" in rel else "") in asset_dirs
        and "." in rel and rel.rsplit(".", 1)[-1] in asset_exts)

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

    mod_edges, mod_res_edges = defaultdict(int), defaultdict(int)
    for counter, graph in ((mod_edges, edges), (mod_res_edges, res_edges)):
        for src, dsts in graph.items():
            ms = mod_key(src)
            for dst in dsts:
                md = mod_key(dst)
                if ms != md:
                    counter[(ms, md)] += 1

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

    # Cycles run over both edge kinds: a pair of templates that include each
    # other is as circular as a pair of modules that import each other.
    adj = defaultdict(set)
    for (a, b) in list(mod_edges) + list(mod_res_edges):
        adj[a].add(b)
    all_mods = sorted(set(mod_loc if not lump else list(keep) + ["(other)"]))
    sccs = find_sccs(all_mods, adj)

    file_adj = defaultdict(set)
    for graph_edges in (edges, res_edges):
        for src, dsts in graph_edges.items():
            file_adj[src] |= dsts
    file_sccs = find_sccs(sorted(node_files), file_adj)
    cyc_members = {m for scc in sccs for m in scc}
    scc_of = {}
    for i, scc in enumerate(sccs):
        for m in scc:
            scc_of[m] = i

    # ---- fan-in/out at file level (imports and loads together: a template
    # rendered from six modules is depended on by six modules) ----
    fan_out = {f: len(d) for f, d in file_adj.items()}
    fan_in = defaultdict(int)
    for d in file_adj.values():
        for t in d:
            fan_in[t] += 1
    mod_fan_in, mod_fan_out = defaultdict(int), defaultdict(int)
    for (a, b) in set(mod_edges) | set(mod_res_edges):
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
    links = []
    for pair in sorted(set(mod_edges) | set(mod_res_edges)):
        a, b = pair
        imports, loads = mod_edges.get(pair, 0), mod_res_edges.get(pair, 0)
        links.append({
            "source": a, "target": b, "weight": imports + loads,
            "imports": imports, "loads": loads,
            "kind": "import" if not loads else ("resource" if not imports else "mixed"),
            "cycle": bool(a in cyc_members and b in cyc_members and scc_of.get(a) == scc_of.get(b)),
        })
    legend = ('<span><i class="sw" style="background:transparent;border:2px solid var(--bad);'
              'box-sizing:border-box"></i>red edges = cycle</span>')
    if mod_res_edges:
        legend += '<span><i class="sw" style="background:var(--text-faint)"></i>dashed edges = runtime load</span>'
    graph = {"nodes": nodes, "links": links, "legendExtra": legend}

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
    res_count = sum(len(d) for d in res_edges.values())

    # An empty or thin graph must never read as "no coupling" when the cause is
    # the resolver: name the languages whose own-repo imports failed to resolve.
    under = stats.under_resolved()
    if not under and edge_count == 0 and len(file_set) >= 20:
        under = [(lang, s) for lang, s in stats.summary().items() if s["first_party"]]
    resolution_html = ""
    if under:
        rows = "".join(
            f"<li><b>{esc(lang)}</b>: {s['resolved']} of {s['first_party']} "
            f"first-party imports resolved"
            + (" — e.g. " + ", ".join(f"<code>{esc(x)}</code>" for x in s["samples"][:3])
               if s["samples"] else "") + "</li>"
            for lang, s in under)
        resolution_html = f"""<div class="callout warn"><b>Import graph likely under-resolved</b> — imports that appear to name this repo's own code could not be matched to files, so the graph understates coupling. Treat sparse areas as a resolution gap, not as decoupling; the <code>resolution</code> block in the analyzer summary has per-language numbers.<ul style="margin-top:8px">{rows}</ul></div>"""

    top_assets = sorted(loaders_of.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:25]
    resource_html = ""
    if scan.refs:
        asset_rows = "\n".join(
            f"<tr><td><code>{esc(a)}</code></td><td class='num'>{len(cites)}</td>"
            f"<td>{' '.join(f'<code>{esc(c)}</code>' for c in cites[:6])}"
            + (f" <span class='dim'>… {len(cites)-6} more</span>" if len(cites) > 6 else "")
            + "</td></tr>"
            for a, cites in top_assets)
        orphan_html = ""
        if orphans:
            items = " ".join(f"<code>{esc(o)}</code>" for o in orphans[:30])
            orphan_html = (
                f"""<div class="callout warn"><b>{len(orphans)} asset{'s' if len(orphans)!=1 else ''} """
                f"""nothing references</b> — same file type, same directories as the loaded ones, but no """
                f"""code path names them. Either dead weight or loaded by a computed path this analysis """
                f"""cannot see.<p style="margin-top:8px">{items}"""
                + (f" <span class='dim'>… {len(orphans)-30} more</span>" if len(orphans) > 30 else "")
                + "</p></div>")
        truncation = ""
        if scan.truncated["patterns"] or scan.truncated["files"]:
            truncation = (f" {scan.truncated['patterns']} computed path(s) and "
                          f"{scan.truncated['files']} file(s) hit the reporting cap.")
        resource_html = f"""
<h2>Runtime resources</h2>
<p class="dim">Files loaded rather than imported — templates, prompts, schemas, embedded data — and the
code that names them. {esc(resources.CAVEAT)}{esc(truncation)}</p>
{orphan_html}
<div class="tbl-wrap"><table class="sortable">
<thead><tr><th>Asset</th><th class="num">Loaders</th><th>Loaded at</th></tr></thead>
<tbody>{asset_rows}</tbody></table></div>
"""
    arms = sorted({n["group"] for n in nodes if n["group"] not in ("cycle", "(other)")}
                  | {group_of(m) for m in cyc_members if m != "(other)"})
    body = f"""
<div class="kpis">
  <div class="kpi accent"><div class="n">{len(nodes)}</div><div class="l">modules</div></div>
  <div class="kpi"><div class="n">{edge_count:,}</div><div class="l">file-level imports</div></div>
  <div class="kpi"><div class="n">{res_count:,}</div><div class="l">runtime loads</div></div>
  <div class="kpi {'bad' if sccs else 'good'}"><div class="n">{len(sccs)}</div><div class="l">cycles</div></div>
  <div class="kpi"><div class="n">{len(arms)}</div><div class="l">top-level {'arm' if len(arms)==1 else 'arms'}</div></div>
</div>
{resolution_html}
{cyc_html}
<h2>Module dependency graph</h2>
<p class="dim">Node area = lines of code · solid arrows = imports, dashed = runtime loads (a rendered template, a prompt read off disk) · drag nodes, Ctrl/⌘-scroll to zoom, hover to isolate a neighborhood.</p>
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
{resource_html}
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
        # Per-language: import statements the repo appears to own vs actually
        # resolved. A language with a low ratio means the graph understates its
        # coupling — say so in the Overview instead of presenting sparseness as
        # architecture (and consider hand-building that part of the graph).
        "resolution": stats.summary(),
        "module_cycles": sccs,
        "file_cycles": sorted(file_sccs, key=len, reverse=True)[:6],
        "top_fan_in_files": [{"path": f, "fan_in": n, "fan_out": fan_out.get(f, 0)} for f, n in hub_in[:10]],
        "top_fan_out_files": [{"path": f, "fan_out": n, "fan_in": fan_in.get(f, 0)} for f, n in hub_out[:10]],
        # Runtime loads. Judgment tabs should treat a high-fan-in template or
        # prompt the way they treat a high-fan-in module, and an orphan asset as
        # a question to answer rather than a fact to report.
        "resource_edges": res_count,
        "resource_kinds": {k: sum(1 for r in scan.refs if r.kind == k)
                           for k in sorted({r.kind for r in scan.refs})},
        "loader_roots": scan.roots,
        "top_referenced_assets": [{"path": a, "loaders": len(c), "loaded_by": c[:6]}
                                  for a, c in top_assets[:10]],
        "orphan_assets": orphans[:30],
        "asset_nodes": {"count": len(asset_nodes), "loc": asset_loc, "paths": asset_nodes[:50]},
        "resource_truncated": scan.truncated,
        "resource_caveat": resources.CAVEAT,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
