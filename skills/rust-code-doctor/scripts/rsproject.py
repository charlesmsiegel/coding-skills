#!/usr/bin/env python3
"""The whole-tree view: crates, their manifests, and the module graph.

Rust's compilation unit is the crate, and what a file *is* depends on whether
some `mod` declaration reaches it from a crate root. A `.rs` file under `src/`
that nothing declares is not dead code in the usual sense — it is not code at
all, because rustc never sees it. Answering that needs the manifest and the
`mod` graph together, which is what this module builds.

`load_project` memoizes per root, so the whole-tree detectors parse the tree
once between them. **Treat the result as read-only** — a detector that rewrote
it would change what every later detector sees.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from common import EXCLUDE_DIRS, find_rs_files
from rsparse import RsFile, RustSyntaxError, parse_file


@dataclass
class Crate:
    """One Cargo package: its manifest, its roots, and what it depends on."""

    name: str
    manifest: Path
    root_dir: Path
    edition: str = ""
    rust_version: str = ""
    is_workspace_root: bool = False
    is_virtual_manifest: bool = False
    lib_root: Path | None = None
    bin_roots: list[Path] = field(default_factory=list)
    # Explicit `[[test]]`/`[[bench]]`/`[[example]]` paths from the manifest.
    auxiliary_roots: list[Path] = field(default_factory=list)
    # name -> the raw manifest value (a version string or a table)
    dependencies: dict[str, object] = field(default_factory=dict)
    dev_dependencies: dict[str, object] = field(default_factory=dict)
    build_dependencies: dict[str, object] = field(default_factory=dict)
    package: dict[str, object] = field(default_factory=dict)
    lints: dict[str, object] = field(default_factory=dict)
    features: dict[str, object] = field(default_factory=dict)
    manifest_error: str = ""

    @property
    def roots(self) -> list[Path]:
        return ([self.lib_root] if self.lib_root else []) + list(self.bin_roots)

    def dependency_names(self) -> set[str]:
        return set(self.dependencies) | set(self.dev_dependencies) | set(self.build_dependencies)


@dataclass
class Module:
    """One file in the module graph, and the `mod` declarations that reach it."""

    path: Path
    crate: str
    module_path: str          # `crate::a::b`
    declared_by: Path | None  # the file whose `mod` statement pulled it in


class Project:
    """A parsed tree: every Rust file, plus the crates and module graph over it."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.files: dict[Path, RsFile] = {}
        self.unparseable: dict[Path, str] = {}
        self.crates: list[Crate] = []
        self.modules: dict[Path, Module] = {}
        # A `mod x;` that names no file on disk. (path, line, name, cfg_gated)
        self.missing_modules: list[tuple[Path, int, str, bool]] = []
        self._load()

    # -- construction ------------------------------------------------------- #

    def _load(self) -> None:
        for path in find_rs_files(self.root):
            try:
                self.files[path] = parse_file(path)
            except (RustSyntaxError, OSError, ValueError) as exc:
                self.unparseable[path] = f"{type(exc).__name__}: {exc}"
        self.crates = _discover_crates(self.root)
        self._build_module_graph()

    def _build_module_graph(self) -> None:
        for crate in self.crates:
            for root_file in crate.roots:
                if root_file in self.files:
                    # A crate root owns its directory whatever it is called:
                    # `[lib] path = "source/root.rs"` makes `mod helper;`
                    # resolve to `source/helper.rs`, not `source/root/helper.rs`.
                    self._walk_module(crate, root_file, "crate", None, is_root=True)

    def _walk_module(self, crate: Crate, path: Path, module_path: str,
                     parent: Path | None, is_root: bool = False) -> None:
        if path in self.modules:
            return
        self.modules[path] = Module(path, crate.name, module_path, parent)
        rsfile = self.files.get(path)
        if rsfile is None:
            return
        for declaration in rsfile.mods:
            if declaration.inline:
                continue
            # An `mod bar;` nested inside `mod foo { … }` resolves under
            # `foo/`, not beside this file. Without the chain, a valid
            # `src/foo/bar.rs` is reported both as a missing module and as
            # never compiled — two high-severity findings for correct code.
            chain = _inline_chain(rsfile, declaration)
            child = _resolve_mod_file(path, declaration.name, rsfile, chain, is_root)
            if child is None:
                # A declaration behind a `cfg` may be disabled in the
                # configuration being built, where rustc never looks for the
                # file at all — so this is a lead, not a proven build failure.
                gated = any(a.replace(" ", "").startswith("cfg(") for a in declaration.attrs)
                self.missing_modules.append(
                    (path, declaration.line, declaration.name, gated))
                continue
            if child in self.files:
                nested = "".join(f"::{name}" for name in chain)
                self._walk_module(crate, child,
                                  f"{module_path}{nested}::{declaration.name}", path)

    # -- queries ------------------------------------------------------------ #

    def crate_for(self, path: Path) -> Crate | None:
        """The innermost crate whose directory contains ``path``."""
        best = None
        for crate in self.crates:
            if crate.is_virtual_manifest:
                continue
            try:
                path.relative_to(crate.root_dir)
            except ValueError:
                continue
            if best is None or len(crate.root_dir.parts) > len(best.root_dir.parts):
                best = crate
        return best

    def compiled_files(self) -> set[Path]:
        """Files rustc actually reaches, plus the ones Cargo compiles by layout."""
        reached = set(self.modules)
        for crate in self.crates:
            for directory in ("tests", "benches", "examples"):
                for path in (crate.root_dir / directory).glob("**/*.rs"):
                    if path in self.files:
                        reached.add(path)
            build = crate.root_dir / "build.rs"
            if build in self.files:
                reached.add(build)
            for path in crate.auxiliary_roots:
                if path in self.files:
                    reached.add(path)
        return reached

    def orphan_files(self) -> list[Path]:
        """`src/` files no `mod` declaration reaches — never compiled at all."""
        if not self.crates:
            return []
        reached = self.compiled_files()
        orphans = []
        for path in self.files:
            if path in reached:
                continue
            crate = self.crate_for(path)
            if crate is None:
                continue
            try:
                relative = path.relative_to(crate.root_dir)
            except ValueError:
                continue
            if relative.parts and relative.parts[0] == "src":
                orphans.append(path)
        return sorted(orphans)


