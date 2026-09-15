"""Smoke tests: every detector fires on a known-bad fixture and stays quiet on good code.

Fixtures are written to tmp_path at runtime rather than committed, so the
deliberately-bad TypeScript never trips the repo's own tooling.

The negative cases matter at least as much as the positive ones. A detector that
fires on correct code is worse than no detector: it trains people to skip the
output, and the real bug goes out with it.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"


def run_detector(script: str, target: Path, *extra: str) -> list[dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), str(target), "--format", "json", *extra],
        # Detectors warn on stderr through the console encoding, which is cp1252
        # on Windows — decode leniently so a warning cannot fail an assertion
        # about stdout, which is always JSON.
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    assert result.returncode == 0, f"{script} exited {result.returncode}: {result.stderr[-800:]}"
    return json.loads(result.stdout)


def smells(findings: list[dict]) -> set[str]:
    return {f["smell_type"] for f in findings}


def write(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# Fixtures: one bad file and one good file per detector family
# --------------------------------------------------------------------------- #

BAD_TYPES = """\
export function handle(payload: any): any {
  const user = payload as unknown as { id: string };
  const name = user.id!;
  // @ts-ignore
  return name.missingMethod();
}
export interface Loose { a?: string; b?: number; c?: boolean; d?: Date }
export function untyped(input): string { return String(input); }
export function boxed(fn: Function, o: Object): void { fn(o); }
"""

GOOD_TYPES = """\
export interface Payload { readonly id: string }

function isPayload(value: unknown): value is Payload {
  return typeof value === "object" && value !== null
    && typeof (value as Record<string, unknown>).id === "string";
}

export function handle(payload: unknown): string {
  if (!isPayload(payload)) throw new Error("unexpected payload");
  return payload.id.toUpperCase();
}
"""

BAD_ASYNC = """\
export async function save(id: string): Promise<void> {
  await fetch(`/api/${id}`, { method: "POST" });
}

export async function run(ids: string[]): Promise<void> {
  ids.forEach(async (id) => { await save(id); });
  const ready = ids.filter(async (id) => Boolean(id));
  save(ids[0]);
  await new Promise(async (resolve) => { resolve(ready); });
  for (const id of ids) { await save(id); }
  fetch("/ping").catch(() => {});
}
"""

GOOD_ASYNC = """\
export async function save(id: string): Promise<void> {
  await fetch(`/api/${id}`, { method: "POST" });
}

export async function run(ids: string[]): Promise<void> {
  await Promise.all(ids.map(save));
}

export function fireAndForget(id: string): void {
  void save(id).catch((error: unknown) => console.error(error));
}
"""

BAD_ERRORS = """\
export function load(): string | null {
  try {
    return read();
  } catch (err) {
    console.log(err);
  }
  try {
    return read();
  } catch {
  }
  try {
    return read();
  } catch (err) {
    throw new Error("read failed");
  } finally {
    return null;
  }
}
export function bad(): never { throw "nope"; }
function read(): string { return "x"; }
"""

GOOD_ERRORS = """\
export function load(): string {
  try {
    return read();
  } catch (err) {
    throw new Error("could not read the config", { cause: err });
  }
}
function read(): string { return "x"; }
"""

BAD_ENCAPSULATION = """\
export let currentUser: string | null = null;

export class Basket {
  public total = 0;
  private readonly seen: string[] = [];
  private label: string;

  constructor(label: string) { this.label = label; }

  getSeen(): string[] { return this.seen; }
  get name(): string { return this.label; }
  set name(v: string) { this.label = v; }
  reorder(other: string[]): void { other.sort(); }
}
"""

GOOD_ENCAPSULATION = """\
export class Basket {
  private readonly items: string[] = [];

  constructor(private readonly label: string) {}

  add(item: string): void { this.items.push(item); }
  list(): readonly string[] { return [...this.items]; }
  get name(): string { return this.label.trim().toUpperCase(); }
}
"""

BAD_MUTATION = """\
import { defaults } from "./config";

export const DEFAULT_TAGS = ["a", "b"];

export function apply(options: { retries: number }, items: string[]): void {
  options.retries = 3;
  defaults.timeout = 10;
  Object.assign(options, { retries: 4 });
  for (const item of items) { items.push(item); }
}
"""

GOOD_MUTATION = """\
export const DEFAULT_TAGS = ["a", "b"] as const;

export function apply(options: { retries: number }, items: readonly string[]): {
  retries: number;
} {
  const extended = [...items, "extra"];
  return { ...options, retries: extended.length };
}
"""

BAD_SECURITY = """\
import { exec } from "child_process";
import { createHash } from "crypto";

const apiKey = "sk-live-9f8a7b6c5d4e3f2a1b";

export function run(input: string, el: HTMLElement): void {
  eval(input);
  exec(`rm -rf ${input}`);
  el.innerHTML = input;
  createHash("md5").update(input).digest("hex");
  window.postMessage({ input }, "*");
}
"""

GOOD_SECURITY = """\
import { execFile } from "child_process";
import { createHash } from "crypto";

const apiKey = process.env.API_KEY ?? "";

export function run(input: string, el: HTMLElement): void {
  execFile("rm", ["-rf", input]);
  el.textContent = input;
  createHash("sha256").update(input + apiKey).digest("hex");
  window.postMessage({ input }, "https://example.test");
}
"""

BAD_LEAKS = """\
export function start(): void {
  setInterval(() => poll(), 1000);
  window.addEventListener("resize", onResize);
}
function poll(): void {}
function onResize(): void {}
"""

GOOD_LEAKS = """\
export function start(): () => void {
  const timer = setInterval(() => poll(), 1000);
  window.addEventListener("resize", onResize);
  return () => {
    clearInterval(timer);
    window.removeEventListener("resize", onResize);
  };
}
function poll(): void {}
function onResize(): void {}
"""

BAD_IDIOMS = """\
const legacy = require("legacy");
export namespace Utils { export const x = 1; }

