#!/usr/bin/env python3
"""
Detect dependency management issues in Python projects.

Cross-file analysis that compares declared third-party dependencies against
what the code actually imports.

Finds:
  - missing_dependency    : third-party module imported but not declared
  - unused_dependency     : declared dependency never imported anywhere
  - unpinned_dependency   : requirements.txt entry with no version specifier
  - no_dependency_manifest: third-party imports exist but no manifest at all
"""

import ast
import sys
import json
import argparse
import functools
import re
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Iterator
from collections import defaultdict

# tomllib is stdlib on 3.11+. On older interpreters we simply skip pyproject.toml
# parsing rather than depend on a third-party backport (this skill is stdlib-only).
try:
    import tomllib
except ImportError:
    tomllib = None  # type: ignore


@dataclass
class CodeSmell:
    file: str
    line: int
    smell_type: str
    description: str
    suggestion: str
    severity: str
    code_snippet: str = ""


def find_python_files(path: Path) -> Iterator[Path]:
    if path.is_file() and path.suffix == ".py":
        yield path
    elif path.is_dir():
        for p in path.rglob("*.py"):
            if ".venv" not in p.parts and "node_modules" not in p.parts and "__pycache__" not in p.parts:
                yield p


# ---------------------------------------------------------------------------
# Known import-name -> distribution-name mappings (safe, well-known only)
# ---------------------------------------------------------------------------
_IMPORT_TO_DIST = {
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "pil": "pillow",
    "sklearn": "scikit_learn",
    "cv2": "opencv_python",
    "dateutil": "python_dateutil",
    "dotenv": "python_dotenv",
    "jwt": "pyjwt",
    "attr": "attrs",
}

# Tooling/plugin deps that are often unimported by name — never flag as unused
_TOOLING_DEPS = {
    "pytest", "pytest-cov", "pytest_cov", "coverage", "tox", "mypy",
    "ruff", "black", "isort", "flake8", "pre-commit", "pre_commit",
    "setuptools", "wheel", "pip", "build", "twine", "bandit", "pylint",
}

# Version specifier characters
_VERSION_CHARS = re.compile(r'[><=!~@]')

# Requirement line pattern: split on version/extras separators
_NAME_SPLIT = re.compile(r'[\s><=!~\[;@]')


def _normalize(name: str) -> str:
    """Lowercase and replace - and . with _ for comparison."""
    return name.lower().replace("-", "_").replace(".", "_")


def _parse_dist_name(raw: str) -> str:
    """Extract the bare distribution name from a requirement specifier."""
    # Strip extras like pkg[extra]
    raw = raw.strip()
    raw = re.sub(r'\[.*?\]', '', raw)
    m = _NAME_SPLIT.search(raw)
    if m:
        raw = raw[:m.start()]
    return raw.strip()


def _has_version_specifier(line: str) -> bool:
    """Return True if the line contains a version constraint."""
    # Strip the name portion first
    name = _parse_dist_name(line)
    rest = line[len(name):]
    return bool(_VERSION_CHARS.search(rest)) or (' @ ' in line)


