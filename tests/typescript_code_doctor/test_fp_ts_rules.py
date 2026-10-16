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


# --------------------------------------------------------------------------- #
# find_code_smells: boolean_blindness is a signature's, charged once
# --------------------------------------------------------------------------- #

BLIND_CALLS_IN_A_TEST = """\
import { useSuggestions } from "./suggest";
test("suggests", () => {
  useSuggestions("c", true, false);
  useSuggestions("d", false, false);
  expect(spy).toHaveBeenCalledWith("c", 1, false, false);
  expect(spy).not.toHaveBeenNthCalledWith(1, "c", true, true);
  sinon.assert.calledWith(spy, true, false);
});
"""


def test_boolean_blindness_is_charged_once_per_callee_and_not_in_matchers(tmp_path):
    found = records("find_code_smells.py", tmp_path, {"suggest.test.ts": BLIND_CALLS_IN_A_TEST},
                    "boolean_blindness")
    assert lines_of(found) == [3]
    assert "2 call sites" in found[0]["description"]
    assert found[0]["related_lines"] == [4]


def test_boolean_blindness_defers_to_a_flag_parameter_declared_in_the_file(tmp_path):
    source = """\
export function runStream(id: string, retry: boolean, quiet: boolean): void { void id; void retry; void quiet; }
runStream("x", true, false);
renderer.paint(true, false);
"""
    found = records("find_code_smells.py", tmp_path, {"stream.ts": source}, "boolean_blindness")
    assert lines_of(found) == [3], "runStream's signature is boolean_flag_parameter's; paint is still blind"


# --------------------------------------------------------------------------- #
# find_design_smells: a type switch has three different arms
# --------------------------------------------------------------------------- #

TYPE_TESTS = """\
export function route(draft: { type: string }, scope: unknown, node: { kind: string }): number {
  if (draft.type === "enum") return 1;
  if (draft.type === "enum" && scope) return 2;
  if (draft.type === "enum") return 3;
  if (typeof scope === "object") return 4;
  if (typeof scope === "object" && scope) return 5;
  if (typeof scope === "object") return 6;
  if (node.kind === "a") return 7;
  else if (node.kind === "b") return 8;
  else if (node.kind === "c") return 9;
  return 0;
}
"""


def test_type_switch_needs_three_different_literals(tmp_path):
    found = records("find_design_smells.py", tmp_path, {"route.ts": TYPE_TESTS}, "type_switch")
    assert lines_of(found) == [8], "only node.kind is tested against three values"
    assert "`node.kind`" in found[0]["description"]


# --------------------------------------------------------------------------- #
# find_test_smells: helpers assert, exact matchers are exact, guards are not
# branches, and an async test with nothing to await is not a race
# --------------------------------------------------------------------------- #

TEST_SUITE = """\
import { it, expect } from "vitest";
function streamAndCompare(input: string): void {
  for (const c of input) expect(c).toBe(c);
}
function deeper(input: string): void { streamAndCompare(input); }
function onlyRenders(): void { mount("<div />"); }
it("helper asserts", () => { streamAndCompare("ab"); });
it("helper chain asserts", () => { deeper("ab"); });
it("helper only renders", () => { onlyRenders(); });
it("null is exact", () => { expect(parse("x")).toBeNull(); });
it("undefined is exact", () => { expect(parse("x")).toBeUndefined(); });
it("getBy throws", () => { expect(screen.getByText("x")).toBeTruthy(); });
it("truthy header", () => { expect(init.headers["X"]).toBeTruthy(); });
it("async sync body", async () => { expect(compute()).toBe(3); });
it("async then", async () => { load().then((r) => expect(r).toBe(1)); });
it("async waitFor", async () => { waitFor(() => expect(x).toBe(1)); });
it("async awaited", async () => { await waitFor(() => expect(x).toBe(1)); });
it("guard", () => {
  const n = find();
  if (n.kind !== "y") throw new Error("fixture");
  expect(n.v).toBe(1);
});
it("guard block", () => {
  const n = find();
  if (!n) { throw new Error("fixture"); }
  expect(n.v).toBe(1);
});
it("real logic", () => {
  const n = find();
  if (n) expect(n.v).toBe(1);
});
it("guard with else", () => {
  if (!n) { throw new Error("fixture"); } else { expect(n.v).toBe(1); }
});
it("finally restore", () => {
  try { expect(f()).toBe(1); } finally { if (saved) restore(saved); }
});
"""


