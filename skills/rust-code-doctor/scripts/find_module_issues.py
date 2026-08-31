#!/usr/bin/env python3
"""
Check the module graph: what rustc actually compiles, and how it is laid out.

The finding that matters most here has no analogue in most languages. A `.rs`
file under `src/` that no `mod` declaration reaches is not dead code — it is
not code at all. rustc never opens it, so it does not type-check, its tests do
not run, and its `unsafe` is never reviewed by the compiler. People discover
this when they fix a bug in a file that was never being compiled.

The rest is layout: a `mod` naming a file that does not exist, the two module
file styles mixed in one crate, god modules, and glob re-exports that make a
name's origin unfindable.
"""

from pathlib import Path

from common import Finding, is_test_file, run_tree_detector
from rsproject import load_project

MAX_MODULE_LINES = 800
MAX_MODULE_ITEMS = 40


def _finding(path, line, smell, description, suggestion, severity, related=None):
    return Finding(file=str(path), line=line, smell_type=smell, description=description,
                   suggestion=suggestion, severity=severity, related_lines=related or [])


def _check_missing_modules(project, findings: list) -> None:
    for path, line, name, gated in project.missing_modules:
        if gated:
            findings.append(_finding(
                path, line, "cfg_gated_module_file_missing",
                f"`mod {name};` is behind a `cfg` and names no `{name}.rs` or `{name}/mod.rs`",
                "Under a configuration where the `cfg` is false this is fine — rustc never looks "
                "for the file. Under one where it is true the crate does not build. Check which "
                "configurations enable it, and whether the file was meant to exist.", "medium"))
            continue
        findings.append(_finding(
            path, line, "module_file_missing",
            f"`mod {name};` names no `{name}.rs` or `{name}/mod.rs` next to this file",
            "rustc rejects this, so the crate does not build. Either the file was moved and the "
            "declaration was not, or it needs a `#[path = \"…\"]`.", "high"))


def _check_orphans(project, findings: list) -> None:
    for path in project.orphan_files():
        if path.name in ("lib.rs", "main.rs", "build.rs"):
            continue
        findings.append(_finding(
            path, 1, "file_never_compiled",
            "no `mod` declaration reaches this file, so rustc never compiles it",
            "Add `mod " + path.stem + ";` to its parent module, or delete the file. Until then "
            "nothing in it is type-checked, its tests do not run, and it will not appear in "
            "coverage — which is how a file people are still editing turns out to have been dead "
            "for months.", "high"))


def _check_module_style(project, findings: list) -> None:
    for crate in project.crates:
        if crate.is_virtual_manifest:
            continue
        mod_rs = sorted(p for p in project.files
                        if p.name == "mod.rs" and _inside(p, crate.root_dir))
        directory_style = sorted(
            p for p in project.files
            if p.name != "mod.rs" and _inside(p, crate.root_dir)
            and (p.parent / p.stem).is_dir())
        if not mod_rs or not directory_style:
            continue
        findings.append(_finding(
            mod_rs[0], 1, "mixed_module_file_style",
            f"`{crate.name}` uses both `foo/mod.rs` and `foo.rs` + `foo/` for its modules "
            f"({len(mod_rs)} and {len(directory_style)} files)",
            "Pick one. The 2018 style (`foo.rs` beside `foo/`) keeps the file name searchable — "
            "a repo with fourteen files called `mod.rs` is one where every editor tab says the "
            "same thing. Mixing them means a reader has to check which convention a given "
            "directory follows.", "low",
            related=[]))


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _check_god_modules(project, findings: list) -> None:
    for path, rsfile in project.files.items():
        if is_test_file(path):
            continue
        items = len(rsfile.functions) + len(rsfile.types) + len(rsfile.traits) + len(rsfile.impls)
        if len(rsfile.lines) > MAX_MODULE_LINES and items > MAX_MODULE_ITEMS:
            findings.append(_finding(
                path, 1, "god_module",
                f"{len(rsfile.lines)} lines and {items} items in one module",
                "Split it along the seams the items already have — the types that are used "
                "together, the functions that touch one struct. A module this size is also a "
                "merge-conflict magnet, because everyone's change lands in the same file.",
                "medium"))


def _check_glob_reexports(project, findings: list) -> None:
    for path, rsfile in project.files.items():
        for use in rsfile.uses:
            if not use.is_glob:
                continue
            compact = "".join(use.path.split())
            if compact.startswith(("super::", "crate::")) and is_test_file(path):
                continue
            index = next((i for i, t in enumerate(rsfile.tokens)
                          if t.line == use.line and t.is_name("use")), 0)
            if compact in ("super::*", "crate::*") or rsfile.in_test_code(index):
                continue  # the conventional import inside a `#[cfg(test)] mod tests`
            if not rsfile.top_level(index):
                continue  # `use self::Kind::*;` inside one function is scoped to it
            severity = "medium" if use.visibility.startswith("pub") else "low"
            findings.append(_finding(
                path, use.line,
                "glob_reexport" if use.visibility.startswith("pub") else "glob_import",
                f"`{use.visibility + ' ' if use.visibility else ''}use {use.path};`",
                "A glob makes a name's origin unfindable: grepping for the definition of a symbol "
                "this pulls in returns nothing at this file. Re-export the names you mean, one "
                "per line — the list is also the crate's public API, which is worth being able "
                "to read.", severity))


def _check_pass_through_modules(project, findings: list) -> None:
    for path, rsfile in project.files.items():
        if is_test_file(path) or path.name in ("lib.rs", "main.rs"):
            continue
        items = len(rsfile.functions) + len(rsfile.types) + len(rsfile.traits) + len(rsfile.impls)
        reexports = [u for u in rsfile.uses if u.visibility.startswith("pub")]
        if items or not reexports or len(rsfile.mods) > 3:
            continue
        if len(rsfile.lines) > 40:
            continue
        findings.append(_finding(
            path, 1, "pass_through_module",
            f"this module defines nothing and only re-exports ({len(reexports)} `pub use`)",
            "A module whose whole content is forwarding is a level of indirection between the "
            "caller and the definition. Keep one at a crate root as the public facade; delete the "
            "internal ones and import from where the item lives.", "low"))


def _check_deep_super_paths(project, findings: list) -> None:
    for path, rsfile in project.files.items():
        for use in rsfile.uses:
            if use.path.count("super::") >= 3:
                findings.append(_finding(
                    path, use.line, "deep_super_path",
                    f"`use {use.path};` walks {use.path.count('super::')} levels up",
                    "`crate::…` is absolute and survives the module being moved; a chain of "
                    "`super` breaks the moment anything is reorganised, and says nothing about "
                    "where the item actually lives.", "low"))


def analyze(root: Path, ignore: set[str], args) -> list:
    project = load_project(root)
    findings: list[Finding] = []
    _check_missing_modules(project, findings)
    _check_orphans(project, findings)
    _check_module_style(project, findings)
    _check_god_modules(project, findings)
    _check_glob_reexports(project, findings)
    _check_pass_through_modules(project, findings)
    _check_deep_super_paths(project, findings)
    return [f for f in findings if f.smell_type not in ignore]


if __name__ == "__main__":
    run_tree_detector(
        "Check the module graph: orphan files, missing modules, layout",
        "No module problems found!",
        analyze,
    )
