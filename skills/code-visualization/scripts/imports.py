"""Import-edge extraction: one file's import statements resolved to repo files.

The counterpart to resources.py (loads); this module covers imports. Resolution
keys on what files DECLARE — a Python module's path suffixes, a JVM file's
package line, a C# namespace, a Cargo crate, a go.mod module path — never on
where a build layout happens to put them, so a Gradle module/src/main/kotlin
tree resolves exactly like a flat one. An ambiguous declaration resolves to
nothing: a wrong edge is worse than a missing one.

Every import statement is also counted per language (ResolutionStats): the ones
naming code this repo plausibly owns are "first_party", and a first-party
import that fails to resolve is a measured gap, not a silent one. Callers
surface the ratio so a sparse graph reads as under-resolution, never as
"no coupling".

Stdlib only; the caller supplies the file index so this module never walks a tree.
"""
import ast
import re
from collections import defaultdict
from pathlib import Path

from common import detect_lang, read_text

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


def extract(paths, all_paths, file_set):
    """Resolve every import in every file: (edges: src -> {dst}, ResolutionStats).

    ``paths`` maps repo-relative POSIX path -> Path for code files; ``all_paths``
    additionally holds non-code files, where the manifests live (go.mod and
    Cargo.toml are what make Go and Rust imports resolvable); ``file_set`` is
    ``paths``'s key set (passed separately because callers already hold it).
    """
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
            _js_edges(rel, text, file_set, by_base, lang, edges, stats)
        elif ext == ".go":
            _go_edges(rel, text, go_mods, go_files_by_dir, lang, edges, stats)
        elif ext == ".rs":
            _rust_edges(rel, text, file_set, rust_crates, rust_own_src, lang, edges, stats)
        elif ext in JVM_EXTS:
            for imp in JVM_IMPORT_RE.findall(text):
                _jvm_edge(rel, imp, jvm_pkg_files, jvm_stem_files, jvm_pkg_roots,
                          lang, edges, stats)
        elif ext == ".cs":
            for imp in CS_USING_RE.findall(text):
                _jvm_edge(rel, imp, jvm_pkg_files, jvm_stem_files, jvm_pkg_roots,
                          lang, edges, stats)
        elif ext == ".rb":
            for spec in RUBY_REQ_RE.findall(text):
                t = resolve_relative_js(rel, spec if spec.endswith(".rb") else spec + ".rb", file_set)
                if t and t != rel:
                    edges[rel].add(t)
                stats.count(lang, spec, rel, True, bool(t))
    return edges, stats


def _js_edges(rel, text, file_set, by_base, lang, edges, stats):
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


def _go_edges(rel, text, go_mods, go_files_by_dir, lang, edges, stats):
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


def _rust_edges(rel, text, file_set, rust_crates, rust_own_src, lang, edges, stats):
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


def _jvm_edge(rel, imp, pkg_files, stem_files, pkg_roots, lang, edges, stats):
    targets, first_party = resolve_jvm(imp, pkg_files, stem_files)
    targets.discard(rel)
    edges[rel] |= targets
    if not first_party:
        first_party = tuple(imp.split(".")[:2]) in pkg_roots
    stats.count(lang, imp, rel, first_party, bool(targets))
