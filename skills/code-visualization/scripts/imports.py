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
from jvmdecl import (JVM_EXTS, build_decl_indexes, cs_usings,
                     jvm_import_specs, resolve_jvm)
from manifests import (join_inside, RustWorkspace, go_modules, nearest_dir,
                       npm_packages, semver_satisfies, ts_config_roots)

JS_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"]
JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[\w*{}\s,$]+\s+from\s+)?|export\s+(?:[\w*{}\s,$]+\s+from\s+)|require\s*\(\s*|import\s*\(\s*)['"]([^'"]+)['"]"""
)
GO_IMPORT_RE = re.compile(r'^\s*(?:[\w.]+\s+)?["`]([^"`]+)["`]', re.M)
# Captures the full statement (a negated class crosses newlines, so a
# rustfmt-wrapped group still arrives whole); rust_use_targets() parses it.
RUST_USE_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+([^;]+);", re.M)
RUBY_REQ_RE = re.compile(
    r"""(require_relative|require)\s*\(?\s*['"]([^'"]+)['"]""")

# Bare specifiers naming Node built-ins are external even without a
# package.json entry — a repo file named path.ts is never what `from "path"`
# means.
NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "dns", "domain", "events", "fs", "http",
    "http2", "https", "inspector", "module", "net", "os", "path", "perf_hooks",
    "process", "punycode", "querystring", "readline", "repl", "stream",
    "string_decoder", "sys", "timers", "tls", "tty", "url", "util", "v8", "vm",
    "worker_threads", "zlib",
}



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
        # a stub beside its implementation is the same module, not a rival
        if rel.endswith(".pyi") and rel[:-1] in files:
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
    # pathologically nested literals. Either must cost one file, not the run —
    # but it must be COUNTED: a repo full of unparseable files (say, syntax
    # newer than this interpreter) must not read as "no dependencies".
    except (SyntaxError, ValueError, RecursionError) as exc:
        if stats is not None:
            stats.count("Python", f"(file failed to parse: {type(exc).__name__})",
                        rel, True, False)
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
    # Inside a package (an __init__.py beside the importer), a plain
    # `import util` is ABSOLUTE — Python 3 never resolves it to a sibling.
    # The sibling rule models scripts, whose own directory is sys.path[0].
    in_package = (f"{src_dir}/__init__.py" if src_dir else "__init__.py") in file_set

    def sibling(cand):
        """The module a script would import with sys.path[0] = its own dir."""
        if in_package:
            return None
        stem = cand.replace(".", "/")
        for suffix in (".py", ".pyi", "/__init__.py"):
            hit = f"{src_dir}/{stem}{suffix}" if src_dir else f"{stem}{suffix}"
            if hit in file_set:
                return hit
        return None

    def root_anchored(cand):
        """The module the repo root's sys.path entry provides — the exact
        path match that stays valid even when the bare suffix is ambiguous
        (a root util.py beside some pkg/util.py)."""
        stem = cand.replace(".", "/")
        for suffix in (".py", ".pyi", "/__init__.py"):
            if f"{stem}{suffix}" in file_set:
                return f"{stem}{suffix}"
        return None

    def resolve(name):
        for cand in (name, name.rsplit(".", 1)[0] if "." in name else None):
            if not cand:
                continue
            # Neighbour first: two directories each shipping their own common.py
            # must resolve to the one beside the importer, never to its twin.
            hit = sibling(cand) or py_idx.get(cand) or root_anchored(cand)
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
            if not parts:
                return None  # climbs above the repo root: external
            parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    norm = "/".join(parts)
    if re.search(r"\.[a-z]+$", norm):
        cands.append(norm)
        # NodeNext TS: source says './util.js' while the repo holds util.ts
        emitted = {".js": (".ts", ".tsx"), ".mjs": (".mts",), ".cjs": (".cts",),
                   ".jsx": (".tsx",)}
        for ext_e, sources in emitted.items():
            if norm.endswith(ext_e):
                cands += [norm[: -len(ext_e)] + s for s in sources]
    for e in JS_EXTS:
        cands.append(norm + e)
        cands.append(norm + "/index" + e)
    for c in cands:
        if c in file_set:
            return c
    return None


def resolve_rust_path(src_root, segs, file_set, entry_of=None,
                      skip_entries=False):
    """a::b::c against a crate src tree: the tail segments are items, not
    modules, so back off until a module file exists; a bare crate reference
    lands on its lib.rs/main.rs (where the pub use re-exports live), or on
    the [lib]-declared entry file when the crate names a custom target.
    skip_entries suppresses that fallback when the owning target is
    ambiguous (both lib.rs and main.rs exist and the importer is neither)."""
    prefix = f"{src_root}/" if src_root else ""
    for n in range(len(segs), 0, -1):
        base = prefix + "/".join(segs[:n])
        for cand in (f"{base}.rs", f"{base}/mod.rs"):
            if cand in file_set:
                return cand
    # mod.rs covers the self::/super:: case, where the root is a module
    # directory rather than a crate src root.
    for entry in (("mod.rs",) if skip_entries else ("lib.rs", "main.rs", "mod.rs")):
        if prefix + entry in file_set:
            return prefix + entry
    # ... and a module using the file layout (src/foo.rs owning src/foo/) is
    # the sibling FILE of the directory the root path names.
    if src_root and f"{src_root}.rs" in file_set:
        return f"{src_root}.rs"
    if entry_of:
        return entry_of.get(src_root)
    return None


def build_basename_index(file_set):
    idx = defaultdict(list)
    for f in file_set:
        idx[f.rsplit("/", 1)[-1]].append(f)
    return idx


def suffix_hits(spec, by_base, exts=("",)):
    """Every file whose path ends with the spec (plus resolvable extensions)."""
    cands = [spec] if re.search(r"\.[A-Za-z0-9]+$", spec) else []
    for e in exts:
        if e:
            cands += [spec + e, f"{spec}/index{e}"]
    for c in cands:
        hits = [f for f in by_base.get(c.rsplit("/", 1)[-1], ())
                if f == c or f.endswith("/" + c)]
        if hits:
            return hits
    return []


def resolve_unique_suffix(spec, by_base, exts=("",), scope=None):
    """The path-suffix fallback for root-relative specs ('@/lib/util',
    'lib/foo'): an edge only when exactly one file ends with the spec.
    With a scope directory, a unique match inside it wins first — monorepo
    packages resolve their own files before the repo-wide tiebreak."""
    hits = suffix_hits(spec, by_base, exts)
    if scope is not None:
        scoped = [h for h in hits if h.startswith(scope + "/")] if scope \
            else hits
        if len(scoped) == 1:
            return scoped[0]
    return hits[0] if len(hits) == 1 else None


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
    go_mods, go_replaces, go_work_replaces = go_modules(all_paths)
    jvm_idx, cs_idx = build_decl_indexes(paths)
    rust_ws = RustWorkspace(all_paths)
    npm_by_dir, npm_names, npm_local, npm_ws_roots = npm_packages(all_paths)
    ts_roots = ts_config_roots(all_paths)
    # Build-module boundaries: independent modules may declare the same fully
    # qualified symbol without sharing a classpath. C# projects are bounded by
    # .csproj files; JVM modules by Gradle/Maven manifests.
    jvm_modules = {rel[: -len(name)].rstrip("/")
                   for rel in all_paths
                   for name in ("build.gradle", "build.gradle.kts", "pom.xml",
                                "settings.gradle", "settings.gradle.kts")
                   if rel == name or rel.endswith("/" + name)}
    cs_modules = {rel.rsplit("/", 1)[0] if "/" in rel else ""
                  for rel in all_paths if rel.endswith(".csproj")}
    by_base = build_basename_index(file_set)
    # dir -> .go files, so Go import resolution is a lookup instead of a scan
    # over every file for every import (quadratic on large Go monorepos).
    go_files_by_dir = defaultdict(list)
    for f in file_set:
        # _test.go files import things but are never compiled into an
        # importer — linking them inflates fan-in and invents cycles.
        if f.endswith(".go") and not f.endswith("_test.go"):
            gd = str(Path(f).parent).replace("\\", "/")
            go_files_by_dir["" if gd == "." else gd].append(f)

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
            _js_edges(rel, text, file_set, by_base, npm_by_dir, npm_names,
                      npm_local, npm_ws_roots, ts_roots, lang, edges, stats)
        elif ext == ".go":
            # _test.go compiles into a separate test binary, but the graph's
            # directory grouping would conflate its edges with the package's —
            # inventing cycles production code does not have. Skip both
            # directions (they are already excluded as import targets).
            if not rel.endswith("_test.go"):
                _go_edges(rel, text, go_mods, go_replaces, go_work_replaces,
                          go_files_by_dir, lang, edges, stats)
        elif ext == ".rs":
            _rust_edges(rel, text, file_set, rust_ws, lang, edges, stats)
        elif ext in JVM_EXTS:
            # Scala resolves imports through enclosing packages: inside
            # `package com.acme` — or a braced `package com { ... }` block —
            # `import util.Helper` names com.acme.util first. Each import
            # carries its own block scope. Java and Kotlin are absolute.
            if ext == ".scala":
                for imp, prefixes in jvm_import_specs(text, with_scopes=True):
                    _cs_edge(rel, imp, prefixes, jvm_idx, jvm_modules,
                             lang, edges, stats)
            else:
                for imp in jvm_import_specs(text):
                    _jvm_edge(rel, imp, jvm_idx, jvm_modules, lang, edges, stats)
        elif ext == ".cs":
            for imp, prefixes in cs_usings(text):
                _cs_edge(rel, imp, prefixes, cs_idx, cs_modules, lang, edges, stats)
        elif ext == ".rb":
            rb_text = re.sub(r"^\s*#[^\n]*", "", text, flags=re.M)
            for kind, spec in RUBY_REQ_RE.findall(rb_text):
                if kind == "require_relative":
                    t = resolve_relative_js(
                        rel, spec if spec.endswith(".rb") else spec + ".rb", file_set)
                    if t and t != rel:
                        edges[rel].add(t)
                    stats.count(lang, spec, rel, True, bool(t))
                else:
                    # plain require: the gem convention roots load paths at
                    # lib/, so a unique lib/<spec>.rb match is first-party;
                    # anything else (stdlib, other gems) is external.
                    t = resolve_unique_suffix(f"lib/{spec}", by_base, (".rb",))
                    if t and t != rel:
                        edges[rel].add(t)
                    stats.count(lang, spec, rel, bool(t), bool(t))
    return edges, stats


def _mask_js_comments(text):
    """JS/TS text with comments blanked. Strings stay: import specs are
    string literals. `://` (URLs) is not a comment start."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"/\*.*?\*/", blank, text, flags=re.S)
    text = re.sub(r"(?<!:)//[^\n]*", " ", text)
    return text


def _js_edges(rel, text, file_set, by_base, npm_by_dir, npm_names,
              npm_local, npm_ws_roots, ts_roots, lang, edges, stats):
    # bare specifiers resolve against the repo only where a tsconfig/jsconfig
    # declares baseUrl or paths — without one, Node treats them as packages
    has_base_url = any(not r or rel == r or rel.startswith(r + "/")
                       for r in ts_roots)
    text = _mask_js_comments(text)
    pkg_dir = nearest_dir(rel, npm_by_dir)
    npm_deps = npm_by_dir.get(pkg_dir, set()) if pkg_dir is not None else set()
    local = npm_local.get(pkg_dir, {}) if pkg_dir is not None else {}

    def workspace_pkg(name):
        """The candidate package nearest the importer: independent trees may
        ship same-named packages, and the longest shared directory prefix
        picks the importer's own tree."""
        cands = npm_names.get(name)
        if not cands:
            return None

        def shared_depth(cand_dir):
            a = cand_dir.split("/") if cand_dir else []
            b = (pkg_dir or "").split("/") if pkg_dir else []
            n = 0
            while n < len(a) and n < len(b) and a[n] == b[n]:
                n += 1
            return n
        return max(cands, key=lambda c: shared_depth(c[0]))

    def workspace_entry(name, mode="import"):
        """The file a workspace package's name resolves to: its exports/main
        entry for the caller's syntax (import vs require), else a
        conventional index module."""
        pkg = workspace_pkg(name)
        if pkg is None:
            return None
        d, (esm, cjs), _ = pkg
        preferred = [esm, cjs] if mode == "import" else [cjs, esm]
        cands = []
        for main in preferred:
            if not main:
                continue
            base = f"{d}/{main}".strip("/")
            # Node resolves an extensionless or directory main
            cands += [base] + [base + e for e in JS_EXTS] \
                + [f"{base}/index{e}" for e in JS_EXTS]
        for stem_c in ("src/index", "index", "src/main", "lib/index"):
            cands += [f"{d}/{stem_c}{e}" for e in JS_EXTS]
        return next((c for c in cands if c in file_set), None)
    for jm in JS_IMPORT_RE.finditer(text):
        # an `import ...` that sits inside a string literal is documentation,
        # not code: skip matches immediately preceded by a quote
        prev = text[:jm.start()].rstrip()[-1:]
        if prev in ("'", '"', "`"):
            continue
        spec = jm.group(1)
        # a require() call resolves the package's CommonJS condition
        mode = "require" if jm.group(0).lstrip().startswith("require") else "import"
        if spec.startswith("."):
            t = resolve_relative_js(rel, spec, file_set)
            if t and t != rel:
                edges[rel].add(t)
            stats.count(lang, spec, rel, True, bool(t))
        elif spec.startswith(("@/", "~/")):
            # The src-root alias (tsconfig paths / Vite / Next): resolve
            # by unique path suffix, no tsconfig parsing required.
            t = resolve_unique_suffix(spec[2:], by_base, JS_EXTS, scope=pkg_dir)
            if t and t != rel:
                edges[rel].add(t)
            stats.count(lang, spec, rel, True, bool(t))
        elif spec.startswith("#"):
            # Node's package `imports` map: "#utils" -> ./src/utils.js,
            # relative to the declaring package
            target = local.get("imports", {}).get(spec)
            t = None
            if target is not None:
                cand = f"{pkg_dir}/{target}".strip("/")
                t = cand if cand in file_set else None
            if t and t != rel:
                edges[rel].add(t)
            stats.count(lang, spec, rel, True, bool(t))
        else:
            # A bare specifier is usually a package — but a name that is not a
            # declared dependency and uniquely names a repo file is a
            # baseUrl-style first-party import.
            t = None
            known_local = False
            root = spec.split("/")[0]
            # a scoped package's name spans two segments (@scope/utils)
            pkg_name = ("/".join(spec.split("/")[:2])
                        if spec.startswith("@") else root)
            alias_dir = local.get("aliases", {}).get(pkg_name)

            def same_workspace(name):
                """npm links a semver range to a sibling when both packages
                live under the same workspaces-declaring root AND the
                sibling's version satisfies the declared range — otherwise
                npm fetches from the registry instead."""
                rng = local.get("ranges", {}).get(name)
                for cand_dir, _, _ in npm_names.get(name, ()):
                    ver = npm_local.get(cand_dir, {}).get("version")
                    if not semver_satisfies(rng, ver):
                        continue
                    for root in npm_ws_roots:
                        if ((not root or cand_dir == root
                             or cand_dir.startswith(root + "/"))
                                and (not root or (pkg_dir or "") == root
                                     or (pkg_dir or "").startswith(root + "/"))):
                            return True
                return False
            if alias_dir is not None or (pkg_name in npm_names
                                         and (pkg_name not in npm_deps
                                              or same_workspace(pkg_name))):
                known_local = True
            if alias_dir is not None:
                # `"alias": "file:../actual"` — the alias IS the installed
                # name; link the target package's entry, or the subpath's
                # export/physical file for `alias/feature` imports
                data_cands = [c for cands_list in npm_names.values()
                              for c in cands_list if c[0] == alias_dir]
                esm_a, cjs_a = data_cands[0][1] if data_cands else ("", "")
                subexports_a = data_cands[0][2] if data_cands else {}
                if spec != pkg_name:
                    sub = spec[len(pkg_name) + 1:]
                    exported = subexports_a.get(sub)
                    cands = [f"{alias_dir}/{exported}"] if exported else []
                    if re.search(r"\.[a-z]+$", sub):
                        cands.append(f"{alias_dir}/{sub}")
                    for e in JS_EXTS:
                        cands += [f"{alias_dir}/{sub}{e}",
                                  f"{alias_dir}/{sub}/index{e}",
                                  f"{alias_dir}/src/{sub}{e}"]
                else:
                    order = [esm_a, cjs_a] if mode == "import" else [cjs_a, esm_a]
                    cands = [f"{alias_dir}/{e}".strip("/") for e in order if e]
                    for stem_c in ("src/index", "index", "src/main", "lib/index"):
                        cands += [f"{alias_dir}/{stem_c}{e}" for e in JS_EXTS]
                t = next((c for c in cands if c in file_set), None)
            elif pkg_name in npm_names and (pkg_name not in npm_deps
                                            or same_workspace(pkg_name)):
                # a sibling workspace package (declared workspace:/file:, or
                # undeclared but present in the repo) — a local edge by name; a
                # subpath import reaches into the package's own tree. A name
                # the importer declares as a REGISTRY dependency stays
                # external even when a same-named local package exists.
                if spec == pkg_name:
                    t = workspace_entry(pkg_name, mode)
                else:
                    wdir, _, subexports = workspace_pkg(pkg_name)
                    sub = spec[len(pkg_name) + 1:]
                    # an explicit exports entry is authoritative — the target
                    # need not share the subpath's stem
                    exported = subexports.get(sub)
                    cands = [f"{wdir}/{exported}"] if exported else []
                    if re.search(r"\.[a-z]+$", sub):
                        cands.append(f"{wdir}/{sub}")
                    for e in JS_EXTS:
                        cands += [f"{wdir}/{sub}{e}", f"{wdir}/{sub}/index{e}",
                                  f"{wdir}/src/{sub}{e}"]
                    t = next((c for c in cands if c in file_set), None)
            elif (has_base_url and not spec.startswith("@")
                    and root not in npm_deps and root not in NODE_BUILTINS):
                t = resolve_unique_suffix(spec, by_base, JS_EXTS, scope=pkg_dir)
            if t and t != rel:
                edges[rel].add(t)
            stats.count(lang, spec, rel, known_local or bool(t), bool(t))


def _mask_go_comments(text):
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"/\*.*?\*/", blank, text, flags=re.S)
    text = re.sub(r"(?<!:)//[^\n]*", " ", text)
    return text


def _go_edges(rel, text, go_mods, go_replaces, go_work_replaces,
              go_files_by_dir, lang, edges, stats):
    text = _mask_go_comments(text)
    my_dir = nearest_dir(rel, {d for _, d in go_mods})
    src_dir = str(Path(rel).parent).replace("\\", "/")
    in_block = False
    # Multiline backtick raw strings may hold import-looking documentation;
    # a line that STARTS inside one is string content, not a declaration.
    # (Backtick import paths are legal Go and sit on lines that start code.)
    in_raw = False
    for line in text.splitlines():
        starts_in_raw = in_raw
        if line.count("`") % 2 == 1:
            in_raw = not in_raw
        if starts_in_raw:
            continue
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
            m = re.search(r'["`]([^"`]+)["`]', line)
        if not m:
            continue
        imp = m.group(1)
        # The importer's own go.mod may rewrite this prefix to a local dir —
        # go.work replaces bind every member and take precedence over the
        # module's own go.mod replaces.
        repl_dirs = [my_dir] if my_dir is not None else []
        probe = my_dir or ""
        while probe:
            probe = probe.rsplit("/", 1)[0] if "/" in probe else ""
            repl_dirs.append(probe)
        if "" not in repl_dirs:
            repl_dirs.append("")
        repl = next(((old, nd)
                     for source in (go_work_replaces, go_replaces)
                     for rd in repl_dirs
                     for old, nd in source.get(rd, ())
                     if imp == old or imp.startswith(old + "/")), None) \
            if my_dir is not None else None
        if repl is not None:
            old, nd = repl
            sub = imp[len(old):].strip("/")
            cand_dir = f"{nd}/{sub}".strip("/") if sub else nd
            hit = False
            if cand_dir != src_dir:
                for f in go_files_by_dir.get(cand_dir, ()):
                    if f != rel:
                        edges[rel].add(f)
                        hit = True
            stats.count(lang, imp, rel, True, hit or cand_dir == src_dir)
            continue
        cands = [(mp, d) for mp, d in go_mods if imp == mp or imp.startswith(mp + "/")]
        # The longest module path owns the import; importer containment only
        # breaks ties between modules declaring the SAME path (example dirs
        # sharing a placeholder) — it must not override a nested module.
        equal = [c for c in cands if c[0] == cands[0][0]] if cands else []
        # deepest containing candidate first: a repo-root module contains
        # every importer, and must not shadow a nested same-path module
        containing = sorted((c for c in equal
                             if not c[1] or rel == c[1]
                             or rel.startswith(c[1] + "/")),
                            key=lambda c: -len(c[1]))
        owner = containing[0] if containing else (equal[0] if equal else None)
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


def _mask_rust(text):
    """Rust text with comments and string literals blanked (newlines kept).

    A brace in a comment would corrupt inline-module scope tracking, and a
    commented-out `use` line would invent an edge.
    """
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r'r#*"(?:[^"]|"(?!#))*"#*', blank, text)
    text = re.sub(r"/\*.*?\*/", blank, text, flags=re.S)
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', blank, text)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def rust_use_targets(stmt):
    """Expand one use statement into the paths it names.

    `use a::b::{c, d as e, self, f::{g}}` names a::b::c, a::b::d, a::b (self),
    and a::b::f::g — truncating at the brace would both lose the real targets
    and mis-resolve the bare prefix to lib.rs. Commas split at brace depth 0
    only, so nested groups stay attached to their own prefix. A prefixless
    group (`use {a::b, c::d};`, rustfmt's merged-imports style) expands each
    member as its own full path.
    """
    stmt = stmt.strip()
    if stmt.startswith("{") and stmt.endswith("}"):
        out = []
        items, cur, depth = [], "", 0
        for ch in stmt[1:-1]:
            if ch == "," and depth == 0:
                items.append(cur)
                cur = ""
                continue
            depth += (ch == "{") - (ch == "}")
            cur += ch
        items.append(cur)
        for item in items:
            out.extend(rust_use_targets(item))
        return out
    m = re.match(r"([\w:#]+?)\s*(?:::)?\s*\{(.*)\}\s*$", stmt, re.S)
    if not m:
        # plain path; drops any trailing ` as x`. '#' rides along so raw
        # identifiers (r#type) survive; segments strip the r# when resolving.
        m = re.match(r"[\w:#]+", stmt)
        return [m.group(0)] if m else []
    prefix, inner = m.group(1).rstrip(":"), m.group(2)
    items, cur, depth = [], "", 0
    for ch in inner:
        if ch == "," and depth == 0:
            items.append(cur)
            cur = ""
            continue
        depth += (ch == "{") - (ch == "}")
        cur += ch
    items.append(cur)
    out = []
    for item in items:
        for sub in rust_use_targets(item):
            out.append(prefix if sub == "self" else f"{prefix}::{sub}")
    return out


