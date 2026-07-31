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
        self.src_of = {}       # crate name (underscored) -> [(crate dir, src root)]
        self.entry_of = {}     # src root dir -> declared entry file, if custom
        self._aliases = {}         # crate dir -> {alias: crate name}
        self._deps = {}            # crate dir -> declared dependency names
        self._name_by_dir = {}     # crate dir -> its own package name
        self._src_by_dir = {}      # crate dir -> its default src/ root
        self._lib_src_by_dir = {}  # crate dir -> custom [lib] root, if any
        self._ws_deps = set()      # [workspace.dependencies] names, any manifest
        self._pkg_of_lib = {}      # [lib] name -> its package name
        self._target_roots = {}    # crate dir -> [(explicit target dir, entry)]
        manifests = []
        for rel, p in all_paths.items():
            if rel == "Cargo.toml" or rel.endswith("/Cargo.toml"):
                manifests.append((rel[: -len("Cargo.toml")].rstrip("/"), read_text(p)))
        for d, text in manifests:
            # [package] specifically: a [[bin]] table with its own name may
            # legally precede it, and its name is not the crate's.
            pkg = _toml_section(text, "package")
            m = pkg and re.search(r'^\s*name\s*=\s*"([^"]+)"', pkg, re.M)
            if not m:
                continue
            src = f"{d}/src" if d else "src"
            lib = _toml_section(text, "lib")
            pm = lib and re.search(r'^\s*path\s*=\s*"([^"]+)"', lib, re.M)
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
            ln = lib and re.search(r'^\s*name\s*=\s*"([^"]+)"', lib, re.M)
            if ln:
                lib_name = ln.group(1).replace("-", "_")
                self.src_of.setdefault(lib_name, []).append((d, src))
                self._pkg_of_lib[lib_name] = name
            # Explicit target paths ([[bin]] path = "cmd/main.rs" and custom
            # tests/examples/benches) root their own crates off src/.
            for header, body in re.findall(
                    r"^\[\[(bin|test|example|bench)\]\]\s*\n((?:(?!^\[)[^\n]*\n?)*)",
                    text, re.M):
                tp = re.search(r'^\s*path\s*=\s*"([^"]+)"', body, re.M)
                if tp:
                    tentry = f"{d}/{tp.group(1)}".lstrip("/")
                    troot = tentry.rsplit("/", 1)[0] if "/" in tentry else ""
                    self._target_roots.setdefault(d, []).append((troot, tentry))
            self._name_by_dir[d] = name
            self._src_by_dir[d] = f"{d}/src" if d else "src"
        for d, text in manifests:
            deps = _cargo_dep_names(text)
            self._ws_deps |= _cargo_workspace_dep_names(text)
            self._deps[d] = deps
            renames = re.findall(
                r'^\s*([\w-]+)\s*=\s*(\{[^}]*package\s*=\s*"[^"]+"[^}]*\})', text, re.M)
            self._aliases[d] = {}
            for alias, entry in renames:
                # A rename without a local path is a registry crate: Cargo will
                # not use a same-named in-repo package, so neither do we.
                if not re.search(r"\bpath\s*=|\bworkspace\s*=\s*true", entry):
                    continue
                target = re.search(r'package\s*=\s*"([^"]+)"', entry).group(1)
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
        for troot, _ in self._target_roots.get(d, ()):
            if rel == troot or rel.startswith(troot + "/") or troot == "":
                return troot
        for tail in CARGO_TARGET_DIRS:
            tdir = f"{d}/{tail}" if d else tail
            if rel.startswith(tdir + "/"):
                sub = rel[len(tdir) + 1:]
                # src/bin/cli/main.rs roots at src/bin/cli; src/bin/cli.rs
                # (and its `mod` siblings) roots at src/bin itself.
                return f"{tdir}/{sub.split('/')[0]}" if "/" in sub else tdir
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
        # Renames bind the declaring manifest, and a member inheriting a
        # dependency (`foo.workspace = true`) inherits the workspace root's
        # rename with it — so enclosing manifests are consulted outward.
        name = None
        probe = d
        while name is None:
            name = self._aliases.get(probe, {}).get(head)
            if not probe:
                break
            probe = probe.rsplit("/", 1)[0] if "/" in probe else ""
        if name is None:
            canonical = self._pkg_of_lib.get(head, head)
            declared = (canonical in self._deps.get(d, ()) or canonical in self._ws_deps
                        or canonical == self._name_by_dir.get(d))
            if not declared:
                return None
            name = head
        cands = self.src_of.get(name)
        if not cands:
            return None

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
        if "dependencies" not in header or header.startswith("workspace"):
            continue
        tail = header.split("dependencies", 1)[1].lstrip(".")
        if tail:  # [dependencies.foo] long form
            if re.search(r"^\s*path\s*=|^\s*workspace\s*=\s*true", body, re.M):
                names.add(tail.replace("-", "_"))
            continue
        for line in body.splitlines():
            lm = re.match(r"\s*([\w-]+)(?:\.workspace\s*=\s*true|\s*=\s*(.+))", line)
            if lm and (lm.group(2) is None or
                       re.search(r"\bpath\s*=|\bworkspace\s*=\s*true", lm.group(2))):
                names.add(lm.group(1).replace("-", "_"))
    return names


