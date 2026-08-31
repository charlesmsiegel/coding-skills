"""The parser is the foundation; if it is wrong every detector is wrong quietly.

These pin the cases a naive regex scanner gets wrong — raw strings with hashes,
block comments that nest, the `'` that is a char in one place and a lifetime in
another, `1..2` against `1.5` — and the extraction decisions the detectors
depend on, above all attaching a method to the right `impl`.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "rust-code-doctor" / "scripts"


@pytest.fixture(scope="module")
def rs():
    """Import the parser facade off the skill's scripts directory."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        for cached in ("rsparse", "rustlex", "rustnodes", "rustextract", "common", "rsproject"):
            sys.modules.pop(cached, None)
        import rsparse
        return rsparse
    finally:
        sys.path.remove(str(SCRIPTS_DIR))


def parse(rs, source: str, name: str = "sample.rs"):
    return rs.parse_source(source, Path(name))


# --- tokenizer ------------------------------------------------------------- #

def test_block_comments_nest(rs):
    file = parse(rs, "/* outer /* inner */ still a comment */ fn after() {}")
    assert [f.name for f in file.functions] == ["after"]
    assert len(file.comments) == 1


def test_raw_string_with_hashes_swallows_quotes(rs):
    file = parse(rs, 'fn f() { let s = r##"a "# inside"##; let after = 1; }')
    strings = [t for t in file.tokens if t.kind == "str"]
    assert len(strings) == 1 and strings[0].value.endswith('"##')
    assert any(t.is_name("after") for t in file.tokens), "the raw string ate the rest of the file"


def test_byte_and_c_string_prefixes(rs):
    file = parse(rs, 'fn f() { let a = b"x"; let b = br#"y"#; let c = c"z"; }')
    assert len([t for t in file.tokens if t.kind == "str"]) == 3


def test_identifiers_beginning_with_a_prefix_letter_are_not_strings(rs):
    file = parse(rs, "fn f() { let rc = 1; let bytes = 2; let cfg = 3; }")
    assert not [t for t in file.tokens if t.kind == "str"]


def test_lifetime_is_not_a_char_literal(rs):
    file = parse(rs, "fn f<'a>(x: &'a str) -> &'a str { x }")
    lifetimes = [t.value for t in file.tokens if t.kind == "lifetime"]
    assert lifetimes == ["'a", "'a", "'a"]
    assert not [t for t in file.tokens if t.kind == "char"]


def test_char_literals_including_escaped_quote(rs):
    file = parse(rs, r"""fn f() { let a = '\''; let b = '\n'; let c = 'z'; let d = '\u{1F600}'; }""")
    chars = [t.value for t in file.tokens if t.kind == "char"]
    assert chars == [r"'\''", r"'\n'", "'z'", r"'\u{1F600}'"]


def test_loop_label_is_a_lifetime_not_a_char(rs):
    file = parse(rs, "fn f() { 'outer: loop { break 'outer; } }")
    assert [t.value for t in file.tokens if t.kind == "lifetime"] == ["'outer", "'outer"]


def test_range_does_not_eat_the_dot(rs):
    file = parse(rs, "fn f(v: &[u8]) { for i in 0..v.len() { let _ = i; } }")
    assert any(t.is_op("..") for t in file.tokens)
    assert [t.value for t in file.tokens if t.kind == "num"] == ["0"]


def test_float_and_suffixed_literals(rs):
    file = parse(rs, "fn f() { let a = 1.5e3f64; let b = 0xFFu8; let c = 1_000usize; let d = 2.; }")
    assert [t.value for t in file.tokens if t.kind == "num"] == \
        ["1.5e3f64", "0xFFu8", "1_000usize", "2."]


def test_shift_and_generic_close_are_distinguishable(rs):
    file = parse(rs, "fn f() { let y: Vec<Vec<u8>> = vec![]; let x = 1u32 << 2; }")
    assert any(t.is_op(">>") for t in file.tokens)
    assert any(t.is_op("<<") for t in file.tokens)


def test_unbalanced_braces_raise_rather_than_report_clean(rs):
    with pytest.raises(rs.RustSyntaxError):
        parse(rs, "fn f() { let x = 1;")


# --- extraction ------------------------------------------------------------ #

SAMPLE = """\
use std::collections::{HashMap, BTreeMap as Sorted};

pub const MAX: usize = 10;
static mut COUNTER: u32 = 0;

/// A record.
#[derive(Debug, Clone)]
pub struct Order<'a, T: Clone> {
    pub id: String,
    total: f64,
    tag: &'a str,
}

pub enum Status { New, Paid(u64), Shipped { at: String } }

pub trait Sink: Send + Sync {
    fn write(&mut self, data: &[u8]) -> Result<usize, std::io::Error>;
    fn flush(&mut self) -> Result<(), std::io::Error> { Ok(()) }
}

impl<'a, T: Clone> Order<'a, T> {
    pub fn new(id: String) -> Self { Order { id, total: 0.0, tag: "" } }
    pub async fn total(&self) -> f64 { self.total }
}

impl Sink for Order<'_, u8> {
    fn write(&mut self, data: &[u8]) -> Result<usize, std::io::Error> { Ok(data.len()) }
}

unsafe impl Send for Status {}

fn helper(v: Vec<u8>) -> usize { v.iter().map(|b| *b as usize).sum() }

macro_rules! shout { ($x:expr) => { println!("{}", $x) }; }

mod inner { pub fn thing() {} }

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn it_works() { assert_eq!(MAX, 10); }
}
"""


