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
from manifests import (join_inside, RustWorkspace, go_modules, nearest_dir,
                       npm_packages)

JS_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"]
JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[\w*{}\s,$]+\s+from\s+)?|export\s+(?:[\w*{}\s,$]+\s+from\s+)|require\s*\(\s*|import\s*\(\s*)['"]([^'"]+)['"]"""
)
GO_IMPORT_RE = re.compile(r'^\s*(?:[\w.]+\s+)?"([^"]+)"', re.M)
JVM_PKG_RE = re.compile(r"^\s*package\s+([\w.]+)", re.M)
# Matches namespace-usings and aliases; 'using var x = ...' and
# 'using (var x = ...)' fail the required trailing ';' after the dotted name.
CS_USING_RE = re.compile(
    r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?:\w+\s*=\s*)?"
    r"(?:global::)?([\w.]+)\s*;", re.M)
CS_NS_RE = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
# Captures the full statement (a negated class crosses newlines, so a
# rustfmt-wrapped group still arrives whole); rust_use_targets() parses it.
RUST_USE_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+([^;]+);", re.M)
RUBY_REQ_RE = re.compile(r"""require_relative\s+['"]([^'"]+)['"]""")

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
            if not parts:
                return None  # climbs above the repo root: external
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


# Top-level (column-0) declarations: Kotlin and Scala imports name declarations,
# not files, so `import pkg.helper` must find whichever file declares
# `fun helper` — the file's name says nothing. Column 0 is what makes this
# textual scan safe: nested declarations are indented by universal convention.
# The optional dotted chain before the captured name skips an extension
# receiver: `fun String.helper()` declares helper, not String.
KT_DECL_RE = re.compile(
    r"^(?:(?:public|private|internal|protected|open|final|abstract|sealed|data|"
    r"inline|expect|actual|external|const|lateinit|tailrec|operator|infix|"
    r"suspend|enum|annotation|value)\s+)*"
    r"(?:fun|val|var|class|interface|object|typealias)\s+(?:<[^>\n]*>\s*)?"
    r"(?:[\w?]+(?:<[^>\n]*>)?\.)*(\w+)", re.M)
SCALA_DECL_RE = re.compile(
    r"^(?:(?:private|protected|implicit|final|sealed|abstract|lazy|case|open|"
    r"transparent|inline|opaque)\s+)*"
    r"(?:def|val|var|class|trait|object|type|given|enum)\s+(\w+)", re.M)
# `package object bar` puts the file's members in <enclosing pkg>.bar; the
# plain package regex must not swallow the `object` keyword as a package name.
SCALA_PKG_OBJ_RE = re.compile(r"^\s*package\s+object\s+(\w+)", re.M)
# C# type names owe nothing to file stems, and C# nests inside namespace
# blocks, so column-0 anchoring would find nothing: match declarations
# anywhere. An inner type over-indexes harmlessly — it still names this file.
CS_TYPE_RE = re.compile(
    r"\b(?:record(?:\s+(?:class|struct))?|class|struct|interface|enum|delegate)"
    r"\s+(\w+)")


SCALA_INNER_DECL_RE = re.compile(
    r"\s*(?:(?:private|protected|implicit|final|sealed|abstract|lazy|case|open|"
    r"transparent|inline|opaque)\s+)*"
    r"(?:def|val|var|class|trait|object|type|given|enum)\s+(\w+)")


def scala_packages(text):
    """The file's package structure: (composed unbraced chain, braced blocks).

    Unbraced leading clauses compose (`package a.b` + `package c` = a.b.c);
    a braced block (`package foo { ... }`) scopes only its own braces, so
    sibling blocks stay separate instead of composing into a phantom a.b.
    Returns the chain package plus {block package: declaration names inside} —
    block members are indented, so the column-0 scan cannot see them. A
    nested member over-indexes into its block's package, which is harmless:
    it still names this file.
    """
    chain, stack, depth, blocks = [], [], 0, {}
    colon_pkg = None  # `package p:` (Scala 3): the indented rest of file is p
    for line in text.splitlines():
        m = re.match(r"\s*package\s+(?:object\s+)?([\w.]+)\s*(\{|:\s*$)?", line)
        if m and re.match(r"\s*package\s+object\b", line) and not (
                m.group(2) == "{" or "{" in line):
            m = None  # a braceless package object line adds no scope here
        if m and m.group(2) == ":":
            chain.append(m.group(1))
            colon_pkg = ".".join(chain)
            blocks.setdefault(colon_pkg, set())
            continue
        if colon_pkg and not m:
            d = SCALA_INNER_DECL_RE.match(line)
            if d and line[:1] in (" ", "\t"):
                blocks[colon_pkg].add(d.group(1))
        if m and (m.group(2) == "{" or "{" in line):
            stack.append({"open": depth, "name": m.group(1), "entered": False})
            blocks.setdefault(".".join(chain + [e["name"] for e in stack]), set())
        elif m:
            chain.append(m.group(1))
        elif stack:
            d = SCALA_INNER_DECL_RE.match(line)
            if d:
                key = ".".join(chain + [e["name"] for e in stack])
                blocks.setdefault(key, set()).add(d.group(1))
        depth += line.count("{") - line.count("}")
        for e in stack:
            e["entered"] = e["entered"] or depth > e["open"]
        while stack and stack[-1]["entered"] and depth <= stack[-1]["open"]:
            stack.pop()
    return ".".join(chain), blocks


def _mask_cs(text):
    """C# text with comments and string/char literals blanked (newlines kept).

    A `// class Util ...` comment or a string containing braces would
    otherwise register phantom declarations and corrupt the namespace
    stack's brace tracking.
    """
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"/\*.*?\*/", blank, text, flags=re.S)
    text = re.sub(r'@"(?:[^"]|"")*"', blank, text)
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', blank, text)
    text = re.sub(r"'(?:\\.|[^'\\\n])*'", blank, text)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def cs_namespaces(text):
    """{namespace: type names declared inside it} for one C# file.

    `namespace A { namespace B { ... } }` declares A.B; a sibling block at the
    same depth must not compose with it, so each block is tracked against the
    brace depth it opened at and popped when that depth closes. A file-scoped
    declaration (`namespace X;`) covers the rest of the file and never pops.
    Each type declaration is attributed to the scope containing it — a file
    declaring A.X and B.Y does not declare A.Y.
    """
    out, stack, file_scoped, depth = {}, [], [], 0

    def key():
        return ".".join(file_scoped + [e["name"] for e in stack])
    for line in _mask_cs(text).splitlines():
        m = re.match(r"\s*namespace\s+([\w.]+)\s*(;)?", line)
        if m and m.group(2):
            file_scoped.append(m.group(1))
            out.setdefault(key(), set())
        elif m:
            stack.append({"open": depth, "name": m.group(1), "entered": False})
            out.setdefault(key(), set())
        else:
            for name in CS_TYPE_RE.findall(line):
                out.setdefault(key(), set()).add(name)
        depth += line.count("{") - line.count("}")
        for e in stack:
            # Allman style puts the brace on the next line; the namespace is
            # only live once its block has actually opened, and only then can
            # a closing brace pop it.
            e["entered"] = e["entered"] or depth > e["open"]
        while stack and stack[-1]["entered"] and depth <= stack[-1]["open"]:
            stack.pop()
    return out


def cs_usings(text):
    """[(using spec, enclosing-namespace prefixes, outermost first)].

    A using inside `namespace App { ... }` may name a sibling namespace
    relative to App — C# tries each enclosing scope before the global one,
    so resolution needs the prefixes, not just the bare spec.
    """
    out, stack, file_scoped, depth = [], [], [], 0
    for line in _mask_cs(text).splitlines():
        nm = re.match(r"\s*namespace\s+([\w.]+)\s*(;)?", line)
        if nm and nm.group(2):
            file_scoped.append(nm.group(1))
        elif nm:
            stack.append({"open": depth, "name": nm.group(1), "entered": False})
        else:
            um = CS_USING_RE.match(line)
            if um:
                chain = file_scoped + [e["name"] for e in stack]
                prefixes = [".".join(chain[:n]) for n in range(len(chain), 0, -1)]
                out.append((um.group(1), prefixes))
        depth += line.count("{") - line.count("}")
        for e in stack:
            e["entered"] = e["entered"] or depth > e["open"]
        while stack and stack[-1]["entered"] and depth <= stack[-1]["open"]:
            stack.pop()
    return out


class DeclIndex:
    """One ecosystem's declarations, keyed for import lookup.

    pkg_files: package/namespace -> files declaring it (a star import or a C#
    using is a dependency on all of them). decl_files: (package, Name) -> files,
    the layout-independent answer to `import a.b.C` — keyed by file stem and by
    top-level declaration names, since wherever a build tool put the file, its
    declarations say what it is. pkg_roots holds the first two segments of every
    declared package, the "does this repo plausibly own dev.rpg.*?" test for
    classifying unresolved imports.
    """

    def __init__(self):
        self.pkg_files = defaultdict(set)
        self.decl_files = defaultdict(set)
        self.pkg_roots = set()

    def add(self, pkg, rel, names):
        self.pkg_files[pkg].add(rel)
        for name in names:
            self.decl_files[(pkg, name)].add(rel)
        if pkg:
            self.pkg_roots.add(tuple(pkg.split(".")[:2]))


def build_decl_indexes(paths):
    """Separate declaration indexes for the JVM family and for C#: the two
    ecosystems never link (a Java package and a C# namespace sharing a dotted
    name are unrelated), so mixing them would invent impossible edges."""
    jvm, cs = DeclIndex(), DeclIndex()
    for rel, p in paths.items():
        ext = "." + rel.rsplit(".", 1)[-1] if "." in rel else ""
        if ext not in JVM_EXTS and ext != ".cs":
            continue
        text = read_text(p)
        stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        names = {stem}
        if ext == ".cs":
            for pkg, decls in (cs_namespaces(text) or {"": set()}).items():
                cs.add(pkg, rel, decls | {stem})
            continue
        if ext == ".scala":
            names.discard(stem)  # Scala file names declare nothing
            pkg, scala_blocks = scala_packages(text)
            names |= set(SCALA_DECL_RE.findall(text))
            # Scala 3 extension blocks: `extension (s: String)` followed by
            # indented defs declares those defs as importable package members.
            for block in re.findall(
                    r"^extension\b[^\n]*\n((?:[ \t]+[^\n]*(?:\n|$))*)", text, re.M):
                names |= set(re.findall(r"\bdef\s+(\w+)", block))
            pobj = SCALA_PKG_OBJ_RE.search(text)
            if pobj:
                # The object's members live one level deeper; index the file
                # there too, so `import foo.bar.helper` finds it.
                jvm.add(f"{pkg}.{pobj.group(1)}".lstrip("."), rel, names)
            for bpkg, decls in scala_blocks.items():
                if bpkg == pkg:
                    names |= decls  # colon-syntax body: same package, indented
                else:
                    jvm.add(bpkg, rel, decls)
        else:
            m = JVM_PKG_RE.search(text)
            pkg = m.group(1) if m else ""
            if ext == ".kt":
                names.discard(stem)  # Kotlin file names declare nothing
                names |= set(KT_DECL_RE.findall(text))
        jvm.add(pkg, rel, names)
    return jvm, cs


def jvm_import_specs(text):
    """Expand every import statement into plain dotted specs.

    A grouped line expands member by member — the naive pattern would truncate
    `import p.{A, B}` to a package-wide `p.` and link unrelated files. Grouped
    members may be renamed (`A => B` in Scala 2, `A as B` in Scala 3); the
    original name is what the declaration index knows. A wildcard member
    (`_`, `*`, `given`) falls back to the package-star form.
    """
    out = []
    lines = text.splitlines()
    i = -1
    while i + 1 < len(lines):
        i += 1
        line = lines[i]
        m = re.match(r"\s*import\s+(?:static\s+)?(.+)", line)
        if not m:
            continue
        # A formatter may wrap a grouped import; accumulate to the brace close.
        joined = m.group(1)
        while joined.count("{") > joined.count("}") and i + 1 < len(lines):
            i += 1
            joined += " " + lines[i].strip()
        m = re.match(r"(.+)", joined)
        # Scala allows several expressions per statement (`import p.A, q.B`);
        # split on commas outside braces so grouped members stay together.
        exprs, cur, depth = [], "", 0
        for ch in m.group(1).rstrip().rstrip(";"):
            if ch == "," and depth == 0:
                exprs.append(cur)
                cur = ""
                continue
            depth += (ch == "{") - (ch == "}")
            cur += ch
        exprs.append(cur)
        for expr in exprs:
            expr = expr.strip()
            g = re.fullmatch(r"([\w.]+)\.\{([^}]*)\}", expr)
            if g:
                prefix, inner = g.groups()
                for item in inner.split(","):
                    name = item.split("=>")[0].split(" as ")[0].strip()
                    if name in ("_", "*", "given") or not name:
                        out.append(f"{prefix}.*")
                    elif re.fullmatch(r"\w+", name):
                        out.append(f"{prefix}.{name}")
            else:
                pm = re.match(r"[\w.*]+", expr)
                if pm:
                    out.append(pm.group(0))
    return out


def resolve_jvm(imp, index, importer_rel=None, module_dirs=()):
    """Resolve a dotted import against a DeclIndex. Returns (files, first_party).

    A `.*`/`._` suffix (or a bare package name — the C# using case) links every
    file of the package. Otherwise the boundary between package and symbol is
    unknown (a.b.C, a.b.C.Inner, a.b.topLevelFn all exist), so the split is
    searched right-to-left against declared (package, name) pairs. Ambiguity —
    two files declaring the same package+name — first narrows to the
    importer's own build module (independent Gradle/Maven modules may declare
    the same symbol without sharing a classpath); what remains ambiguous
    resolves to nothing: a wrong edge is worse than a missing one.
    """
    def narrow(hits):
        if len(hits) > 1 and importer_rel is not None and module_dirs:
            mine = nearest_dir(importer_rel, module_dirs)
            if mine is not None:
                local = {h for h in hits if nearest_dir(h, module_dirs) == mine}
                if local:
                    return local
        return hits
    imp = imp.rstrip(".").removeprefix("_root_.")
    star = imp.endswith((".*", "._"))
    if star:
        imp = imp[:-2]
    elif imp.endswith(".given"):
        # Scala 3: `import p.given` pulls all givens from p — a wildcard,
        # not a declaration named `given` (a reserved word).
        star, imp = True, imp[:-len(".given")]
    if star or imp in index.pkg_files:
        files = narrow(index.pkg_files.get(imp, set()))
        if files or not star:
            first_party = bool(files) or tuple(imp.split(".")[:2]) in index.pkg_roots
            return (set(sorted(files)[:MAX_STAR_TARGETS]), first_party)
        # `import static com.acme.Utility.*`: the wildcard hangs off a declared
        # type, not a package — fall through and resolve the type itself.
    segs = imp.split(".")
    for i in range(len(segs) - 1, 0, -1):
        hits = narrow(index.decl_files.get((".".join(segs[:i]), segs[i]), set()))
        if len(hits) == 1:
            return (set(hits), True)
        if hits:
            return (set(), True)  # ambiguous: first-party, deliberately unresolved
    pkg = ".".join(segs[:-1])
    files = narrow(index.pkg_files.get(pkg, set()))
    if len(files) == 1:
        # `import a.b.unrecognizedName` in a single-file package: the file is certain.
        return (set(files), True)
    return (set(), bool(files))


def resolve_rust_path(src_root, segs, file_set, entry_of=None):
    """a::b::c against a crate src tree: the tail segments are items, not
    modules, so back off until a module file exists; a bare crate reference
    lands on its lib.rs/main.rs (where the pub use re-exports live), or on
    the [lib]-declared entry file when the crate names a custom target."""
    for n in range(len(segs), 0, -1):
        base = f"{src_root}/{'/'.join(segs[:n])}"
        for cand in (f"{base}.rs", f"{base}/mod.rs"):
            if cand in file_set:
                return cand
    # mod.rs covers the self::/super:: case, where the root is a module
    # directory rather than a crate src root.
    for entry in ("lib.rs", "main.rs", "mod.rs"):
        if f"{src_root}/{entry}" in file_set:
            return f"{src_root}/{entry}"
    # ... and a module using the file layout (src/foo.rs owning src/foo/) is
    # the sibling FILE of the directory the root path names.
    if f"{src_root}.rs" in file_set:
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
    go_mods, go_replaces = go_modules(all_paths)
    jvm_idx, cs_idx = build_decl_indexes(paths)
    rust_ws = RustWorkspace(all_paths)
    npm_by_dir, npm_names = npm_packages(all_paths)
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
        if f.endswith(".go"):
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
            _js_edges(rel, text, file_set, by_base, npm_by_dir, npm_names, lang, edges, stats)
        elif ext == ".go":
            _go_edges(rel, text, go_mods, go_replaces, go_files_by_dir, lang, edges, stats)
        elif ext == ".rs":
            _rust_edges(rel, text, file_set, rust_ws, lang, edges, stats)
        elif ext in JVM_EXTS:
            for imp in jvm_import_specs(text):
                _jvm_edge(rel, imp, jvm_idx, jvm_modules, lang, edges, stats)
        elif ext == ".cs":
            for imp, prefixes in cs_usings(text):
                _cs_edge(rel, imp, prefixes, cs_idx, cs_modules, lang, edges, stats)
        elif ext == ".rb":
            for spec in RUBY_REQ_RE.findall(text):
                t = resolve_relative_js(rel, spec if spec.endswith(".rb") else spec + ".rb", file_set)
                if t and t != rel:
                    edges[rel].add(t)
                stats.count(lang, spec, rel, True, bool(t))
    return edges, stats


def _js_edges(rel, text, file_set, by_base, npm_by_dir, npm_names, lang, edges, stats):
    pkg_dir = nearest_dir(rel, npm_by_dir)
    npm_deps = npm_by_dir.get(pkg_dir, set()) if pkg_dir is not None else set()

    def workspace_entry(name):
        """The file a workspace package's name resolves to: its declared main,
        else a conventional index module."""
        if name not in npm_names:
            return None
        d, main = npm_names[name]
        cands = [f"{d}/{main}".strip("/")] if main else []
        for stem_c in ("src/index", "index", "src/main", "lib/index"):
            cands += [f"{d}/{stem_c}{e}" for e in JS_EXTS]
        return next((c for c in cands if c in file_set), None)
    for spec in JS_IMPORT_RE.findall(text):
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
        else:
            # A bare specifier is usually a package — but a name that is not a
            # declared dependency and uniquely names a repo file is a
            # baseUrl-style first-party import.
            t = None
            root = spec.split("/")[0]
            # a scoped package's name spans two segments (@scope/utils)
            pkg_name = ("/".join(spec.split("/")[:2])
                        if spec.startswith("@") else root)
            if pkg_name in npm_names and pkg_name not in npm_deps:
                # a sibling workspace package (declared workspace:/file:, or
                # undeclared but present in the repo) — a local edge by name; a
                # subpath import reaches into the package's own tree. A name
                # the importer declares as a REGISTRY dependency stays
                # external even when a same-named local package exists.
                if spec == pkg_name:
                    t = workspace_entry(pkg_name)
                else:
                    wdir = npm_names[pkg_name][0]
                    sub = spec[len(pkg_name) + 1:]
                    cands = [f"{wdir}/{sub}"] if re.search(r"\.[a-z]+$", sub) else []
                    for e in JS_EXTS:
                        cands += [f"{wdir}/{sub}{e}", f"{wdir}/{sub}/index{e}",
                                  f"{wdir}/src/{sub}{e}"]
                    t = next((c for c in cands if c in file_set), None)
            elif (not spec.startswith("@") and root not in npm_deps
                    and root not in NODE_BUILTINS):
                t = resolve_unique_suffix(spec, by_base, JS_EXTS, scope=pkg_dir)
            if t and t != rel:
                edges[rel].add(t)
            stats.count(lang, spec, rel, bool(t), bool(t))


def _go_edges(rel, text, go_mods, go_replaces, go_files_by_dir, lang, edges, stats):
    my_dir = nearest_dir(rel, {d for _, d in go_mods})
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
        # The importer's own go.mod may rewrite this prefix to a local dir —
        # and its replace binds only this module.
        repl = next(((old, nd) for old, nd in go_replaces.get(my_dir, ())
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
        owner = next(((mp, d) for mp, d in equal
                      if not d or rel == d or rel.startswith(d + "/")),
                     equal[0] if equal else None)
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
    own = ws.own_src(rel)
    own_dir = str(Path(rel).parent).replace("\\", "/")
    stem = rel.rsplit("/", 1)[-1]
    is_root = stem in ("lib.rs", "main.rs", "mod.rs") or ws.is_target_root(rel)
    # The directory holding this module's CHILD modules: crate/module roots own
    # their directory; foo.rs owns foo/. self:: resolves here, and each super::
    # pops one level from here — so one super from a/child.rs lands in a/, the
    # importer's own directory, not a level above it.
    child_dir = own_dir if is_root else f"{own_dir}/{stem[:-3]}"
    # `mod x;` declares a child module — of the enclosing INLINE module when
    # one is open (`mod platform { mod imp; }` names platform/imp.rs), so the
    # scan tracks `mod x {` blocks by brace depth like any other scope.
    mod_stack, depth, pending_path = [], 0, None
    for line in text.splitlines():
        am = re.match(r'\s*#\[path\s*=\s*"([^"]+)"\]', line)
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
                bases = ((f"{child_dir}/{scope}",) if scope
                         else (child_dir, own_dir))
                t = next((c for b in bases
                          for c in (f"{b}/{name}.rs", f"{b}/{name}/mod.rs")
                          if c in file_set and c != rel), None)
            if t:
                edges[rel].add(t)
            stats.count(lang, f"mod {name}", rel, True, bool(t))
            pending_path = None
        elif dm:
            mod_stack.append({"open": depth, "name": dm.group(1), "entered": False})
            pending_path = None
        depth += line.count("{") - line.count("}")
        for e in mod_stack:
            e["entered"] = e["entered"] or depth > e["open"]
        while mod_stack and mod_stack[-1]["entered"] and depth <= mod_stack[-1]["open"]:
            mod_stack.pop()
    for stmt in RUST_USE_RE.findall(text):
        for use in rust_use_targets(stmt):
            segs = [s.removeprefix("r#") for s in use.strip(":").split("::") if s]
            if not segs:
                continue
            head = segs[0]
            if head == "crate":
                root, segs = own, segs[1:]
            elif head in ("self", "super"):
                root = child_dir
                while segs and segs[0] == "super":
                    root, segs = root.rsplit("/", 1)[0], segs[1:]
                if segs and segs[0] == "self":
                    segs = segs[1:]
            elif ws.crate_root(head, rel) is not None:
                root, segs = ws.crate_root(head, rel), segs[1:]
            else:
                stats.count(lang, use, rel, False, False)
                continue
            t = resolve_rust_path(root, segs, file_set, ws.entry_of)
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