export function shout(input: string | null): string {
  var out = "";
  const value = <string>(input as unknown);
  const safe = input && input.length;
  const count = safe || 0;
  if (legacy.hasOwnProperty(value)) out += value;
  return out;
}
"""

GOOD_IDIOMS = """\
import legacy from "legacy";

export function shout(input: string | null): string {
  const length = input?.length ?? 0;
  return Object.hasOwn(legacy, String(length)) ? String(length) : "";
}
"""

BAD_LOOPS = """\
export function collect(xs: string[]): string[] {
  const out: string[] = [];
  for (let i = 0; i < xs.length; i++) {
    out.push(xs[i].trim());
  }
  const hits = xs.filter((x) => x.length > 0).length > 0;
  const first = xs.filter((x) => x.length > 0)[0];
  return hits && first ? out : [];
}
"""

GOOD_LOOPS = """\
export function collect(xs: readonly string[]): string[] {
  const out = xs.map((x) => x.trim());
  return xs.some((x) => x.length > 0) ? out : [];
}
"""

BAD_DESIGN = """\
export function describe(node: { kind: string; value: unknown }): string {
  if (node.kind === "text") return "text";
  if (node.kind === "list") return "list";
  if (node.kind === "table") return "table";
  return "?";
}

export function move(from: string, to: string, label: string, note: string): void {
  void from; void to; void label; void note;
}

export function copy(from: string, to: string, label: string): void {
  void from; void to; void label;
}

export function link(from: string, to: string, label: string): void {
  void from; void to; void label;
}

export function toggle(id: string, force: boolean): void { void id; void force; }
"""

GOOD_DESIGN = """\
type Node =
  | { kind: "text"; value: string }
  | { kind: "list"; items: string[] };

function assertNever(value: never): never {
  throw new Error(`unhandled: ${JSON.stringify(value)}`);
}

export function describe(node: Node): string {
  switch (node.kind) {
    case "text": return node.value;
    case "list": return node.items.join(", ");
    default: return assertNever(node);
  }
}
"""

BAD_SCAFFOLDING = """\
export function load(id: string): string {
  throw new Error("Not implemented");
}

export function render(template: string, options: { pretty?: boolean }): string {
  return template;
}

export function load(id: string): string { return id; }
"""

BAD_TESTS = """\
import { describe, it, expect } from "vitest";

describe.only("thing", () => {
  it("does something", () => {
    const result = compute();
    void result;
  });

  it("rejects", () => {
    expect(compute()).rejects.toThrow();
  });

  it("is defined", () => {
    expect(compute()).toBeDefined();
  });
});

function compute(): unknown { return 1; }
"""

GOOD_TESTS = """\
import { describe, it, expect } from "vitest";

describe("thing", () => {
  it("returns the computed total", () => {
    expect(compute()).toEqual(3);
  });

  it("rejects an empty input", async () => {
    await expect(failing()).rejects.toThrow("empty");
  });
});

function compute(): number { return 3; }
async function failing(): Promise<never> { throw new Error("empty"); }
"""

BAD_COMPLEXITY = """\
export function grade(a: number, b: number, c: number, d: number, e: number, f: number): string {
  if (a > 0) {
    if (b > 0) {
      if (c > 0) {
        if (d > 0) {
          if (e > 0 && f > 0) {
            return "all";
          }
        }
      }
    }
  }
  return a > 0 ? (b > 0 ? "two" : "one") : "none";
}
"""

BAD_NAMING = """\
export interface IUser { active: boolean }
export class user_repo { _cache = 1 }
const name = "shadowed";
export default user_repo;
"""

BAD_COMMENTS = """\
// const disabled = compute();
// if (disabled) {
//   return disabled;
// }
// TODO: handle the empty case
/**
 * @param {string} id the identifier
 */
export function load(id: string): string { return id; }
"""

BAD_DEBUG = """\
/* eslint-disable */
export function go(x: number): number {
  debugger;
  console.log("here", x);
  alert("hi");
  return x;
}
"""


# --------------------------------------------------------------------------- #
# One case per detector: fires on the bad fixture, silent on the good one
# --------------------------------------------------------------------------- #

CASES = [
    ("find_type_gaps.py", BAD_TYPES, GOOD_TYPES,
     {"explicit_any", "double_assertion", "non_null_assertion", "ts_ignore",
      "all_optional_type", "untyped_parameter", "unsafe_builtin_type"}),
    ("find_async_issues.py", BAD_ASYNC, GOOD_ASYNC,
     {"async_callback_in_foreach", "async_callback_in_predicate", "floating_promise",
      "async_promise_executor", "await_in_loop", "swallowed_rejection"}),
    ("find_exception_issues.py", BAD_ERRORS, GOOD_ERRORS,
     {"catch_logs_and_continues", "empty_catch", "rethrow_without_cause",
      "throw_non_error", "control_flow_in_finally"}),
    ("find_encapsulation_issues.py", BAD_ENCAPSULATION, GOOD_ENCAPSULATION,
     {"exported_mutable_binding", "public_mutable_field", "exposes_internal_collection",
      "pass_through_accessors", "mutates_parameter"}),
    ("find_mutation_hazards.py", BAD_MUTATION, GOOD_MUTATION,
     {"mutates_argument_property", "mutates_imported_object", "mutation_during_iteration",
      "mutable_exported_constant", "object_assign_onto_argument"}),
    ("find_security_issues.py", BAD_SECURITY, GOOD_SECURITY,
     {"dynamic_code_execution", "shell_injection_risk", "html_injection_sink",
      "weak_hash_algorithm", "hardcoded_secret", "postmessage_wildcard_origin"}),
    ("find_resource_leaks.py", BAD_LEAKS, GOOD_LEAKS, {"unreleased_resource"}),
    ("find_outdated_idioms.py", BAD_IDIOMS, GOOD_IDIOMS,
     {"commonjs_require", "typescript_namespace", "angle_bracket_cast",
      "manual_optional_chaining", "falsy_default_with_or", "legacy_has_own_property"}),
    ("find_loop_simplifications.py", BAD_LOOPS, GOOD_LOOPS,
     {"index_loop_over_array", "filter_length_instead_of_some", "filter_first_instead_of_find"}),
    ("find_design_smells.py", BAD_DESIGN, GOOD_DESIGN,
     {"type_switch", "data_clump", "primitive_obsession", "boolean_flag_parameter"}),
    ("find_test_smells.py", BAD_TESTS, GOOD_TESTS,
     {"focused_test", "test_without_assertion", "unawaited_async_assertion", "weak_assertion_only"}),
]


@pytest.mark.parametrize("script,bad,good,expected", CASES,
                         ids=[case[0].removesuffix(".py") for case in CASES])
def test_detector_fires_on_bad_and_is_quiet_on_good(tmp_path, script, bad, good, expected):
    suffix = ".test.ts" if script == "find_test_smells.py" else ".ts"
    bad_root = write(tmp_path / "bad", {f"sample{suffix}": bad})
    found = smells(run_detector(script, bad_root))
    missing = expected - found
    assert not missing, f"{script} missed {sorted(missing)}; it found {sorted(found)}"

    good_root = write(tmp_path / "good", {f"sample{suffix}": good})
    noise = smells(run_detector(script, good_root))
    assert not noise, f"{script} fired on clean code: {sorted(noise)}"


def test_complexity_measures_nesting_and_arity(tmp_path):
    root = write(tmp_path, {"deep.ts": BAD_COMPLEXITY})
    found = smells(run_detector("analyze_complexity.py", root))
    assert {"deep_nesting", "long_parameter_list"} <= found


def test_code_smells(tmp_path):
    root = write(tmp_path, {"smelly.ts": """\
