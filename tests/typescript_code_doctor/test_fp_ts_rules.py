"""False positives removed from the TypeScript doctor's rule-level detectors.

Each rule here was written against a real frontend's report, where a detector
charged a defect to code that had none: a `describe` block measured as one long
function, `toBeNull()` called a weak assertion, a `useEffect` callback's
optional cleanup called an inconsistent return, a `confirm()` that gates a
delete filed as a debug leftover. Every test that pins a removed false positive
sits next to one that pins the true positive the same rule must keep catching —
a fix that quietened both would pass the first test alone.

Fixtures are written to tmp_path at runtime, as in test_detectors.py.
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"


def run_detector(script: str, target: Path, *extra: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json", *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[-800:]}"
    return json.loads(result.stdout)


def write(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def records(script: str, tmp_path: Path, files: dict[str, str], smell: str) -> list[dict]:
    """The records of one smell type for one fixture tree."""
    root = write(tmp_path / "proj", files)
    return [r for r in run_detector(script, root) if r["smell_type"] == smell]


def lines_of(found: list[dict]) -> list[int]:
    return sorted(r["line"] for r in found)


def numbered(template: str, count: int, indent: str = "  ") -> str:
    return "\n".join(indent + template.format(i=i) for i in range(count))


# --------------------------------------------------------------------------- #
# analyze_complexity: one complexity finding per function; length not charged
# to a describe block or to the wrapper around a long function
# --------------------------------------------------------------------------- #

BOTH_METRICS = """\
export function both(a: number[]): number {
  let n = 0;
  for (const x of a) {
    if (x > 0) {
%s
    }
  }
  return n;
}
""" % numbered("if (x === {i}) n += 1;", 20, "      ")

COGNITIVE_ONLY = """\
export function deep(a: number): number {
  if (a) { if (a > 1) { if (a > 2) { if (a > 3) { if (a > 4) { if (a > 5) { if (a > 6) { return 1; } } } } } } }
  return 0;
}
"""


def test_a_function_over_both_complexity_thresholds_is_reported_once(tmp_path):
    root = write(tmp_path / "proj", {"both.ts": BOTH_METRICS})
    found = [r for r in run_detector("analyze_complexity.py", root)
             if r["smell_type"] in ("high_cyclomatic_complexity", "high_cognitive_complexity")]
    assert len(found) == 1, found
    record = found[0]
    assert record["smell_type"] == "high_cyclomatic_complexity"
    assert record["severity"] == "high"
    assert "cyclomatic complexity 23" in record["description"]
    assert "cognitive complexity" in record["description"]


def test_cognitive_complexity_alone_is_still_reported(tmp_path):
    found = records("analyze_complexity.py", tmp_path, {"deep.ts": COGNITIVE_ONLY},
                    "high_cognitive_complexity")
    assert lines_of(found) == [1]


def _suite(head: str) -> str:
    tests = numbered('it("t{i}", () => {{ expect({i}).toBe({i}); }});', 70)
    return f'{head}("suite", () => {{\n{tests}\n}});\n'


def test_a_describe_callback_is_not_a_long_function(tmp_path):
    source = (
        'import { describe, it, expect, test } from "vitest";\n'
        + _suite("describe")
        + _suite("describe.each([1, 2])")
        + _suite("suite")
    )
    found = records("analyze_complexity.py", tmp_path, {"a.test.ts": source}, "long_function")
    assert found == []


def test_a_long_test_callback_is_still_a_long_function(tmp_path):
    body = numbered("expect({i}).toBe({i});", 70)
    source = f'import {{ test, expect }} from "vitest";\ntest("long", () => {{\n{body}\n}});\n'
    found = records("analyze_complexity.py", tmp_path, {"a.test.ts": source}, "long_function")
    assert lines_of(found) == [2]


def test_a_wrapper_around_one_long_function_is_charged_once(tmp_path):
    steps = numbered("await step({i});", 70, "      ")
    source = f"""\
export function useLoader(step: (n: number) => Promise<void>): void {{
  useEffect(() => {{
    void (async () => {{
{steps}
    }})();
  }}, []);
}}
"""
    found = records("analyze_complexity.py", tmp_path, {"loader.ts": source}, "long_function")
    assert lines_of(found) == [3], "only the async body, which holds the lines, is long"


def test_a_long_function_with_long_handlers_inside_is_still_long(tmp_path):
    own = numbered("const v{i} = {i};", 70)
    handler = numbered("total += {i};", 65, "    ")
    source = f"""\
export function Component(): number {{
{own}
  let total = 0;
  const onClick = () => {{
{handler}
  }};
  onClick();
  return total;
}}
"""
    found = records("analyze_complexity.py", tmp_path, {"comp.ts": source}, "long_function")
    assert lines_of(found) == [1, 73]
    outer = next(r for r in found if r["line"] == 1)
    assert "outside the long functions nested in it" in outer["description"]


# --------------------------------------------------------------------------- #
# find_type_gaps: one `any` token, one finding
# --------------------------------------------------------------------------- #

CATCH_ANY = """\
export function load(read: () => string): string {
  try {
    return read();
  } catch (err: any) {
    return String(err.detail);
  }
}
export function walk(node: any): number { return node.children.length; }
"""


def test_a_catch_binding_any_is_catch_typed_any_only(tmp_path):
    root = write(tmp_path / "proj", {"load.ts": CATCH_ANY})
    found = run_detector("find_type_gaps.py", root)
    by_type = {}
    for record in found:
        by_type.setdefault(record["smell_type"], []).append(record["line"])
    assert by_type.get("catch_typed_any") == [4]
    assert by_type.get("explicit_any") == [8], "the parameter `any` is still explicit_any"
