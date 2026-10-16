"""False positives removed at their root, in the scanner the detectors share.

Each defect here lived below the detectors — in the lexer, the declaration
extractor, the statement-end heuristic or the test-file predicate — and showed
up as findings in whichever detectors read the part it got wrong: a parameter
list split inside `Record<string, X>`, a name read only inside a template's
`${...}`, a comment located by comparing a character offset with a token index,
a JSX attribute read as an assignment, shared test scaffolding read as
production code, a statement that ran past its own line break. Every test that
pins a removed false positive sits next to one that pins the true positive the
same rule must keep catching — a fix that quietened both would pass the first
test alone.

Fixtures are written to tmp_path at runtime, as in test_detectors.py.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"


def run_detector(script: str, target: Path) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[-800:]}"
    return json.loads(result.stdout)


def project(root: Path, files: dict[str, str]) -> Path:
    """A throwaway project: a package.json at the top makes `root` the project root."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text('{"name": "fixture"}', encoding="utf-8")
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def records(script: str, root: Path, smell: str) -> list[dict]:
    return [r for r in run_detector(script, root) if r["smell_type"] == smell]


@pytest.fixture
def ts(load_module):
    """The parser facade, imported off the skill's scripts directory."""
    return load_module(SCRIPTS_DIR, "tsparse")


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS_DIR, "common")


def parse(ts, source: str, name: str = "sample.ts"):
    return ts.parse_source(source, Path(name))


# --------------------------------------------------------------------------- #
# Comments are located by character offset
# --------------------------------------------------------------------------- #

def test_an_explained_empty_catch_is_low_and_a_silent_one_high(tmp_path):
    root = project(tmp_path, {"src/a.ts": """\
export function f(g: () => void): void {
  try { g(); } catch { /* best effort: the cache is rebuilt on the next read */ }
  try { g(); } catch {}
}
"""})
    found = records("find_exception_issues.py", root, "empty_catch")
    assert sorted((r["line"], r["severity"]) for r in found) == [(2, "low"), (3, "high")]


def test_an_explained_empty_function_is_not_reported(tmp_path):
    root = project(tmp_path, {"src/a.ts": """\
export function quiet(): void {
  // deliberately a no-op: the platform has nothing to release
}
export function silent(): void {}
"""})
    found = records("find_code_smells.py", root, "empty_function_body")
    assert [r["description"] for r in found] == ["`silent` has an empty body"]


# --------------------------------------------------------------------------- #
# Shared test scaffolding is test code
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("rel", [
    "src/testkit/harness.tsx", "src/test-utils/render.tsx", "src/testutils/render.ts",
    "src/test-helpers/build.ts", "src/__mocks__/api.ts", "src/__fixtures__/rows.ts",
    "src/test-setup.ts", "src/setupTests.ts", "vitest.setup.ts", "jest.setup.js",
])
def test_scaffolding_is_test_code(common, tmp_path, rel):
    root = project(tmp_path / "repo", {rel: "export {};\n"})
    assert common.is_test_file(root / rel)


@pytest.mark.parametrize("rel", [
    "src/setup.ts", "src/kit/harness.ts", "src/testing.ts", "src/utils/test.ts", "src/setupTestsLater.ts",
])
def test_production_code_near_those_names_is_not(common, tmp_path, rel):
    root = project(tmp_path / "repo", {rel: "export {};\n"})
    assert not common.is_test_file(root / rel)


def test_a_checkout_under_a_testkit_directory_is_still_production(common, tmp_path):
    """The new directory names obey the old rule: matched below the project root only."""
    root = project(tmp_path / "testkit" / "repo", {"src/app.ts": "export {};\n"})
    assert not common.is_test_file(root / "src" / "app.ts")


def test_as_any_in_a_testkit_is_low_and_in_production_high(tmp_path):
    root = project(tmp_path, {
        "src/testkit/harness.tsx": 'import { vi } from "vitest";\n'
                                   "export const stub = (x: unknown) => (x as any).mockReturnValue(1);\n",
        "src/app.ts": "export const read = (x: unknown) => (x as any).value;\n",
    })
    found = records("find_type_gaps.py", root, "as_any")
    assert sorted((Path(r["file"]).name, r["severity"]) for r in found) == [
        ("app.ts", "high"), ("harness.tsx", "low"),
    ]


