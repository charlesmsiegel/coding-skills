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

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-simplifier" / "scripts"


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
