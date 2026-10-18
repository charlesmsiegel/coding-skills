"""The parser is the foundation; if it is wrong every detector is wrong quietly.

These pin the cases a naive regex scanner gets wrong — regex literals against
division, template interpolation, JSX text containing apostrophes and `<` — and
the extraction decisions the detectors depend on, above all telling a function
type from a function.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "typescript-code-doctor" / "scripts"


@pytest.fixture(scope="module")
def ts():
    """Import the parser facade off the skill's scripts directory."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        for cached in ("tsparse", "tslex", "tsnodes", "tsextract", "tsmodules", "common", "tsproject"):
            sys.modules.pop(cached, None)
        import tsparse
        return tsparse
    finally:
        sys.path.remove(str(SCRIPTS_DIR))


def parse(ts, source: str, name: str = "sample.ts"):
    return ts.parse_source(source, Path(name))


# --- tokenizer ------------------------------------------------------------- #

def test_regex_literal_is_not_division(ts):
    file = parse(ts, r"const re = /a\/b[/]c/g; const half = total / 2;")
    kinds = [t.kind for t in file.tokens]
    assert "regex" in kinds
    assert sum(1 for t in file.tokens if t.is_op("/")) == 1, "the divide survived, the regex did not split"


def test_division_after_a_call_is_not_a_regex(ts):
    file = parse(ts, "const ratio = count() / total; const other = x / y;")
    assert not any(t.kind == "regex" for t in file.tokens)


def test_template_literal_keeps_nested_interpolation_together(ts):
    file = parse(ts, "const s = `a ${b} c ${`inner ${d}`} e`; const after = 1;")
    templates = [t for t in file.tokens if t.kind == "template"]
    assert len(templates) == 1
    assert templates[0].value.endswith("`")
    assert any(t.is_name("after") for t in file.tokens)


def test_apostrophe_in_jsx_text_does_not_open_a_string(ts):
    source = "export const C = () => <p>don't panic</p>;\nconst after = 1;\n"
    file = parse(ts, source, "C.tsx")
    assert any(t.is_name("after") for t in file.tokens), "the JSX text swallowed the rest of the file"
    assert any("don't" in t.value for t in file.jsx_text)


def test_comparison_operators_are_not_jsx(ts):
    file = parse(ts, "export function f(a: number, b: number) { return a < b && b > a; }", "f.tsx")
    assert file.functions[0].name == "f"


def test_unbalanced_brackets_raise_rather_than_report_nonsense(ts):
    with pytest.raises(ts.TsSyntaxError):
        parse(ts, "function f() { return 1;")


def test_a_regex_inside_a_template_substitution_is_not_a_string(ts):
    """`/"/g` inside `${...}` must not open a string that runs to the next `"`.

    The substitution is code, so a `/` after `(` there starts a regex exactly as
    it would outside the template — and a regex may contain any quote character.
    """
    source = (
        "export function cell(value: string): string {\n"
        "  return value.includes('\"') ? `\"${value.replace(/\"/g, '\"\"')}\"` : value\n"
        "}\n"
        "const after = 1;\n"
    )
    file = parse(ts, source)  # raised "unclosed '{'" before
    templates = [t for t in file.tokens if t.kind == "template"]
    assert len(templates) == 1 and templates[0].value.endswith('"`')
    assert any(t.is_name("after") for t in file.tokens)
    assert file.functions[0].name == "cell" and file.line_of(file.functions[0].body_close) == 3


def test_division_inside_a_template_substitution_is_still_division(ts):
    file = parse(ts, "const s = `${total / count} and ${a}/${b}`; const after = 1;")
    assert [t.kind for t in file.tokens].count("template") == 1
    assert any(t.is_name("after") for t in file.tokens)


def test_a_brace_inside_a_substitution_string_does_not_end_the_template(ts):
    file = parse(ts, "const s = `${ '}' + \"{\" + `${'}'}` }`; const after = 1;")
    assert [t.kind for t in file.tokens].count("template") == 1
    assert any(t.is_name("after") for t in file.tokens)


