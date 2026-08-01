"""What build manifests declare: Cargo workspaces, Go modules, npm packages.

imports.py resolves import statements against source declarations; this module
supplies the manifest side — which crate a `use` head names from a given file,
which go.mod (and replace directives) govern an import path, which npm names
are external dependencies for a given package. All lookups are scoped to the
importing file's enclosing manifest, because that is how every one of these
ecosystems actually works: a Cargo alias, a Go replace, and an npm dependency
list each bind only the package that declares them.

Stdlib only; callers supply the file index so this module never walks a tree.
"""
import json
import re

from common import read_text

# Cargo target directories that are separate crates from the package's library:
# every file under them roots its own crate:: at the target, not at src/.
CARGO_TARGET_DIRS = ("src/bin", "tests", "examples", "benches")


class RustWorkspace:
    """Cargo workspace map: which crate a `use` head names, seen from a file.

    Crate names are global to a workspace, but everything binding them is
    local: dependency renames (`foo = { package = "bar", ... }`) belong to the
    manifest declaring them, and an unaliased name links only when the
    importer's manifest (or the workspace dependency table) declares it — an
    in-repo crate that happens to share a name with a crates.io dependency
    must not capture every `use` of that name. A custom lib target
    (`[lib] path = "source/root.rs"`) moves the crate root off src/; the
    declared entry file doubles as the bare-reference target.
    """

    def __init__(self, all_paths):
        self._main_dirs = {rel[: -len("/main.rs")] for rel in all_paths
                           if rel.endswith("/main.rs")}
        self.src_of = {}       # crate name (underscored) -> [(crate dir, src root)]
        self.entry_of = {}     # src root dir -> declared entry file, if custom
        self._aliases = {}         # crate dir -> {alias: crate name}
        self._deps = {}            # crate dir -> declared dependency names
        self._name_by_dir = {}     # crate dir -> its own package name
        self._src_by_dir = {}      # crate dir -> its default src/ root
        self._lib_src_by_dir = {}  # crate dir -> custom [lib] root, if any
        self._pkg_of_lib = {}      # [lib] name -> its package name
        self._dep_paths = {}       # crate dir -> {dep name: declared target dir}
        self._target_roots = {}    # crate dir -> [(explicit target dir, entry)]
        manifests = []
        for rel, p in all_paths.items():
            if rel == "Cargo.toml" or rel.endswith("/Cargo.toml"):
                manifests.append((rel[: -len("Cargo.toml")].rstrip("/"), read_text(p)))
        for d, text in manifests:
            # [package] specifically: a [[bin]] table with its own name may
            # legally precede it, and its name is not the crate's.
            pkg = _toml_section(text, "package")
            m = pkg and re.search(r"^\s*name\s*=\s*[\"']([^\"']+)[\"']", pkg, re.M)
            if not m:
                continue
            src = f"{d}/src" if d else "src"
            lib = _toml_section(text, "lib")
            pm = lib and re.search(r"^\s*path\s*=\s*[\"']([^\"']+)[\"']", lib, re.M)
            if pm:
                entry = f"{d}/{pm.group(1)}".lstrip("/")
                src = entry.rsplit("/", 1)[0] if "/" in entry else ""
                self.entry_of[src] = entry
                self._lib_src_by_dir[d] = src
            name = m.group(1).replace("-", "_")
            # Candidates, not a single winner: separate workspaces (or fixture
            # trees) may each ship a crate with this name, and crate_root()
            # picks the one nearest the importer.
            self.src_of.setdefault(name, []).append((d, src))
            # `[lib] name = "actual_lib"` is what `use` statements spell; the
            # dependency key stays the package name, so both are recorded.
            ln = lib and re.search(r"^\s*name\s*=\s*[\"']([^\"']+)[\"']", lib, re.M)
            if ln:
                lib_name = ln.group(1).replace("-", "_")
                self.src_of.setdefault(lib_name, []).append((d, src))
                self._pkg_of_lib[lib_name] = name
            # Explicit target paths ([[bin]] path = "cmd/main.rs" and custom
            # tests/examples/benches) root their own crates off src/.
            for header, body in re.findall(
                    r"^\[\[(bin|test|example|bench)\]\]\s*\n((?:(?!^\[)[^\n]*\n?)*)",
                    text, re.M):
                tp = re.search(r"^\s*path\s*=\s*[\"']([^\"']+)[\"']", body, re.M)
                if tp:
                    tentry = f"{d}/{tp.group(1)}".lstrip("/")
                    troot = tentry.rsplit("/", 1)[0] if "/" in tentry else ""
                    self._target_roots.setdefault(d, []).append((troot, tentry))
            self._name_by_dir[d] = name
            self._src_by_dir[d] = f"{d}/src" if d else "src"
        for d, text in manifests:
            deps = _cargo_dep_names(text)
            self._deps[d] = deps
            self._dep_paths[d] = _cargo_dep_paths(text, d)
            renames = re.findall(
                r"^\s*([\w-]+)\s*=\s*(\{[^}]*package\s*=\s*[\"'][^\"']+[\"'][^}]*\})", text, re.M)
            # the long form: [dependencies.foo] with package/path on own lines
            for header, body in re.findall(
                    r"^\[+([^\]]+)\]+\s*\n((?:(?!^\[)[^\n]*\n?)*)", text, re.M):
                parts_h = header.split(".")
                if (len(parts_h) >= 2 and parts_h[-2].endswith("dependencies")
                        and "metadata" not in parts_h):
                    pk = re.search(r"^\s*package\s*=\s*[\"']([^\"']+)[\"']", body, re.M)
                    if pk:
                        renames.append((parts_h[-1], body))
            self._aliases[d] = {}
            for alias, entry in renames:
                # A rename without a local path is a registry crate: Cargo will
                # not use a same-named in-repo package, so neither do we.
                if not re.search(r"\bpath\s*=|\bworkspace\s*=\s*true", entry):
                    continue
                target = re.search(r"package\s*=\s*[\"']([^\"']+)[\"']", entry).group(1)
                if target.replace("-", "_") in self.src_of:
                    self._aliases[d][alias.replace("-", "_")] = target.replace("-", "_")
        self._dirs = sorted(self._src_by_dir, key=len, reverse=True)

    def _crate_dir(self, rel):
        return next((d for d in self._dirs if not d or rel.startswith(d + "/")), "")

    def is_target_root(self, rel):
        """True when rel is itself a crate root by target position: a file
        directly inside src/bin, tests/, examples/, or benches/ (its `mod`
        children are its siblings), or the [lib]-declared entry file."""
        d = self._crate_dir(rel)
        if rel == self.entry_of.get(self._lib_src_by_dir.get(d, ""), ""):
            return True
        if any(rel == entry for _, entry in self._target_roots.get(d, ())):
            return True
        for tail in CARGO_TARGET_DIRS:
            tdir = f"{d}/{tail}" if d else tail
            if rel.startswith(tdir + "/") and "/" not in rel[len(tdir) + 1:]:
                return True
        return False

    def own_src(self, rel):
        """The crate root governing this file's crate:: — chosen per target.

        A package carries many crates: the library (possibly at a custom
        [lib] root), the default binary at src/main.rs, and auto-discovered
        targets under src/bin, tests/, examples/, benches/ — each roots its
        own tree.
        """
        d = self._crate_dir(rel)
        for troot, tentry in self._target_roots.get(d, ()):
            # A root-level entry ([[bin]] path = "main.rs") roots only that
            # file — matching the whole package would capture src/ modules.
            if rel == tentry or (troot != d and
                                 (rel == troot or rel.startswith(troot + "/"))):
                return troot
        for tail in CARGO_TARGET_DIRS:
            tdir = f"{d}/{tail}" if d else tail
            if rel.startswith(tdir + "/"):
                sub = rel[len(tdir) + 1:]
                # src/bin/cli/main.rs roots at src/bin/cli, but only when the
                # subdirectory is a real dir-target (holds a main.rs) —
                # tests/common/mod.rs belongs to the tests/ crate that
                # includes it, not to a phantom crate at tests/common.
                cand = f"{tdir}/{sub.split('/')[0]}"
                if "/" in sub and cand in self._main_dirs:
                    return cand
                return tdir
        default = self._src_by_dir.get(d, f"{d}/src" if d else "src")
        custom = self._lib_src_by_dir.get(d)
        if custom is not None and (rel.startswith(custom + "/") or
                                   (custom == "" and "/" not in rel)):
            return custom
        if rel.startswith(default + "/"):
            return default
        return custom if custom is not None else default

    def crate_root(self, head, importer_rel):
        """src root the head names from this file, or None.

        Honors local renames, then requires the name to be declared — by the
        importer's manifest, the workspace dependency table, or as the
        importer's own package (its tests and binaries use the library by
        name without a dependency entry).
        """
        d = self._crate_dir(importer_rel)
        # Renames bind the declaring manifest; a member inherits the workspace
        # root's rename only by opting in (`foo.workspace = true`, which lands
        # in the member's own dependency table).
        name = self._aliases.get(d, {}).get(head)
        if name is None and head in self._deps.get(d, ()):
            probe = d
            while name is None and probe:
                probe = probe.rsplit("/", 1)[0] if "/" in probe else ""
                name = self._aliases.get(probe, {}).get(head)
        if name is None:
            canonical = self._pkg_of_lib.get(head, head)
            # A [workspace.dependencies] entry is only visible to members that
            # opt in with `name.workspace = true` — and those entries land in
            # the member's own _deps, so no global union is consulted.
            declared = (canonical in self._deps.get(d, ())
                        or canonical == self._name_by_dir.get(d))
            if not declared:
                return None
            name = head
        cands = self.src_of.get(name)
        if not cands:
            return None
        # An explicit `path = "..."` names the target unambiguously — a
        # same-named crate nearer the importer must not win over it.
        declared_path = (self._dep_paths.get(d, {}).get(head)
                         or self._dep_paths.get(d, {}).get(name))
        if declared_path is not None:
            for cand_dir, cand_src in cands:
                if cand_dir == declared_path:
                    return cand_src

        def shared_depth(cand_dir):
            a = cand_dir.split("/") if cand_dir else []
            b = d.split("/") if d else []
            n = 0
            while n < len(a) and n < len(b) and a[n] == b[n]:
                n += 1
            return n
        # The candidate sharing the longest directory prefix with the importer
        # is the one its path dependency actually reaches.
        return max(cands, key=lambda c: shared_depth(c[0]))[1]


