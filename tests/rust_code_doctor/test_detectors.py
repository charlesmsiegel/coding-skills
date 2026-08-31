"""Smoke tests: every detector fires on a known-bad fixture and stays quiet on good code.

Fixtures are written to tmp_path at runtime rather than committed, so the
deliberately-bad Rust never trips the repo's own tooling.

The negative cases matter at least as much as the positive ones. A detector that
fires on correct code is worse than no detector: it trains people to skip the
output, and the real bug goes out with it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "rust-code-doctor" / "scripts"


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


def crate(root: Path, files: dict[str, str], manifest: str | None = None) -> Path:
    """A minimal Cargo crate, for the whole-tree detectors."""
    default = '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n'
    return write(root, {"Cargo.toml": manifest or default, **files})


# --------------------------------------------------------------------------- #
# Fixtures: one bad file and one good file per detector family
# --------------------------------------------------------------------------- #

BAD_ERRORS = """\
pub fn load(path: &str) -> Result<String, std::io::Error> {
    let raw = std::fs::read_to_string(path).unwrap();
    let n = match raw.trim().parse::<u32>() {
        Ok(v) => v,
        Err(e) => return Err(e.into()),
    };
    if n == 0 {
        panic!("empty");
    }
    std::fs::write("log", &raw).ok();
    match cleanup() {
        Ok(_) => {}
        Err(_) => {}
    }
    Ok(raw)
}
fn cleanup() -> Result<(), std::io::Error> { Ok(()) }
"""

GOOD_ERRORS = """\
use std::io;

/// Reads and validates a counter file.
///
/// # Errors
///
/// Returns an error when the file cannot be read or does not hold a number.
pub fn load(path: &str) -> io::Result<u32> {
    let raw = std::fs::read_to_string(path)?;
    raw.trim()
        .parse::<u32>()
        .map_err(|source| io::Error::new(io::ErrorKind::InvalidData, source))
}
"""

BAD_UNSAFE = """\
pub static mut TOTAL: u64 = 0;

pub unsafe fn peek(buf: &[u8], idx: usize) -> u8 {
    *buf.get_unchecked(idx)
}

pub fn bump() {
    unsafe {
        TOTAL += 1;
    }
}

pub struct Handle(*mut u8);
unsafe impl Send for Handle {}
"""

GOOD_UNSAFE = """\
use std::sync::atomic::{AtomicU64, Ordering};

pub static TOTAL: AtomicU64 = AtomicU64::new(0);

pub fn bump() {
    TOTAL.fetch_add(1, Ordering::Relaxed);
}

/// Reads one byte without a bounds check.
///
/// # Safety
///
/// `idx` must be less than `buf.len()`.
pub unsafe fn peek(buf: &[u8], idx: usize) -> u8 {
    // SAFETY: the caller guarantees `idx` is in bounds, per the contract above.
    unsafe { *buf.get_unchecked(idx) }
}
"""

BAD_ASYNC = """\
use std::sync::{Arc, Mutex};
use std::collections::HashMap;

pub struct Cache { inner: Arc<Mutex<HashMap<String, String>>> }

impl Cache {
    pub async fn refresh(&self, keys: &[String]) -> Result<(), std::io::Error> {
        let mut guard = self.inner.lock().unwrap();
        for key in keys {
            let body = fetch(key).await?;
            guard.insert(key.clone(), body);
        }
        std::thread::sleep(std::time::Duration::from_secs(1));
        tokio::spawn(background());
        Ok(())
    }
}

async fn fetch(_k: &str) -> Result<String, std::io::Error> { Ok(String::new()) }
async fn background() {}
"""

GOOD_ASYNC = """\
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::Mutex;

pub struct Cache { inner: Arc<Mutex<HashMap<String, String>>> }

impl Cache {
    pub async fn refresh(&self, keys: &[String]) -> Result<(), std::io::Error> {
        let fetched = futures::future::try_join_all(keys.iter().map(|k| fetch(k))).await?;
        let mut guard = self.inner.lock().await;
        for (key, body) in keys.iter().zip(fetched) {
            guard.insert(key.clone(), body);
        }
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
        Ok(())
    }
}

async fn fetch(_k: &str) -> Result<String, std::io::Error> { Ok(String::new()) }
"""

BAD_LOOPS = """\
pub fn totals(values: &[u32], other: &[u32]) -> u32 {
    let mut total = 0;
    for i in 0..values.len() {
        total += values[i];
    }
    let mut j = 0;
    while j < other.len() {
        total += other[j];
        j += 1;
    }
    let mut out = Vec::new();
    for v in values.iter() {
        out.push(v * 2);
    }
    let _ = out;
    total
}
"""

GOOD_LOOPS = """\
pub fn totals(values: &[u32], other: &[u32]) -> u32 {
    let doubled: Vec<u32> = values.iter().map(|v| v * 2).collect();
    let _ = doubled;
    values.iter().chain(other).sum()
}
"""

BAD_UNRUSTIC = """\
use std::collections::HashMap;

pub fn describe(map: &HashMap<String, u32>, key: &str, opt: Option<u32>) -> String {
    if map.contains_key(key) {
        let _ = map[key];
    }
    let names: Vec<String> = Vec::new();
    if names.len() == 0 {
        let _ = String::from("");
    }
    let first = match names.first() {
        Some(n) => n.clone(),
        None => String::new(),
    };
    let count = match opt {
        Some(v) => v,
        None => 0,
    };
    let _ = count;
    if opt.is_some() {
        let _ = opt.unwrap();
    }
    let picked = names.iter().filter(|n| n.len() > 1).next();
    let _ = picked;
    let value = opt.unwrap_or(fallback());
    let flag = true;
    match flag {
        true => {}
        false => {}
    }
    return format!("{}", value) + &first;
}
fn fallback() -> u32 { 0 }
"""

GOOD_UNRUSTIC = """\
use std::collections::HashMap;

pub fn describe(map: &HashMap<String, u32>, key: &str, opt: Option<u32>) -> String {
    if let Some(found) = map.get(key) {
        let _ = found;
    }
    let names: Vec<String> = Vec::new();
    if names.is_empty() {
        let _ = String::new();
    }
    let first = names.first().cloned().unwrap_or_default();
    if let Some(value) = opt {
        let _ = value;
    }
    let picked = names.iter().find(|n| n.len() > 1);
    let _ = picked;
    let value = opt.unwrap_or_else(fallback);
    format!("{value}{first}")
}
fn fallback() -> u32 { 0 }
"""

BAD_OWNERSHIP = """\
pub struct Node { pub children: std::rc::Rc<std::cell::RefCell<Vec<u32>>> }