def _inline_chain(rsfile: RsFile, declaration) -> list[str]:
    """Names of the inline modules enclosing ``declaration``, outermost first."""
    enclosing = [m for m in rsfile.mods
                 if m.inline and m.body_open < declaration.start < m.body_close]
    enclosing.sort(key=lambda m: m.body_open)
    return [m.name for m in enclosing]


def _resolve_mod_file(parent: Path, name: str, rsfile: RsFile,
                      chain: list[str] | None = None,
                      is_root: bool = False) -> Path | None:
    """Where `mod name;` in ``parent`` points, honouring `#[path = "…"]`."""
    # A crate root or a `mod.rs` owns a directory; any other file owns the
    # directory named after it. Each enclosing inline module adds a level.
    if is_root or parent.name in ("lib.rs", "main.rs", "mod.rs"):
        directory = parent.parent
    else:
        directory = parent.parent / parent.stem
    for segment in chain or []:
        directory = directory / segment

    # `#[path]` is relative to the *module's* directory, so the inline chain
    # applies to it too — resolving it from the file's own directory reported a
    # valid declaration as missing and its file as never compiled.
    for declaration in rsfile.mods:
        if declaration.name != name:
            continue
        for attribute in declaration.attrs:
            stripped = attribute.replace(" ", "")
            if stripped.startswith("path="):
                target = stripped[len("path="):].strip('"')
                candidate = (directory / target).resolve()
                return candidate if candidate.is_file() else None
    for candidate in (directory / f"{name}.rs", directory / name / "mod.rs"):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _discover_crates(root: Path) -> list[Crate]:
    crates: list[Crate] = []
    manifests = [root / "Cargo.toml"] if (root / "Cargo.toml").is_file() else []
    for candidate in sorted(root.rglob("Cargo.toml")):
        if candidate in manifests:
            continue
        if EXCLUDE_DIRS.isdisjoint(candidate.relative_to(root).parts):
            manifests.append(candidate)
    for manifest in manifests:
        crates.append(_read_manifest(manifest))
    return crates


