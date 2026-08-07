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

from common import EXCLUDE_DIRS, Finding, is_test_file, run_tree_detector
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
    found = [p for p in root.rglob("package.json")
             if EXCLUDE_DIRS.isdisjoint(p.relative_to(root).parts)]
    found.sort(key=lambda p: len(p.relative_to(root).parts))
    return found[:5]


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
        if project.files:
            add(root / "package.json", 1, "no_manifest",
                "TypeScript sources but no package.json under this path",
                "Add one so the dependency set is declared rather than inherited from whatever "
                "happens to be installed.", "medium")
        return findings

    manifest = manifests[0]
    try:
        package = json.loads(manifest.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, json.JSONDecodeError) as exc:
        add(manifest, 1, "unparseable_manifest",
            f"package.json could not be parsed ({exc}), so dependencies were not reconciled",
            "Fix the JSON.", "medium")
        return findings

    runtime = dict(package.get("dependencies") or {})
    dev = dict(package.get("devDependencies") or {})
    peer = dict(package.get("peerDependencies") or {})
    optional = dict(package.get("optionalDependencies") or {})
    declared = {**runtime, **dev, **peer, **optional}

    # package -> where it is imported from
    used_in_source: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    used_in_tests: dict[str, list[tuple[Path, int]]] = defaultdict(list)
    for path, tsfile in project.files.items():
        bucket = used_in_tests if is_test_file(path) else used_in_source
        for record in tsfile.imports:
            specifier = record.module
            if not specifier or specifier.startswith(".") or specifier.startswith("/"):
                continue
            if project.resolve(path, specifier) is not None:
                continue  # a tsconfig path alias, not a package
            name = _package_of(specifier.removeprefix("node:"))
            if name in NODE_BUILTINS:
                continue
            bucket[name].append((path, record.line))

    _report_missing(add, manifest, declared, used_in_source, used_in_tests)
    _report_unused(add, manifest, runtime, dev, used_in_source, used_in_tests)
    _report_misplaced(add, manifest, runtime, used_in_source, used_in_tests)
    _report_versions(add, manifest, runtime, dev)
    _report_lockfiles(add, manifest)
    return findings


def _line_in_manifest(manifest: Path, name: str) -> int:
    # Falling back to line 1 is fine: the finding is about the declaration,
    # not about where in the file it happens to sit.
    with contextlib.suppress(OSError):
        for number, line in enumerate(manifest.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
            if f'"{name}"' in line:
                return number
    return 1


def _report_missing(add, manifest, declared, used_in_source, used_in_tests) -> None:
    for name, sites in sorted({**used_in_source, **used_in_tests}.items()):
        if name in declared:
            continue
        path, line = sites[0]
        add(path, line, "missing_dependency",
            f"`{name}` is imported but declared in no dependency field",
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