pub fn render(title: &String, items: &Vec<u32>, tags: Vec<String>) -> String {
    let mut out = String::new();
    for item in items {
        out.push_str(&title.clone());
        let _ = item;
    }
    let _ = tags.len();
    out
}
"""

GOOD_OWNERSHIP = """\
pub struct Node { pub children: Vec<u32> }

pub fn render(title: &str, items: &[u32], tags: &[String]) -> String {
    let mut out = String::new();
    for item in items {
        out.push_str(title);
        let _ = item;
    }
    let _ = tags.len();
    out
}
"""

BAD_TYPES = """\
pub struct Record {
    pub a: Option<String>,
    pub b: Option<u32>,
    pub c: Option<Option<bool>>,
}

pub fn shrink(total: u64, ratio: f64) -> u32 {
    let narrowed = total as u32;
    let rounded = ratio as i32;
    let signed: i64 = -1;
    let flipped = signed as u64;
    narrowed + rounded as u32 + flipped as u32
}
"""

GOOD_TYPES = """\
pub enum Record {
    Pending { id: String },
    Complete { id: String, size: u32 },
}

/// # Errors
///
/// Returns an error when `total` does not fit in a `u32`.
pub fn shrink(total: u64, ratio: f64) -> Result<u32, std::num::TryFromIntError> {
    let narrowed = u32::try_from(total)?;
    Ok(narrowed.saturating_add(ratio.round().max(0.0) as u32))
}
"""

BAD_SMELLS = """\
#![allow(warnings)]

pub fn classify(kind: &str, n: usize, xs: &[u8]) -> Option<u8> {
    if kind == "a" {
        return Some(1);
    } else if kind == "b" {
        return Some(2);
    } else if kind == "c" {
        return Some(3);
    }
    let last = xs.len() - 1;
    let value = xs[n];
    let ok = if value > 7 { true } else { false };
    let _ = (last, ok, 8191, 8191);
    match value {
        0 => None,
        1 => Some(1),
        _ => None,
    }
}
"""

GOOD_SMELLS = """\
/// The kinds this module understands.
pub enum Kind { A, B, C }

const CHUNK_BYTES: usize = 8191;

/// Classifies one byte.
pub fn classify(kind: &Kind, n: usize, xs: &[u8]) -> Option<u8> {
    let code = match kind {
        Kind::A => 1,
        Kind::B => 2,
        Kind::C => 3,
    };
    let value = xs.get(n)?;
    let _ = (CHUNK_BYTES, *value > 7);
    Some(code)
}
"""

BAD_DESIGN = """\
pub struct Ctx;

pub fn a(host: String, port: u16, timeout: u64) {}
pub fn b(host: String, port: u16, timeout: u64) {}
pub fn c(host: String, port: u16, timeout: u64) {}

pub fn render(doc: &str, numbered: bool, wrapped: bool) -> String { doc.to_string() }

pub fn transfer(from: u64, to: u64, cents: u64) -> u64 { from + to + cents }
"""

GOOD_DESIGN = """\
/// Where to connect.
pub struct Endpoint { pub host: String, pub port: u16, pub timeout_secs: u64 }

/// Whether to number the output lines.
pub enum Numbering { On, Off }
/// Whether to hard-wrap.
pub enum Wrapping { Hard, Soft }

/// An account identifier.
pub struct AccountId(pub u64);
/// An amount in cents.
pub struct Cents(pub u64);

/// Connects to `endpoint`.
pub fn a(endpoint: &Endpoint) { let _ = endpoint; }
/// Renders `doc`.
pub fn render(doc: &str, numbering: Numbering, wrapping: Wrapping) -> String {
    let _ = (numbering, wrapping);
    doc.to_string()
}
/// Moves money.
pub fn transfer(from: AccountId, to: AccountId, amount: Cents) -> u64 {
    from.0 + to.0 + amount.0
}
"""

BAD_API = """\
pub struct Config { pub retries: u32, pub host: String }

impl Config {
    pub fn new() -> Self { Config { retries: 0, host: String::new() } }
    pub fn with_retries(mut self, n: u32) -> Self { self.retries = n; self }
}

pub enum EventKind { Created, Updated, Deleted, Archived }

pub fn build() -> Config { Config::new() }
"""

GOOD_API = """\
/// How the client should behave.
#[derive(Debug, Default, Clone)]
pub struct Config {
    retries: u32,
    host: String,
}

impl Config {
    /// A configuration with no retries.
    pub fn new() -> Self { Self::default() }

    /// Sets the retry count.
    #[must_use]
    pub fn with_retries(mut self, n: u32) -> Self { self.retries = n; self }
}

/// What happened to a record.
#[derive(Debug, Clone, Copy)]
#[non_exhaustive]
pub enum EventKind { Created, Updated, Deleted, Archived }

/// Builds the default configuration.
pub fn build() -> Config { Config::new() }
"""

BAD_SECURITY = """\
use std::process::Command;
use md5::Md5;

const DB_PASSWORD: &str = "hunter2-production-value";

pub fn run(name: &str, id: &str) {
    Command::new("sh").arg("-c").arg(format!("rm -rf {}", name)).output().ok();
    let q = format!("SELECT * FROM users WHERE id = {}", id);
    let _ = (q, Md5::default());
    let _ = "http://api.example.com/v1";
}
"""

GOOD_SECURITY = """\
use sha2::Sha256;
use std::process::Command;

/// Removes a path.
pub fn run(name: &str) -> std::io::Result<()> {
    Command::new("rm").arg("-rf").arg(name).output()?;
    let _ = (Sha256::default(), "https://api.example.com/v1");
    Ok(())
}
"""

BAD_DEBUG = """\
pub fn compute(x: u32) -> u32 {
    dbg!(x);
    println!("computing {}", x);
    eprintln!("noise");
    x * 2
}
"""

GOOD_DEBUG = """\
/// Doubles `x`.
pub fn compute(x: u32) -> u32 {
    tracing::debug!(x, "computing");
    x * 2
}
"""

BAD_SCAFFOLD = """\
pub trait Store {
    fn get(&self, k: &str) -> Option<String>;
    fn put(&mut self, k: &str, v: String);
}

pub struct Memory;

impl Store for Memory {
    fn get(&self, k: &str) -> Option<String> { todo!() }
    fn put(&mut self, k: &str, v: String) { unimplemented!() }
}

