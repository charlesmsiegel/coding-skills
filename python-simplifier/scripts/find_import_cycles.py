#!/usr/bin/env python3
"""
Detect import-graph structural problems across a Python project.

Analyzes the whole directory tree and flags:
  - circular_import : a cycle in the intra-project import graph (HIGH)
  - god_module      : a file that is too large or has too many top-level defs (MEDIUM)
  - wildcard_import : a `from x import *` statement (MEDIUM)
  - logic_in_init   : function/class definitions inside __init__.py (LOW)
"""

import ast
import json
import argparse
import contextlib
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Iterator, Dict, List, Set, Optional
from collections import defaultdict


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""


# ---------------------------------------------------------------------------
# Shared helpers (verbatim contract from find_global_state.py)
# ---------------------------------------------------------------------------

def find_python_files(path: Path) -> Iterator[Path]:
    if path.is_file() and path.suffix == ".py":
        yield path
    elif path.is_dir():
        for p in path.rglob("*.py"):
            if ".venv" not in p.parts and "node_modules" not in p.parts and "__pycache__" not in p.parts:
                yield p


def _get_line(lines: List[str], lineno: int) -> str:
    if 0 < lineno <= len(lines):
        return lines[lineno - 1].strip()[:80]
    return ""


# ---------------------------------------------------------------------------
# Module-name mapping
# ---------------------------------------------------------------------------

def _find_package_root(directory: Path) -> Path:
    """Walk UP from directory while __init__.py exists; return the topmost such dir.

    The module name is then derived from the package root's PARENT.  A lone
    file in a directory without __init__.py has no package root (the directory
    itself is the implicit namespace root).
    """
    pkg_root = directory
    while (pkg_root / "__init__.py").exists():
        parent = pkg_root.parent
        if parent == pkg_root:
            # Filesystem root – stop
            break
        pkg_root = parent
    # pkg_root is now the first directory going up that does NOT contain
    # __init__.py, so the actual package root is one level back down.
    # Re-derive: the package boundary is the deepest directory that still has
    # __init__.py.  We overshot by one, so back up.
    actual_pkg_root = directory
    while (actual_pkg_root / "__init__.py").exists():
        parent = actual_pkg_root.parent
        if parent == actual_pkg_root:
            break
        actual_pkg_root = parent
    # actual_pkg_root is the first ancestor WITHOUT __init__.py — it is the
    # parent of the package root.
    return actual_pkg_root


