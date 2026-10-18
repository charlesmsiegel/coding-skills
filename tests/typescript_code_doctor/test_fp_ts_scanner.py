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
# Type arguments: the commas inside `<...>` do not separate anything
# --------------------------------------------------------------------------- #

def test_a_generic_parameter_type_is_one_parameter(ts):
    file = parse(ts, """\
export function f(a: Record<string, number>, b: Map<string, Array<number>>,
                  c = new Map<string, number>(), d = x < y, e = 2): void {}
""")
    (func,) = file.functions
    assert [(p.name, p.type_text) for p in func.params] == [
        ("a", "Record<string, number>"), ("b", "Map<string, Array<number>>"),
        ("c", ""), ("d", ""), ("e", ""),
    ]


def test_a_generic_return_type_keeps_the_function_body(ts):
    """`): Record<string, number> {` used to stop the return type at the comma,
    so the `{` after it was never reached and the function had no body at all
    — its complexity, length and contents went unmeasured."""
    file = parse(ts, "export function h(x: number): Record<string, number> | null {\n  return null;\n}\n")
    (func,) = file.functions
    assert func.return_type == "Record<string, number> | null"
    assert func.has_body


def test_a_generic_member_type_is_one_member(ts):
    file = parse(ts, "interface I { fragments: Record<string, unknown>; ready: boolean }\n"
                     "class K { cache: Map<string, number> = new Map(); size = 2 }\n")
    assert [m.name for m in file.types[0].members] == ["fragments", "ready"]
    assert [(p.name, p.type_text, p.initializer) for p in file.classes[0].props] == [
        ("cache", "Map<string, number>", "new Map()"), ("size", "", "2"),
    ]


def test_untyped_parameter_ignores_a_generic_annotation_and_keeps_a_missing_one(tmp_path):
    root = project(tmp_path, {"src/a.ts": """\
export function counts(table: Record<string, number>): number { return Object.keys(table).length; }
export function loose(input, table: Record<string, number>): number { return table[input]; }
"""})
    found = records("find_type_gaps.py", root, "untyped_parameter")
    assert [(r["line"], r["description"]) for r in found] == [
        (2, "Exported function `loose` takes `input` with no type"),
    ]


def test_long_parameter_list_counts_parameters_not_type_arguments(tmp_path):
    root = project(tmp_path, {"src/a.ts": """\
export function three(a: Map<string, number>, b: Record<string, Array<number>>, c: Set<string>): void {}
export function five(a: string, b: string, c: string, d: string, e: Map<string, number>): void {}
"""})
    found = records("analyze_complexity.py", root, "long_parameter_list")
    assert [r["description"] for r in found] == ["five takes 5 parameters"]


# --------------------------------------------------------------------------- #
# Template substitutions are code
# --------------------------------------------------------------------------- #

def test_template_substitutions_are_tokenized_on_the_side(ts):
    """The literal stays one token; the code inside `${...}` is read by a
    nested scanner, which also finds the closing `}` past a regex holding one."""
    file = parse(ts, "const s = `a ${seg(id)} ${/}/.test(x) ? `in ${deep}` : ''} b`;\nconst after = 1;\n")
    templates = [i for i, t in enumerate(file.tokens) if t.kind == "template"]
    assert len(templates) == 1
    assert any(t.is_name("after") for t in file.tokens)
    inner = [t.value for t in file.substitutions[templates[0]] if t.kind == "name"]
    assert inner[:2] == ["seg", "id"] and "deep" in inner
    assert {"seg", "id", "deep", "after"} <= {t.value for t in file.names()}


def test_an_import_read_only_inside_a_template_is_used(tmp_path):
    root = project(tmp_path, {
        "src/b.ts": "export function seg(s: string): string { return s; }\n"
                    "export function other(): number { return 1; }\n",
        "src/a.ts": 'import { seg, other } from "./b";\n'
                    "export const path = `/x/${seg(\"id\")}`;\n",
    })
    found = records("find_dead_code.py", root, "unused_import")
    assert [r["description"] for r in found] == ["`other` is imported from './b' and never used"]


def test_a_dotted_jsx_tag_reads_its_head_name(tmp_path):
    """`<Ctx.Provider>` was one token, so the import binding `Ctx` looked unused."""
    root = project(tmp_path, {"src/a.tsx": """\
import React from "react";
import Unused from "./unused";
export const App = () => <React.StrictMode><p>hi</p></React.StrictMode>;
"""})
    found = records("find_dead_code.py", root, "unused_import")
    assert [r["description"] for r in found] == ["`Unused` is imported from './unused' and never used"]


def test_options_read_in_a_template_are_read(tmp_path):
    root = project(tmp_path, {"src/a.ts": """\
export function label(options: { prefix: string }): string { return `${options.prefix}:`; }
export function ignore(options: { prefix: string }): string { return "x"; }
"""})
    found = records("find_ai_scaffolding.py", root, "ignored_options_parameter")
    assert [r["line"] for r in found] == [2]