// In a real implementation you would validate the options here.
pub fn configure(options: &str) -> u32 { 7 }
"""

GOOD_SCAFFOLD = """\
use std::collections::HashMap;

/// A key-value store.
pub trait Store {
    /// Reads a key.
    fn get(&self, k: &str) -> Option<String>;
}

/// An in-memory store.
#[derive(Debug, Default)]
pub struct Memory { entries: HashMap<String, String> }

impl Store for Memory {
    fn get(&self, k: &str) -> Option<String> { self.entries.get(k).cloned() }
}

/// Parses the option string into a retry count.
pub fn configure(options: &str) -> u32 { options.len() as u32 }
"""

BAD_TESTS = """\
pub fn add(a: u32, b: u32) -> u32 { a + b }

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn it_runs() { let _ = add(1, 2); }

    #[test]
    fn always() { assert!(true); }

    #[test]
    #[should_panic]
    fn rejects() { panic!("x"); }

    #[test]
    #[ignore]
    fn slow() { assert_eq!(add(1, 1), 2); }

    #[test]
    fn timed() {
        std::thread::sleep(std::time::Duration::from_millis(10));
        assert_eq!(add(1, 1), 2);
    }
}
"""

GOOD_TESTS = """\
/// Adds two numbers.
pub fn add(a: u32, b: u32) -> u32 { a + b }

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adds_small_numbers() { assert_eq!(add(1, 2), 3); }

    #[test]
    #[should_panic(expected = "attempt to add with overflow")]
    fn overflows_loudly() { let _ = add(u32::MAX, 1); }
}
"""

BAD_NAMING = """\
pub struct my_record { pub v: u32 }
pub const maxSize: usize = 10;

impl my_record {
    pub fn get_value(&self) -> u32 { self.v }
    pub fn into_string(&self) -> String { self.v.to_string() }
    pub fn valid(&self) -> bool { self.v > 0 }
}

pub fn DoThing() {}
"""

GOOD_NAMING = """\
/// A record.
#[derive(Debug)]
pub struct MyRecord { value: u32 }

/// The largest record we accept.
pub const MAX_SIZE: usize = 10;

impl MyRecord {
    /// The stored value.
    pub fn value(&self) -> u32 { self.value }
    /// Renders the value.
    pub fn to_text(&self) -> String { self.value.to_string() }
    /// Whether the record is usable.
    pub fn is_valid(&self) -> bool { self.value > 0 }
}

/// Does the thing.
pub fn do_thing() {}
"""

BAD_IDIOMS = """\
extern crate serde;

#[macro_use]
pub mod helpers {}

pub fn describe(name: &str, count: u32) -> String {
    let boxed: Box<std::fmt::Debug + Send> = Box::new(1u8);
    let _ = boxed;
    format!("{} {}", name, count)
}
"""

GOOD_IDIOMS = """\
/// Describes a named counter.
pub fn describe(name: &str, count: u32) -> String {
    let boxed: Box<dyn std::fmt::Debug + Send> = Box::new(1u8);
    let _ = boxed;
    format!("{name} {count}")
}
"""

BAD_COMPLEXITY = """\
pub fn decide(a: u32, b: u32, c: u32, d: u32, e: u32, f: u32, g: u32) -> u32 {
    let mut total = 0;
    for i in 0..a {
        if i % 2 == 0 && i > b {
            for j in 0..c {
                if j > d || j < e {
                    while total < f {
                        if total % 3 == 0 && total > b {
                            total += g;
                        } else if total % 5 == 0 || total < d {
                            if total > e && total < f {
                                total += 1;
                            } else if total == 0 {
                                total += 4;
                            }
                        } else {
                            total += 2;
                        }
                        if total > 100 && total % 7 == 0 {
                            break;
                        }
                    }
                }
            }
        }
    }
    total
}
"""

GOOD_COMPLEXITY = """\
/// Sums the even inputs.
pub fn decide(values: &[u32]) -> u32 {
    values.iter().filter(|v| **v % 2 == 0).sum()
}
"""

BAD_COMMENTS = """\
pub fn run() -> u32 {
    // let old = compute_the_thing();
    // if old > 0 {
    //     return old;
    // }
    // FIXME: this is wrong for negative inputs
    7
}

/// Returns the display name.
pub fn display_name() -> &'static str { "x" }
"""

GOOD_COMMENTS = """\
/// Produces the sentinel value the v1 protocol expects.
///
/// The value is fixed by the wire format; changing it breaks archived files.
pub fn run() -> u32 { 7 }