def _read_manifest(manifest: Path) -> Crate:
    directory = manifest.parent
    crate = Crate(name=directory.name, manifest=manifest, root_dir=directory)
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        crate.manifest_error = f"{type(exc).__name__}: {exc}"
        return crate

    package = data.get("package") or {}
    workspace = data.get("workspace")
    crate.is_workspace_root = workspace is not None
    crate.is_virtual_manifest = workspace is not None and not package
    if isinstance(package, dict):
        crate.package = package
        crate.name = str(package.get("name") or directory.name)
        crate.edition = _plain(package.get("edition"))
        crate.rust_version = _plain(package.get("rust-version"))
    for key, target in (("dependencies", crate.dependencies),
                        ("dev-dependencies", crate.dev_dependencies),
                        ("build-dependencies", crate.build_dependencies)):
        section = data.get(key)
        if isinstance(section, dict):
            target.update(section)
    # Target-specific tables (`[target.'cfg(unix)'.dependencies]`) count too.
    for section in (data.get("target") or {}).values():
        if not isinstance(section, dict):
            continue
        for key, target in (("dependencies", crate.dependencies),
                            ("dev-dependencies", crate.dev_dependencies),
                            ("build-dependencies", crate.build_dependencies)):
            nested = section.get(key)
            if isinstance(nested, dict):
                target.update(nested)
    crate.lints = data.get("lints") or {}
    crate.features = data.get("features") or {}

    lib = data.get("lib") or {}
    lib_path = lib.get("path") if isinstance(lib, dict) else None
    candidate = directory / (lib_path or "src/lib.rs")
    if candidate.is_file():
        crate.lib_root = candidate.resolve()
    for entry in data.get("bin") or []:
        if isinstance(entry, dict) and entry.get("path"):
            candidate = directory / str(entry["path"])
            if candidate.is_file():
                crate.bin_roots.append(candidate.resolve())
    # Cargo compiles a `[[test]] path = "qa/check.rs"` target wherever it lives.
    # Discovering test targets by layout alone missed those, and a crate whose
    # tests all sit at a declared path drew the `no_tests_at_all` blocker.
    for section in ("test", "bench", "example"):
        for entry in data.get(section) or []:
            if isinstance(entry, dict) and entry.get("path"):
                candidate = directory / str(entry["path"])
                if candidate.is_file():
                    crate.auxiliary_roots.append(candidate.resolve())
    # `autobins = false` turns off exactly this discovery, leaving only the
    # explicit `[[bin]]` entries above. Adding the implicit roots anyway would
    # treat a deliberately-excluded `src/main.rs` as compiled and hide the
    # orphan findings for everything it declares.
    if package.get("autobins", True) is not False:
        default_main = directory / "src" / "main.rs"
        if default_main.is_file() and default_main.resolve() not in crate.bin_roots:
            crate.bin_roots.append(default_main.resolve())
        # Cargo auto-discovers both `src/bin/name.rs` and `src/bin/name/main.rs`.
        # Missing the directory form makes every module under it look uncompiled.
        for candidate in sorted((directory / "src" / "bin").glob("*.rs")):
            crate.bin_roots.append(candidate.resolve())
        for candidate in sorted((directory / "src" / "bin").glob("*/main.rs")):
            crate.bin_roots.append(candidate.resolve())
    return crate


def _plain(value) -> str:
    """A manifest value that may be inherited from the workspace (`{workspace = true}`)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("workspace"):
        return "workspace"
    return ""


class _ProjectCache:
    """The last project built, held only until a different root is asked for.

    Every tree detector calls load_project for itself. Running them in one
    process without this means one full parse of the tree per detector — the
    very thing the runner exists to stop doing.

    One entry: a run asks about one project, and holding a second tree costs
    what the first one cost.
    """

    def __init__(self) -> None:
        self._key: Path | None = None
        self._project: "Project | None" = None

    def get(self, key: Path) -> "Project | None":
        return self._project if key == self._key else None

    def put(self, key: Path, project: "Project") -> None:
        self._key, self._project = key, project


_PROJECTS = _ProjectCache()


def load_project(root: Path) -> Project:
    """Parse the tree once per root. The result is shared — do not mutate it."""
    key = root.resolve()
    cached = _PROJECTS.get(key)
    if cached is not None:
        return cached
    project = Project(key)
    _PROJECTS.put(key, project)
    return project