def test_a_private_member_read_in_a_template_is_referenced(tmp_path):
    root = project(tmp_path, {"src/a.ts": """\
export class Badge {
  private label = "x";
  private unused = 1;
  show(): string { return `[${this.label}]`; }
}
"""})
    found = records("find_dead_code.py", root, "unused_private_member")
    assert [r["description"] for r in found] == [
        "`Badge.unused` is private and referenced nowhere in the class",
    ]


# --------------------------------------------------------------------------- #
# Comments are located by character offset
# --------------------------------------------------------------------------- #

def test_an_explained_empty_catch_is_omitted_and_a_silent_one_high(tmp_path):
    root = project(tmp_path, {"src/a.ts": """\
export function f(g: () => void): void {
  try { g(); } catch { /* best effort: the cache is rebuilt on the next read */ }
  try { g(); } catch {}
}
"""})
    found = records("find_exception_issues.py", root, "empty_catch")
    assert sorted((r["line"], r["severity"]) for r in found) == [(3, "high")]


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
# A JSX attribute name is neither a write nor a read
# --------------------------------------------------------------------------- #

def test_passing_a_parameter_as_a_jsx_attribute_is_not_reassigning_it(tmp_path):
    root = project(tmp_path, {"src/a.tsx": """\
export function Show(err: string, onDone: () => void) {
  return <Note err={err} onDone={onDone} />;
}
export function Retry(err: string) {
  err = err.trim();
  return <Note err={err} />;
}
"""})
    found = records("find_mutation_hazards.py", root, "parameter_reassigned")
    assert [r["line"] for r in found] == [5]


def test_a_jsx_attribute_sink_is_still_seen(tmp_path):
    """Attribute names stay `name` tokens, so a sink named by one still fires."""
    root = project(tmp_path, {"src/a.tsx": """\
export const Raw = (props: { html: string }) => <div dangerouslySetInnerHTML={{ __html: props.html }} />;
"""})
    assert records("find_security_issues.py", root, "html_injection_sink")


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


# --------------------------------------------------------------------------- #
# Statements end where JavaScript ends them
# --------------------------------------------------------------------------- #

def unreachable(tmp_path: Path, source: str, name: str = "src/a.ts") -> list[int]:
    root = project(tmp_path, {name: source})
    return [r["line"] for r in records("find_dead_code.py", root, "unreachable_code")]


def test_a_hoisted_async_function_after_a_throw_is_reachable(tmp_path):
    assert unreachable(tmp_path, """\
export async function g(ok: boolean): Promise<boolean> {
  if (ok) return moved();
  throw new Error("no");
  async function moved(): Promise<boolean> { return true; }
}
""") == []


def test_a_statement_after_a_throw_is_still_unreachable(tmp_path):
    assert unreachable(tmp_path, """\
export function g(): number {
  throw new Error("no");
  const x = 1;
  return x;
}
""") == [3]


def test_a_return_continued_after_as_is_one_statement(tmp_path):
    assert unreachable(tmp_path, """\
export function h(x: number | null): Record<string, number> | null {
  if (!x) return null;
  return Object.fromEntries([["a", x]]) as
    Record<string, number>;
}
""") == []


def test_a_multiline_jsx_element_in_a_returned_ternary_is_one_statement(tmp_path):
    assert unreachable(tmp_path, """\
export function Tile(props: { on: boolean; label: string }) {
  return props.on ? <Card label={props.label} kind="tile"
    selected={props.on} />
    : <button className="tile">
      {props.label}
    </button>;
}
""", "src/a.tsx") == []


def test_code_without_semicolons_ends_each_statement_at_its_line(tmp_path):
    """`throw new Error("x")` followed by a newline and `}` used to run on past
    the `}` — the scan stepped over the call's parentheses without looking at
    the break behind them — and the next function was called unreachable."""
    assert unreachable(tmp_path, """\
export function f(x: number) {
  if (x) {
    throw new Error("x")
  }
  if (!x) {
    return
  }
  if (x > 1) { return x }
  return g(x)
}
export function g(x: number) {
  return x
}
""") == []


def test_code_without_semicolons_still_has_unreachable_statements(tmp_path):
    assert unreachable(tmp_path, """\
export function f(x: number) {
  throw new Error("x")
  console.log(x)
}
""") == [3]


def test_class_fields_without_semicolons_are_each_a_member(ts):
    file = parse(ts, "class A {\n  x: number\n  y: string\n  constructor() { this.x = 1; this.y = '' }\n}\n")
    assert [p.name for p in file.classes[0].props] == ["x", "y"]
    assert [m.name for m in file.classes[0].methods] == ["constructor"]