/// The record's display name, normalised to lowercase at construction.
pub fn display_name() -> &'static str { "x" }
"""


# --------------------------------------------------------------------------- #
# Per-file detectors
# --------------------------------------------------------------------------- #

FILE_CASES = [
    ("find_error_handling.py", BAD_ERRORS, GOOD_ERRORS,
     {"unwrap_in_fallible_fn", "manual_question_mark", "panic_in_library",
      "error_discarded_by_ok", "empty_error_arm"}),
    ("find_unsafe_issues.py", BAD_UNSAFE, GOOD_UNSAFE,
     {"static_mut", "unsafe_block_without_safety_comment", "unsafe_fn_without_safety_docs",
      "unsafe_impl_without_safety_comment", "unchecked_operation"}),
    ("find_concurrency_issues.py", BAD_ASYNC, GOOD_ASYNC,
     {"guard_held_across_await", "blocking_call_in_async", "join_handle_dropped",
      "sequential_await_in_loop"}),
    ("find_loop_simplifications.py", BAD_LOOPS, GOOD_LOOPS,
     {"index_loop_over_len", "while_index_loop", "loop_building_collection",
      "explicit_iter_in_for"}),
    ("find_unrustic.py", BAD_UNRUSTIC, GOOD_UNRUSTIC,
     {"contains_key_then_lookup", "len_zero_instead_of_is_empty", "manual_unwrap_or",
      "is_some_then_unwrap", "filter_then_next", "eager_fallback_argument",
      "match_on_bool", "needless_return", "empty_string_from_literal"}),
    ("find_ownership_issues.py", BAD_OWNERSHIP, GOOD_OWNERSHIP,
     {"ref_string_parameter", "ref_vec_parameter", "clone_inside_loop",
      "shared_mutable_field"}),
    ("find_type_issues.py", BAD_TYPES, GOOD_TYPES,
     {"narrowing_cast", "float_to_int_cast", "sign_changing_cast", "all_optional_struct",
      "nested_option"}),
    ("find_code_smells.py", BAD_SMELLS, GOOD_SMELLS,
     {"blanket_lint_suppression", "if_ladder_on_one_value", "unchecked_length_subtraction",
      "needless_bool", "repeated_magic_number", "wildcard_match_arm"}),
    ("find_design_smells.py", BAD_DESIGN, GOOD_DESIGN,
     {"data_clump", "multiple_flag_parameters", "adjacent_same_typed_parameters"}),
    ("find_api_hygiene.py", BAD_API, GOOD_API,
     {"public_type_without_debug", "new_without_default", "builder_method_without_must_use",
      "undocumented_public_items", "public_enum_without_non_exhaustive"}),
    ("find_security_issues.py", BAD_SECURITY, GOOD_SECURITY,
     {"shell_command_construction", "sql_built_by_interpolation", "weak_hash_algorithm",
      "credential_named_literal", "plaintext_http_url"}),
    ("find_debug_leftovers.py", BAD_DEBUG, GOOD_DEBUG,
     {"dbg_macro", "print_in_library", "eprintln_instead_of_log"}),
    ("find_ai_scaffolding.py", BAD_SCAFFOLD, GOOD_SCAFFOLD,
     {"unfinished_stub", "ignored_parameter", "placeholder_narration"}),
    ("find_test_smells.py", BAD_TESTS, GOOD_TESTS,
     {"test_without_assertion", "tautological_assertion", "should_panic_without_expected",
      "ignored_test", "sleep_in_test"}),
    ("find_naming_issues.py", BAD_NAMING, GOOD_NAMING,
     {"non_camel_case_type", "non_screaming_case_constant", "non_snake_case_function",
      "getter_with_get_prefix", "into_prefix_borrows", "boolean_fn_not_a_question"}),
    ("find_outdated_idioms.py", BAD_IDIOMS, GOOD_IDIOMS,
     {"extern_crate", "macro_use_attribute", "bare_trait_object", "uninlined_format_args"}),
    ("analyze_complexity.py", BAD_COMPLEXITY, GOOD_COMPLEXITY,
     {"high_cyclomatic_complexity", "high_cognitive_complexity", "deep_nesting",
      "too_many_parameters"}),
    ("find_comment_smells.py", BAD_COMMENTS, GOOD_COMMENTS,
     {"commented_out_code", "debt_marker", "doc_restates_signature"}),
]


@pytest.mark.parametrize("script,bad,_good,expected", FILE_CASES,
                         ids=[c[0] for c in FILE_CASES])
def test_detector_fires_on_bad_code(tmp_path, script, bad, _good, expected):
    target = write(tmp_path / "bad", {"lib.rs": bad}) / "lib.rs"
    found = smells(run_detector(script, target))
    missing = expected - found
    assert not missing, f"{script} missed {sorted(missing)}; it found {sorted(found)}"


@pytest.mark.parametrize("script,_bad,good,expected", FILE_CASES,
                         ids=[c[0] for c in FILE_CASES])
def test_detector_is_quiet_on_good_code(tmp_path, script, _bad, good, expected):
    target = write(tmp_path / "good", {"lib.rs": good}) / "lib.rs"
    found = smells(run_detector(script, target))
    noisy = found & expected
    assert not noisy, f"{script} fired {sorted(noisy)} on code that does it right"


def test_ignore_suppresses_a_finding_type(tmp_path):
    target = write(tmp_path / "bad", {"lib.rs": BAD_ERRORS}) / "lib.rs"
    assert "panic_in_library" in smells(run_detector("find_error_handling.py", target))
    suppressed = run_detector("find_error_handling.py", target, "--ignore", "panic_in_library")
    assert "panic_in_library" not in smells(suppressed)


def test_unwrap_in_a_test_module_is_not_a_finding(tmp_path):
    source = """\
