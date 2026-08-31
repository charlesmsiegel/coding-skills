# Building the safety net before you refactor

Refactoring untested code is rewriting and hoping. This is the procedure for
getting to a state where a refactor can be *shown* not to have changed
behaviour.

## Rust's advantage

Unit tests live inside the module they test:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_minimal_header() {
        assert_eq!(parse("v1\n"), Ok(Header { version: 1 }));
    }
}
```

`use super::*` means the test can reach **private** items directly. That removes
the usual reason people skip writing a characterization test — no visibility has
to change, nothing has to be made `pub` "just for tests". There is no excuse
based on access.

`#[cfg(test)]` keeps the module and its dependencies out of the release build. A
`mod tests` without it compiles into the shipped binary.

## Characterization tests

A characterization test pins what the code *does*, not what it should do. It is
not a good test and it is not meant to be — it is a tripwire.

1. Pick the highest-churn untested module (`git log --since="1 year ago"
   --name-only --pretty=format: | grep '\.rs$' | sort | uniq -c | sort -rn`).
2. Call the function with a realistic input.
3. Assert on whatever comes back — even if it looks wrong. **Especially** if it
   looks wrong: that is behaviour someone may depend on, and changing it is a
   separate decision from refactoring.
4. Repeat for the edge cases you can find: empty, one element, the error path.
5. Now refactor. Any change in the pinned behaviour shows up immediately.

Snapshot testing (`insta`) is very good for this. `assert_yaml_snapshot!(output)`
records whatever the function produces today, and `cargo insta review` shows you
the diff when it changes.

## Where tests go

| Location | Sees | Use for |
|---|---|---|
| `#[cfg(test)] mod tests` in the file | private items | unit tests, characterization |
| `tests/*.rs` | the public API only | integration tests, API contracts |
| Doc examples | the public API | documentation that cannot rot — `cargo test` runs them |
| `benches/` | with criterion | performance claims |

Each file in `tests/` is compiled as its own crate against your published API,
which makes it the honest check of whether the public surface is usable.

## Tests that cannot fail

The reason to read a test suite rather than count it. Six shapes:

```rust
#[test]
fn it_works() { let _ = parse("input"); }          // no assertion at all

#[test]
fn it_works() { assert!(true); }                   // tautology

#[test]
#[should_panic]                                    // passes on ANY panic,
fn rejects_bad_input() { parse("bad"); }           // including a typo in setup

#[test]
#[ignore]                                          // not run, not deleted
fn the_important_one() { … }

#[test]
fn timing() { thread::sleep(ms(100)); assert!(done()); }   // flaky by design

#[test]
fn round_trip() { assert_eq!(encode(decode(x)), encode(decode(x))); }  // proves nothing
```

`#[should_panic]` should always carry `expected = "…"`. Without it the test
passes when the setup panics for an unrelated reason, which is exactly the case
you least want to miss.

## The test that proves the test works

Break the implementation on purpose and confirm the test fails. A test suite
that has never been seen red is a suite nobody has verified. This takes thirty
seconds and is the single highest-value thing in this file.

Mutation testing automates it: `cargo mutants` changes your code in small ways
and reports which mutations no test caught. Slow, but the output is a precise
list of untested behaviour.

## Coverage, honestly

`cargo llvm-cov --lcov --output-path lcov.info` gives line coverage.

Coverage answers "did this line execute", not "is this behaviour checked". A
file at 90% with no assertions is 0% tested. Use it the way it is actually
useful: **find the files at zero**, which are the ones nothing exercises at all.
`find_untested_modules.py` answers the cheaper version of the same question
(does any test even import this) with no build required.

## Property tests, for the cases you would not think of

```rust
proptest! {
    #[test]
    fn round_trips(input: Vec<u8>) {
        prop_assert_eq!(decode(&encode(&input)), input);
    }
}
```

Worth the setup for parsers, encoders, and anything with an inverse. `proptest`
shrinks a failing case to the minimal input, which is often the whole debugging
session.

## The order of work in an untested repo

1. `cargo check --all-targets` green. Nothing else matters until it compiles.
2. `cargo fmt` in one behaviour-free commit.
3. Characterization tests for the top five churn files.
4. *Now* start on the findings.

Skipping to (4) is how a cleanup becomes an outage.
