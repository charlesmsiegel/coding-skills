"""JVM-family and C# declaration indexing and import resolution.

Kotlin, Scala, Java, and C# all name DECLARATIONS in their imports — a
package line, a namespace block, a top-level fun — never file paths, so
resolution here is a declaration index (DeclIndex) consulted by dotted-name
lookup (resolve_jvm), with per-language scanners feeding it: column-0
declarations for Kotlin/Scala, scope-tracked namespace blocks for C#
(comments and strings masked first), package objects and Scala 3 syntax
included. imports.py owns the per-file loop and calls in here.

Stdlib only; callers supply file text and the module boundaries.
"""
import re
from collections import defaultdict

from common import read_text
from manifests import nearest_dir

JVM_PKG_RE = re.compile(r"^\s*package\s+([\w.]+)", re.M)
# Matches namespace-usings and aliases; 'using var x = ...' and
# 'using (var x = ...)' fail the required trailing ';' after the dotted name.
CS_USING_RE = re.compile(
    r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?:\w+\s*=\s*)?"
    r"(?:global::)?([\w.]+)\s*;", re.M)
CS_NS_RE = re.compile(r"^\s*namespace\s+([\w.]+)", re.M)
JVM_EXTS = (".java", ".kt", ".scala")
# A star import (or a C# namespace-using) is a real dependency on every file of
# the package, but a package that large is a namespace, not a unit — linking
# hundreds of files off one line would drown the graph in noise.
MAX_STAR_TARGETS = 25


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
# `fun \`when\`()` declares the escaped name; imports strip backticks too
KT_BACKTICK_DECL_RE = re.compile(
    r"^(?:(?:public|private|internal|protected|open|final|abstract|sealed|data|"
    r"inline|expect|actual|external|const|lateinit|tailrec|operator|infix|"
    r"suspend|enum|annotation|value)\s+)*"
    r"(?:fun|val|var|class|interface|object|typealias)\s+`([^`\n]+)`", re.M)
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
    r"\b(?:record(?:\s+(?:class|struct))?|class|struct|interface|enum)"
    r"\s+(\w+)")
CS_PARTIAL_RE = re.compile(
    r"\bpartial\s+(?:record(?:\s+(?:class|struct))?|class|struct|interface)"
    r"\s+(\w+)")
# a delegate's name follows its return type: `delegate void Handler();`
CS_DELEGATE_RE = re.compile(
    r"\bdelegate\s+[\w<>\[\],?.\s]+?\b(\w+)\s*\(")


def _cs_decl_names(segment):
    return CS_TYPE_RE.findall(segment) + CS_DELEGATE_RE.findall(segment)


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
            stack.append({"open": depth, "name": m.group(1),
                          "entered": line.count("{") == line.count("}") + 1
                          and line.rstrip().endswith("}")})
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
    out, partials, stack, file_scoped, depth = {}, {}, [], [], 0

    def key():
        return ".".join(file_scoped + [e["name"] for e in stack])

    def add_names(segment):
        k = key()
        for name in _cs_decl_names(segment):
            out.setdefault(k, set()).add(name)
        for name in CS_PARTIAL_RE.findall(segment):
            partials.setdefault(k, set()).add(name)
    for line in _mask_cs(text).splitlines():
        m = re.match(r"\s*namespace\s+([\w.]+)\s*(;)?", line)
        if m and m.group(2):
            file_scoped.append(m.group(1))
            out.setdefault(key(), set())
        elif m:
            # a brace on the namespace's own line opens (and may close) the
            # block immediately — `namespace P { class A {} }` must pop below
            stack.append({"open": depth, "name": m.group(1),
                          "entered": "{" in line[m.end():]})
            out.setdefault(key(), set())
        if m:
            # compact form: `namespace P { class Util {} }` declares on the
            # same line, inside the just-opened scope
            add_names(line[m.end():])
        else:
            add_names(line)
        depth += line.count("{") - line.count("}")
        for e in stack:
            # Allman style puts the brace on the next line; the namespace is
            # only live once its block has actually opened, and only then can
            # a closing brace pop it.
            e["entered"] = e["entered"] or depth > e["open"]
        while stack and stack[-1]["entered"] and depth <= stack[-1]["open"]:
            stack.pop()
    return out, partials


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
            stack.append({"open": depth, "name": nm.group(1),
                          "entered": "{" in line[nm.end():]})
        else:
            um = CS_USING_RE.match(line)
            if um:
                if re.match(r"\s*(?:global\s+)?using\s+(?:static\s+)?"
                            r"(?:\w+\s*=\s*)?global::", line):
                    # `using global::X;` names the global namespace explicitly
                    prefixes = []
                else:
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
        self.partial_files = defaultdict(set)  # (pkg, name) -> files saying partial
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
        if ext != ".cs":
            # a declaration-looking line inside a comment must not become a
            # rival claim that turns the real declaration ambiguous
            text = _mask_jvm(text)
        if ext == ".cs":
            # C# filenames declare nothing — a stem claim would satisfy (or
            # make ambiguous) usings the file's real types never declare
            ns_decls, ns_partials = cs_namespaces(text)
            for pkg, decls in (ns_decls or {"": set()}).items():
                cs.add(pkg, rel, decls)
            for pkg, names in ns_partials.items():
                for name in names:
                    cs.partial_files[(pkg, name)].add(rel)
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
                names |= set(KT_BACKTICK_DECL_RE.findall(text))
        jvm.add(pkg, rel, names)
    return jvm, cs


