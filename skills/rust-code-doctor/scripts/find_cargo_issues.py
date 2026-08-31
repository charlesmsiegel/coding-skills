#!/usr/bin/env python3
"""
Audit Cargo.toml: edition, lint configuration, and what the manifest claims
about dependencies versus what the source actually imports.

Read this first. The edition decides which of the other detectors' findings
were ever going to be caught by the compiler, and whether the `[lints]` table
exists decides whether clippy is a ratchet or a suggestion. A crate on the 2015
edition has a different set of real problems than its detector output suggests.

The reconciliation half is the one that finds live bugs: a crate imported but
not declared compiles today only because something else in the workspace pulled
it in, and a declared crate nothing imports is build time and audit surface
paid for nothing.
"""

import contextlib
import re
from pathlib import Path

from common import Finding, find_rs_files, run_tree_detector
from rsparse import RustSyntaxError, parse_file
from rsproject import Crate, load_project

# Editions and what each one costs a reader who learned Rust later.
_EDITION_NOTES = {
    "": ("2015 (the default when `edition` is absent)",
         "`extern crate` is required, `dyn` is optional, module paths differ, and `async`/`await` "
         "are not keywords", "medium"),
    "2015": ("2015", "`extern crate` is required and module paths differ from every tutorial "
                     "written since 2018", "medium"),
    "2018": ("2018", "you are missing disjoint closure captures, `IntoIterator` for arrays, and "
                     "the 2021 panic macro consistency", "low"),
    "2021": ("2021", "", ""),
    "2024": ("2024", "", ""),
}

# Crates that are always in scope and are never Cargo dependencies.
_IMPLICIT_CRATES = frozenset({"std", "core", "alloc", "crate", "self", "super", "proc_macro"})

# A crate identifier is snake_case; `Status::New` and `Self::default` are not
# crate paths, and filtering on case is what keeps the path scan usable inside a
# crate rather than reporting one "undeclared dependency" per enum.
_CRATE_IDENT = re.compile(r"[a-z][a-z0-9_]*")


def _crate_ident(name: str) -> str:
    """The identifier a dependency is imported under: hyphens become underscores."""
    return name.replace("-", "_")


def _imported_crates(paths) -> tuple[set[str], set[str]]:
    """(external crate roots, local module names) across every file given.

    Only the first segment of a `use` path counts as a crate reference. A
    token-level scan for `name::` looks tempting and is unusable: inside a crate
    it matches every local module, every enum used as `Status::New`, and every
    associated function, so a workspace produces one "undeclared dependency"
    per module.
    """
    found: set[str] = set()
    local: set[str] = set()
    for path in paths:
        try:
            rsfile = parse_file(path)
        except (RustSyntaxError, OSError):
            continue
        local.update(m.name for m in rsfile.mods)
        local.update(f.name for f in rsfile.functions if f.name)
        # `use std::sync::atomic;` puts `atomic` in scope, so `atomic::Ordering`
        # later in the file is not a crate reference.
        for statement in rsfile.uses:
            local.update(statement.names)
        for index, token in enumerate(rsfile.tokens):
            if token.is_name("extern") and rsfile.tok(index + 1) is not None \
                    and rsfile.tokens[index + 1].is_name("crate"):
                found.add(rsfile.value(index + 2))
            if token.kind != "name" or not _CRATE_IDENT.fullmatch(token.value):
                continue
            following, previous = rsfile.tok(index + 1), rsfile.tok(index - 1)
            if following is None or not following.is_op("::"):
                continue
            if previous is not None and previous.is_op("::", ".", "!"):
                continue  # a middle segment, or a method on the left of the path
            if rsfile.value(index + 2) == "<":
                continue  # `parse::<u32>()` is a turbofish, not a crate path
            found.add(token.value)
        for use in rsfile.uses:
            head = use.path.split("::", 1)[0].strip().lstrip("{").strip()
            if head and head not in _IMPLICIT_CRATES:
                found.add(head)
    return found - local, local


def _finding(path, line, smell, description, suggestion, severity, related=None):
    return Finding(file=str(path), line=line, smell_type=smell, description=description,
                   suggestion=suggestion, severity=severity, related_lines=related or [])


def _manifest_line(manifest: Path, needle: str) -> int:
    # An unreadable manifest still deserves a finding, anchored at line 1.
    with contextlib.suppress(OSError):
        for number, text in enumerate(manifest.read_text(encoding="utf-8",
                                                         errors="replace").splitlines(), 1):
            if needle in text:
                return number
    return 1


