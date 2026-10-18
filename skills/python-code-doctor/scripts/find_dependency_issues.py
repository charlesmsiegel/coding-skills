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

Deliberately NOT charged as missing:
  - an optional import: one inside the body of a `try` whose `except` names
    ImportError or ModuleNotFoundError and visibly degrades — its last
    statement is `pass`, `continue`, a falsy `return`, a rebinding of the
    guarded name (`x = None`, `x = fallback`, a fallback import of the same
    name), or a warnings.warn / logging call. The code runs on without the
    package, so it is optional by construction. When the try body imports
    more than one third-party module (stdlib and relative imports aside), only
    an import whose name a degrading handler rebinds — `x = None`, `x =
    fallback`, or a fallback import — is optional. An optional site speaks only
    for itself: another site of the same module that stops is still a lead, and
    an unguarded one is still a finding. It still counts as a use of a declared
    dependency (an extra). A
    bare `except:` or `except Exception:` also catches the error but does not
    say the import was meant to be optional, so it does not count — and an
    `except ImportError:` placed after one never runs, so it does not count either.

Reported as a candidate, not a finding:
  - an undeclared module imported only under such a guard, where the guard
    does not make it optional: no handler visibly degrades — it raises or exits
    (how a script says a package is required, and also how an optional
    feature's entry point reports a missing extra), or ends any other way the
    detector cannot call a degrade (a helper that may exit, `return 1`,
    argparse's error()) — or the try body imports other modules and the handler
    replaces none of this one's names. An unrecognised handler costs a lead,
    never silence. One residual: a repo with no manifest whose third-party
    imports are all guarded this way gets one candidate per module, not the
    no_dependency_manifest finding.
  - an undeclared module every import of which follows, in its own file, a
    sys.path addition (sys.path.insert/append/extend, `sys.path += [...]`,
    site.addsitedir) whose directory the source cannot place inside the project:
    one that resolves outside the repository (a relative entry is read from the
    project root and from the file's own directory, the two working
    directories that occur, and is inside if either is), or one that reads a function
    parameter, os.environ/os.getenv, sys.argv, an argparse result (`args.x`
    from parse_args() or parse_known_args()), or a name bound more than once.
    A harness run under another program's interpreter imports that program's
    modules this way, and no manifest here could declare them. A directory the
    source does resolve inside the project explains nothing (every module there
    is already first-party), nor does one spelled in a way the path reader cannot
    follow but with none of those run-time sources in it, and a module-level
    import is not served by an addition inside a function, which cannot have
    run yet when the module loads.
    Scope, stated plainly: one such addition demotes EVERY undeclared module
    whose imports all come after it in that file, not only the modules the added
    directory provides. That covers a production module that loads plugins from
    $PLUGIN_DIR and then imports `requests`, and an ordinary lazy import in any
    function below a `def add_dir(d): sys.path.append(d)` helper. The source
    cannot say which names the directory serves, so all of them are leads.
"""

import ast
import sys
import json
import argparse
import functools
import re
from pathlib import Path
from dataclasses import dataclass, asdict
from collections import defaultdict
from import_sites import ImportSite, import_sites
from common import (sort_findings, SEVERITY_ICONS, configure_output, find_python_files,
                    first_party_modules, in_nested_repository, warn_detector_error, warn_unparseable)

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
    # A scored defect by default; "candidate" for a lead the downstream merge
    # must not score. A candidate carries no fix and names, in also_caused_by,
    # the ways a healthy codebase produces the same observation.
    kind: str = "finding"
    also_caused_by: tuple[str, ...] = ()


# Why a module imported only after its file puts an unreadable directory on
# sys.path need not be a dependency this project should declare.
_RUNTIME_PATH_REASONS = (
    "the file runs inside another program's checkout or interpreter (a test harness, a plugin "
    "host, a script driving a vendored tree) and imports that program's own modules from the "
    "directory it puts on sys.path, which no manifest of this project could declare",
    "the directory comes from a function argument, the environment, the command line or a name rebound as "
    "the program runs, or resolves outside this repository, so only the machine the code runs on "
    "shows what it provides",
)


# Why a module imported only under an ImportError guard that does not carry on
# need not be a dependency this project should declare.
_GUARD_REASONS = {
    "exits": (
        "the handler turns a missing optional extra into a clear error at the entry point that "
        "needs it, and every other path through the program runs without the package",
        "the package is installed by a step the project documents rather than declares (a plugin "
        "host, a GPU stack, a system package), and the handler is that step's error message",
    ),
    "shared": (
        "the imports in the try are one optional feature that the handler switches off as a whole, "
        "through placeholders for some of the names or a flag such as `HAVE_X = False`",
    ),
}


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
        if in_nested_repository(root, req_file):
            continue  # another repository's manifest, not this project's
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
    except Exception as exc:
        warn_unparseable(pyproject, exc)
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


def _collect_imports(root: Path) -> dict[str, list[ImportSite]]:
    """
    Walk all Python files and collect top-level module imports.
    Returns dict: module_name -> [ImportSite, ...]
    """
    imports: dict[str, list[ImportSite]] = defaultdict(list)
    for filepath in find_python_files(root):
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(filepath))
        except (SyntaxError, ValueError) as exc:
            warn_unparseable(filepath, exc)
            continue
        except Exception as exc:
            warn_detector_error(filepath, exc)
            continue
        try:
            sites = list(import_sites(tree, filepath, root))
        except Exception as exc:  # one file the reader trips on must not sink the tree's report
            warn_detector_error(filepath, exc)
            continue
        for top, site in sites:
            imports[top].append(site)
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
    - The stem of every <name>.py and the name of every <name>/__init__.py
      anywhere in the tree (outside venvs, node_modules and the rest of
      common.EXCLUDE_DIRS). A script that puts `tests/models` on sys.path and
      imports `sampler_case` is importing a file of this repository; nothing
      can install it, so no manifest should declare it.
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
        if any(part in _skip for part in init_file.parts) or in_nested_repository(root, init_file):
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
            if (p.is_file() and p.suffix == ".py" and p.stem != "__init__"
                    and not in_nested_repository(root, p)):
                names.add(p.stem)

    # Any module or package that exists as a file in the repository is first-party.
    names |= first_party_modules(root)

    return frozenset(names)


def _resolves_beside(name: str, importer: Path) -> bool:
    """True when `name` is a module or directory in the importing file's own directory."""
    beside = importer.parent
    return (beside / f"{name}.py").is_file() or (beside / name).is_dir()


def _is_namespace_dir(directory: Path) -> bool:
    """A directory of .py files with no __init__.py is an importable namespace
    package (PEP 420) — `tests/` is the everyday example."""
    return directory.is_dir() and any(p.suffix == ".py" for p in directory.iterdir())


def _is_local(name: str, root: Path) -> bool:
    """Return True if `name` appears to be a local module under root."""
    # Fast direct checks
    if (
        (root / f"{name}.py").exists()
        or (root / name / "__init__.py").exists()
        or (root / "src" / f"{name}.py").exists()
        or (root / "src" / name / "__init__.py").exists()
        or _is_namespace_dir(root / name)
        or _is_namespace_dir(root / "src" / name)
    ):
        return True
    # Full package scan (cached)
    return name in _compute_local_packages(root)


def _lead(mod: str, sites: list[ImportSite]) -> CodeSmell:
    """The candidate for an undeclared module no import of which is plain: each
    is guarded without being optional, or follows an opaque sys.path addition."""
    first = min(sites, key=lambda s: (s.file, s.line))
    if first.guard == "exits":
        why = ("it is imported here inside a try whose ImportError handler raises or exits, which is "
               "how a required dependency announces itself and also how an optional extra's entry "
               "point reports that it is missing")
    elif first.guard == "shared":
        why = ("it is imported here in a try with other third-party imports, and the ImportError handler "
               "rebinds none of its names, so it may be optional along with them or required and merely caught")
    else:
        why = (f"it is imported here after line {first.after_path} puts a directory on sys.path that "
               f"the source cannot place inside this project, so it may be served from that directory")
    return CodeSmell(
        file=first.file,
        line=first.line,
        smell_type="missing_dependency",
        description=f"Module '{mod}' is not declared in any dependency manifest, but {why}",
        suggestion="",
        severity="high",
        kind="candidate",
        also_caused_by=_GUARD_REASONS.get(first.guard, _RUNTIME_PATH_REASONS),
    )


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
        or any(not in_nested_repository(root, p) for p in root.rglob("requirements*.txt"))
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
    third_party_imports: dict[str, list[ImportSite]] = {}
    for mod, sites in all_imports.items():
        if _is_stdlib(mod):
            continue
        if _is_local(mod, root):
            continue
        # pytest (and any script run from its own directory) puts the importing
        # file's directory on sys.path, so `from test_chat import helper` in
        # tests/ resolves to the file next door. No manifest declares that, and
        # a tests/ directory needs no __init__.py for it to work.
        sites = [s for s in sites if not _resolves_beside(mod, Path(s.file))]
        # An optional import is one the code runs without: the package being
        # absent is handled where it is imported, so no manifest owes it. It
        # speaks only for itself: a site that stops, or follows an opaque
        # sys.path addition, stays a lead, and a plain import stays a finding.
        sites = [s for s in sites if s.guard != "optional"]
        if sites:
            third_party_imports[mod] = sites

    # ------------------------------------------------------------------ #
    # 4. No manifest at all
    # ------------------------------------------------------------------ #
    # A module with no plain import is guarded without being optional, or comes
    # after a sys.path addition the source cannot read: a lead, reported below.
    unexplained = {mod for mod, sites in third_party_imports.items() if any(s.plain for s in sites)}
    if not has_manifest and unexplained:
        if "no_dependency_manifest" not in ignore:
            issues.append(CodeSmell(
                file=str(root),
                line=1,
                smell_type="no_dependency_manifest",
                description=(
                    f"No requirements*.txt or pyproject.toml found, but "
                    f"{len(unexplained)} third-party module(s) are imported"
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
        for mod, sites in sorted(third_party_imports.items()):
            norm_mod = _normalize(mod)
            # Check direct match
            if norm_mod in declared:
                continue
            # Check via import->dist map
            mapped = _IMPORT_TO_DIST.get(mod.lower())
            if mapped and _normalize(mapped) in declared:
                continue
            if mod not in unexplained:
                issues.append(_lead(mod, sites))
                continue
            # Not found — report once, pointing at the root
            example_file = sorted({s.file for s in sites if s.plain})[0]
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


def analyze_tree(path: Path, ignore: set) -> list:
    """Findings for the whole tree, ordered as main() orders them."""
    findings = analyze(Path(path), ignore)
    sort_findings(findings)
    return findings


def to_record(issue: CodeSmell) -> dict:
    """The JSON shape this detector emits, shared with the runner.

    `kind` is always present, so a consumer never reads an absent key as a
    classification; the reasons tuple serialises as a list.
    """
    record = asdict(issue)
    record["also_caused_by"] = list(record["also_caused_by"])
    return record


def main():
    configure_output()
    parser = argparse.ArgumentParser(description="Detect dependency management issues in Python")
    parser.add_argument("path", nargs="?", default=".", help="File or directory")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--ignore", type=str, default="", help="Comma-separated smell types to ignore")
    args = parser.parse_args()
    ignore = set(args.ignore.split(",")) if args.ignore else set()

    root = Path(args.path)
    all_issues = analyze(root, ignore)
    sort_findings(all_issues)

    if args.format == "json":
        print(json.dumps([to_record(i) for i in all_issues], indent=2))
    else:
        if not all_issues:
            print("✅ No dependency issues found!")
            return
        by_type: dict[str, int] = defaultdict(int)
        for i in all_issues:
            by_type[i.smell_type] += 1
        # A candidate is a lead, not a defect, so it is counted apart.
        leads = sum(1 for i in all_issues if i.kind == "candidate")
        print(f"Found {len(all_issues) - leads} dependency issue(s), "
              f"{leads} candidate(s):\n\nSummary:")
        for s, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        print()
        icons = SEVERITY_ICONS
        for i in all_issues:
            # A severity icon on a lead reads as a verdict the evidence does not support.
            marker = ("? [CANDIDATE]" if i.kind == "candidate"
                      else f"{icons[i.severity]} [{i.severity.upper()}]")
            print(f"{marker} {i.file}:{i.line}")
            print(f"   {i.smell_type}: {i.description}")
            if i.code_snippet:
                print(f"   Code: {i.code_snippet}")
            if i.suggestion:
                print(f"   → {i.suggestion}")
            for reason in i.also_caused_by:
                print(f"   ? also caused by: {reason}")
            print()


if __name__ == "__main__":
    main()