def _mask_jvm(text):
    """Java/Kotlin/Scala text with comments and strings blanked (newlines
    kept) — a commented-out import must not become an edge."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"/\*.*?\*/", blank, text, flags=re.S)
    text = re.sub(r'"""(?:[^"]|"(?!""))*"""', blank, text)
    text = re.sub(r'"(?:\\.|[^"\\\n])*"', blank, text)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def jvm_import_specs(text):
    """Expand every import statement into plain dotted specs.

    A grouped line expands member by member — the naive pattern would truncate
    `import p.{A, B}` to a package-wide `p.` and link unrelated files. Grouped
    members may be renamed (`A => B` in Scala 2, `A as B` in Scala 3); the
    original name is what the declaration index knows. A wildcard member
    (`_`, `*`, `given`) falls back to the package-star form.
    """
    out = []
    lines = _mask_jvm(text).splitlines()
    i = -1
    while i + 1 < len(lines):
        i += 1
        line = lines[i]
        # Scala 3 `export lib.Util` is a compile-time dependency like an
        # import (Java module-info's `exports p;` fails the \s+ after
        # 'export', so it cannot match).
        m = re.match(r"\s*(?:import|export)\s+(?:static\s+)?(.+)", line)
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
                # Kotlin backtick-escaped segments (import p.`when`) survive
                # and are stripped for lookup against the escaped declaration
                seg = r"(?:`[^`\n]+`|[\w*]+)"
                pm = re.match(rf"{seg}(?:\.{seg})*", expr)
                if pm:
                    out.append(pm.group(0).replace("`", ""))
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
    for i in range(len(segs) - 1, -1, -1):
        # i == 0 is the global-namespace split: `using static Util;` names a
        # type declared outside any namespace, keyed ("", "Util").
        key = (".".join(segs[:i]), segs[i])
        hits = narrow(index.decl_files.get(key, set()))
        if len(hits) == 1:
            return (set(hits), True)
        if hits:
            # partial C# types split one declaration across files — when every
            # claimant says partial, the import depends on all of them
            if hits <= index.partial_files.get(key, set()):
                return (set(sorted(hits)[:MAX_STAR_TARGETS]), True)
            return (set(), True)  # ambiguous: first-party, deliberately unresolved
    pkg = ".".join(segs[:-1])
    files = narrow(index.pkg_files.get(pkg, set()))
    if len(files) == 1:
        # `import a.b.unrecognizedName` in a single-file package: the file is certain.
        return (set(files), True)
    return (set(), bool(files))
