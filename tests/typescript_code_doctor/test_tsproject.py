"""Whole-tree loading: every tsconfig contributes its path aliases."""

import json
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"


@pytest.fixture
def tsproject(load_module):
    return load_module(SCRIPTS_DIR, "tsproject")


def test_aliases_from_the_sixth_config_resolve(tsproject, tmp_path):
    """Alias loading used to stop after five configs, so imports through the
    sixth package's `paths` silently became external."""
    for n in range(5):
        pkg = tmp_path / "packages" / f"p{n}"
        pkg.mkdir(parents=True)
        (pkg / "tsconfig.json").write_text(json.dumps({"compilerOptions": {}}))
    deep = tmp_path / "packages" / "zz"
    (deep / "lib").mkdir(parents=True)
    (deep / "tsconfig.json").write_text(json.dumps(
        {"compilerOptions": {"baseUrl": ".", "paths": {"@zz/*": ["lib/*"]}}}))
    (deep / "lib" / "util.ts").write_text("export const u = 1;\n")
    (deep / "main.ts").write_text("import { u } from '@zz/util';\nexport const m = u;\n")
    project = tsproject.load_project(tmp_path)
    resolved = project.resolve((deep / "main.ts").resolve(), "@zz/util")
    assert resolved == (deep / "lib" / "util.ts").resolve()


def test_nearest_package_json_is_found_below_excluded_dirs_only(tsproject, tmp_path):
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "package.json").write_text('{"name": "x"}')
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "package.json").write_text('{"name": "app"}')
    manifest, data = tsproject.read_package_json(tmp_path)
    assert manifest == tmp_path / "app" / "package.json" and data["name"] == "app"