def _collect_requirements_files(root: Path):
    """
    Find all requirements*.txt files at or under root.
    Returns list of (path, [(line_number, raw_line)]) entries of package lines.
    """
    results = []
    for req_file in sorted(root.rglob("requirements*.txt")):
        if ".venv" in req_file.parts or "node_modules" in req_file.parts:
            continue
        entries = []
        try:
            text = req_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, raw in enumerate(text.splitlines(), 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(("-r ", "-e ", "-c ", "--")):
                continue
            entries.append((lineno, stripped))
        results.append((req_file, entries))
    return results


def _collect_pyproject_deps(root: Path):
    """
    Parse pyproject.toml at root.
    Returns list of (dist_name, line_number_approx, has_version).
    Line numbers are approximate (we scan the raw text for the entry).
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.exists() or tomllib is None:
        return []

    try:
        raw_text = pyproject.read_text(encoding="utf-8", errors="replace")
        data = tomllib.loads(raw_text)
    except Exception:
        return []

    raw_lines = raw_text.splitlines()

    def find_line(name: str) -> int:
        """Best-effort: find the line containing this dep name."""
        pat = re.compile(r'\b' + re.escape(name) + r'\b', re.IGNORECASE)
        for i, line in enumerate(raw_lines, 1):
            if pat.search(line):
                return i
        return 1

    deps = []

    def add_dep(spec: str):
        name = _parse_dist_name(spec)
        if not name or _normalize(name) == "python":
            return
        lineno = find_line(name)
        has_ver = _has_version_specifier(spec)
        deps.append((name, lineno, has_ver))

    # [project].dependencies
    project = data.get("project", {})
    for spec in project.get("dependencies", []):
        add_dep(spec)

    # [project].optional-dependencies.*
    for group_deps in project.get("optional-dependencies", {}).values():
        for spec in group_deps:
            add_dep(spec)

    # [tool.poetry.dependencies] and [tool.poetry.group.*.dependencies]
    poetry = data.get("tool", {}).get("poetry", {})
    for name in poetry.get("dependencies", {}):
        if _normalize(name) != "python":
            lineno = find_line(name)
            deps.append((name, lineno, True))  # Poetry always has version constraint

    for group_info in poetry.get("group", {}).values():
        for name in group_info.get("dependencies", {}):
            if _normalize(name) != "python":
                lineno = find_line(name)
                deps.append((name, lineno, True))

    return deps


def _collect_imports(root: Path) -> dict[str, list[str]]:
    """
    Walk all Python files and collect top-level module imports.
    Returns dict: module_name -> [filepath, ...]
    """
    imports: dict[str, list[str]] = defaultdict(list)
    for filepath in find_python_files(root):
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(filepath))
        except (SyntaxError, Exception):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top and top != "__future__":
                        imports[top].append(str(filepath))
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # relative import
                if node.module:
                    top = node.module.split(".")[0]
                    if top and top != "__future__":
                        imports[top].append(str(filepath))
    return imports


def _is_stdlib(name: str) -> bool:
    return name in sys.stdlib_module_names


@functools.lru_cache(maxsize=None)
def _compute_local_packages(root: Path) -> frozenset:
    """
    Compute the set of local top-level package/module names for the project
    rooted at `root`.  Cached per root (lru_cache) — no shared mutable state.

    Includes:
    - root/<name>.py  or root/<name>/__init__.py  (directly under root)
    - root/src/<name>.py  or root/src/<name>/__init__.py  (src layout)
    - The top-level package name of any __init__.py found anywhere under root
      (skipping .venv / node_modules / __pycache__): walk up from the package
      directory while the parent also contains __init__.py; the topmost such
      directory's name is a local top-level package.
    - Stems of all .py files sitting directly inside any "src" directory under
      root.
    """
    names: set[str] = set()

    # Direct children: root/<name>.py or root/<name>/__init__.py
    for p in root.iterdir():
        if p.suffix == ".py" and p.stem != "__init__":
            names.add(p.stem)
        elif p.is_dir() and (p / "__init__.py").exists():
            names.add(p.name)

    # src/ children: root/src/<name>.py or root/src/<name>/__init__.py
    src_dir = root / "src"
    if src_dir.is_dir():
        for p in src_dir.iterdir():
            if p.suffix == ".py" and p.stem != "__init__":
                names.add(p.stem)
            elif p.is_dir() and (p / "__init__.py").exists():
                names.add(p.name)

    # Walk all __init__.py files under root to find top-level packages
    _skip = {".venv", "node_modules", "__pycache__"}
    for init_file in root.rglob("__init__.py"):
        # Skip ignored directories anywhere in the path
        if any(part in _skip for part in init_file.parts):
            continue
        pkg_dir = init_file.parent
        # Walk up while the parent also has __init__.py (i.e. is still a pkg)
        while (pkg_dir.parent / "__init__.py").exists():
            pkg_dir = pkg_dir.parent
        # pkg_dir is now the top-level package directory
        names.add(pkg_dir.name)

    # Also add stems of all .py files directly inside any "src" directory
    for src_candidate in root.rglob("src"):
        if not src_candidate.is_dir():
            continue
        if any(part in _skip for part in src_candidate.parts):
            continue
        for p in src_candidate.iterdir():
            if p.is_file() and p.suffix == ".py" and p.stem != "__init__":
                names.add(p.stem)

    return frozenset(names)


def _is_local(name: str, root: Path) -> bool:
    """Return True if `name` appears to be a local module under root."""
    # Fast direct checks
    if (
        (root / f"{name}.py").exists()
        or (root / name / "__init__.py").exists()
        or (root / "src" / f"{name}.py").exists()
        or (root / "src" / name / "__init__.py").exists()
    ):
        return True
    # Full package scan (cached)
    return name in _compute_local_packages(root)


def analyze(root: Path, ignore: set) -> list[CodeSmell]:
    issues: list[CodeSmell] = []
    root = root.resolve()

    # ------------------------------------------------------------------ #
    # 1. Declared dependencies
    # ------------------------------------------------------------------ #
    req_files = _collect_requirements_files(root)
    pyproject_deps = _collect_pyproject_deps(root)

    # Manifest presence is about the FILE existing (and parsing), not about how
    # many deps it declares — an intentionally-empty dependency list is still a
    # manifest, and undeclared imports against it are missing_dependency, not
    # no_dependency_manifest.
    def _pyproject_parses() -> bool:
        pyproject = root / "pyproject.toml"
        if not pyproject.exists() or tomllib is None:
            return False
        try:
            tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
            return True
        except Exception:
            return False

    has_manifest = (
        bool(req_files)
        or bool(pyproject_deps)
        or bool(list(root.rglob("requirements*.txt")))
        or _pyproject_parses()
    )

    # Build set of normalized declared names and lookup for location
    # declared_norm -> (source_file, lineno)
    declared: dict[str, tuple[str, int]] = {}

    for req_file, entries in req_files:
        for lineno, raw in entries:
            name = _parse_dist_name(raw)
            if not name:
                continue
            norm = _normalize(name)
            declared[norm] = (str(req_file), lineno)

            # unpinned_dependency
            if "unpinned_dependency" not in ignore and not _has_version_specifier(raw):
                issues.append(CodeSmell(
                    file=str(req_file),
                    line=lineno,
                    smell_type="unpinned_dependency",
                    description=f"Dependency '{name}' has no version specifier",
                    suggestion=(
                        "Pin a version (e.g. 'requests==2.31.0') or add a floor "
                        "(e.g. 'requests>=2.0') for reproducible installs."
                    ),
                    severity="low",
                    code_snippet=raw[:80],
                ))

    for name, lineno, _has_ver in pyproject_deps:
        norm = _normalize(name)
        if norm not in declared:
            pyproject_path = str(root / "pyproject.toml")
            declared[norm] = (pyproject_path, lineno)

    # Also build reverse map: norm -> canonical name (for reporting)
    declared_display: dict[str, str] = {}
    for req_file, entries in req_files:
        for _lineno, raw in entries:
            name = _parse_dist_name(raw)
            if name:
                declared_display[_normalize(name)] = name
    for name, _lineno, _hv in pyproject_deps:
        declared_display[_normalize(name)] = name

    # ------------------------------------------------------------------ #
    # 2. Actual imports
    # ------------------------------------------------------------------ #
    all_imports = _collect_imports(root)

    # ------------------------------------------------------------------ #
    # 3. Classify imports as third-party
    # ------------------------------------------------------------------ #
    third_party_imports: dict[str, list[str]] = {}
    for mod, files in all_imports.items():
        if _is_stdlib(mod):
            continue
        if _is_local(mod, root):
            continue
        third_party_imports[mod] = files

    # ------------------------------------------------------------------ #
    # 4. No manifest at all
    # ------------------------------------------------------------------ #
    if not has_manifest and third_party_imports:
        if "no_dependency_manifest" not in ignore:
            issues.append(CodeSmell(
                file=str(root),
                line=1,
                smell_type="no_dependency_manifest",
                description=(
                    f"No requirements*.txt or pyproject.toml found, but "
                    f"{len(third_party_imports)} third-party module(s) are imported"
                ),
                suggestion=(
                    "Add a pyproject.toml (or requirements.txt) declaring your "
                    "dependencies so the project can be installed reproducibly."
                ),
                severity="medium",
            ))
        return issues

    # ------------------------------------------------------------------ #
    # 5. missing_dependency: imported but not declared
    # ------------------------------------------------------------------ #
    if "missing_dependency" not in ignore:
        for mod, files in sorted(third_party_imports.items()):
            norm_mod = _normalize(mod)
            # Check direct match
            if norm_mod in declared:
                continue
            # Check via import->dist map
            mapped = _IMPORT_TO_DIST.get(mod.lower())
            if mapped and _normalize(mapped) in declared:
                continue
            # Not found — report once, pointing at the root
            example_file = sorted(set(files))[0]
            issues.append(CodeSmell(
                file=str(root),
                line=1,
                smell_type="missing_dependency",
                description=(
                    f"Module '{mod}' is imported (e.g. in {example_file}) "
                    f"but not declared in any dependency manifest"
                ),
                suggestion=(
                    f"Add '{mod}' (or its distribution name) to pyproject.toml "
                    f"or requirements.txt so installs are reproducible."
                ),
                severity="high",
            ))

    # ------------------------------------------------------------------ #
    # 6. unused_dependency: declared but never imported
    # ------------------------------------------------------------------ #
    if "unused_dependency" not in ignore:
        # A declared dep imported anywhere (even via a stdlib-shadowing name)
        # counts as used, so compare against ALL imports, not just third-party.
        all_imported_norms = {_normalize(m) for m in all_imports}

        for norm_dep, (src_file, lineno) in sorted(declared.items()):
            display_name = declared_display.get(norm_dep, norm_dep)
            # Skip tooling deps
            if norm_dep in {_normalize(t) for t in _TOOLING_DEPS}:
                continue
            if display_name.lower() in {t.lower() for t in _TOOLING_DEPS}:
                continue

            # Check if any import matches this dep
            found = False
            # Direct: dep name == module name (normalized)
            if norm_dep in all_imported_norms:
                found = True
            # Reverse map: dist name -> import name
            if not found:
                for imp_name, dist_name in _IMPORT_TO_DIST.items():
                    if _normalize(dist_name) == norm_dep and _normalize(imp_name) in all_imported_norms:
                        found = True
                        break
            if not found:
                issues.append(CodeSmell(
                    file=src_file,
                    line=lineno,
                    smell_type="unused_dependency",
                    description=(
                        f"Dependency '{display_name}' is declared but never imported anywhere"
                    ),
                    suggestion=(
                        "Remove it if it is no longer needed, or add a comment explaining "
                        "why it is required (e.g. a runtime plugin, CLI tool, or implicit dep)."
                    ),
                    severity="medium",
                    code_snippet=display_name[:80],
                ))

    return issues


def main():
    parser = argparse.ArgumentParser(description="Detect dependency management issues in Python")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    root = Path(args.path)
    all_issues = analyze(root, ignore)
    all_issues.sort(key=lambda x: (x.severity != "high", x.severity != "medium", x.file, x.line))

    if args.format == "json":
        print(json.dumps([asdict(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No dependency issues found!")
            return
        by_type: dict[str, int] = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        print(f"Found {len(all_issues)} dependency issue(s):\n\nSummary:")
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