def _test_smells(tmp_path: Path) -> dict[str, list[dict]]:
    root = write(tmp_path / "proj", {"suite.test.ts": TEST_SUITE})
    by_type: dict[str, list[dict]] = {}
    for record in run_detector("find_test_smells.py", root):
        by_type.setdefault(record["smell_type"], []).append(record)
    return by_type


def test_an_assertion_in_a_same_file_helper_counts(tmp_path):
    found = _test_smells(tmp_path).get("test_without_assertion", [])
    assert lines_of(found) == [9], "only the test whose helper merely renders asserts nothing"


def test_exact_matchers_and_throwing_queries_are_not_weak(tmp_path):
    found = _test_smells(tmp_path).get("weak_assertion_only", [])
    assert lines_of(found) == [13], "toBeNull/toBeUndefined are exact; getBy* throws when absent"


def test_an_async_test_is_charged_as_a_race_only_when_a_promise_is_left_behind(tmp_path):
    found = {r["line"]: r for r in _test_smells(tmp_path).get("async_test_never_awaits", [])}
    assert sorted(found) == [14, 15, 16]
    assert found[14]["severity"] == "low", "a synchronous body: the async is redundant, not a race"
    assert found[15]["severity"] == "medium" and ".then(" in found[15]["description"]
    assert found[16]["severity"] == "medium" and "waitFor" in found[16]["description"]


def test_a_throw_guard_and_a_finally_restore_are_not_logic_in_a_test(tmp_path):
    found = _test_smells(tmp_path).get("logic_in_test", [])
    assert lines_of(found) == [30, 33], "a guard with an else, and an if around an expect, still branch"


# --------------------------------------------------------------------------- #
# find_code_smells: effect cleanups, separate-expression ternaries, switches
# the compiler already checks, and self-describing numbers
# --------------------------------------------------------------------------- #

EFFECTS = """\
export function Ticker({ live }: { live: boolean }) {
  useEffect(() => {
    if (!live) return;
    const id = setInterval(tick, 10);
    return () => clearInterval(id);
  }, [live]);
  React.useLayoutEffect(() => {
    if (!live) return;
    return () => undefined;
  });
  const flush = async () => {
    if (!live) return;
    return void await save();
  };
  void flush;
  return null;
}
export function pick(live: boolean) {
  if (!live) return;
  return 5;
}
"""


def test_an_effect_callback_may_return_a_cleanup_on_some_paths(tmp_path):
    found = records("find_code_smells.py", tmp_path, {"ticker.tsx": EFFECTS}, "inconsistent_returns")
    assert lines_of(found) == [19], "only `pick` really returns a value on one path and nothing on another"


TERNARIES = """\
export function View({ x, items, editing, saving, y, a, b }: Props) {
  const m = x ? items.map((i) => i.a ? 1 : 2) : [];
  const n = x ? items.map((i) => { return i.a ? 1 : 2; }) : [];
  const j = editing ? <><b>{saving ? "Saving" : "Save"}</b></> : <i>no</i>;
  const k = x ? <A p={y ? 1 : 2} /> : null;
  const real = a ? (b ? 1 : 2) : 3;
  const bare = a ? b ? 1 : 2 : 3;
  const obj = a ? { v: b ? 1 : 2 } : null;
  const sibling = a ? f((i) => i, b ? 1 : 2) : null;
  return [m, n, j, k, real, bare, obj, sibling];
}
"""


def test_a_ternary_in_a_callback_or_jsx_container_is_not_nested(tmp_path):
    found = records("find_code_smells.py", tmp_path, {"view.tsx": TERNARIES}, "nested_ternary")
    assert lines_of(found) == [6, 7, 8, 9]


SWITCHES = """\
type Scope = "world" | "campaign";
export function path(scope: Scope): string {
  switch (scope) {
    case "world":
      return "/w";
    case "campaign": {
      return "/c";
    }
  }
}
export const shape = (scope: Scope): { v: number } => {
  switch (scope) {
    case "world":
    case "campaign":
      return { v: 1 };
  }
};
export function notTail(scope: Scope): string {
  switch (scope) {
    case "world": return "x";
  }
  return "y";
}
export function maybe(scope: Scope): string | undefined {
  switch (scope) {
    case "world": return "x";
  }
}
"""


def test_a_tail_switch_the_compiler_checks_is_not_reported(tmp_path):
    files = {"tsconfig.json": '{ "compilerOptions": { "strict": true } }', "paths.ts": SWITCHES}
    found = records("find_code_smells.py", tmp_path, files, "switch_without_default")
    assert lines_of(found) == [19, 25]
    assert all(r["kind"] == "candidate" and r["also_caused_by"] for r in found)