export function check(a: unknown, b: unknown): string {
  var flag = a == b;
  switch (String(a)) {
    case "x": return "x";
  }
  const label = flag ? (a ? "both" : "one") : "none";
  return label + String(parseInt(String(b)));
}
"""})
    found = smells(run_detector("find_code_smells.py", root))
    assert {"loose_equality", "var_declaration", "switch_without_default",
            "nested_ternary", "parseint_without_radix"} <= found


def test_naming_comments_and_debug_detectors(tmp_path):
    root = write(tmp_path, {"names.ts": BAD_NAMING, "notes.ts": BAD_COMMENTS, "dbg.ts": BAD_DEBUG})
    assert {"hungarian_interface_prefix", "non_pascal_case_type", "underscore_without_private",
            "shadows_browser_global"} <= smells(run_detector("find_naming_issues.py", root))
    assert {"commented_out_code", "todo_marker", "jsdoc_repeats_types"} \
        <= smells(run_detector("find_comment_smells.py", root))
    assert {"debugger_statement", "console_leftover", "browser_dialog",
            "file_wide_lint_suppression"} <= smells(run_detector("find_debug_leftovers.py", root))


def test_ai_scaffolding_finds_stubs_and_duplicate_definitions(tmp_path):
    root = write(tmp_path, {"stub.ts": BAD_SCAFFOLDING})
    found = smells(run_detector("find_ai_scaffolding.py", root))
    assert {"not_implemented_stub", "ignored_options_parameter", "duplicate_definition"} <= found


def test_dead_code_finds_unreachable_and_unused(tmp_path):
    root = write(tmp_path, {
        "main.ts": "import { used } from './lib';\nexport const app = used();\n",
        "lib.ts": """\
import { unusedImport } from "./other";

export function used(): string {
  return "value";
  const dead = 1;
}

export const neverImported = 2;
""",
        "other.ts": "export const unusedImport = 1;\n",
    })
    found = smells(run_detector("find_dead_code.py", root))
    assert {"unreachable_code", "unused_import", "unused_export"} <= found


def test_guard_clause_is_not_unreachable_code(tmp_path):
    """`if (done) break;` leaves the next line perfectly reachable."""
    root = write(tmp_path, {"loop.ts": """\
