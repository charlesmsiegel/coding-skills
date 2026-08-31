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


# Module basenames that more than one skill's scripts/ directory ships, plus a
# few kept defensively for modules that have been shared before. `load_module`
# evicts every one of them from sys.modules around each import, because
# `sys.modules` is keyed by bare module name: without the eviction the *second*
# skill's tests silently exercise the *first* skill's code, in whatever order
# pytest happened to collect them.
#
# The failure is not a subtle one — it is an AttributeError on a function the
# other skill's module simply does not have — but it only appears when both
# skills' tests run in one session, which is exactly what CI does and what a
# single-file test run does not. `rubric` was the live example: code-overview
# and science-investigation both ship one, and every science-investigation
# rubric test failed on `pytest -q` while passing on its own.
#
# `test_shared_module_names.py` fails if a name shared across skills is missing
# here, so a new collision cannot arrive silently.
SHARED_NAMES = (
    "analyze_all", "analyze_complexity", "analyze_diff", "assemble",
    "check_codemap_state", "common", "coverage_data", "diffutil", "extract_tabs",
    "find_ai_scaffolding", "find_async_issues", "find_code_smells",
    "find_comment_smells", "find_coupling_issues", "find_dead_code",
    "find_debug_leftovers", "find_dependency_issues", "find_design_smells",
    "find_duplicates", "find_exception_issues", "find_loop_simplifications",
    "find_module_issues", "find_mutation_hazards", "find_naming_issues", "find_outdated_idioms",
    "find_overengineering", "find_resource_leaks", "find_security_issues",
    "find_test_smells", "find_type_gaps", "find_untested_modules",
    "format_findings", "imports", "jvmdecl", "lint_fragments", "llmops",
    "manifests", "resources", "rubric", "run_external_tools", "runner",
    "verify_citations",
)


def shared_script_basenames() -> set[str]:
    """Module basenames that appear in more than one skill's scripts/ directory.

    Computed from the tree rather than remembered, so the guard test compares
    `SHARED_NAMES` against what is actually on disk today.
    """
    owners: dict[str, set[str]] = {}
    for script in SKILLS_DIR.glob("*/scripts/**/*.py"):
        owners.setdefault(script.stem, set()).add(script.parts[len(SKILLS_DIR.parts)])
    return {name for name, skills in owners.items() if len(skills) > 1}


@pytest.fixture
def shared_names() -> tuple[str, ...]:
    """The eviction list itself, exposed as a fixture.

    `--import-mode=importlib` means a test module cannot `import conftest`, so
    the guard test reaches the list this way.
    """
    return SHARED_NAMES


@pytest.fixture
def shared_basenames_on_disk() -> set[str]:
    return shared_script_basenames()


@pytest.fixture
def load_module():
    """Import a helper module out of a skill's scripts/ directory.

    Several skills ship same-named modules (common.py and friends), so any
    cached copy is dropped first — otherwise the second skill's tests would
    silently exercise the first skill's code. See SHARED_NAMES above.
    """

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