def _check_edition(crate: Crate, findings: list) -> None:
    note = _EDITION_NOTES.get(crate.edition)
    if note is None or not note[1]:
        return
    label, cost, severity = note
    findings.append(_finding(
        crate.manifest, _manifest_line(crate.manifest, "edition") if crate.edition else 1,
        "old_edition",
        f"`{crate.name}` is on edition {label}",
        f"Run `cargo fix --edition` and bump it — {cost}. The tool does the mechanical part; the "
        "diff it produces is what needs reviewing.", severity))


def _check_lint_config(crate: Crate, findings: list) -> None:
    if crate.lints or crate.is_virtual_manifest:
        return
    findings.append(_finding(
        crate.manifest, 1, "no_lint_configuration",
        f"`{crate.name}` has no `[lints]` table",
        "Without it, clippy is whatever each developer runs locally and CI is whatever the "
        "workflow remembered. Declare the level once in the manifest "
        "(`[lints.clippy] unwrap_used = \"warn\"`) so `cargo clippy` and the IDE agree — that is "
        "what turns a cleared class of finding into one that stays cleared.", "low"))


def _check_dependency_specs(crate: Crate, root: Path, findings: list) -> None:
    for section, table in (("dependencies", crate.dependencies),
                           ("dev-dependencies", crate.dev_dependencies),
                           ("build-dependencies", crate.build_dependencies)):
        for name, spec in table.items():
            line = _manifest_line(crate.manifest, name)
            if isinstance(spec, str):
                if spec.strip() in ("*", ">=0", ""):
                    findings.append(_finding(
                        crate.manifest, line, "wildcard_dependency",
                        f"`{name} = \"{spec}\"` accepts any version, including the next breaking one",
                        "Pin a caret range (`\"1.2\"`). crates.io refuses to publish a crate with "
                        "a wildcard dependency, so this also blocks release.", "high"))
                continue
            if not isinstance(spec, dict):
                continue
            if spec.get("git") and not (spec.get("rev") or spec.get("tag")):
                findings.append(_finding(
                    crate.manifest, line, "unpinned_git_dependency",
                    f"`{name}` comes from git with no `rev` or `tag`",
                    "Pin a `rev`. A branch dependency means the build is reproducible only until "
                    "someone pushes, and `Cargo.lock` records a commit that nothing else "
                    "documents.", "medium"))
            if spec.get("path") and ".." in str(spec["path"]) \
                    and not crate.is_workspace_root and not _in_workspace(crate, root):
                findings.append(_finding(
                    crate.manifest, line, "path_dependency_outside_crate",
                    f"`{name}` is a path dependency pointing outside the crate directory",
                    "Fine inside a workspace. Outside one it makes the crate unbuildable for "
                    "anybody who does not have the sibling checkout — and unpublishable without "
                    "a version alongside the path.", "low"))

    overlap = set(crate.dependencies) & set(crate.dev_dependencies)
    for name in sorted(overlap):
        findings.append(_finding(
            crate.manifest, _manifest_line(crate.manifest, name), "duplicated_dev_dependency",
            f"`{name}` is in both `[dependencies]` and `[dev-dependencies]`",
            "The dev entry is redundant — a normal dependency is already available to tests. "
            "Keep it only when the two need different features.", "low"))


def _check_publish_metadata(crate: Crate, findings: list) -> None:
    if crate.is_virtual_manifest or crate.package.get("publish") is False:
        return
    if not crate.lib_root:
        return  # a binary is not published to crates.io by default
    missing = [key for key in ("description", "license", "repository")
               if not crate.package.get(key) and not isinstance(crate.package.get(key), dict)]
    if not missing:
        return
    findings.append(_finding(
        crate.manifest, 1, "incomplete_publish_metadata",
        f"`{crate.name}` is a library missing {', '.join(missing)} in `[package]`",
        "crates.io rejects a publish without `description` and `license`. Filling them in now "
        "costs one line each; discovering it at release time costs a version bump.", "low"))
    if not crate.rust_version:
        findings.append(_finding(
            crate.manifest, 1, "no_msrv_declared",
            f"`{crate.name}` declares no `rust-version`",
            "Without an MSRV, a dependency bump can silently raise the compiler your users need "
            "and the first report is a build failure. Declare it and let "
            "`cargo +<msrv> check` keep it honest.", "low"))


def _crate_sources(crate: Crate) -> list[Path]:
    """The files this crate's lib and bin targets compile.

    Not everything under `crate.root_dir`: a manifest that is both a workspace
    root and a package (ripgrep's own layout) would otherwise claim every member
    crate's source as its own, and every member's dependency as undeclared.
    """
    directories = {root.parent for root in crate.roots} or {crate.root_dir / "src"}
    return _collect(directories)