def _build_module_map(root: Path, py_files: List[Path]) -> Dict[Path, str]:
    """Return {absolute_path: dotted_module_name} using package-boundary resolution.

    For each file, we walk UP from its directory while __init__.py is present.
    The topmost directory that still has __init__.py is the package root; its
    parent is used as the base for computing the dotted name.

    Example:  src/pkg/__init__.py exists, src/__init__.py does NOT.
              src/pkg/a.py  →  package root = src/pkg, base = src
              dotted name = "pkg.a"

    A lone top-level module (no __init__.py in its directory) keeps its stem.
    """
    mapping: Dict[Path, str] = {}
    for p in py_files:
        p_abs = p.resolve()
        directory = p_abs.parent

        # Find the base directory (first ancestor without __init__.py)
        base = _find_package_root(directory)

        try:
            rel = p_abs.relative_to(base)
        except ValueError:
            # File is not under base — fall back to root-relative path
            try:
                rel = p_abs.relative_to(root)
            except ValueError:
                continue

        parts = list(rel.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
            if not parts:
                # base/__init__.py — shouldn't happen given algorithm, but skip
                continue
            dotted = ".".join(parts)
        else:
            parts[-1] = parts[-1][:-3]  # strip .py
            dotted = ".".join(parts)

        mapping[p_abs] = dotted
    return mapping


def _module_to_path(module_name: str, root: Path) -> Optional[Path]:
    """Try to find root/a/b/c.py or root/a/b/c/__init__.py for 'a.b.c'."""
    parts = module_name.split(".")
    candidate_file = root / Path(*parts).with_suffix(".py")
    candidate_pkg = root / Path(*parts) / "__init__.py"
    if candidate_file.exists():
        return candidate_file.resolve()
    if candidate_pkg.exists():
        return candidate_pkg.resolve()
    return None


def _resolve_relative_import(
    level: int,
    module: Optional[str],
    file_path: Path,
    root: Path,
    file_mod_name: Optional[str] = None,
) -> Optional[str]:
    """Resolve a relative import to a dotted module name.

    When *file_mod_name* is provided (the package-boundary-derived dotted name
    for *file_path*), use it as the authoritative base for relative resolution
    instead of computing the path relative to *root*.  This handles src/ layouts
    where the file's dotted name is e.g. "pkg.a" even though the file lives at
    "src/pkg/a.py".
    """
    if file_mod_name is not None:
        # Derive the package parts from the file's own module name.
        # For a module "pkg.a" the package is ["pkg"]; for "pkg.sub.a" it's ["pkg", "sub"].
        # level=1 means "current package"; level=2 means "one package up", etc.
        mod_parts = file_mod_name.split(".")
        # Drop the file's own name to get the package parts
        pkg_parts = mod_parts[:-1]
        # Walk up (level - 1) more package levels
        for _ in range(level - 1):
            if pkg_parts:
                pkg_parts = pkg_parts[:-1]
        if module:
            full = ".".join(pkg_parts + module.split(".")) if pkg_parts else module
        else:
            full = ".".join(pkg_parts) if pkg_parts else None
        return full

    # Fallback: derive from filesystem path relative to root (original behaviour)
    pkg_path = file_path.parent
    for _ in range(level - 1):
        pkg_path = pkg_path.parent
    try:
        rel = pkg_path.relative_to(root)
    except ValueError:
        return None
    pkg_parts = list(rel.parts) if rel.parts != (Path("."),) else []
    # filter out empty
    pkg_parts = [p for p in pkg_parts if p]
    if module:
        full = ".".join(pkg_parts + module.split(".")) if pkg_parts else module
    else:
        full = ".".join(pkg_parts) if pkg_parts else None
    return full


# ---------------------------------------------------------------------------
# Import extraction
# ---------------------------------------------------------------------------

def _collect_imported_modules(
    tree: ast.AST,
    file_path: Path,
    root: Path,
    mod_map: Dict[Path, str],
    name_to_path: Dict[str, Path],
) -> List[str]:
    """Return list of project module names imported by this file."""
    imported: List[str] = []
    # The package-boundary-derived module name for this file (may be None if unmapped)
    file_mod_name: Optional[str] = mod_map.get(file_path.resolve())

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                # Check the name and each prefix
                for candidate in _prefixes(name):
                    if candidate in name_to_path:
                        imported.append(candidate)
                        break

        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            module = node.module  # may be None for `from . import x`

            if level > 0:
                # Relative import — use the file's package-boundary module name
                # so that src/ layouts resolve correctly.
                resolved = _resolve_relative_import(
                    level, module, file_path, root, file_mod_name=file_mod_name
                )
                if resolved and resolved in name_to_path:
                    imported.append(resolved)
                elif resolved:
                    # Try sub-names: `from . import x` -> resolved pkg + ".x"
                    for alias in node.names:
                        sub = resolved + "." + alias.name if resolved else alias.name
                        for candidate in _prefixes(sub):
                            if candidate in name_to_path:
                                imported.append(candidate)
                                break
            else:
                if module:
                    # First try module.name for each imported name, in case
                    # it is a submodule (e.g. `from pkg import mod_b` where
                    # pkg.mod_b is a real project module).
                    for alias in node.names:
                        sub = module + "." + alias.name
                        for candidate in _prefixes(sub):
                            if candidate in name_to_path:
                                imported.append(candidate)
                                break
                    # Then fall back to the module itself
                    for candidate in _prefixes(module):
                        if candidate in name_to_path:
                            imported.append(candidate)
                            break

    return list(set(imported))


def _prefixes(dotted: str) -> List[str]:
    """Return all prefixes of a dotted name, longest first."""
    parts = dotted.split(".")
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]


# ---------------------------------------------------------------------------
# Cycle detection via iterative DFS (Tarjan-style coloring)
# ---------------------------------------------------------------------------

def _find_sccs(graph: Dict[str, Set[str]]) -> List[List[str]]:
    """Return strongly connected components with >1 node, or self-loops."""
    # Iterative Kosaraju's algorithm
    nodes = list(graph.keys())
    visited: Set[str] = set()
    order: List[str] = []

    # First pass: DFS on original graph, record finish order
    for start in nodes:
        if start in visited:
            continue
        stack = [(start, False)]
        while stack:
            node, returning = stack.pop()
            if returning:
                order.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            for neighbor in graph.get(node, set()):
                if neighbor not in visited:
                    stack.append((neighbor, False))

    # Build reverse graph
    rev_graph: Dict[str, Set[str]] = defaultdict(set)
    for node, neighbors in graph.items():
        for nb in neighbors:
            rev_graph[nb].add(node)

    # Second pass: DFS on reverse graph in reverse finish order
    visited2: Set[str] = set()
    sccs: List[List[str]] = []
    for start in reversed(order):
        if start in visited2:
            continue
        comp: List[str] = []
        stack = [start]
        while stack:
            node = stack.pop()
            if node in visited2:
                continue
            visited2.add(node)
            comp.append(node)
            for neighbor in rev_graph.get(node, set()):
                if neighbor not in visited2:
                    stack.append(neighbor)
        if len(comp) > 1:
            sccs.append(sorted(comp))
        elif comp and comp[0] in graph.get(comp[0], set()):
            # self-loop
            sccs.append(comp)

    return sccs


# ---------------------------------------------------------------------------
# Per-file smell detectors
# ---------------------------------------------------------------------------