pub fn parse(s: &str) -> Result<u32, std::num::ParseIntError> { s.parse() }

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parses() { assert_eq!(parse("7").unwrap(), 7); }
}
"""
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    found = smells(run_detector("find_error_handling.py", target))
    assert "unwrap_outside_tests" not in found and "unwrap_in_fallible_fn" not in found


def test_an_unparseable_file_is_named_rather_than_reported_clean(tmp_path):
    target = write(tmp_path / "t", {"lib.rs": "fn broken() { let x = 1;"}) / "lib.rs"
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_error_handling.py"), str(target),
         "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == []
    assert "does not tokenize cleanly" in result.stderr


# --------------------------------------------------------------------------- #
# Whole-tree detectors
# --------------------------------------------------------------------------- #

def test_cargo_issues_reads_the_manifest(tmp_path):
    root = crate(tmp_path / "c", {"src/lib.rs": "pub fn a() {}\n"},
                 manifest='[package]\nname = "demo"\nversion = "0.1.0"\n\n'
                          '[dependencies]\nregex = "*"\n'
                          'weird = { git = "https://example.com/x" }\n')
    found = smells(run_detector("find_cargo_issues.py", root))
    assert {"old_edition", "wildcard_dependency", "unpinned_git_dependency",
            "no_lint_configuration"} <= found


def test_cargo_issues_quiet_on_a_well_formed_manifest(tmp_path):
    root = crate(tmp_path / "c", {"src/lib.rs": "use serde::Serialize;\npub fn a() {}\n"},
                 manifest='[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'
                          'rust-version = "1.75"\ndescription = "d"\nlicense = "MIT"\n'
                          'repository = "https://example.com"\n\n'
                          '[dependencies]\nserde = "1"\n\n[lints.clippy]\nunwrap_used = "warn"\n')
    found = smells(run_detector("find_cargo_issues.py", root))
    assert not found, f"fired on a clean manifest: {sorted(found)}"


def test_module_graph_finds_files_rustc_never_compiles(tmp_path):
    root = crate(tmp_path / "c", {
        "src/lib.rs": "mod store;\nmod gone;\npub fn a() {}\n",
        "src/store.rs": "pub fn save() {}\n",
        "src/orphan.rs": "pub fn forgotten() {}\n",
    })
    findings = run_detector("find_module_issues.py", root)
    assert "file_never_compiled" in smells(findings)
    assert "module_file_missing" in smells(findings)
    orphan = next(f for f in findings if f["smell_type"] == "file_never_compiled")
    assert orphan["file"].endswith("orphan.rs")


def test_module_graph_quiet_when_every_file_is_reached(tmp_path):
    root = crate(tmp_path / "c", {
        "src/lib.rs": "mod store;\npub use store::save;\n",
        "src/store.rs": "pub fn save() {}\n",
    })
    assert "file_never_compiled" not in smells(run_detector("find_module_issues.py", root))


def test_dead_code_finds_unreachable_and_unreferenced(tmp_path):
    root = crate(tmp_path / "c", {
        "src/lib.rs": "use std::fmt::Debug;\n"
                      "fn never_called() -> u32 { 1 }\n"
                      "pub fn run() -> u32 {\n    return 2;\n    let _dead = 3;\n}\n",
    })
    found = smells(run_detector("find_dead_code.py", root))
    # Unused *imports* are rustc's to report — it can tell a trait imported for
    # method resolution from a dead name, and this scanner cannot.
    assert {"unreachable_code", "unused_private_function"} <= found
    assert "unused_import" not in found


def test_overengineering_finds_single_impl_traits_and_namespace_structs(tmp_path):
    root = crate(tmp_path / "c", {
        "src/lib.rs": "pub trait Backend { fn get(&self) -> u32; }\n"
                      "pub struct Memory;\n"
                      "impl Backend for Memory { fn get(&self) -> u32 { 1 } }\n"
                      "pub struct Util;\n"
                      "impl Util {\n"
                      "    pub fn a(x: u32) -> u32 { x }\n"
                      "    pub fn b(x: u32) -> u32 { x }\n"
                      "}\n",
    })
    found = smells(run_detector("find_overengineering.py", root))
    assert {"trait_with_one_implementor", "stateless_struct_as_namespace"} <= found


def test_overengineering_quiet_when_two_implementors_exist(tmp_path):
    root = crate(tmp_path / "c", {
        "src/lib.rs": "pub trait Backend { fn get(&self) -> u32; }\n"
                      "pub struct Memory;\npub struct Disk;\n"
                      "impl Backend for Memory { fn get(&self) -> u32 { 1 } }\n"
                      "impl Backend for Disk { fn get(&self) -> u32 { 2 } }\n",
    })
    assert "trait_with_one_implementor" not in smells(run_detector("find_overengineering.py", root))


def test_no_tests_at_all_is_its_own_alarm(tmp_path):
    root = crate(tmp_path / "c", {
        "src/lib.rs": "pub fn a() {}\npub fn b() {}\npub fn c() {}\n",
    })
    assert "no_tests_at_all" in smells(run_detector("find_untested_modules.py", root))


def test_untested_modules_quiet_when_the_module_tests_itself(tmp_path):
    root = crate(tmp_path / "c", {
        "src/lib.rs": "pub fn a() -> u32 { 1 }\npub fn b() -> u32 { 2 }\npub fn c() -> u32 { 3 }\n"
                      "#[cfg(test)]\nmod tests {\n    use super::*;\n"
                      "    #[test]\n    fn works() { assert_eq!(a(), 1); }\n}\n",
    })
    found = smells(run_detector("find_untested_modules.py", root))
    assert "no_tests_at_all" not in found and "untested_module" not in found


def test_duplicates_finds_a_copied_block(tmp_path):
    block = "\n".join(f"    let v{i} = compute({i}) + offset * {i} - base;" for i in range(14))
    root = crate(tmp_path / "c", {
        "src/lib.rs": f"fn compute(x: u32) -> u32 {{ x }}\n"
                      f"pub fn one(offset: u32, base: u32) {{\n{block}\n}}\n"
                      f"pub fn two(offset: u32, base: u32) {{\n{block}\n}}\n",
    })
    assert "duplicated_block" in smells(run_detector("find_duplicates.py", root))


# --------------------------------------------------------------------------- #
# Regressions from the first review pass
#
# Every one of these was a confident false finding, or a silently unusable
# output contract, on the initial commit.
# --------------------------------------------------------------------------- #

def test_a_module_declared_inside_an_inline_module_resolves_under_it(tmp_path):
    """`mod foo { mod bar; }` resolves `bar` under `foo/`, not beside lib.rs.
    Resolving it beside lib.rs produced two high-severity findings for a
    correctly wired file: missing, and never compiled."""
    root = crate(tmp_path / "c", {
        "src/lib.rs": "mod foo { mod bar; }\n",
        "src/foo/bar.rs": "pub fn b() {}\n",
    })
    found = smells(run_detector("find_module_issues.py", root))
    assert "module_file_missing" not in found
    assert "file_never_compiled" not in found


def test_a_directory_style_binary_root_is_a_crate_root(tmp_path):
    """Cargo compiles `src/bin/<name>/main.rs`; missing it made every module
    reachable from that binary look like it was never compiled."""
    root = crate(tmp_path / "c", {
        "src/lib.rs": "pub fn a() {}\n",
        "src/bin/server/main.rs": "mod helper;\nfn main() {}\n",
        "src/bin/server/helper.rs": "pub fn h() {}\n",
    })
    assert "file_never_compiled" not in smells(run_detector("find_module_issues.py", root))


def test_a_guard_dropped_before_the_await_is_not_held_across_it(tmp_path):
    source = """\
use std::sync::Mutex;
pub struct C { inner: Mutex<u32> }
impl C {
    pub async fn bump(&self) -> u32 {
        let guard = self.inner.lock().unwrap();
        let seen = *guard;
        drop(guard);
        fetch(seen).await
    }
}
async fn fetch(n: u32) -> u32 { n }
"""
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "guard_held_across_await" not in smells(run_detector("find_concurrency_issues.py", target))


def test_a_guard_still_alive_at_the_await_is_still_reported(tmp_path):
    """The `drop` exemption must not disarm the check it guards."""
    source = """\
use std::sync::Mutex;
pub struct C { inner: Mutex<u32> }
impl C {
    pub async fn bump(&self) -> u32 {
        let guard = self.inner.lock().unwrap();
        let out = fetch(*guard).await;
        drop(guard);
        out
    }
}
async fn fetch(n: u32) -> u32 { n }
"""
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "guard_held_across_await" in smells(run_detector("find_concurrency_issues.py", target))


def test_a_rustdoc_safety_section_documents_an_unsafe_trait(tmp_path):
    source = """\