export function drain(items: string[]): string[] {
  const out: string[] = [];
  for (const item of items) {
    if (!item) continue;
    out.push(item);
  }
  return out;
}
"""})
    assert "unreachable_code" not in smells(run_detector("find_dead_code.py", root))


def test_module_issues_finds_cycles_and_barrels(tmp_path):
    root = write(tmp_path, {
        "a.ts": "import { b } from './b';\nexport const a = () => b();\n",
        "b.ts": "import { a } from './a';\nexport const b = () => a();\n",
        "index.ts": "export * from './a';\nexport * from './b';\nexport { a as alias } from './a';\n",
        "deep/nested/leaf.ts": "import { a } from '../../../deep/nested/../../a';\nexport const leaf = a;\n",
    })
    found = smells(run_detector("find_module_issues.py", root))
    assert {"import_cycle", "barrel_file", "deep_relative_import"} <= found


def test_overengineering_needs_the_whole_tree(tmp_path):
    root = write(tmp_path, {
        "port.ts": "export interface Store { save(k: string): void }\n",
        "impl.ts": "import type { Store } from './port';\nexport class MemoryStore implements Store { save(k: string): void { void k; } }\n",
        "statics.ts": "export class MathUtil { static add(a: number, b: number): number { return a + b; } }\n",
    })
    found = smells(run_detector("find_overengineering.py", root))
    assert {"single_implementation_interface", "all_static_class"} <= found


def test_dependency_reconciliation(tmp_path):
    write(tmp_path, {
        "package.json": json.dumps({
            "name": "probe",
            "dependencies": {"declared-but-unused": "^1.0.0", "wild": "*"},
        }, indent=2),
        "package-lock.json": "{}",
        "src/app.ts": "import { thing } from 'undeclared-package';\nimport { readFile } from 'node:fs';\nexport const x = [thing, readFile];\n",
    })
    found = smells(run_detector("find_dependency_issues.py", tmp_path))
    assert {"missing_dependency", "unused_dependency", "unpinned_dependency"} <= found
    descriptions = " ".join(f["description"] for f in run_detector("find_dependency_issues.py", tmp_path))
    assert "node:fs" not in descriptions and "'fs'" not in descriptions, "a Node builtin was treated as a package"


def test_tsconfig_audit(tmp_path):
    write(tmp_path, {
        "tsconfig.json": '{\n  // a comment, and a trailing comma\n  "compilerOptions": {\n    "strict": false,\n    "target": "es5",\n  },\n}\n',
        "src/a.ts": "export const a = 1;\n",
    })
    found = smells(run_detector("find_tsconfig_issues.py", tmp_path))
    assert "strict_mode_off" in found
    assert "outdated_target" in found


# --------------------------------------------------------------------------- #
# Candidates: leads the syntax cannot prove, with the benign readings attached
# --------------------------------------------------------------------------- #

def _candidates(records, smell):
    return [r for r in records if r["smell_type"] == smell]


@pytest.mark.parametrize("script, source, smell", [
    ("find_type_gaps.py",
     "export function port(raw: string): number { return (JSON.parse(raw) as { port: number }).port; }\n",
     "type_assertion"),
    ("find_type_gaps.py",
     "export function total(xs: number[]) { return xs.reduce((a, b) => a + b, 0); }\n",
     "missing_return_type"),
    ("find_type_gaps.py",
     "export interface Loose { a?: string; b?: number; c?: boolean; d?: Date }\n",
     "all_optional_type"),
    ("find_async_issues.py",
     "export async function run(ids: string[]): Promise<void> {\n  for (const id of ids) { await fetch(id); }\n}\n",
     "await_in_loop"),
    ("find_encapsulation_issues.py",
     "export class Counter { count = 0; }\n",
     "public_mutable_field"),
])
def test_file_level_heuristics_are_candidates(tmp_path, script, source, smell):
    root = write(tmp_path / smell, {"sample.ts": source})
    found = _candidates(run_detector(script, root), smell)
    assert found, f"{script} did not report {smell}"
    for record in found:
        assert record["kind"] == "candidate"
        assert record["also_caused_by"], "a candidate names how healthy code produces it"
        assert not record["suggestion"], "a candidate carries no fix"


def test_design_smells_are_candidates(tmp_path):
    root = write(tmp_path, {"sample.ts": BAD_DESIGN})
    records = run_detector("find_design_smells.py", root)
    for smell in ("data_clump", "primitive_obsession"):
        found = _candidates(records, smell)
        assert found and all(r["kind"] == "candidate" and r["also_caused_by"]
                             and not r["suggestion"] for r in found), smell


def test_tree_level_heuristics_are_candidates(tmp_path):
    root = write(tmp_path, {
        "package.json": '{"name": "p"}',
        "src/index.ts": "export * from './a';\nexport * from './b';\nexport { c } from './c';\n",
        "src/a.ts": "export const a = 1;\n",
        "src/b.ts": "export const b = 2;\n",
        "src/c.ts": "export const c = 3;\n",
        "src/svc.ts": "export interface Service { run(): void }\nexport class Impl implements Service { run(): void {} }\n",
    })
    barrels = _candidates(run_detector("find_module_issues.py", root), "barrel_file")
    singles = _candidates(run_detector("find_overengineering.py", root), "single_implementation_interface")
    for found in (barrels, singles):
        assert found
        assert all(r["kind"] == "candidate" and r["also_caused_by"] and not r["suggestion"]
                   for r in found)


def test_tsconfig_audit_is_quiet_on_a_strict_config(tmp_path):
    write(tmp_path, {
        "tsconfig.json": json.dumps({"compilerOptions": {
            "strict": True, "target": "es2022", "noUncheckedIndexedAccess": True,
            "noImplicitOverride": True, "exactOptionalPropertyTypes": True,
            "verbatimModuleSyntax": True, "isolatedModules": True, "noUnusedLocals": True,
        }}, indent=2),
        "src/a.ts": "export const a = 1;\n",
    })
    assert not smells(run_detector("find_tsconfig_issues.py", tmp_path))


def test_untested_modules_and_the_no_tests_alarm(tmp_path):
    root = write(tmp_path, {"src/logic.ts": "export const compute = (): number => 1;\n"})
    assert "no_tests_at_all" in smells(run_detector("find_untested_modules.py", root))

    covered = write(tmp_path / "covered", {
        "src/logic.ts": "export const compute = (): number => 1;\n",
        "src/orphan.ts": "export const orphan = (): number => 2;\n" * 60,
        "src/logic.test.ts": "import { compute } from './logic';\nit('works', () => { expect(compute()).toBe(1); });\n",
    })
    findings = run_detector("find_untested_modules.py", covered)
    untested = {Path(f["file"]).name for f in findings if f["smell_type"] == "untested_module"}
    assert "orphan.ts" in untested and "logic.ts" not in untested


def test_duplicates_finds_repeated_blocks_and_type_shapes(tmp_path):
    block = """\
export function {name}(rows: Row[]): Total {{
  const filtered = rows.filter((row) => row.active && row.amount > 0);
  const scaled = filtered.map((row) => ({{ ...row, amount: row.amount * 1.2 }}));
  const total = scaled.reduce((sum, row) => sum + row.amount, 0);
  const label = total > 100 ? "large" : "small";
  return {{ total, label, count: scaled.length }};
}}
"""
    root = write(tmp_path, {
        "types.ts": "export interface Row { id: string; amount: number; active: boolean }\n"
                    "export interface Line { id: string; amount: number; active: boolean }\n",
        "a.ts": "import type { Row, Total } from './types';\n" + block.format(name="alpha"),
        "b.ts": "import type { Row, Total } from './types';\n" + block.format(name="beta"),
    })
    found = smells(run_detector("find_duplicates.py", root))
    assert {"duplicate_block", "duplicate_type_shape"} <= found


def test_coupling_finds_feature_envy(tmp_path):
    root = write(tmp_path, {"envy.ts": """\