def test_without_strict_null_checks_a_tail_switch_is_still_a_lead(tmp_path):
    files = {"tsconfig.json": '{ "compilerOptions": { "strict": false } }', "paths.ts": SWITCHES}
    found = records("find_code_smells.py", tmp_path, files, "switch_without_default")
    assert lines_of(found) == [3, 12, 19, 25]


MAGIC = """\
export function classify(err: { status: number }, bytes: number, step: number): string {
  if (err.status === 404 || err.status === 410) return "gone";
  if (err.status >= 500 && bytes > 0) return "server";
  const mb = bytes / 1_048_576 + 0;
  if (step === 4 || step > 9) return "last";
  return String(mb * 37 + 1);
}
"""


def test_status_codes_and_unit_constants_are_not_magic(tmp_path):
    found = records("find_code_smells.py", tmp_path, {"classify.ts": MAGIC}, "magic_number")
    assert lines_of(found) == [5, 6], "`step === 4` and `* 37` still need names"


# --------------------------------------------------------------------------- #
# find_async_issues: a rejection turned into a value is handled; an explained or
# shared one is a lead; a two-argument then handles; a test stub may be async
# --------------------------------------------------------------------------- #

REJECTIONS = """\
export async function recover(api: Api, settling: Promise<void>): Promise<string> {
  const live = await api.find().catch(() => null);
  if (!live) throw new Error("gone");
  chain.current = task.catch(() => undefined);
  void p.catch(() => {}).then(() => next());
  queue(api.load().catch(() => null), 1);
  const later = () => api.find().catch(() => null);
  // A budget is optional: a failed read leaves the bar undrawn.
  api.budget().then(setBudget).catch(() => {});
  await settling.catch(() => {});
  api.cast().then(setCast).catch(() => {});
  await reload().catch(() => {});
  return String(live) + String(later);
}
"""


def test_a_rejection_turned_into_a_value_is_not_swallowed(tmp_path):
    found = records("find_async_issues.py", tmp_path, {"recover.ts": REJECTIONS}, "swallowed_rejection")
    findings = [r["line"] for r in found if r["kind"] == "finding"]
    candidates = [r["line"] for r in found if r["kind"] == "candidate"]
    assert findings == [11, 12], "a discarded `.catch(() => {})` on a promise made here is still swallowed"
    assert candidates == [9, 10], "a commented optional read and a shared promise are leads"


def test_a_two_argument_then_handles_the_rejection(tmp_path):
    source = """\
export async function save(): Promise<void> { await fetch("/x"); }
export function run(onOk: () => void, onErr: (e: unknown) => void): void {
  const pending = save();
  pending.then(onOk, onErr);
  pending.then(onOk);
}
"""
    found = records("find_async_issues.py", tmp_path, {"run.ts": source}, "floating_promise")
    assert lines_of(found) == [5]


def test_an_empty_async_stub_in_a_test_is_not_async_without_await(tmp_path):
    source = """\
import { it, expect } from "vitest";
const props = { onSaved: async () => {} };
async function slowly(): Promise<number> { return 1; }
it("renders", async () => { await render(props); expect(await slowly()).toBe(1); });
"""
    found = records("find_async_issues.py", tmp_path, {"panel.test.ts": source}, "async_without_await")
    assert lines_of(found) == [3], "a named async helper with a body and no await is still reported"


# --------------------------------------------------------------------------- #
# find_exception_issues: a catch that records the failure is handling it
# --------------------------------------------------------------------------- #

CATCHES = """\
export async function sweep(api: Api, ids: string[]): Promise<string> {
  let undiscoverable = false;
  let failed = 0;
  let message = "";
  try { await api.find(); } catch { undiscoverable = true; }
  for (const id of ids) {
    try { await api.localize(id); } catch { failed += 1; }
  }
  try { await api.refresh(); } catch (err: unknown) { message = `failed: ${describe(err)}`; }
  try { await api.reload(); } catch { setError("reload failed"); }
  try { await api.select(); } catch { this.stale = true; }
  let unused = 0;
  try { await api.ping(); } catch { unused = 1; }
  try { await api.ping(); } catch { resetForm(); }
  return `${undiscoverable} ${failed} ${message}`;
}
"""


def test_a_catch_that_records_the_failure_is_not_swallowed(tmp_path):
    found = records("find_exception_issues.py", tmp_path, {"sweep.ts": CATCHES}, "swallowed_error")
    assert lines_of(found) == [13, 14], "a write nothing reads, or a reset, still loses the failure"