def test_line_numbers_survive_multiline_literals(ts):
    file = parse(ts, "const a = `one\ntwo\nthree`;\nconst b = 2;\n")
    binding = next(t for t in file.tokens if t.is_name("b"))
    assert binding.line == 4


# --- declarations ---------------------------------------------------------- #

def test_function_signature_is_extracted(ts):
    file = parse(ts, "export async function load(id: string, deep = false): Promise<User | null> {\n  return null;\n}\n")
    func = file.functions[0]
    assert (func.name, func.kind, func.is_async, func.is_exported) == ("load", "function", True, True)
    assert func.return_type == "Promise<User | null>"
    assert [(p.name, p.type_text, p.has_default) for p in func.params] == [
        ("id", "string", False), ("deep", "", True),
    ]
    assert func.has_body


def test_class_members_carry_their_modifiers(ts):
    source = """\
export abstract class Repo extends Base implements Reader {
  private readonly cache = new Map<string, number>();
  public name: string = 'x';
  static count = 0;
  constructor(private readonly db: Db) { super(); }
  get size(): number { return this.cache.size; }
  set size(v: number) {}
  abstract load(id: string): Promise<void>;
  private async save(): Promise<void> { await this.db.write(); }
}
"""
    file = parse(ts, source)
    klass = file.classes[0]
    assert (klass.name, klass.is_abstract, klass.extends, klass.implements) == ("Repo", True, "Base", ["Reader"])
    props = {p.name: p for p in klass.props}
    assert props["cache"].accessibility == "private" and props["cache"].readonly
    assert props["name"].accessibility == "public" and not props["name"].readonly
    assert props["count"].is_static
    methods = {(m.name, m.kind) for m in klass.methods}
    assert {("constructor", "constructor"), ("size", "getter"), ("size", "setter"),
            ("load", "method"), ("save", "method")} <= methods
    load = next(m for m in klass.methods if m.name == "load")
    assert load.is_abstract and not load.has_body
    assert next(m for m in klass.methods if m.kind == "constructor").params[0].accessibility == "private"


def test_a_comma_inside_a_generic_return_type_does_not_end_it(ts):
    """`): Record<string, number> {` — the `{` after the `>` is the body."""
    source = """\
export function counts(): Record<string, number> {
  return {}
}
class Store {
  getCache(): Map<string, Array<number>> {
    return new Map()
  }
  run(): void {}
}
"""
    file = parse(ts, source)
    counts = next(f for f in file.functions if f.name == "counts")
    assert counts.return_type == "Record<string, number>" and counts.has_body
    klass = file.classes[0]
    cache = next(m for m in klass.methods if m.name == "getCache")
    assert cache.has_body and cache.return_type == "Map<string, Array<number>>"
    assert [m.name for m in klass.methods] == ["getCache", "run"] and klass.props == []


def test_a_comma_inside_a_generic_parameter_type_does_not_split_the_parameter(ts):
    """`Map<string, number>` is one parameter's type, not two parameters."""
    source = (
        "export function g(m: Map<string, number>, pick: Pick<A, 'a' | 'b'> = {},\n"
        "  n = a < b, o = c > d ? e : f, cb: (x: Array<Set<number>>, y: T) => R): void {}\n"
        "export const h = (a: Record<string, number>, flag: boolean) => a\n"
    )
    file = parse(ts, source)
    by_name = {f.name: f for f in file.functions}
    assert [(p.name, p.type_text, p.has_default) for p in by_name["g"].params] == [
        ("m", "Map<string, number>", False), ("pick", "Pick<A, 'a' | 'b'>", True),
        ("n", "", True), ("o", "", True), ("cb", "(x: Array<Set<number>>, y: T) => R", False),
    ]
    assert [(p.name, p.type_text) for p in by_name["h"].params] == [
        ("a", "Record<string, number>"), ("flag", "boolean"),
    ]