export class Report {
  private title = "t";

  render(order: { customer: { name: string; city: string; zip: string } }): string {
    const name = order.customer.name;
    const city = order.customer.city;
    const zip = order.customer.zip;
    const upper = order.customer.name.toUpperCase();
    return `${name} ${city} ${zip} ${upper} ${this.title}`;
  }
}
"""})
    assert "feature_envy" in smells(run_detector("find_coupling_issues.py", root))


def test_unparseable_file_is_named_not_reported_clean(tmp_path):
    write(tmp_path, {"broken.ts": "export function f() { return 1;\n"})
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_code_smells.py"), str(tmp_path), "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == []
    assert "broken.ts" in result.stderr, "a file that could not be tokenized was silently dropped"


def test_ignore_suppresses_a_finding_type(tmp_path):
    root = write(tmp_path, {"a.ts": "export const x = 1 == 1;\nvar y = 2;\n"})
    assert "loose_equality" in smells(run_detector("find_code_smells.py", root))
    assert "loose_equality" not in smells(run_detector("find_code_smells.py", root, "--ignore", "loose_equality"))


def test_test_files_get_a_lighter_standard(tmp_path):
    """A cast that installs a mock is not a claim about the product's types."""
    source = "export const m = (api.get as any).mockReturnValue(1);\n"
    product = write(tmp_path / "prod", {"a.ts": source})
    test = write(tmp_path / "spec", {"a.test.ts": source})
    assert any(f["severity"] == "high" for f in run_detector("find_type_gaps.py", product))
    assert all(f["severity"] == "low" for f in run_detector("find_type_gaps.py", test))


def test_an_imported_type_annotation_is_not_a_mutated_import(tmp_path):
    """`const x: api.Foo = {…}` reads token-for-token like `api.Foo = …`.

    The imported name is the *type* of the declaration; the `=` initializes `x`,
    which is local. Flagging it said an import was being mutated at exactly the
    place a codebase is being careful about its types — and the second file here
    proves the fix did not simply switch the detector off: a real assignment
    onto an imported object is still reported.
    """
    root = write(tmp_path / "src", {
        "annotated.ts": (
            'import * as api from "./api";\n'
            "\n"
            "export function make(): api.Config {\n"
            "  const config: api.Config = { retries: 1 };\n"
            "  return config;\n"
            "}\n"
        ),
        "mutated.ts": (
            'import { defaults } from "./api";\n'
            "\n"
            "export function bump(): void {\n"
            "  defaults.retries = 2;\n"
            "}\n"
        ),
    })

    reported = [f for f in run_detector("find_mutation_hazards.py", root)
                if f["smell_type"] == "mutates_imported_object"]

    assert [Path(f["file"]).name for f in reported] == ["mutated.ts"]


def test_marker_words_inside_prose_are_not_todo_markers(tmp_path):
    """`todo` as the name of a command, or a word in a sentence, is vocabulary.
    A marker sits at the start of the comment or is tagged with `:`/`(`."""
    root = write(tmp_path, {"notes.ts": """\
// the key of a default run: `evals todo` then `evals run`
// the retry hack above is what the old client did; keep it until v2 ships
/* the grammar accepts \\uXXXX anywhere a basic character may appear */
export const a = 1;
// TODO: handle the empty case
// FIXME(alice) negative inputs
/*
 * XXX this is fragile
 * see TODO: retry budget
 */
export const b = 2;
"""})
    lines = sorted(f["line"] for f in run_detector("find_comment_smells.py", root)
                   if f["smell_type"] == "todo_marker")

    assert lines == [5, 6, 8, 9]


@pytest.mark.parametrize("name", ["App.tsx", "worker.mts", "legacy.cts"])
def test_no_tsconfig_is_reported_for_any_typescript_extension(tmp_path, name):
    """The `no_tsconfig` gate used to glob `*.ts` only, so a TSX-only repo with
    no tsconfig was reported clean on the check the guide says to answer first."""
    root = write(tmp_path, {f"src/{name}": "export const a = 1;\n"})
    assert "no_tsconfig" in smells(run_detector("find_tsconfig_issues.py", root))


def test_the_eleventh_tsconfig_is_audited(tmp_path):
    """Discovery used to keep the first ten configs and drop the rest silently."""
    files = {"src/a.ts": "export const a = 1;\n",
             "tsconfig.json": json.dumps({"compilerOptions": {"strict": True, "target": "es2022"}})}
    for n in range(10):
        files[f"packages/p{n:02d}/tsconfig.json"] = json.dumps(
            {"compilerOptions": {"strict": True, "target": "es2022"}})
        files[f"packages/p{n:02d}/index.ts"] = "export const x = 1;\n"
    files["packages/zz/tsconfig.json"] = json.dumps({"compilerOptions": {"strict": False}})
    files["packages/zz/index.ts"] = "export const z = 1;\n"
    records = run_detector("find_tsconfig_issues.py", write(tmp_path, files))
    assert any(r["smell_type"] == "strict_mode_off" and r["file"].endswith("zz/tsconfig.json")
               for r in records)


def test_the_sixth_manifest_is_reconciled(tmp_path):
    files = {}
    for n in range(5):
        files[f"packages/p{n}/package.json"] = json.dumps({"name": f"p{n}", "dependencies": {}})
        files[f"packages/p{n}/index.ts"] = "export const x = 1;\n"
    files["packages/zz/package.json"] = json.dumps({"name": "zz", "dependencies": {"left-pad": "*"}})
    files["packages/zz/index.ts"] = "export const z = 1;\n"
    records = run_detector("find_dependency_issues.py", write(tmp_path, files))
    assert any(r["smell_type"] == "unused_dependency" and "left-pad" in r["description"]
               for r in records)