def _toml_section(text, name):
    m = re.search(rf"^\[{name}\]\s*\n((?:(?!^\[)[^\n]*\n?)*)", text, re.M)
    return m.group(1) if m else None


def _cargo_dep_names(text):
    """LOCAL dependency names in one manifest's *dependencies sections —
    [dev-dependencies], [target.'cfg(...)'.dependencies], and the long form
    included. Only path and workspace entries count: `log = "0.4"` declares
    the registry crate, and must not make an in-repo crate named log visible.
    """
    names = set()
    for header, body in re.findall(
            r"^\[+([^\]]+)\]+\s*\n((?:(?!^\[)[^\n]*\n?)*)", text, re.M):
        segs = header.split(".")
        # [package.metadata.*] is arbitrary tool config Cargo ignores — a
        # dependencies-shaped table inside it must not declare anything
        if ("metadata" in segs or "dependencies" not in header
                or header.startswith("workspace")):
            continue
        tail = header.split("dependencies", 1)[1].lstrip(".")
        if tail:  # [dependencies.foo] long form
            if re.search(r"^\s*path\s*=|^\s*workspace\s*=\s*true", body, re.M):
                names.add(tail.replace("-", "_"))
            continue
        for line in body.splitlines():
            lm = re.match(r"\s*([\w-]+)(?:\.workspace\s*=\s*true"
                          r"|\.path\s*=\s*.+|\s*=\s*(.+))", line)
            if lm and (lm.group(2) is None or
                       re.search(r"\bpath\s*=|\bworkspace\s*=\s*true", lm.group(2))):
                names.add(lm.group(1).replace("-", "_"))
    return names


