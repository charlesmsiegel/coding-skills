#!/usr/bin/env python3
"""
Reconcile package.json with what the code actually imports.

Three failures live here and each is invisible in review: a package imported but
never declared (works locally because a transitive dependency happens to hoist
it, breaks on a clean install), a package declared but never used (installed,
audited and shipped for nothing), and a runtime dependency that only test files
import.

This reads manifests, not the registry. Known advisories against the versions
you have pinned are `npm audit`'s job — run_external_tools.py drives it.
"""

import contextlib
import json
from collections import defaultdict
from pathlib import Path

from common import Finding, is_test_file, run_tree_detector, walk_tree
from tsproject import load_project

# Node's own modules, which are never dependencies.
NODE_BUILTINS = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "string_decoder", "sys", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
})

LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock")

# Declared but rarely imported by name — a false "unused" would be worse than
# the silence, because these are load-bearing.
IMPLICITLY_USED = frozenset({
    "typescript", "vite", "webpack", "rollup", "esbuild", "eslint", "prettier",
    "jest", "vitest", "ts-node", "tsx", "nodemon", "husky", "lint-staged",
    "postcss", "tailwindcss", "autoprefixer", "sass", "less",
})


def _package_of(specifier: str) -> str:
    """The installable package name a bare specifier belongs to."""
    if specifier.startswith("@"):
        return "/".join(specifier.split("/")[:2])
    return specifier.split("/")[0]


def _manifests(root: Path) -> list[Path]:
    if root.is_file():
        root = root.parent
    found = [p for p in walk_tree(root) if p.name == "package.json"]
    found.sort(key=lambda p: (len(p.relative_to(root).parts), str(p)))
    return found