def test_workspace_lockfile_is_checked_once_at_the_root(tmp_path):
    """A workspace's lockfile lives only at the root by design — auditing
    every sub-package's directory for one used to flag each as missing it."""
    root = write(tmp_path, {
        "package.json": json.dumps({"name": "root", "workspaces": ["packages/*"]}),
        "package-lock.json": "{}",
        "packages/a/package.json": json.dumps({"name": "a", "dependencies": {}}),
        "packages/a/index.ts": "export const a = 1;\n",
        "packages/b/package.json": json.dumps({"name": "b", "dependencies": {}}),
        "packages/b/index.ts": "export const b = 1;\n",
    })
    records = run_detector("find_dependency_issues.py", root)
    assert not [r for r in records if r["smell_type"] == "no_lockfile"]


def test_a_root_with_no_lockfile_is_still_reported_exactly_once(tmp_path):
    root = write(tmp_path, {
        "package.json": json.dumps({"name": "root", "workspaces": ["packages/*"]}),
        "packages/a/package.json": json.dumps({"name": "a", "dependencies": {}}),
        "packages/a/index.ts": "export const a = 1;\n",
        "packages/b/package.json": json.dumps({"name": "b", "dependencies": {}}),
        "packages/b/index.ts": "export const b = 1;\n",
    })
    records = run_detector("find_dependency_issues.py", root)
    assert len([r for r in records if r["smell_type"] == "no_lockfile"]) == 1


def test_a_leftover_is_reported_in_a_repo_cloned_under_tests(tmp_path):
    root = write(tmp_path / "tests" / "repo", {
        "package.json": '{"name": "repo"}',
        "src/app.ts": "export function f() { console.log('debug'); return 1; }\n",
    })
    assert "console_leftover" in smells(run_detector("find_debug_leftovers.py", root))


def test_a_d_mts_declaration_is_not_scanned_as_implementation(tmp_path):
    """`.mts` is a supported source extension, so `foo.d.mts` was collected and
    then fed to every code detector as if it were a module with a body."""
    root = write(tmp_path, {
        "package.json": '{"name": "p"}',
        "src/shapes.d.mts": "export declare function shape(x: any): any;\n",
        "src/main.ts": "export const m = 1;\n",
    })
    records = run_detector("find_type_gaps.py", root)
    assert not any(r["file"].endswith("shapes.d.mts") for r in records)
    dead = run_detector("find_dead_code.py", root)
    assert not any(r["file"].endswith("shapes.d.mts") for r in dead)


def test_generated_files_are_skipped_and_counted(tmp_path):
    root = write(tmp_path, {
        "package.json": '{"name": "p"}',
        "src/api.ts": "// @generated by a tool\nexport function f(x: any): any { return x; }\n",
        "src/main.ts": "export function g(x: any): any { return x; }\n",
    })
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_type_gaps.py"), str(root), "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    records = json.loads(result.stdout)
    assert all(r["file"].endswith("main.ts") for r in records) and records
    assert "1 generated file(s) skipped" in result.stderr


# --------------------------------------------------------------------------- #
# The CR-review lens: a candidate is a lead, not a ranked defect
# --------------------------------------------------------------------------- #

def _git_init(root: Path) -> None:
    for args in (["init", "-q"], ["config", "user.email", "t@t.co"],
                 ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"],
                 ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=120)


def test_the_diff_lens_marks_a_candidate_as_a_lead_not_a_ranked_defect(tmp_path):
    """An `as` cast on a changed line is a candidate, and the lens must say so.

    A severity icon on a lead reads as a verdict the syntax never proved, and a
    bare arrow reads as a fix that was forgotten. What the record has instead is
    the benign readings the reader must rule out first.
    """
    root = write(tmp_path / "repo", {
        "package.json": '{"name": "p"}',
        "src/main.ts": "export const base = 1;\n",
    })
    _git_init(root)
    (root / "src" / "main.ts").write_text(
        "export const base = 1;\n"
        "export function port(raw: string): number {\n"
        "  return (JSON.parse(raw) as { port: number }).port;\n"
        "}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], capture_output=True, timeout=120)

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "analyze_diff.py"), "HEAD"],
        cwd=root, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600)
    assert result.returncode == 0, result.stderr[-800:]
    output = result.stdout
    assert "type_assertion" in output, "the fixture must reach the renderer"

    assert "[CANDIDATE]" in output
    assert "? also caused by:" in output
    marked = [line for line in output.splitlines() if "[CANDIDATE]" in line]
    assert marked and not any(icon in line for line in marked for icon in "🔴🟡🟢"), \
        "a candidate was given a severity icon it did not earn"
    assert [line for line in output.splitlines() if line.strip() == "→"] == [], \
        "a candidate rendered a fix arrow with nothing after it"
    assert "candidate(s)" in output, "the count line still calls every record a finding"


# --------------------------------------------------------------------------- #
# Dependency reconciliation is scoped to a file's own manifest chain
# --------------------------------------------------------------------------- #

def test_a_siblings_declaration_does_not_satisfy_this_workspaces_import(tmp_path):
    """A package declared in workspace b is not installed for workspace a.

    Unioning every manifest in the tree made b's declaration cover a's import,
    so the clean-install break in a went unreported *and* b's now-orphaned
    declaration looked used. Both halves are wrong, and they hide each other.
    """
    root = write(tmp_path, {
        "package.json": json.dumps({"name": "root", "workspaces": ["packages/*"]}),
        "package-lock.json": "{}",
        "packages/a/package.json": json.dumps({"name": "a", "dependencies": {}}),
        "packages/a/index.ts": "import pad from 'left-pad';\nexport const a = pad;\n",
        "packages/b/package.json": json.dumps({"name": "b", "dependencies": {"left-pad": "^1.0.0"}}),
        "packages/b/index.ts": "export const b = 1;\n",
    })
    records = run_detector("find_dependency_issues.py", root)

    missing = [r for r in records
               if r["smell_type"] == "missing_dependency" and "left-pad" in r["description"]]
    assert missing, "a's import was covered by a sibling workspace's declaration"
    assert Path(missing[0]["file"]).as_posix().endswith("packages/a/index.ts")

    unused = [r for r in records
              if r["smell_type"] == "unused_dependency" and "left-pad" in r["description"]]
    assert unused, "b's declaration was kept alive by a sibling workspace's import"
    assert Path(unused[0]["file"]).as_posix().endswith("packages/b/package.json")