@pytest.fixture(scope="module")
def sample(rs):
    return parse(rs, SAMPLE, "lib.rs")


def test_use_statements_record_bound_names_and_aliases(sample):
    paths = {u.path: u.names for u in sample.uses}
    assert paths["std::collections::{HashMap, BTreeMap as Sorted}"] == ["HashMap", "Sorted"]
    assert any(u.path == "super::*" and u.is_glob for u in sample.uses)


def test_struct_fields_and_derives(sample):
    order = next(t for t in sample.types if t.name == "Order")
    assert order.kind == "struct" and order.visibility == "pub"
    assert order.derives == ["Debug", "Clone"]
    assert [f.name for f in order.fields] == ["id", "total", "tag"]
    assert order.fields[0].is_public and not order.fields[1].is_public
    assert order.doc_lines == 1


def test_enum_variants_carry_their_payloads(sample):
    status = next(t for t in sample.types if t.name == "Status")
    assert [v.name for v in status.variants] == ["New", "Paid", "Shipped"]
    assert status.variants[0].payload == ""
    assert status.variants[1].payload.startswith("(")


def test_trait_supertraits_and_default_bodies(sample):
    sink = next(t for t in sample.traits if t.name == "Sink")
    assert sink.supertraits == ["Send", "Sync"]
    write, flush = sink.methods
    assert not write.has_body and flush.has_body


def test_methods_attach_to_the_right_impl(sample):
    owners = {(f.name, f.owner, f.trait_name) for f in sample.functions}
    assert ("new", "Order<'a, T>", None) in owners
    assert ("write", "Order<'_, u8>", "Sink") in owners
    assert ("helper", None, None) in owners


def test_self_parameters_are_recognised(sample):
    total = next(f for f in sample.functions if f.name == "total" and f.owner)
    assert total.is_async and total.takes_self and total.params[0].by_ref
    new = next(f for f in sample.functions if f.name == "new")
    assert not new.takes_self and new.kind == "assoc_fn"


def test_unsafe_impl_is_flagged(sample):
    block = next(i for i in sample.impls if i.type_name == "Status")
    assert block.is_unsafe and block.trait_name == "Send"


def test_bindings_record_const_and_static_mut(sample):
    kinds = {(b.name, b.kind, b.is_mut, b.is_public) for b in sample.bindings}
    assert ("MAX", "const", False, True) in kinds
    assert ("COUNTER", "static", True, False) in kinds


def test_macro_body_is_not_parsed_as_code(sample):
    assert sample.macro_bodies
    assert not any(f.name == "shout" for f in sample.functions)


def test_cfg_test_module_marks_a_test_span(rs, sample):
    it_works = next(f for f in sample.functions if f.name == "it_works")
    assert sample.in_test_code(it_works.start)
    helper = next(f for f in sample.functions if f.name == "helper")
    assert not sample.in_test_code(helper.start)


def test_closures_are_extracted_separately_from_functions(sample):
    assert not any(f.name == "" for f in sample.functions)
    assert len(sample.closures) == 1
    assert [p.name for p in sample.closures[0].params] == ["b"]


def test_bitwise_or_is_not_a_closure(rs):
    file = parse(rs, "fn f(a: u8, b: u8) -> u8 { a | b }")
    assert file.closures == []


# --- query helpers --------------------------------------------------------- #

def test_callee_of_handles_paths_methods_and_turbofish(rs):
    file = parse(rs, 'fn f() { std::fs::read("x"); a.b.c(); "1".parse::<u32>(); }')
    callees = [callee for _, callee in rs.iter_calls(file)]
    assert "std::fs::read" in callees
    assert "a.b.c" in callees
    assert "parse" in callees


def test_iter_calls_reports_macros_and_skips_control_flow(rs):
    file = parse(rs, 'fn f(c: bool) { if c { println!("x"); } while c { vec![1]; } }')
    callees = {callee for _, callee in rs.iter_calls(file)}
    assert "println!" in callees and "vec!" in callees
    assert "if" not in callees and "while" not in callees


def test_receiver_text_stops_at_a_keyword(rs):
    file = parse(rs, "fn f(m: std::collections::HashMap<u8, u8>) { if m.contains_key(&1) {} }")
    dot = next(i for i, t in enumerate(file.tokens) if t.is_op("."))
    assert rs.receiver_text(file, dot) == "m"