def test_every_member_of_a_semicolon_less_interface_is_read(ts):
    """Without separators the line break ends a member; the next line is the next member."""
    source = """\
interface Props {
  a: string
  b: number
  readonly c?: boolean
  d(x: number): string
  e: Map<string, number>
  f: () => void
  g: { nested: string; other: number }
}
type Q = {
  h: string
  i: number
}
"""
    file = parse(ts, source)
    members = {t.name: [(m.name, m.type_text) for m in t.members] for t in file.types}
    assert members["Props"] == [
        ("a", "string"), ("b", "number"), ("c", "boolean"), ("d", "(x: number): string"),
        ("e", "Map<string, number>"), ("f", "() => void"), ("g", "{ nested: string; other: number }"),
    ]
    assert members["Q"] == [("h", "string"), ("i", "number")]
    props = {m.name: m for m in file.types[0].members}
    assert props["c"].readonly and props["c"].optional


def test_a_generic_member_type_ends_at_its_line(ts):
    """The `>` closing `Map<…>` ends the member; `kind` on the next line is not part of it."""
    file = parse(ts, "interface Props {\n  grouped: Map<string, number[]>;\n  kind: string | null;\n}\n")
    members = {m.name: m.type_text for m in file.types[0].members}
    assert members == {"grouped": "Map<string, number[]>", "kind": "string | null"}


def test_a_function_type_is_not_a_function(ts):
    """`(e: E) => void` in a type position must not enter the function inventory."""
    source = """\
export type Handler = (event: string, extra?: number) => Promise<void>;
interface Props { onClose: () => void; title: string }
const cb: (n: number) => string = (n) => String(n);
export const real = (a: number) => a + 1;
"""
    file = parse(ts, source)
    names = {f.name for f in file.functions}
    assert "real" in names
    assert names.isdisjoint({"Handler", "onClose", "cb"}), f"type annotations became functions: {names}"


def test_interfaces_and_type_aliases_are_extracted(ts):
    source = """\
export interface User {
  readonly id: string;
  name?: string;
  tags: string[];
  [key: string]: unknown;
}
type Props = { title: string; count: number };
export enum Colour { Red, Green }
"""
    file = parse(ts, source)
    by_name = {t.name: t for t in file.types}
    user = by_name["User"]
    assert user.kind == "interface" and user.is_exported
    members = {m.name: m for m in user.members}
    assert members["id"].readonly and members["id"].type_text == "string"
    assert members["name"].optional
    assert "[index]" in members
    assert {m.name for m in by_name["Props"].members} == {"title", "count"}
    assert by_name["Colour"].kind == "enum"


def test_imports_and_exports_are_extracted(ts):
    source = """\
import React from 'react';
import { useState, useEffect as effect } from "react";
import type { Foo } from './types';
import './side-effect.css';
export * from './barrel';
export { a, b as c } from './other';
export const value = 1;
"""
    file = parse(ts, source)
    modules = [(i.kind, i.module) for i in file.imports]
    assert ("import", "react") in modules and ("export-from", "./barrel") in modules
    named = next(i for i in file.imports if i.names)
    assert named.names == ["useState", "effect"]
    assert next(i for i in file.imports if i.module == "./types").is_type_only
    assert next(i for i in file.imports if i.module == "./side-effect.css").side_effect_only
    exported = {(e.kind, e.name) for e in file.exports}
    assert {("star", "*"), ("named", "a"), ("named", "c"), ("declaration", "value")} <= exported


def test_import_as_an_object_key_is_not_an_import(ts):
    """`import.meta.glob(..., { import: "default" })` must not invent a module."""
    file = parse(ts, 'const R = import.meta.glob("../**/*.ts", { import: "default", eager: true });')
    assert [i.module for i in file.imports] == []


def test_object_literal_methods_are_functions(ts):
    file = parse(ts, "const api = {\n  onError(err: Error) { report(err); },\n  transform: (v: string) => v.trim(),\n};\n")
    names = {f.name for f in file.functions}
    assert {"onError", "transform"} <= names


def test_arrow_names_and_export_status(ts):
    file = parse(ts, "export const makeAdder = (base: number) => (extra: number) => base + extra;\n")
    outer = next(f for f in file.functions if f.name == "makeAdder")
    assert outer.is_exported and [p.name for p in outer.params] == ["base"]


# --- query helpers --------------------------------------------------------- #