def test_a_root_declaration_covers_a_workspace_below_it(tmp_path):
    """Hoisted root declarations really are installed for every workspace, so a
    file's chain is its nearest manifest *and every ancestor above it*."""
    root = write(tmp_path, {
        "package.json": json.dumps({"name": "root", "workspaces": ["packages/*"],
                                    "dependencies": {"left-pad": "^1.0.0"}}),
        "package-lock.json": "{}",
        "packages/a/package.json": json.dumps({"name": "a", "dependencies": {}}),
        "packages/a/index.ts": "import pad from 'left-pad';\nexport const a = pad;\n",
    })
    records = run_detector("find_dependency_issues.py", root)

    assert not [r for r in records
                if r["smell_type"] in ("missing_dependency", "unused_dependency")
                and "left-pad" in r["description"]]


# --------------------------------------------------------------------------- #
# A generated file is not a finding location, but it is still evidence
# --------------------------------------------------------------------------- #

def test_a_package_imported_only_by_a_generated_client_is_not_unused(tmp_path):
    """Dropping generated files from the project dropped their imports too, so a
    runtime package the generated API client needs looked declared-for-nothing.
    Acting on that "unused" finding breaks the next build."""
    root = write(tmp_path, {
        "package.json": json.dumps({"name": "p", "dependencies": {"axios": "^1.0.0"}}),
        "package-lock.json": "{}",
        "src/client.ts": "// @generated by openapi-generator\n"
                         "import axios from 'axios';\nexport const get = axios.get;\n",
        "src/main.ts": "export const m = 1;\n",
    })
    records = run_detector("find_dependency_issues.py", root)

    assert not [r for r in records
                if r["smell_type"] == "unused_dependency" and "axios" in r["description"]]


def test_a_module_reexported_by_a_generated_barrel_is_not_unreferenced(tmp_path):
    """The generated barrel is the only thing importing a.ts. Dropping the
    barrel from the tree made a.ts look orphaned — and nothing may be reported
    against the barrel itself, which no one may edit."""
    root = write(tmp_path, {
        "package.json": '{"name": "p"}',
        "src/index.ts": "// @generated DO NOT EDIT\nexport * from './a';\n",
        "src/a.ts": "export function a(): number { return 1; }\n",
    })
    records = run_detector("find_dead_code.py", root)

    assert not [r for r in records
                if r["smell_type"] == "unreferenced_module"
                and Path(r["file"]).as_posix().endswith("src/a.ts")]
    assert not [r for r in records if Path(r["file"]).as_posix().endswith("src/index.ts")], \
        "a finding was located in a file a tool owns"


def test_an_import_cycle_through_a_generated_file_is_reported_on_an_editable_one(tmp_path):
    """The cycle is real evidence and must still be found — but anchoring it on
    the generated member points the reader at a file they may not edit."""
    root = write(tmp_path, {
        "package.json": '{"name": "p"}',
        # Named so the generated file is the one the cycle walk reaches first.
        "src/a_gen.ts": "// @generated DO NOT EDIT\n"
                        "import { z } from './z';\nexport const g = z;\n",
        "src/z.ts": "import { g } from './a_gen';\nexport const z = () => g;\n",
    })
    records = run_detector("find_module_issues.py", root)

    cycles = [r for r in records if r["smell_type"] == "import_cycle"]
    assert cycles, "the cycle through the generated file was lost"
    assert all(not Path(r["file"]).as_posix().endswith("src/a_gen.ts") for r in cycles), \
        "a cycle was anchored on a file a tool owns"


def test_a_generated_test_still_counts_as_coverage_evidence(tmp_path):
    """Generated files are never finding locations, but a generated test still
    exercises what it imports. Dropping it from `Project.tests` made a project
    whose tests are all generated look untested — the high-severity
    `no_tests_at_all` alarm, on a project with a test for every module."""
    root = write(tmp_path, {
        "package.json": '{"name": "p"}',
        "src/work.ts": "export function work(): number { return 1; }\n",
        "tests/work.test.ts": "// @generated by contract-tests\n"
                              "import { work } from '../src/work';\nwork();\n",
    })
    found = smells(run_detector("find_untested_modules.py", root))

    assert "no_tests_at_all" not in found
    assert "untested_module" not in found


# --------------------------------------------------------------------------- #
# Fixture projects under test directories are not workspaces
# --------------------------------------------------------------------------- #

def test_a_fixture_manifest_under_tests_is_not_reconciled(tmp_path):
    """`tests/fixtures/app/package.json` is a self-contained project a test
    points a tool at; its `"bad-fixture": "*"` is the point of the fixture, not
    a defect of this repo. It is neither a workspace to report against nor a
    consumer whose imports the root manifest has to declare."""
    root = write(tmp_path, {
        "package.json": json.dumps({"name": "root", "dependencies": {"left-pad": "1.3.0"}}),
        "package-lock.json": "{}",
        "src/main.ts": "import pad from 'left-pad';\nexport const m = pad;\n",
        "tests/fixtures/app/package.json": json.dumps(
            {"name": "fixture", "dependencies": {"bad-fixture": "*"}}),
        "tests/fixtures/app/src/x.ts": "import bad from 'bad-fixture';\nimport y from 'not-declared';\n"
                                       "export const x = [bad, y];\n",
    })
    records = run_detector("find_dependency_issues.py", root)

    on_fixture = [r for r in records if "tests/fixtures/app" in Path(r["file"]).as_posix()]
    assert not on_fixture, on_fixture
    assert not [r for r in records if "not-declared" in r["description"]], \
        "the fixture's import was reconciled against the root manifest"
    assert not [r for r in records if "bad-fixture" in r["description"]]


