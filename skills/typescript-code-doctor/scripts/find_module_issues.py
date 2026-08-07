#!/usr/bin/env python3
"""
Find module-graph problems: import cycles, barrel files, god modules, deep
relative paths and side-effect imports.

An import cycle in TypeScript does not fail to compile. It fails at runtime,
intermittently, as `undefined is not a constructor` — and which module wins
depends on which one the bundler happened to reach first. Barrel files
(`index.ts` that re-exports a folder) are the most common way to create one by
accident, and they also defeat tree shaking.
"""

from pathlib import Path

from common import Finding, is_test_file, run_tree_detector
from tsproject import load_project

GOD_MODULE_EXPORTS = 25
DEEP_RELATIVE_SEGMENTS = 3
BARREL_NAMES = frozenset({"index.ts", "index.tsx", "index.mts"})


def _find_cycles(graph: dict[Path, list[Path]]) -> list[list[Path]]:
    """Every elementary cycle reachable by DFS, shortest first, de-duplicated."""
    cycles: list[list[Path]] = []
    seen_signatures: set[frozenset] = set()
    colour: dict[Path, int] = {}
    stack: list[Path] = []

    def visit(node: Path) -> None:
        colour[node] = 1
        stack.append(node)
        for neighbour in graph.get(node, ()):
            state = colour.get(neighbour, 0)
            if state == 1:
                cycle = stack[stack.index(neighbour):]
                signature = frozenset(cycle)
                if signature not in seen_signatures:
                    seen_signatures.add(signature)
                    cycles.append(list(cycle))
            elif state == 0:
                visit(neighbour)
        stack.pop()
        colour[node] = 2

    for node in list(graph):
        if colour.get(node, 0) == 0:
            visit(node)
    cycles.sort(key=len)
    return cycles


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def analyze(root: Path, ignore: set[str], _args) -> list[Finding]:
    project = load_project(root)
    findings: list[Finding] = []

    def add(path, line, smell, description, suggestion, severity, related=None):
        if smell not in ignore:
            findings.append(Finding(file=str(path), line=line, smell_type=smell,
                                    description=description, suggestion=suggestion,
                                    severity=severity, related_lines=related or []))

    graph: dict[Path, list[Path]] = {}
    edge_lines: dict[tuple[Path, Path], int] = {}
    for path in project.files:
        targets = []
        for target, line, _spec in project.internal_imports(path):
            targets.append(target)
            edge_lines.setdefault((path, target), line)
        graph[path] = targets

    for cycle in _find_cycles(graph)[:20]:
        names = [_relative(project.root, p) for p in cycle]
        head = cycle[0]
        add(head, edge_lines.get((cycle[-1], head), 1), "import_cycle",
            f"Import cycle across {len(cycle)} module(s): {' → '.join(names)} → {names[0]}",
            "Break it by moving the shared piece into a module both sides import, or by inverting "
            "one dependency. A cycle compiles and then fails at runtime with a partially-"
            "initialised module, and which side loses depends on the bundler's entry order.",
            "high" if len(cycle) <= 3 else "medium")

    for path, tsfile in project.files.items():
        relative_name = _relative(project.root, path)
        if is_test_file(path):
            continue

        exports = [e for e in tsfile.exports if not e.is_type_only]
        if len(exports) > GOD_MODULE_EXPORTS:
            add(path, 1, "god_module",
                f"{relative_name} exports {len(exports)} symbols",
                "Split it by what changes together. A module this wide is imported by everything, "
                "so every edit to it rebuilds and retests everything.", "medium")

        if path.name in BARREL_NAMES:
            re_exports = [e for e in tsfile.exports if e.kind in ("star", "named")]
            own_code = [f for f in tsfile.functions if f.has_body] + tsfile.classes
            if len(re_exports) >= 3 and not own_code:
                star = sum(1 for e in tsfile.exports if e.kind == "star")
                add(path, 1, "barrel_file",
                    f"{relative_name} is a barrel re-exporting {len(re_exports)} symbol(s)"
                    + (f", {star} of them with `export *`" if star else ""),
                    "Import from the defining module instead. A barrel makes every importer load "
                    "the whole folder, is the usual way an import cycle appears by accident, and "
                    "`export *` hides which module actually owns a name.",
                    "medium" if star else "low")

        for record in tsfile.imports:
            if record.module.count("../") >= DEEP_RELATIVE_SEGMENTS:
                add(path, record.line, "deep_relative_import",
                    f"`{record.module}` climbs {record.module.count('../')} directories",
                    "Add a tsconfig `paths` alias (`@app/*`) and import by name. A path this deep "
                    "breaks whenever either end moves, and says nothing about what is being "
                    "imported.", "low")
            # Only internal modules: importing a polyfill, a stylesheet or
            # `@testing-library/jest-dom` for its side effects is that package's
            # documented interface, not a design choice this repo made.
            if record.side_effect_only and record.module.startswith(".") and not record.module.endswith(
                    (".css", ".scss", ".sass", ".less", ".json", ".svg", ".png")):
                add(path, record.line, "side_effect_import",
                    f"`import '{record.module}'` is imported only for its side effects",
                    "Export a function and call it. A side-effect import runs at an order the "
                    "bundler chooses, cannot be tree-shaken, and disappears if someone 'cleans up "
                    "unused imports'.", "medium")

        _check_type_imports(tsfile, path, add)

    return findings


def _check_type_imports(tsfile, path: Path, add) -> None:
    """Imports used only in type positions should say `import type`."""
    for record in tsfile.imports:
        if record.is_type_only or record.kind != "import" or not record.names:
            continue
        for name in record.names:
            uses = [i for i, token in enumerate(tsfile.tokens)
                    if token.kind == "name" and token.value == name and token.line != record.line]
            if not uses:
                continue
            if all(tsfile.in_type_position(i) for i in uses):
                add(path, record.line, "missing_import_type",
                    f"`{name}` from '{record.module}' is only used as a type",
                    f"Write `import type {{ {name} }} from '{record.module}'`. The runtime import "
                    "otherwise survives into the bundle and can drag a whole module in with it — "
                    "and `verbatimModuleSyntax` requires the annotation anyway.", "low")


if __name__ == "__main__":
    run_tree_detector(
        "Find import cycles, barrel files, god modules and deep relative paths",
        "No module-graph problems found!",
        analyze,
    )
