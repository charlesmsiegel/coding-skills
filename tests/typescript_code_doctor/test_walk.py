"""The walker prunes excluded directories before descending into them.

`sorted(path.rglob("*"))` materialised every file under node_modules before the
first one was discarded — on a built checkout that is the difference between a
scan and a hang.
"""

import os
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS_DIR, "common")


@pytest.fixture
def run_external_tools(load_module):
    return load_module(SCRIPTS_DIR, "run_external_tools")


def _tree(root: Path) -> Path:
    for rel in ("src/a.ts", "src/b.tsx", "src/c.mts", "src/d.cts", "src/e.js",
                "node_modules/pkg/index.ts", "node_modules/pkg/deep/er/x.ts",
                "dist/out.ts", "docs/readme.md"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export {};\n", encoding="utf-8")
    return root


def test_find_ts_files_yields_every_supported_extension_and_nothing_excluded(common, tmp_path):
    found = [p.relative_to(tmp_path).as_posix() for p in common.find_ts_files(_tree(tmp_path))]
    assert found == ["src/a.ts", "src/b.tsx", "src/c.mts", "src/d.cts"]


def test_excluded_directories_are_never_entered(common, tmp_path, monkeypatch):
    _tree(tmp_path)
    entered = []
    real_walk = os.walk

    def spy(top, *args, **kwargs):
        for directory, subdirectories, names in real_walk(top, *args, **kwargs):
            entered.append(Path(directory).name)
            yield directory, subdirectories, names

    monkeypatch.setattr(common.os, "walk", spy)
    list(common.walk_tree(tmp_path))
    assert "node_modules" not in entered and "dist" not in entered
    assert "src" in entered


def test_walk_tree_on_a_single_file_and_a_missing_path(common, tmp_path):
    single = tmp_path / "only.ts"
    single.write_text("export {};\n", encoding="utf-8")
    assert list(common.find_ts_files(single)) == [single]
    assert list(common.find_ts_files(tmp_path / "missing")) == []


def test_keep_re_admits_one_excluded_directory_without_reopening_the_rest(common, tmp_path):
    _tree(tmp_path)
    nested = tmp_path / "packages" / "web" / "coverage" / "coverage-final.json"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("{}", encoding="utf-8")
    found = {p.relative_to(tmp_path).as_posix() for p in common.walk_tree(tmp_path, keep={"coverage"})}
    assert "packages/web/coverage/coverage-final.json" in found
    assert not any(p.startswith("node_modules/") for p in found)
    assert not any(p.startswith("dist/") for p in found)


def test_coverage_files_finds_a_nested_workspace_package_report(run_external_tools, tmp_path):
    """`coverage/` is pruned by `walk_tree` like any other excluded directory,
    so a monorepo package's own `coverage/coverage-final.json` — not at the
    scanned root — used to be unreachable once that pruning moved into the walk."""
    nested = tmp_path / "packages" / "web" / "coverage" / "coverage-final.json"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text("{}", encoding="utf-8")
    assert run_external_tools._coverage_files(tmp_path) == nested


def _nested_checkouts(root: Path) -> Path:
    """A repo with a git worktree (`.git` file) and a nested clone (`.git` directory) inside it."""
    for rel, content in {
        ".git/HEAD": "ref: refs/heads/main\n",
        "package.json": "{}\n",
        "src/a.ts": "export {};\n",
        ".worktrees/feature/.git": "gitdir: ../../.git/worktrees/feature\n",
        ".worktrees/feature/src/a.ts": "export {};\n",
        "vendor-clone/.git/HEAD": "ref: refs/heads/main\n",
        "vendor-clone/index.ts": "export {};\n",
        "sub/.git": "gitdir: /elsewhere\n",
        "sub/deep/x.ts": "export {};\n",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def test_a_nested_git_checkout_is_a_different_repository(common, tmp_path):
    """A worktree or a nested clone below the root is not part of this repo's code."""
    found = [p.relative_to(tmp_path).as_posix() for p in common.walk_tree(_nested_checkouts(tmp_path))]
    assert found == ["package.json", "src/a.ts"]


def test_a_nested_checkout_scanned_directly_is_still_scanned(common, tmp_path):
    """Pointing the scanner at the worktree itself analyses it: only checkouts *below* the root are skipped."""
    worktree = _nested_checkouts(tmp_path) / ".worktrees" / "feature"
    assert [p.relative_to(worktree).as_posix() for p in common.find_ts_files(worktree)] == ["src/a.ts"]