def _cargo_dep_paths(text, d):
    """{dependency name: declared path target dir} for one manifest — the
    explicit `path = "..."` that disambiguates same-named crates."""
    out = {}
    for header, body in re.findall(
            r"^\[+([^\]]+)\]+\s*\n((?:(?!^\[)[^\n]*\n?)*)", text, re.M):
        segs = header.split(".")
        if ("metadata" in segs or "dependencies" not in header
                or header.startswith("workspace")):
            continue
        tail = header.split("dependencies", 1)[1].lstrip(".")
        if tail:
            pm = re.search(r"^\s*path\s*=\s*[\"']([^\"']+)[\"']", body, re.M)
            if pm:
                tgt = join_inside(d, pm.group(1))
                if tgt is not None:
                    out[tail.replace("-", "_")] = tgt
            continue
        for line in body.splitlines():
            dm = re.match(r"\s*([\w-]+)\.path\s*=\s*[\"']([^\"']+)[\"']", line)
            if dm:  # dotted form: foo.path = "../foo"
                name_l, path_l = dm.group(1), dm.group(2)
            else:   # inline table: foo = { ..., path = "../foo" }
                lm = re.match(r"\s*([\w-]+)\s*=\s*(.+)", line)
                pm = lm and re.search(
                    r"\bpath\s*=\s*[\"']([^\"']+)[\"']", lm.group(2))
                if not pm:
                    continue
                name_l, path_l = lm.group(1), pm.group(1)
            tgt = join_inside(d, path_l)
            if tgt is not None:
                out[name_l.replace("-", "_")] = tgt
    return out