def _auxiliary_sources(crate: Crate) -> list[Path]:
    """`build.rs`, `tests/`, `benches/`, `examples/` — the other Cargo targets.

    A dev-dependency is used in `tests/`, and a build-dependency in `build.rs`;
    neither appears under `src/`, so reconciling them against the lib sources
    alone reports every one of them as unused.
    """
    directories = {crate.root_dir / name for name in ("tests", "benches", "examples")}
    paths = _collect(directories)
    build = crate.root_dir / "build.rs"
    if build.is_file():
        paths.append(build)
    return paths


def _collect(directories) -> list[Path]:
    seen: dict[Path, None] = {}
    for directory in sorted(directories):
        for path in find_rs_files(directory):
            seen.setdefault(path, None)
    return list(seen)


def _check_reconciliation(crate: Crate, root: Path, findings: list) -> None:
    sources = _crate_sources(crate)
    if not sources:
        return
    used, local_modules = _imported_crates(sources)
    # A dev- or build-dependency lives in a target that is not `src/`, so the
    # search for those has to cover `tests/`, `benches/`, `examples/` and
    # `build.rs` as well.
    auxiliary, auxiliary_local = _imported_crates(_auxiliary_sources(crate))
    everywhere = used | auxiliary
    local_modules |= auxiliary_local
    declared = {_crate_ident(name): name for name in crate.dependency_names()}

    for ident, name in sorted(declared.items()):
        if ident in everywhere or name in everywhere:
            continue
        # A macro-only or trait-only crate may never be named at a path root.
        findings.append(_finding(
            crate.manifest, _manifest_line(crate.manifest, name), "unused_dependency",
            f"`{name}` is declared but no source file in `{crate.name}` names it",
            "Remove it, or say why it is here (a proc-macro re-exported by another crate, a "
            "feature-gated import, a linked C library). Every dependency is compile time, an "
            "advisory surface and a supply-chain edge. `cargo +nightly udeps` confirms this from "
            "the build graph rather than from syntax.", "low"))

    known_local = {_crate_ident(crate.name)} | _IMPLICIT_CRATES | local_modules
    for ident in sorted(used):
        if ident in declared or ident in known_local:
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]*", ident) or len(ident) < 3:
            continue
        if any(_crate_ident(other.name) == ident for other in _sibling_crates(root)):
            continue
        findings.append(_finding(
            crate.manifest, 1, "undeclared_dependency_candidate",
            f"source in `{crate.name}` refers to `{ident}::…` but no dependency declares it",
            f"Either `{ident}` is a module of this crate reached through a `use` this scan could "
            "not follow (harmless), or it is a real dependency resolving through a workspace "
            "member — which works until this crate is built alone. Confirm with "
            "`cargo build -p " + crate.name + "`.", "low"))


def _sibling_crates(root: Path):
    return load_project(root).crates


def _in_workspace(crate: Crate, root: Path) -> bool:
    """True when some manifest above this crate declares a `[workspace]`."""
    return any(other.is_workspace_root and other.root_dir in crate.root_dir.parents
               for other in _sibling_crates(root))


def analyze(root: Path, ignore: set[str], args) -> list:
    project = load_project(root)
    findings: list[Finding] = []

    if not project.crates:
        findings.append(_finding(
            root, 1, "no_cargo_manifest",
            "No Cargo.toml under this path",
            "Nothing here is a Cargo project, so the manifest-level checks (edition, lints, "
            "dependency reconciliation) have nothing to read and the per-file detectors are all "
            "you get from this run.", "medium"))
        return [f for f in findings if f.smell_type not in ignore]

    for crate in project.crates:
        if crate.manifest_error:
            findings.append(_finding(
                crate.manifest, 1, "unparseable_manifest",
                f"Cargo.toml could not be parsed ({crate.manifest_error})",
                "Fix the TOML. Until then `cargo` cannot read it either, and every manifest-level "
                "finding for this crate is missing rather than clean.", "high"))
            continue
        if crate.is_virtual_manifest:
            continue
        _check_edition(crate, findings)
        _check_lint_config(crate, findings)
        _check_dependency_specs(crate, root, findings)
        _check_publish_metadata(crate, findings)
        _check_reconciliation(crate, root, findings)

    return [f for f in findings if f.smell_type not in ignore]


if __name__ == "__main__":
    run_tree_detector(
        "Audit Cargo.toml and reconcile it against what the source imports",
        "No Cargo manifest problems found!",
        analyze,
    )