# --------------------------------------------------------------------------- #
# `!`: a non-null assertion, or a definite-assignment assertion
# --------------------------------------------------------------------------- #

def test_non_null_assertion_is_low_in_a_test_and_medium_in_production(tmp_path):
    root = project(tmp_path, {
        "src/a.test.ts": "export const first = (m: Map<string, number>) => m.get('a')!;\n",
        "src/a.ts": "export const first = (m: Map<string, number>) => m.get('a')!;\n",
    })
    found = records("find_type_gaps.py", root, "non_null_assertion")
    assert sorted((Path(r["file"]).name, r["severity"]) for r in found) == [
        ("a.test.ts", "low"), ("a.ts", "medium"),
    ]


def test_a_definite_assignment_is_not_a_non_null_assertion(tmp_path):
    root = project(tmp_path, {
        "src/a.test.ts": """\
export function capture(): Promise<void> {
  let finish!: () => void, fail!: (e: unknown) => void;
  const done = new Promise<void>((resolve, reject) => { finish = resolve; fail = reject; });
  finish();
  return done;
}
""",
        "src/a.ts": """\
export class View {
  host!: HTMLElement;
  pick(on: boolean, a: string | null, b: string): string { return on ? a! : b; }
}
""",
    })
    found = run_detector("find_type_gaps.py", root)
    non_null = [(Path(r["file"]).name, r["line"]) for r in found if r["smell_type"] == "non_null_assertion"]
    assert non_null == [("a.ts", 3)], "the ternary's `a!` is a non-null assertion; the declarations are not"
    definite = [r for r in found if r["smell_type"] == "definite_assignment_assertion"]
    assert [(Path(r["file"]).name, r["line"], r["kind"]) for r in definite] == [("a.ts", 2, "candidate")]
    assert definite[0]["also_caused_by"] and not definite[0]["suggestion"]


# --------------------------------------------------------------------------- #
# An inline `type` specifier is already a type-only import
# --------------------------------------------------------------------------- #

def test_inline_type_specifiers_are_recorded(ts):
    file = parse(ts, 'import { a, type B, d as e } from "x";\n')
    (record,) = file.imports
    assert record.names == ["a", "B", "e"] and record.type_names == ["B"]


def test_missing_import_type_skips_an_inline_type_specifier(tmp_path):
    # Under verbatimModuleSyntax, where a value-form import of a type is the
    # thing that survives into the bundle and the rule applies at all.
    root = project(tmp_path, {
        "tsconfig.json": '{ "compilerOptions": { "verbatimModuleSyntax": true } }',
        "src/client.ts": "export interface Entry { id: string }\nexport interface Row { id: string }\n"
                         "export function api(e: { id: string }): string { return e.id; }\n",
        "src/a.ts": 'import { api, type Entry, Row } from "./client";\n'
                    "export function f(e: Entry, r: Row): string { return api(e) + r.id; }\n",
    })
    found = records("find_module_issues.py", root, "missing_import_type")
    assert [r["description"] for r in found] == ["`Row` from './client' is only used as a type"]


@pytest.mark.parametrize("source", [
    "const x = ready ? convert(value) : {};",
    "const [counts] = useState<Record<string, number>>(() => (seed ? stored(seed) : {}));",
    "const row = <Row {...(ready ? props(value) : {})} />;",
    "const result = ready ? load() : Promise.resolve(fixture<Awaited<ReturnType<typeof api.getX>>>({meta:{}}));",
    "const x = ready ? value : {};",
])
def test_rejected_method_probes_do_not_mark_ternary_values_as_types(ts, tmp_path, source):
    file = parse(ts, source, "sample.tsx")
    empty_objects = [i for i, token in enumerate(file.tokens)
                     if token.is_op("{") and file.closer(i) == i + 1]
    assert empty_objects
    assert not any(file.in_type_position(i) for i in empty_objects)
    root = project(tmp_path, {"src/sample.tsx": source})
    assert records("find_type_gaps.py", root, "empty_object_type") == []


@pytest.mark.parametrize("source, expected", [
    ("type Empty = {};", 1),
    ("const value: {} = {};", 1),
    ("function accept(value: {}): {} { return {}; }", 2),
    ("const methods = { make(): {} { return {}; } };", 1),
    ("const methods = { make<T>(): Promise<{}> { return load(); } };", 1),
])
def test_confirmed_empty_object_types_keep_their_scored_findings(ts, tmp_path, source, expected):
    file = parse(ts, source)
    empty_types = [i for i, token in enumerate(file.tokens)
                   if token.is_op("{") and file.closer(i) == i + 1 and file.in_type_position(i)]
    assert len(empty_types) == expected
    root = project(tmp_path, {"src/sample.ts": source})
    found = records("find_type_gaps.py", root, "empty_object_type")
    assert len(found) == expected
    assert all(r["kind"] == "finding" and r["suggestion"] for r in found)
