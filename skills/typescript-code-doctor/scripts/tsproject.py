#!/usr/bin/env python3
"""Whole-tree view: parse every file once and resolve imports between them.

Three of the detectors — import cycles, unused exports, untested modules — are
only correct with the whole tree in hand, and each of them would otherwise pay
for its own parse of every file. This builds the index once.

Module resolution here is deliberately partial. It resolves relative specifiers
and tsconfig `paths` aliases, which is what intra-project edges are made of, and
treats everything else as external. It does not read `node_modules`, so it never
claims to know what a bare specifier points at.
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from common import EXCLUDE_DIRS, TS_EXTENSIONS, find_ts_files, is_test_file, warn_unparseable
from find_tsconfig_issues import load_jsonc
from tsparse import TsFile, TsSyntaxError, parse_file

# Tried in order when a specifier has no extension.
_CANDIDATE_SUFFIXES = (
    ".ts", ".tsx", ".mts", ".cts", ".d.ts",
    "/index.ts", "/index.tsx", "/index.mts", "/index.cts",
)


@dataclass
class Project:
    root: Path
    files: dict[Path, TsFile] = field(default_factory=dict)
    failed: dict[Path, str] = field(default_factory=dict)
    aliases: dict[str, list[Path]] = field(default_factory=dict)

    @property
    def sources(self) -> list[Path]:
        return [p for p in self.files if not is_test_file(p)]

    @property
    def tests(self) -> list[Path]:
        return [p for p in self.files if is_test_file(p)]

    def resolve(self, importer: Path, specifier: str) -> Path | None:
        """The file a specifier points at, or None when it is external."""
        if specifier.startswith("."):
            base = (importer.parent / specifier).resolve()
            return self._existing(base)
        for prefix, targets in self.aliases.items():
            if specifier == prefix or specifier.startswith(prefix + "/"):
                tail = specifier[len(prefix):].lstrip("/")
                for target in targets:
                    found = self._existing((target / tail).resolve() if tail else target.resolve())
                    if found is not None:
                        return found
        return None

    def _existing(self, base: Path) -> Path | None:
        if base in self.files:
            return base
        for suffix in _CANDIDATE_SUFFIXES:
            candidate = Path(str(base) + suffix)
            if candidate in self.files:
                return candidate
        # An explicit `.js` specifier in an ESM/NodeNext project means the .ts.
        if base.suffix in (".js", ".jsx", ".mjs", ".cjs"):
            stem = base.with_suffix("")
            for suffix in TS_EXTENSIONS:
                candidate = Path(str(stem) + suffix)
                if candidate in self.files:
                    return candidate
        return None

    def internal_imports(self, path: Path) -> list[tuple[Path, int, str]]:
        """(target, line, specifier) for each import that lands inside the tree."""
        edges = []
        tsfile = self.files.get(path)
        if tsfile is None:
            return edges
        for record in tsfile.imports:
            if not record.module:
                continue
            target = self.resolve(path, record.module)
            if target is not None and target != path:
                edges.append((target, record.line, record.module))
        return edges


def _load_aliases(root: Path) -> dict[str, list[Path]]:
    """tsconfig `paths` as prefix -> directories, so `@app/x` resolves."""
    aliases: dict[str, list[Path]] = {}
    configs = [p for p in root.rglob("tsconfig*.json")
               if EXCLUDE_DIRS.isdisjoint(p.relative_to(root).parts)] if root.is_dir() else []
    for config in sorted(configs, key=lambda p: len(p.relative_to(root).parts))[:5]:
        data = load_jsonc(config) or {}
        options = data.get("compilerOptions") or {}
        if not isinstance(options, dict):
            continue
        base = (config.parent / str(options.get("baseUrl", "."))).resolve()
        paths = options.get("paths") or {}
        if not isinstance(paths, dict):
            continue
        for pattern, targets in paths.items():
            if not isinstance(targets, list):
                continue
            prefix = pattern.rstrip("/*").rstrip("/")
            resolved = [(base / str(t).rstrip("/*").rstrip("/")).resolve() for t in targets]
            aliases.setdefault(prefix, []).extend(resolved)
    return aliases


def load_project(root: Path, *, quiet: bool = False) -> Project:
    """Parse every TypeScript file under ``root`` once."""
    root = root.resolve()
    project = Project(root=root, aliases=_load_aliases(root) if root.is_dir() else {})
    for path in find_ts_files(root):
        resolved = path.resolve()
        try:
            project.files[resolved] = parse_file(path)
        except TsSyntaxError as exc:
            project.failed[resolved] = str(exc)
            if not quiet:
                warn_unparseable(path, exc)
        except OSError as exc:
            project.failed[resolved] = str(exc)
            if not quiet:
                warn_unparseable(path, exc)
    if project.failed and not quiet:
        print(f"⚠️  {len(project.failed)} file(s) did not tokenize; whole-tree findings below "
              "are incomplete for them", file=sys.stderr)
    return project


def read_package_json(root: Path) -> tuple[Path | None, dict]:
    """The nearest package.json and its parsed contents."""
    if root.is_file():
        root = root.parent
    for candidate in [root, *sorted(root.rglob("package.json"), key=lambda p: len(p.parts))[:3]]:
        manifest = candidate if candidate.name == "package.json" else candidate / "package.json"
        if not manifest.is_file():
            continue
        if not EXCLUDE_DIRS.isdisjoint(manifest.parts):
            continue
        try:
            return manifest, json.loads(manifest.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return manifest, {}
    return None, {}
