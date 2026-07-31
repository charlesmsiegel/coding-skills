"""Shared fixtures for the skills' regression tests.

The analyzer skills are CLI scripts, so the tests drive them the way the skills
themselves do — as subprocesses over a throwaway repo built in tmp_path — and
assert on the JSON summary and the HTML fragments they emit.
"""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


class Repo:
    """A throwaway git repo, built a commit at a time."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q")
        self.git("config", "user.email", "t@t.co")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str, date: str | None = None) -> str:
        env = None
        if date:  # backdates the commit so --since windows can be exercised
            env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True, text=True, timeout=120, env=env,
        )
        assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr[:400]}"
        return result.stdout

    def write(self, rel: str, content: str) -> Path:
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def delete(self, rel: str) -> None:
        (self.path / rel).unlink()

    def commit(self, message: str = "wip", date: str | None = None) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message, date=date)
        return self.sha()

    def sha(self, ref: str = "HEAD", short: bool = True) -> str:
        args = ["rev-parse"] + (["--short"] if short else []) + [ref]
        return self.git(*args).strip()


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(tmp_path / "repo")


@pytest.fixture
def tabs(tmp_path) -> Path:
    """Output directory the analyzers write their HTML fragments into."""
    d = tmp_path / "tabs"
    d.mkdir()
    return d


@pytest.fixture
def run_script():
    """Run a skill script as a subprocess, asserting its exit code."""

    def _run(script: Path, *args, expect_rc: int | None = 0) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, str(script), *[str(a) for a in args]],
            capture_output=True, text=True, timeout=300,
        )
        if expect_rc is not None:
            assert result.returncode == expect_rc, (
                f"{script.name} exited {result.returncode} (wanted {expect_rc}): {result.stderr[-600:]}"
            )
        return result

    return _run


@pytest.fixture
def load_module():
    """Import a helper module out of a skill's scripts/ directory.

    The two visualization skills ship same-named modules (common.py and
    friends), so any cached copy is dropped first — otherwise the second
    skill's tests would silently exercise the first skill's code.
    """
    SHARED_NAMES = ("common", "diffutil", "assemble", "extract_tabs", "resources",
                    "llmops", "imports")

    def _load(scripts_dir: Path, name: str):
        for cached in SHARED_NAMES:
            sys.modules.pop(cached, None)
        sys.path.insert(0, str(scripts_dir))
        try:
            return importlib.import_module(name)
        finally:
            sys.path.remove(str(scripts_dir))
            for cached in SHARED_NAMES:
                sys.modules.pop(cached, None)

    return _load


class Fragment:
    """Reads the '<!-- tab: Title -->' protocol assemble.py expects of fragments."""

    @staticmethod
    def title(path: Path) -> str:
        header = path.read_text(encoding="utf-8").split("\n", 1)[0]
        assert header.startswith("<!-- tab: "), f"{path.name} lacks a tab header: {header!r}"
        return header[len("<!-- tab: "):].removesuffix(" -->")

    @staticmethod
    def body(path: Path) -> str:
        return path.read_text(encoding="utf-8").split("\n", 1)[1]


@pytest.fixture
def fragment() -> type[Fragment]:
    return Fragment