def test_a_declared_workspace_named_like_a_test_directory_is_still_reconciled(tmp_path):
    """`apps/e2e` is a real package — the root's `workspaces` says so — and its
    dependencies matter, whatever its directory is called."""
    root = write(tmp_path, {
        "package.json": json.dumps({"name": "root", "workspaces": ["apps/*"]}),
        "package-lock.json": "{}",
        "apps/e2e/package.json": json.dumps({"name": "e2e", "dependencies": {"bad-e2e": "*"}}),
        "apps/e2e/run.ts": "import bad from 'bad-e2e';\nexport const r = bad;\n",
    })
    records = run_detector("find_dependency_issues.py", root)

    unpinned = [r for r in records if r["smell_type"] == "unpinned_dependency"]
    assert unpinned and Path(unpinned[0]["file"]).as_posix().endswith("apps/e2e/package.json")


def test_a_pnpm_workspace_named_like_a_test_directory_is_still_reconciled(tmp_path):
    root = write(tmp_path, {
        "package.json": json.dumps({"name": "root"}),
        "pnpm-lock.yaml": "",
        "pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n  - '!**/node_modules/**'\n",
        "packages/e2e/package.json": json.dumps({"name": "e2e", "dependencies": {"bad-e2e": "*"}}),
        "packages/e2e/run.ts": "import bad from 'bad-e2e';\nexport const r = bad;\n",
    })
    records = run_detector("find_dependency_issues.py", root)

    unpinned = [r for r in records if r["smell_type"] == "unpinned_dependency"]
    assert unpinned and Path(unpinned[0]["file"]).as_posix().endswith("packages/e2e/package.json")


@pytest.mark.parametrize("workspaces", [["/apps/*"], "apps/*", {"packages": ["/x"]}, [42]],
                         ids=["absolute", "bare-string", "yarn-object-absolute", "non-string"])
def test_a_malformed_workspaces_field_does_not_crash_reconciliation(tmp_path, workspaces):
    """`Path.glob` raises NotImplementedError on an absolute pattern; a bare
    string is one pattern, not a list of its characters. Neither may take the
    whole detector down — the manifest is still reconciled."""
    root = write(tmp_path, {
        "package.json": json.dumps({"name": "root", "workspaces": workspaces,
                                    "dependencies": {"left-pad": "*"}}),
        "package-lock.json": "{}",
        "src/main.ts": "import pad from 'left-pad';\nexport const m = pad;\n",
    })
    records = run_detector("find_dependency_issues.py", root)

    assert "unpinned_dependency" in smells(records)


def test_a_package_imported_only_by_a_generated_file_is_missing_at_the_manifest(tmp_path):
    """A generated client's import of an undeclared package is a real clean-install
    break. The finding cannot be located in the generated file (nobody edits it),
    so it lands on the manifest that should declare the package, naming the
    generated importer as the evidence."""
    root = write(tmp_path, {
        "package.json": json.dumps({"name": "p", "dependencies": {}}),
        "package-lock.json": "{}",
        "src/client.ts": "// @generated by openapi-generator\n"
                         "import axios from 'axios';\nexport const get = axios.get;\n",
        "src/main.ts": "export const m = 1;\n",
    })
    records = run_detector("find_dependency_issues.py", root)

    missing = [r for r in records
               if r["smell_type"] == "missing_dependency" and "axios" in r["description"]]
    assert missing, "a package only a generated file imports was not reported missing"
    assert Path(missing[0]["file"]).name == "package.json"
    assert "src/client.ts" in missing[0]["description"]


def test_an_editable_importer_is_the_missing_site_even_beside_a_generated_one(tmp_path):
    root = write(tmp_path, {
        "package.json": json.dumps({"name": "p", "dependencies": {}}),
        "package-lock.json": "{}",
        "src/a-client.ts": "// @generated\nimport axios from 'axios';\nexport const a = axios;\n",
        "src/z-main.ts": "import axios from 'axios';\nexport const z = axios;\n",
    })
    records = run_detector("find_dependency_issues.py", root)

    missing = [r for r in records
               if r["smell_type"] == "missing_dependency" and "axios" in r["description"]]
    assert len(missing) == 1
    assert Path(missing[0]["file"]).name == "z-main.ts"


def test_every_standalone_sibling_package_gets_its_own_lockfile_verdict(tmp_path):
    """Two independent packages under one scanned directory are not a
    workspace; checking only the first manifest found left the second's
    missing lockfile unreported."""
    root = write(tmp_path, {
        "a/package.json": json.dumps({"name": "a", "dependencies": {}}),
        "a/package-lock.json": "{}",
        "a/index.ts": "export const a = 1;\n",
        "b/package.json": json.dumps({"name": "b", "dependencies": {}}),
        "b/index.ts": "export const b = 1;\n",
    })
    records = run_detector("find_dependency_issues.py", root)

    missing = [r for r in records if r["smell_type"] == "no_lockfile"]
    assert len(missing) == 1
    assert Path(missing[0]["file"]).as_posix().endswith("b/package.json")


def test_a_workspace_member_scanned_alone_is_covered_by_the_root_lockfile(tmp_path):
    root = write(tmp_path, {
        "package.json": json.dumps({"name": "root", "workspaces": ["packages/*"]}),
        "pnpm-lock.yaml": "",
        ".git/HEAD": "ref: refs/heads/main\n",
        "packages/a/package.json": json.dumps({"name": "a", "dependencies": {}}),
        "packages/a/index.ts": "export const a = 1;\n",
    })
    records = run_detector("find_dependency_issues.py", root / "packages" / "a")

    assert not [r for r in records if r["smell_type"] == "no_lockfile"]