def join_inside(base, target):
    """target resolved relative to base, or None if it escapes the repo root.

    A committed `replace ... => ../../dep` may point outside the analyzed
    tree; silently clamping it to an in-repo path would link unrelated files.
    """
    parts = base.split("/") if base else []
    for seg in target.split("/"):
        if seg == "..":
            if not parts:
                return None
            parts.pop()
        elif seg not in ("", "."):
            parts.append(seg)
    return "/".join(parts)


def go_modules(all_paths):
    """Every go.mod in the repo: ([(module_path, dir)] longest module first,
    {dir: [...]} go.mod replaces, {dir: [...]} go.work replaces).

    A `replace old => ../new` maps import paths under `old` into the local
    directory regardless of what module path that directory declares — and it
    binds only the module whose go.mod carries it.
    """
    mods, replaces, work_replaces = [], {}, {}
    for rel, p in all_paths.items():
        is_mod = rel == "go.mod" or rel.endswith("/go.mod")
        # go.work replacements use the same syntax, bind every workspace
        # member, and take precedence over go.mod replaces
        is_work = rel == "go.work" or rel.endswith("/go.work")
        if not (is_mod or is_work):
            continue
        text = read_text(p)
        d = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if is_mod:
            m = re.search(r"^module\s+(\S+)", text, re.M)
            if m:
                mods.append((m.group(1), d))
        repls = []
        for old, target in re.findall(
                r"^\s*(?:replace\s+)?(\S+)(?:\s+\S+)?\s*=>\s*(\S+)", text, re.M):
            # absolute targets point outside the scanned tree
            if target.startswith(("./", "../")) or target in (".", ".."):
                local = join_inside(d, target)
                if local is not None:
                    repls.append((old, local))
        if repls:
            (work_replaces if is_work else replaces).setdefault(d, []).extend(repls)
    return sorted(mods, key=lambda t: -len(t[0])), replaces, work_replaces