def _cargo_workspace_dep_names(text):
    body = _toml_section(text, r"workspace\.dependencies") or ""
    return {m.group(1).replace("-", "_")
            for m in re.finditer(r"^\s*([\w-]+)\s*=\s*(.+)", body, re.M)
            if re.search(r"\bpath\s*=", m.group(2))}


def _join_inside(base, target):
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
    {dir: [(old_prefix, local_dir)]} for local replace directives).

    A `replace old => ../new` maps import paths under `old` into the local
    directory regardless of what module path that directory declares — and it
    binds only the module whose go.mod carries it.
    """
    mods, replaces = [], {}
    for rel, p in all_paths.items():
        if rel == "go.mod" or rel.endswith("/go.mod"):
            text = read_text(p)
            d = rel[: -len("go.mod")].rstrip("/")
            m = re.search(r"^module\s+(\S+)", text, re.M)
            if m:
                mods.append((m.group(1), d))
            repls = []
            for old, target in re.findall(
                    r"^\s*(?:replace\s+)?(\S+)(?:\s+\S+)?\s*=>\s*(\S+)", text, re.M):
                if target.startswith(("./", "../", "/")) or target in (".", ".."):
                    local = _join_inside(d, target)
                    if local is not None:
                        repls.append((old, local))
            if repls:
                replaces[d] = repls
    return sorted(mods, key=lambda t: -len(t[0])), replaces


def npm_packages(all_paths):
    """(registry deps by package dir, workspace package name -> (dir, main)).

    A bare import matching a *registry* dependency is external for files in
    that package; a `workspace:`/`file:`/`link:` value names a sibling
    workspace package — a local edge, resolved via the name map. One monorepo
    package's dependency list says nothing about its neighbours."""
    by_dir, name_map = {}, {}
    for rel, p in all_paths.items():
        if rel == "package.json" or rel.endswith("/package.json"):
            try:
                data = json.loads(read_text(p))
            except ValueError:
                continue
            d = rel[: -len("package.json")].rstrip("/")
            if isinstance(data.get("name"), str):
                name_map[data["name"]] = (d, data.get("main") or "")
            deps = set()
            for key in ("dependencies", "devDependencies",
                        "peerDependencies", "optionalDependencies"):
                for dep_name, val in (data.get(key) or {}).items():
                    if not str(val).startswith(("workspace:", "file:", "link:", "portal:")):
                        deps.add(dep_name)
            by_dir[d] = deps
    return by_dir, name_map


def nearest_dir(rel, dirs):
    """The deepest directory in `dirs` containing rel, or None."""
    return next((d for d in sorted(dirs, key=len, reverse=True)
                 if not d or rel.startswith(d + "/")), None)