def _rust_edges(rel, text, file_set, ws, lang, edges, stats):
    orig_lines = text.splitlines()
    text = _mask_rust(text)
    own = ws.own_src(rel)
    own_dir = str(Path(rel).parent).replace("\\", "/")
    own_dir = "" if own_dir == "." else own_dir  # root files join without "./"
    stem = rel.rsplit("/", 1)[-1]
    is_root = stem in ("lib.rs", "main.rs", "mod.rs") or ws.is_target_root(rel)
    # The directory holding this module's CHILD modules: crate/module roots own
    # their directory; foo.rs owns foo/. self:: resolves here, and each super::
    # pops one level from here — so one super from a/child.rs lands in a/, the
    # importer's own directory, not a level above it.
    child_dir = own_dir if is_root else \
        (f"{own_dir}/{stem[:-3]}" if own_dir else stem[:-3])
    # both entry files present and this file is neither: its owning target
    # (lib vs bin) is unknowable textually, so bare crate:: falls to nothing
    dual_entries = (stem not in ("lib.rs", "main.rs")
                    and (f"{own}/lib.rs" if own else "lib.rs") in file_set
                    and (f"{own}/main.rs" if own else "main.rs") in file_set)
    # `mod x;` declares a child module — of the enclosing INLINE module when
    # one is open (`mod platform { mod imp; }` names platform/imp.rs), so the
    # scan tracks `mod x {` blocks by brace depth like any other scope.
    mod_stack, depth, pending_path = [], 0, None
    scope_at_line = []  # inline-module path enclosing each line
    for lineno, line in enumerate(text.splitlines()):
        scope_at_line.append("/".join(e["name"] for e in mod_stack))
        # the attribute's path is a string literal the mask blanked — read it
        # from the original line
        am = re.match(r'\s*#\[path\s*=\s*"([^"]+)"\]', orig_lines[lineno])
        if am:
            pending_path = am.group(1)
        dm = re.match(r"\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(?:r#)?(\w+)\s*(;|\{)", line)
        if dm and dm.group(2) == ";":
            name = dm.group(1)
            scope = "/".join(e["name"] for e in mod_stack)
            if pending_path:
                # attribute paths are relative to the declaring file's dir;
                # `..` segments normalize (an escape past the repo root is
                # None, hence external)
                cand = join_inside(own_dir, pending_path)
                t = cand if cand and cand in file_set and cand != rel else None
            else:
                bases = ((f"{child_dir}/{scope}".lstrip("/"),) if scope
                         else (child_dir, own_dir))
                t = next((c for b in bases
                          for pb in (f"{b}/" if b else "",)
                          for c in (f"{pb}{name}.rs", f"{pb}{name}/mod.rs")
                          if c in file_set and c != rel), None)
            if t:
                edges[rel].add(t)
            stats.count(lang, f"mod {name}", rel, True, bool(t))
            pending_path = None
        elif dm:
            # the block's brace is on this very line, so the module is live
            # immediately — a compact `mod m { .. }` closes by end of line
            mod_stack.append({"open": depth, "name": dm.group(1), "entered": True})
            pending_path = None
        depth += line.count("{") - line.count("}")
        for e in mod_stack:
            e["entered"] = e["entered"] or depth > e["open"]
        while mod_stack and mod_stack[-1]["entered"] and depth <= mod_stack[-1]["open"]:
            mod_stack.pop()
    for em in re.finditer(
            r"^\s*(?:#\[[^\]]*\]\s*)*(?:pub\s+)?extern\s+crate\s+"
            r"(?:r#)?(\w+)(?:\s+as\s+\w+)?\s*;", text, re.M):
        crate_name = em.group(1)
        if crate_name in ("std", "core", "alloc", "proc_macro", "test", "self"):
            continue
        root = ws.crate_root(crate_name, rel)
        t2 = (resolve_rust_path(root, [], file_set, ws.entry_of)
              if root is not None else None)
        if t2 and t2 != rel:
            edges[rel].add(t2)
        stats.count(lang, f"extern crate {crate_name}", rel,
                    root is not None, bool(t2))
    for sm in RUST_USE_RE.finditer(text):
        stmt = sm.group(1)
        use_scope = scope_at_line[text.count("\n", 0, sm.start())]
        for use in rust_use_targets(stmt):
            segs = [s.removeprefix("r#") for s in use.strip(":").split("::") if s]
            if not segs:
                continue
            head = segs[0]
            if head == "crate":
                root, segs = own, segs[1:]
            elif head in ("self", "super"):
                # self/super are relative to the LEXICAL module — including
                # any inline `mod x { ... }` blocks enclosing this use
                root = f"{child_dir}/{use_scope}" if use_scope else child_dir
                while segs and segs[0] == "super":
                    root, segs = root.rsplit("/", 1)[0], segs[1:]
                if segs and segs[0] == "self":
                    segs = segs[1:]
            elif ws.crate_root(head, rel) is not None:
                root, segs = ws.crate_root(head, rel), segs[1:]
            else:
                stats.count(lang, use, rel, False, False)
                continue
            t = resolve_rust_path(root, segs, file_set, ws.entry_of,
                                  skip_entries=(root == own and dual_entries))
            if t and t != rel:
                edges[rel].add(t)
            stats.count(lang, use, rel, True, bool(t))


def _jvm_edge(rel, imp, index, jvm_modules, lang, edges, stats):
    targets, first_party = resolve_jvm(imp, index, rel, jvm_modules)
    targets.discard(rel)
    edges[rel] |= targets
    if not first_party:
        first_party = tuple(imp.split(".")[:2]) in index.pkg_roots
    stats.count(lang, imp, rel, first_party, bool(targets))


def _cs_edge(rel, imp, prefixes, index, module_dirs, lang, edges, stats):
    """One using, tried against each enclosing namespace scope, innermost
    first, then the bare (global-scope) spec — the first that yields files
    wins, mirroring C#'s outward name lookup."""
    first_party = False
    for cand in [f"{pref}.{imp}" for pref in prefixes] + [imp]:
        targets, fp = resolve_jvm(cand, index, rel, module_dirs)
        targets.discard(rel)
        first_party = first_party or fp
        if targets:
            edges[rel] |= targets
            stats.count(lang, imp, rel, True, True)
            return
    if not first_party:
        first_party = tuple(imp.split(".")[:2]) in index.pkg_roots
    stats.count(lang, imp, rel, first_party, False)