def npm_packages(all_paths):
    """(registry deps by dir, name -> [(dir, entry, subpath exports)],
    per-dir local info: the package's `imports` map and its local
    dependency aliases).

    A bare import matching a *registry* dependency is external for files in
    that package; a `workspace:`/`file:`/`link:` value names a sibling
    workspace package — a local edge, resolved via the name map. One monorepo
    package's dependency list says nothing about its neighbours."""
    by_dir, name_map, local_by_dir = {}, {}, {}
    for rel, p in all_paths.items():
        if rel == "package.json" or rel.endswith("/package.json"):
            try:
                data = json.loads(read_text(p))
            except ValueError:
                continue
            d = rel[: -len("package.json")].rstrip("/")
            if isinstance(data.get("name"), str):
                # candidates, not a single winner: independent trees may ship
                # same-named packages, and the caller picks the nearest
                name_map.setdefault(data["name"], []).append(
                    (d, _npm_entry(data), _npm_subpath_exports(data)))
            deps = set()
            # `"alias": "file:../actual"` installs the local package under the
            # alias; record alias -> target dir so imports of it resolve
            aliases = {}
            for key in ("dependencies", "devDependencies",
                        "peerDependencies", "optionalDependencies"):
                section = data.get(key)
                if not isinstance(section, dict):
                    continue  # a fixture may hold anything; skip, don't crash
                for dep_name, val in section.items():
                    sval = str(val)
                    if sval.startswith(("file:", "link:", "portal:")):
                        target = join_inside(d, sval.split(":", 1)[1])
                        if target is not None:
                            aliases[dep_name] = target
                    elif not sval.startswith("workspace:"):
                        deps.add(dep_name)
            # Node's own `imports` map (`#utils` -> ./src/utils.js)
            imports_map = {}
            imp = data.get("imports")
            if isinstance(imp, dict):
                for key, val in imp.items():
                    if isinstance(val, dict):
                        val = next((val[k] for k in ("import", "require", "default")
                                    if isinstance(val.get(k), str)), None)
                    if isinstance(key, str) and key.startswith("#") and isinstance(val, str):
                        imports_map[key] = val.lstrip("./")
            by_dir[d] = deps
            local_by_dir[d] = {"aliases": aliases, "imports": imports_map}
    return by_dir, name_map, local_by_dir


def _npm_subpath_exports(data):
    """{subpath: target} for explicit non-root exports entries
    ('./feature': './src/actual.ts', string or conditional form)."""
    exp = data.get("exports")
    out = {}
    if isinstance(exp, dict):
        for key, val in exp.items():
            if key in (".",) or not isinstance(key, str) or not key.startswith("./"):
                continue
            if isinstance(val, dict):
                val = next((val[k] for k in ("import", "require", "default")
                            if isinstance(val.get(k), str)), None)
            if isinstance(val, str):
                out[key[2:]] = val.lstrip("./")
    return out


def _npm_entry(data):
    """(esm entry, cjs entry) for the package. Node gives `exports`
    precedence over `main`; a conditional root serves its import condition
    to ESM callers and its require condition to CommonJS callers."""
    def clean(v):
        return v.lstrip("./") if isinstance(v, str) and v else ""
    esm = cjs = ""
    exp = data.get("exports")
    root = exp.get(".", exp) if isinstance(exp, dict) else exp
    if isinstance(root, str):
        esm = cjs = clean(root)
    elif isinstance(root, dict):
        esm = clean(root.get("import")) or clean(root.get("default"))
        cjs = clean(root.get("require")) or clean(root.get("default"))
    main = clean(data.get("main"))
    return (esm or main, cjs or main)


def nearest_dir(rel, dirs):
    """The deepest directory in `dirs` containing rel, or None."""
    return next((d for d in sorted(dirs, key=len, reverse=True)
                 if not d or rel.startswith(d + "/")), None)