/// A marker for uniquely-owned pointers.
///
/// # Safety
///
/// Implementors must guarantee the contained pointer has no other owner.
pub unsafe trait Unique {}
"""
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "unsafe_trait_without_safety_docs" not in smells(run_detector("find_unsafe_issues.py", target))


def test_a_production_name_containing_test_is_not_a_test_file(tmp_path):
    """`contest_runner.rs` contains `test_`. Classified as a test, every
    error-handling finding in it was suppressed."""
    source = "pub fn run(p: &str) -> Result<String, std::io::Error> {\n" \
             "    Ok(std::fs::read_to_string(p).unwrap())\n}\n"
    target = write(tmp_path / "t", {"contest_runner.rs": source}) / "contest_runner.rs"
    assert "unwrap_in_fallible_fn" in smells(run_detector("find_error_handling.py", target))


def test_a_test_in_an_orphan_file_does_not_clear_the_no_tests_alarm(tmp_path):
    """`cargo test` runs zero tests when the only `#[test]` is in a file no
    `mod` declaration reaches."""
    root = crate(tmp_path / "c", {
        "src/lib.rs": "pub fn a() {}\npub fn b() {}\npub fn c() {}\n",
        "src/orphan.rs": "#[test]\nfn t() { assert_eq!(1, 1); }\n",
    })
    assert "no_tests_at_all" in smells(run_detector("find_untested_modules.py", root))


def test_dev_and_build_dependencies_are_looked_for_in_their_own_targets(tmp_path):
    """A dev-dependency lives in `tests/` and a build-dependency in `build.rs`;
    reconciling both against `src/` alone reported every one as unused."""
    root = crate(tmp_path / "c", {
        "src/lib.rs": "use serde::Serialize;\npub fn a() {}\n",
        "tests/it.rs": "use proptest::prelude::*;\n#[test]\nfn t() { assert_eq!(1, 1); }\n",
        "build.rs": "fn main() { cc::Build::new(); }\n",
    }, manifest='[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'
                '\n[dependencies]\nserde = "1"\n'
                '\n[dev-dependencies]\nproptest = "1"\n'
                '\n[build-dependencies]\ncc = "1"\n')
    assert "unused_dependency" not in smells(run_detector("find_cargo_issues.py", root))


def test_format_findings_emits_json_for_a_clean_report(tmp_path):
    """The documented JSON pipeline was unusable in its most common case."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "format_findings.py"), "--format", "json"],
        input="[]", capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == []


def test_an_unknown_tool_name_is_an_error_not_a_silent_skip(tmp_path):
    """`--tools chek` reporting "no findings" is a compilation check that never
    ran, presented as a clean bill."""
    root = crate(tmp_path / "c", {"src/lib.rs": "pub fn a() {}\n"})
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "run_external_tools.py"), str(root),
         "--tools", "chek"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0
    assert "unknown tools" in result.stderr


def test_missing_cargo_still_produces_a_json_document(tmp_path, monkeypatch):
    """The no-cargo case is exactly when a caller is most likely piping this."""
    root = crate(tmp_path / "c", {"src/lib.rs": "pub fn a() {}\n"})
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "run_external_tools.py"), str(root),
         "--format", "json"],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "PATH": str(tmp_path / "empty")},
    )
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert [f["smell_type"] for f in report["findings"]] == ["cargo:not-installed"]


# --- second review pass ---------------------------------------------------- #

def test_a_blocking_call_inside_spawn_blocking_is_not_on_the_executor(tmp_path):
    """Reporting it would recommend the wrapper already in use."""
    source = """\
pub async fn load(path: String) -> Vec<u8> {
    tokio::task::spawn_blocking(move || std::fs::read(path).unwrap()).await.unwrap()
}
"""
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "blocking_call_in_async" not in smells(run_detector("find_concurrency_issues.py", target))


def test_a_bare_blocking_call_in_async_is_still_reported(tmp_path):
    source = 'pub async fn load() -> Vec<u8> { std::fs::read("x").unwrap() }\n'
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "blocking_call_in_async" in smells(run_detector("find_concurrency_issues.py", target))


def test_a_cast_resolves_the_nearest_shadowed_binding(tmp_path):
    """Rust shadows freely; a forward scan found the first annotation in the
    file rather than the one in scope, inventing a truncation."""
    source = "pub fn f() { let x: u64 = 1; let x: u8 = 2; let _ = x as u16; }\n"
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "narrowing_cast" not in smells(run_detector("find_type_issues.py", target))


def test_an_unshadowed_narrowing_cast_is_still_reported(tmp_path):
    source = "pub fn f() { let x: u64 = 1; let _ = x as u16; }\n"
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "narrowing_cast" in smells(run_detector("find_type_issues.py", target))