def _check_god_module(tree: ast.AST, filepath: Path, lines: List[str], ignore: Set[str]) -> Optional[CodeSmell]:
    if "god_module" in ignore:
        return None
    source_lines = sum(1 for ln in lines if ln.strip() and not ln.strip().startswith("#"))
    top_defs = sum(
        1 for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    reasons = []
    if source_lines > 800:
        reasons.append(f"{source_lines} source lines")
    if top_defs > 40:
        reasons.append(f"{top_defs} top-level definitions")
    if not reasons:
        return None
    return CodeSmell(
        file=str(filepath),
        line=1,
        smell_type="god_module",
        description=f"Module is too large: {', '.join(reasons)}",
        suggestion="Split by responsibility into smaller, focused modules.",
        severity="medium",
        code_snippet=_get_line(lines, 1),
    )


def _check_wildcard_imports(tree: ast.AST, filepath: Path, lines: List[str], ignore: Set[str]) -> List[CodeSmell]:
    if "wildcard_import" in ignore:
        return []
    results = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    results.append(CodeSmell(
                        file=str(filepath),
                        line=node.lineno,
                        smell_type="wildcard_import",
                        description=f"Wildcard import from '{node.module or '?'}'",
                        suggestion="Import names explicitly; star imports hide origins and break tooling.",
                        severity="medium",
                        code_snippet=_get_line(lines, node.lineno),
                    ))
    return results


def _check_logic_in_init(tree: ast.AST, filepath: Path, lines: List[str], ignore: Set[str]) -> Optional[CodeSmell]:
    if "logic_in_init" in ignore:
        return None
    if filepath.name != "__init__.py":
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return CodeSmell(
                file=str(filepath),
                line=node.lineno,
                smell_type="logic_in_init",
                description=f"__init__.py contains a {'class' if isinstance(node, ast.ClassDef) else 'function'} definition '{node.name}'",
                suggestion="Keep packages' __init__ thin — move implementation into a submodule and re-export.",
                severity="low",
                code_snippet=_get_line(lines, node.lineno),
            )
    return None


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(root: Path, ignore: Set[str]) -> List[CodeSmell]:
    py_files = list(find_python_files(root))
    if not py_files:
        return []

    # Build module map
    mod_map = _build_module_map(root, py_files)
    name_to_path: Dict[str, Path] = {v: k for k, v in mod_map.items()}

    # Parse all files
    trees: Dict[Path, ast.AST] = {}
    file_lines: Dict[Path, List[str]] = {}
    for fp in py_files:
        # Unreadable/unparseable files are skipped by design.
        with contextlib.suppress(Exception):
            source = fp.read_text(encoding="utf-8", errors="replace")
            trees[fp.resolve()] = ast.parse(source, filename=str(fp))
            file_lines[fp.resolve()] = source.splitlines()

    issues: List[CodeSmell] = []

    # Per-file checks (god_module, wildcard_import, logic_in_init)
    for fp_abs, tree in trees.items():
        fp = Path(fp_abs)
        lines = file_lines[fp_abs]
        gm = _check_god_module(tree, fp, lines, ignore)
        if gm:
            issues.append(gm)
        issues.extend(_check_wildcard_imports(tree, fp, lines, ignore))
        li = _check_logic_in_init(tree, fp, lines, ignore)
        if li:
            issues.append(li)

    # Build import graph (module_name -> set of module_names it imports)
    graph: Dict[str, Set[str]] = {name: set() for name in name_to_path}
    for fp_abs, tree in trees.items():
        fp = Path(fp_abs)
        if fp_abs not in mod_map:
            continue
        src_mod = mod_map[fp_abs]
        imported = _collect_imported_modules(tree, fp, root, mod_map, name_to_path)
        for imp_mod in imported:
            if imp_mod != src_mod and imp_mod in graph:
                graph[src_mod].add(imp_mod)

    # Detect cycles
    if "circular_import" not in ignore:
        sccs = _find_sccs(graph)
        for scc in sccs:
            # Report at first module's file
            first_mod = scc[0]
            fp_abs = name_to_path.get(first_mod)
            if fp_abs is None:
                continue
            lines = file_lines.get(fp_abs, [])
            cycle_str = " → ".join(scc + [scc[0]])
            issues.append(CodeSmell(
                file=str(fp_abs),
                line=1,
                smell_type="circular_import",
                description=f"Import cycle detected: {cycle_str}",
                suggestion="Break the cycle by extracting shared code into a new module, using lazy imports, or restructuring dependencies.",
                severity="high",
                code_snippet=_get_line(lines, 1),
            ))

    return issues


def main():
    parser = argparse.ArgumentParser(description="Detect import-graph structural problems in Python projects")
    parser.add_argument("path", nargs="?", default=".", help="File or directory to analyze")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    root = Path(args.path)
    if root.is_file():
        root = root.parent
    root = root.resolve()

    all_issues = analyze(root, ignore)
    all_issues.sort(key=lambda x: (x.severity != "high", x.severity != "medium", x.file, x.line))

    if args.format == "json":
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No import-graph issues found!")
            return
        by_type = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} import-graph issue(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        for i in all_issues:
            print(f"{icons[i.severity]} [{i.severity.upper()}] {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            print(f"   → {i.suggestion}\n")


if __name__ == "__main__":
    main()