def test_argument_spans_ignore_nested_groups(ts):
    file = parse(ts, "run(true, () => { const inner = [true, false]; }, false);")
    paren = next(i for i, t in enumerate(file.tokens) if t.is_op("("))
    spans = ts.argument_spans(file, paren)
    assert len(spans) == 3


def test_statement_end_spans_a_multiline_expression(ts):
    file = parse(ts, "function f() {\n  return items\n    .map(x => x)\n    .filter(Boolean);\n}\nconst after = 1;\n")
    ret = next(i for i, t in enumerate(file.tokens) if t.is_name("return"))
    end = ts.statement_end(file, ret + 1)
    assert file.tokens[end].is_op("}"), "the ASI heuristic cut a chained expression in half"


def _end_after_return(ts, source: str):
    file = parse(ts, source)
    ret = next(i for i, t in enumerate(file.tokens) if t.is_name("return"))
    return file, file.tokens[ts.statement_end(file, ret + 1)]


def test_statement_end_stops_at_the_brace_that_closes_its_block(ts):
    """`return {...}` as a body's last statement ends at the body's `}`, not after it."""
    file, end = _end_after_return(ts, "function f() {\n  return {\n    a: 1,\n  }\n}\nlet counter = 0\n")
    assert end.is_op("}") and end.line == 5, f"the statement ran on to {end.value!r} on line {end.line}"


def test_statement_end_stops_at_an_enclosing_brace_after_a_call(ts):
    file, end = _end_after_return(ts, "function f() {\n  if (x) {\n    return g(x)\n  }\n  return 2\n}\n")
    assert end.is_op("}") and end.line == 4


def test_a_bare_return_ends_at_the_closing_brace(ts):
    file, end = _end_after_return(ts, "function f() {\n  return\n}\nlet counter = 0\n")
    assert end.is_op("}") and end.line == 3


def test_statement_end_breaks_at_a_line_break_after_a_bracket_group(ts):
    """`return g(a)\\n  log(a)` is two statements; the second starts a new line."""
    file, end = _end_after_return(ts, "function f() {\n  return g(a)\n  log(a)\n}\n")
    assert end.is_name("log")


def test_statement_end_keeps_a_chain_that_continues_after_a_group(ts):
    file, end = _end_after_return(ts, "function f() {\n  return g(a)\n    .then(h)\n}\n")
    assert end.is_op("}")


def test_a_multiline_type_alias_does_not_swallow_what_follows_it(ts):
    """The alias ends at its closing brace; the declarations after it are still found."""
    source = """\
type Props = {
  title: string
}
export const make = (n: number) => {
  return n + 1
}
type Other = { a: string }
"""
    file = parse(ts, source)
    by_name = {t.name: t for t in file.types}
    assert file.line_of(by_name["Props"].end) == 3
    assert "Other" in by_name
    assert "make" in {f.name for f in file.functions}


def test_a_multiline_property_initializer_does_not_swallow_the_methods_after_it(ts):
    source = """\
class Store {
  options = {
    retries: 1,
  }
  run() {
    return this.options.retries
  }
}
"""
    klass = parse(ts, source).classes[0]
    assert [m.name for m in klass.methods] == ["run"]
    assert [p.name for p in klass.props] == ["options"]


def test_callback_target_names_the_call_a_function_is_passed_to(ts):
    source = """\
useEffect(() => { tick() }, [open])
describe.each(rows)('t', function () { run() })
const bare = () => { run() }
const wrapped = [() => { run() }]
const value = run(() => { other() } && x)
"""
    file = parse(ts, source)
    targets = {file.line_of(f.start): ts.callback_target(file, f) for f in file.functions}
    assert targets == {
        1: ("useEffect", 0), 2: ("describe.each", 1),
        3: ("", -1), 4: ("", -1), 5: ("", -1),
    }


def test_callee_of_reads_a_dotted_call(ts):
    file = parse(ts, "a.b.c(1);")
    paren = next(i for i, t in enumerate(file.tokens) if t.is_op("("))
    assert ts.callee_of(file, paren) == "a.b.c"