def test_pointer_sized_casts_are_reported_as_target_dependent(tmp_path):
    """`usize` is 32 bits on a 32-bit target. Hard-coding 64 both missed
    `u64 as usize` and called `usize as u32` a definite narrowing."""
    source = "pub fn f(a: u64, b: usize, c: usize) {\n" \
             "    let _ = a as usize;\n    let _ = b as u32;\n    let _ = c as u64;\n}\n"
    found = smells(run_detector("find_type_issues.py",
                                write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"))
    assert "target_dependent_cast" in found
    assert "narrowing_cast" not in found, "neither cast narrows on every target"


def test_same_name_in_two_inline_modules_is_not_a_duplicate(tmp_path):
    source = "mod a { fn helper() {} }\nmod b { fn helper() {} }\n"
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "duplicate_definition" not in smells(run_detector("find_ai_scaffolding.py", target))


def test_a_real_duplicate_in_one_scope_is_still_reported(tmp_path):
    target = write(tmp_path / "t", {"lib.rs": "fn helper() {}\nfn helper() {}\n"}) / "lib.rs"
    assert "duplicate_definition" in smells(run_detector("find_ai_scaffolding.py", target))


def test_a_formatted_command_argument_is_only_flagged_when_value_leading(tmp_path):
    """`.arg(format!(…))` is one argument passed to execve — no shell, no word
    splitting. The hazard is an argument whose first character is the value."""
    benign = 'use std::process::Command;\npub fn f(n: &str) {\n' \
             '    Command::new("cp").arg(format!("/tmp/{n}.txt")).output().ok();\n}\n'
    risky = 'use std::process::Command;\npub fn f(n: &str) {\n' \
            '    Command::new("rm").arg(format!("{n}")).output().ok();\n}\n'
    assert "option_injectable_argument" not in smells(run_detector(
        "find_security_issues.py", write(tmp_path / "ok", {"lib.rs": benign}) / "lib.rs"))
    assert "option_injectable_argument" in smells(run_detector(
        "find_security_issues.py", write(tmp_path / "bad", {"lib.rs": risky}) / "lib.rs"))


def test_a_credential_in_an_inline_test_module_is_downgraded(tmp_path):
    """It is not compiled into a release build, and the same fixture in
    `tests/` was already downgraded — the two should not disagree."""
    source = '#[cfg(test)]\nmod tests {\n    const API_KEY: &str = "hunter2-fixture-value";\n' \
             '    #[test]\n    fn t() { assert_eq!(API_KEY.len(), 21); }\n}\n'
    findings = run_detector("find_security_issues.py",
                            write(tmp_path / "t", {"lib.rs": source}) / "lib.rs")
    credential = [f for f in findings if f["smell_type"] == "credential_named_literal"]
    assert credential and credential[0]["severity"] == "low"


def test_a_production_credential_is_still_high(tmp_path):
    source = 'const API_KEY: &str = "hunter2-production-value";\n'
    findings = run_detector("find_security_issues.py",
                            write(tmp_path / "t", {"lib.rs": source}) / "lib.rs")
    credential = [f for f in findings if f["smell_type"] == "credential_named_literal"]
    assert credential and credential[0]["severity"] == "high"


def test_a_cfg_gated_missing_module_is_not_a_proven_build_failure(tmp_path):
    """Under a configuration where the `cfg` is false, rustc never looks."""
    root = crate(tmp_path / "c", {"src/lib.rs": '#[cfg(feature = "extra")]\nmod maybe;\n'})
    found = smells(run_detector("find_module_issues.py", root))
    assert "cfg_gated_module_file_missing" in found
    assert "module_file_missing" not in found


def test_a_custom_lib_path_is_still_a_crate_root(tmp_path):
    """`[lib] path = "source/root.rs"` resolves `mod helper;` to
    `source/helper.rs`; inferring roots from the basename looked in
    `source/root/`."""
    root = crate(tmp_path / "c", {
        "source/root.rs": "mod helper;\n",
        "source/helper.rs": "pub fn h() {}\n",
    }, manifest='[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'
                '\n[lib]\npath = "source/root.rs"\n')
    found = smells(run_detector("find_module_issues.py", root))
    assert "module_file_missing" not in found and "file_never_compiled" not in found


def test_autobins_false_stops_implicit_binary_discovery(tmp_path):
    """Cargo does not build `src/main.rs` then, so its modules are orphans."""
    root = crate(tmp_path / "c", {
        "src/lib.rs": "pub fn a() {}\n",
        "src/main.rs": "mod extra;\nfn main() {}\n",
        "src/extra.rs": "pub fn e() {}\n",
    }, manifest='[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'
                "autobins = false\n")
    assert "file_never_compiled" in smells(run_detector("find_module_issues.py", root))


# --- third review pass ----------------------------------------------------- #

def test_a_path_attribute_resolves_under_its_inline_module(tmp_path):
    """`#[path]` is relative to the module's directory, so the inline chain
    applies to it too."""
    root = crate(tmp_path / "c", {
        "src/lib.rs": 'mod a {\n    #[path = "other.rs"]\n    mod x;\n}\n',
        "src/a/other.rs": "pub fn f() {}\n",
    })
    found = smells(run_detector("find_module_issues.py", root))
    assert "module_file_missing" not in found and "file_never_compiled" not in found


def test_the_no_tests_alarm_is_decided_per_crate(tmp_path):
    """`cargo test -p b` runs zero tests whatever crate `a` contains."""
    root = write(tmp_path / "w", {
        "Cargo.toml": '[workspace]\nmembers = ["a", "b"]\n',
        "a/Cargo.toml": '[package]\nname = "a"\nversion = "0.1.0"\nedition = "2021"\n',
        "a/src/lib.rs": "pub fn x() {}\n#[cfg(test)]\nmod t { #[test] fn q() { assert_eq!(1, 1); } }\n",
        "b/Cargo.toml": '[package]\nname = "b"\nversion = "0.1.0"\nedition = "2021"\n',
        "b/src/lib.rs": "pub fn a() {}\npub fn b() {}\npub fn c() {}\n",
    })
    findings = run_detector("find_untested_modules.py", root)
    blockers = [f for f in findings if f["smell_type"] == "no_tests_at_all"]
    assert len(blockers) == 1 and "/b/" in blockers[0]["file"].replace("\\", "/")


def test_a_manifest_declared_test_target_counts_as_a_test(tmp_path):
    """Cargo compiles `[[test]] path = "qa/check.rs"` wherever it lives."""
    root = crate(tmp_path / "c", {
        "src/lib.rs": "pub fn a() {}\npub fn b() {}\npub fn c() {}\n",
        "qa/check.rs": "#[test]\nfn works() { assert_eq!(1, 1); }\n",
    }, manifest='[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'
                '\n[[test]]\nname = "qa"\npath = "qa/check.rs"\n')
    assert "no_tests_at_all" not in smells(run_detector("find_untested_modules.py", root))


def test_sql_written_as_a_raw_string_is_still_checked(tmp_path):
    """A raw string is the common way to write a query containing quotes —
    exactly the queries most likely to be interpolated."""
    source = 'pub fn q(user: &str) -> String {\n' \
             '    format!(r#"SELECT * FROM users WHERE name = \'{}\'"#, user)\n}\n'
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "sql_built_by_interpolation" in smells(run_detector("find_security_issues.py", target))


def test_thread_rng_is_not_reported_as_non_cryptographic(tmp_path):
    """rand's thread-local generator is a CSPRNG seeded from OS entropy. The
    old finding misstated the primitive, which is worse than not firing."""
    source = "pub fn token() -> u64 { let secret_key = rand::thread_rng().gen(); secret_key }\n"
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "non_cryptographic_rng_for_secret" not in smells(
        run_detector("find_security_issues.py", target))


def test_a_deterministically_seeded_generator_is_reported(tmp_path):
    source = "use rand::rngs::StdRng;\n" \
             "pub fn token() -> u64 { let secret_key = StdRng::seed_from_u64(7).gen(); secret_key }\n"
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "deterministic_rng_for_secret" in smells(run_detector("find_security_issues.py", target))


def test_a_specific_lint_allow_is_not_a_blanket_suppression(tmp_path):
    """`unused_mut` is one lint; matching it against the `unused` group as a
    substring called a precise suppression a blanket one."""
    precise = write(tmp_path / "a", {"lib.rs": "#![allow(unused_mut)]\npub fn a() {}\n"}) / "lib.rs"
    blanket = write(tmp_path / "b", {"lib.rs": "#![allow(unused)]\npub fn a() {}\n"}) / "lib.rs"
    assert "blanket_lint_suppression" not in smells(run_detector("find_code_smells.py", precise))
    assert "blanket_lint_suppression" in smells(run_detector("find_code_smells.py", blanket))


def test_a_const_generic_brace_is_not_the_function_body(tmp_path):
    """Taking the first `{` made the const expression the body, so the real one
    was never analysed — silently, since a body was still found."""
    source = 'pub async fn f<T>() -> u32 where T: Bound<{ N + 1 }> {\n' \
             '    std::fs::read("x").unwrap();\n    7\n}\n'
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "blocking_call_in_async" in smells(run_detector("find_concurrency_issues.py", target))


# --- fourth review pass ---------------------------------------------------- #
#
# This round was mostly checks that never ran and helpers never called — the
# kind of defect that looks like coverage and is not.

def test_the_external_test_module_check_actually_runs(tmp_path):
    """`#[cfg(test)] mod tests;` in an ordinary lib.rs creates no inline test
    span, so gating on that meant this documented check never fired."""
    source = "pub fn f() {}\n#[cfg(test)]\nmod tests;\n"
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "missing_test_module_file" in smells(run_detector("find_test_smells.py", target))


def test_test_directory_markers_are_crate_relative(tmp_path):
    """A checkout under a directory named `tests` had every file classified as
    test code, so findings depended on where the repo was cloned."""
    root = crate(tmp_path / "tests" / "project",
                 {"src/lib.rs": 'pub fn f() { panic!("boom"); }\n'})
    assert "panic_in_library" in smells(run_detector("find_error_handling.py", root / "src" / "lib.rs"))


def test_a_source_subdirectory_still_finds_its_manifest(tmp_path):
    """SKILL.md documents `analyze_all.py src/`. Searching for manifests only
    below that path found none, so every tree detector returned clean."""
    root = crate(tmp_path / "c", {"src/lib.rs": "mod gone;\npub fn f() {}\n"})
    assert "module_file_missing" in smells(run_detector("find_module_issues.py", root / "src"))


def test_a_mutable_container_parameter_is_left_alone(tmp_path):
    """`&mut String` exists so the callee can mutate it; `&str` does not compile."""
    source = "pub fn clear(s: &mut String) { s.clear(); }\n" \
             "pub fn read(s: &String) -> usize { s.len() }\n"
    found = smells(run_detector("find_ownership_issues.py",
                                write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"))
    assert found.count("ref_string_parameter") if isinstance(found, list) else True
    findings = run_detector("find_ownership_issues.py",
                            write(tmp_path / "t2", {"lib.rs": source}) / "lib.rs")
    lines = [f["line"] for f in findings if f["smell_type"] == "ref_string_parameter"]
    assert lines == [2], "only the immutable `&String` should be reported"


def test_a_bare_matches_is_not_an_assertion(tmp_path):
    """`matches!(v, P);` computes a boolean and discards it — the exact mistake
    this check exists to find."""
    source = "#[cfg(test)]\nmod t {\n    #[test]\n    fn q() { matches!(1u8, 1); }\n}\n"
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "test_without_assertion" in smells(run_detector("find_test_smells.py", target))


def test_a_build_script_unwrap_is_not_a_production_defect(tmp_path):
    """SKILL.md already said so, and `is_build_script` existed unused."""
    source = 'fn main() { let out = std::env::var("OUT_DIR").unwrap(); let _ = out; }\n'
    target = write(tmp_path / "t", {"build.rs": source}) / "build.rs"
    assert "unwrap_outside_tests" not in smells(run_detector("find_error_handling.py", target))


def test_a_synchronous_recv_in_async_is_reported(tmp_path):
    """`recv` was selected by the guard and then had no branch, so the check
    looked like it existed and did nothing."""
    source = "use std::sync::mpsc::Receiver;\n" \
             "pub async fn f(rx: Receiver<u8>) -> u8 { rx.recv().unwrap() }\n"
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "blocking_recv_in_async" in smells(run_detector("find_concurrency_issues.py", target))


def test_an_awaited_recv_is_not_blocking(tmp_path):
    source = "pub async fn f(mut rx: tokio::sync::mpsc::Receiver<u8>) -> Option<u8> " \
             "{ rx.recv().await }\n"
    target = write(tmp_path / "t", {"lib.rs": source}) / "lib.rs"
    assert "blocking_recv_in_async" not in smells(run_detector("find_concurrency_issues.py", target))


def test_filter_is_only_suggested_when_the_conditional_guards_the_push(tmp_path):
    """`.filter(…)` changes how many elements come out; an `if` wrapping a side
    effect beside an unconditional push does not."""
    side_effect = "pub fn f(xs: &[u8], tracing: bool, out: &mut Vec<u8>) {\n" \
                  "    for x in xs {\n        if tracing { audit(*x); }\n" \
                  "        out.push(*x);\n    }\n}\nfn audit(_x: u8) {}\n"
    guarding = "pub fn f(xs: &[u8], out: &mut Vec<u8>) {\n" \
               "    for x in xs {\n        if *x > 3 { out.push(*x); }\n    }\n}\n"
    a = run_detector("find_loop_simplifications.py",
                     write(tmp_path / "a", {"lib.rs": side_effect}) / "lib.rs")
    b = run_detector("find_loop_simplifications.py",
                     write(tmp_path / "b", {"lib.rs": guarding}) / "lib.rs")
    assert "filter" not in next(f["suggestion"] for f in a
                                if f["smell_type"] == "loop_building_collection")
    assert "filter" in next(f["suggestion"] for f in b
                            if f["smell_type"] == "loop_building_collection")


def test_a_seeded_accumulator_is_a_fold_not_a_sum(tmp_path):
    """`.sum()` starts from 0 and would silently drop the seed."""
    seeded = "pub fn f(xs: &[u32]) -> u32 {\n    let mut total = 10;\n" \
             "    for x in xs { total += x; }\n    total\n}\n"
    plain = "pub fn f(xs: &[u32]) -> u32 {\n    let mut total = 0;\n" \
            "    for x in xs { total += x; }\n    total\n}\n"
    assert "manual_fold" in smells(run_detector(
        "find_loop_simplifications.py", write(tmp_path / "a", {"lib.rs": seeded}) / "lib.rs"))
    assert "manual_sum" in smells(run_detector(
        "find_loop_simplifications.py", write(tmp_path / "b", {"lib.rs": plain}) / "lib.rs"))