def analyze(root: Path, ignore: set[str], _args) -> list[Finding]:
    root = root.resolve()
    findings: list[Finding] = []

    def add(path, line, smell, description, suggestion, severity):
        if smell not in ignore:
            findings.append(Finding(file=str(path), line=line, smell_type=smell,
                                    description=description, suggestion=suggestion, severity=severity))

    manifests = _manifests(root)
    project = load_project(root)
    if not manifests:
        if project.analyzable:
            add(root / "package.json", 1, "no_manifest",
                "TypeScript sources but no package.json under this path",
                "Add one so the dependency set is declared rather than inherited from whatever "
                "happens to be installed.", "medium")
        return findings

    # Every manifest under the tree gets its own dependencies reconciled — a
    # monorepo's second, third, or sixth package.json is not shadowed by its
    # first, the way a single-manifest reading would silently shadow it.
    packages: list[tuple[Path, dict]] = []
    for manifest in manifests:
        try:
            package = json.loads(manifest.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            add(manifest, 1, "unparseable_manifest",
                f"package.json could not be parsed ({exc}), so dependencies were not reconciled",
                "Fix the JSON.", "medium")
            continue
        packages.append((manifest, package))
    parsed = packages
    packages = _without_fixtures(parsed)
    if not packages:
        return findings
    kept = {manifest for manifest, _ in packages}
    fixture_dirs = [manifest.parent for manifest, _ in parsed if manifest not in kept]

    # A source file's declared set is its NEAREST manifest plus every ANCESTOR
    # manifest above it. A root's declarations really are hoisted down to every
    # workspace; a sibling workspace's are not installed here at all. Unioning
    # the whole tree made b's declaration cover a's import, which hid the clean-
    # install break in a *and* kept b's orphaned declaration looking used.
    manifest_dirs = [(manifest, manifest.parent) for manifest, _ in packages]

    def _chain_of(path: Path) -> list[Path]:
        ancestors = set(path.parents)
        return [manifest for manifest, directory in manifest_dirs if directory in ancestors]

    # (manifest, package) sites, so "missing" is answered per workspace. A site
    # is (file, line, via): `via` names the generated importer when the site had
    # to be moved onto the manifest because no editable file imports the package.
    missing_sites: dict[Path, dict[str, tuple[Path, int, Path | None]]] = defaultdict(dict)
    # manifest -> what its own files (and its workspaces' files) import
    source_by_manifest: dict[Path, set[str]] = defaultdict(set)
    tests_by_manifest: dict[Path, set[str]] = defaultdict(set)
    # Usage is read from every file, generated ones included — a runtime package
    # imported only by a generated API client is genuinely used, and one it
    # imports that nothing declares is genuinely missing (a clean install cannot
    # resolve it). A finding is never *located* in a generated file, though: a
    # missing package seen only from one is reported against the manifest that
    # should declare it, with the generated importer named as the evidence, and
    # an editable importer takes the site back whenever there is one.
    owned_by_a_tool = set(project.generated)
    for path, tsfile in project.files.items():
        if _below(path, fixture_dirs):
            continue  # a fixture project's own file: not this project's usage
        is_test = is_test_file(path)
        chain = _chain_of(path)
        for record in tsfile.imports:
            specifier = record.module
            if not specifier or specifier.startswith(".") or specifier.startswith("/"):
                continue
            if project.resolve(path, specifier) is not None:
                continue  # a tsconfig path alias, not a package
            name = _package_of(specifier.removeprefix("node:"))
            if name in NODE_BUILTINS:
                continue
            for manifest in chain:
                (tests_by_manifest if is_test else source_by_manifest)[manifest].add(name)
            nearest = chain[-1] if chain else packages[0][0]
            site = (nearest, 1, path) if path in owned_by_a_tool else (path, record.line, None)
            existing = missing_sites[nearest].get(name)
            if existing is None or (existing[2] is not None and site[2] is None):
                missing_sites[nearest][name] = site

    declared_by_manifest = {
        manifest: _declared(package) for manifest, package in packages
    }
    for nearest, sites in missing_sites.items():
        visible: dict[str, str] = {}
        for manifest in _chain_of(nearest):  # the ancestors of the manifest's own directory
            visible.update(declared_by_manifest.get(manifest) or {})
        visible.update(declared_by_manifest.get(nearest) or {})
        _report_missing(add, visible, sites)

    for manifest, package in packages:
        runtime = dict(package.get("dependencies") or {})
        dev = dict(package.get("devDependencies") or {})
        _report_unused(add, manifest, runtime, dev,
                       source_by_manifest[manifest], tests_by_manifest[manifest])
        _report_misplaced(add, manifest, runtime,
                          source_by_manifest[manifest], tests_by_manifest[manifest])
        _report_versions(add, manifest, runtime, dev)

    # Lockfiles are a workspace-wide concern, not a per-package one: npm/yarn/
    # pnpm workspaces keep a single lockfile at the root by design, so
    # checking every sub-package's directory would flag each one as missing
    # a lockfile it was never meant to have.
    _report_lockfiles(add, packages[0][0])
    return findings


def _without_fixtures(packages: list[tuple[Path, dict]]) -> list[tuple[Path, dict]]:
    """Drop the manifests that belong to fixture projects.

    A self-contained app committed under `tests/fixtures/` so a test can point
    a tool at it carries its own `package.json`, and its dependencies are
    synthetic by design — a deliberately unpinned or unused one is often the
    point of the fixture. Reconciling it as a workspace reported those against
    the fixture manifest, and reconciling its files against the root manifest
    reported its imports as missing there.

    A manifest below a test directory is therefore a fixture, unless an
    ancestor manifest *declares* that directory as a workspace: `apps/e2e` in
    a monorepo is a real package whose dependencies matter, and its name is
    not a reason to skip it. Everything below a fixture manifest goes with
    it, nested workspaces of the fixture included.
    """
    declared: set[Path] = set()
    for manifest, package in packages:
        declared.update(_declared_workspaces(manifest, package))
    fixture_dirs = [manifest.parent for manifest, _ in packages
                    if is_test_file(manifest) and manifest.parent not in declared]
    return [(manifest, package) for manifest, package in packages
            if not _below(manifest, fixture_dirs)]


def _below(path: Path, directories: list[Path]) -> bool:
    return any(directory == path.parent or directory in path.parents for directory in directories)


def _declared_workspaces(manifest: Path, package: dict) -> set[Path]:
    """The directories a manifest's `workspaces` globs (or a `pnpm-workspace.yaml`
    beside it) name. Negated patterns are ignored: they only ever narrow."""
    patterns = package.get("workspaces") or []
    if isinstance(patterns, dict):          # yarn's {"packages": [...], "nohoist": [...]}
        patterns = patterns.get("packages") or []
    if isinstance(patterns, str):           # a bare string is a single pattern, not its characters
        patterns = [patterns]
    patterns = [p for p in patterns if isinstance(p, str)]
    pnpm = manifest.parent / "pnpm-workspace.yaml"
    if pnpm.is_file():
        patterns.extend(_pnpm_workspace_patterns(pnpm))
    found: set[Path] = set()
    for pattern in patterns:
        # Negations only narrow; an absolute pattern is not a workspace glob
        # (and pathlib raises NotImplementedError on it, not ValueError).
        if pattern.startswith(("!", "/")) or pattern.startswith("\\"):
            continue
        with contextlib.suppress(OSError, ValueError, NotImplementedError):
            found.update(match.resolve() for match in manifest.parent.glob(pattern.rstrip("/"))
                         if match.is_dir())
    return found


def _pnpm_workspace_patterns(path: Path) -> list[str]:
    """The `packages:` list of a pnpm-workspace.yaml — the one list in that file
    naming directories — read without a YAML parser, which the stdlib lacks."""
    patterns: list[str] = []
    key = None
    with contextlib.suppress(OSError):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line and not line[0].isspace() and line.rstrip().endswith(":"):
                key = line.rstrip()[:-1].strip()
                continue
            stripped = line.strip()
            if key == "packages" and stripped.startswith("- "):
                patterns.append(stripped[2:].strip().strip("'\""))
    return patterns


def _line_in_manifest(manifest: Path, name: str) -> int:
    # Falling back to line 1 is fine: the finding is about the declaration,
    # not about where in the file it happens to sit.
    with contextlib.suppress(OSError):
        for number, line in enumerate(manifest.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
            if f'"{name}"' in line:
                return number
    return 1


def _declared(package: dict) -> dict:
    """Every dependency field of one manifest, flattened."""
    declared: dict = {}
    for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        declared.update(package.get(field) or {})
    return declared


def _report_missing(add, declared, sites) -> None:
    for name, (path, line, via) in sorted(sites.items()):
        if name in declared:
            continue
        importer = f" (by {via.name}, a generated file)" if via is not None else ""
        add(path, line, "missing_dependency",
            f"`{name}` is imported{importer} but declared in no dependency field",
            "Add it to package.json. It resolves today only because something else installed it; "
            "a clean install, a different package manager, or a dependency bump removes it without "
            "warning.", "high")


def _report_unused(add, manifest, runtime, dev, used_in_source, used_in_tests) -> None:
    used = set(used_in_source) | set(used_in_tests)
    for name in sorted({**runtime, **dev}):
        if name in used or name in IMPLICITLY_USED:
            continue
        if name.startswith("@types/"):
            continue  # consumed by the compiler, never imported by name
        if name.startswith(("eslint-", "@eslint/", "eslint@", "babel-", "@babel/", "vite-plugin-",
                            "rollup-plugin-", "postcss-", "@vitejs/", "@testing-library/")):
            continue  # configured by name in a config file, not imported
        add(manifest, _line_in_manifest(manifest, name), "unused_dependency",
            f"`{name}` is declared but imported by no TypeScript file in this tree",
            "Remove it, or note why it is needed (a peer requirement, a CLI, a plugin resolved by "
            "name). Every declared package is installed, audited, and part of your supply chain.",
            "low")


def _report_misplaced(add, manifest, runtime, used_in_source, used_in_tests) -> None:
    for name in sorted(runtime):
        if name in used_in_source or name not in used_in_tests:
            continue
        add(manifest, _line_in_manifest(manifest, name), "test_only_runtime_dependency",
            f"`{name}` is a runtime dependency but only test files import it",
            "Move it to devDependencies. It is otherwise installed in production and included in "
            "every vulnerability report about your deployed image.", "low")


def _report_versions(add, manifest, runtime, dev) -> None:
    for name, spec in sorted({**runtime, **dev}.items()):
        if not isinstance(spec, str):
            continue
        if spec.strip() in ("*", "latest", "") or spec.strip().startswith("x"):
            add(manifest, _line_in_manifest(manifest, name), "unpinned_dependency",
                f"`{name}: \"{spec}\"` accepts any published version",
                "Pin a range you have tested (`^1.4.0`). An unbounded range means today's install "
                "and tomorrow's install are different software with the same lockfile-less build.",
                "medium")
        elif name.startswith("@types/") and name in runtime:
            add(manifest, _line_in_manifest(manifest, name), "types_in_runtime_dependencies",
                f"`{name}` is a runtime dependency",
                "Type packages are erased at build time — move them to devDependencies.", "low")


def _report_lockfiles(add, manifest) -> None:
    directory = manifest.parent
    present = [name for name in LOCKFILES if (directory / name).is_file()]
    if not present:
        add(manifest, 1, "no_lockfile",
            "No lockfile beside package.json",
            "Commit one. Without it, CI and every developer resolve versions independently, and "
            "'works on my machine' becomes literally true.", "medium")
    elif len(present) > 1:
        add(manifest, 1, "multiple_lockfiles",
            f"{len(present)} lockfiles present: {', '.join(present)}",
            "Keep the one your package manager writes and delete the rest. Two lockfiles means two "
            "different dependency graphs depending on who ran what.", "medium")


if __name__ == "__main__":
    run_tree_detector(
        "Reconcile package.json with the imports: missing, unused, misplaced and unpinned",
        "Dependencies reconcile with the imports!",
        analyze,
    )
