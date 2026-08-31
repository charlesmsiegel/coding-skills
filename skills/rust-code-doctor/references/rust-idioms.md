# Rust idioms: before and after

Concrete swaps. Each is shorter, and each says something the loop or the match
left the reader to infer.

## Options and Results

```rust
// before
let name = match user.name { Some(n) => n, None => String::from("anonymous") };
// after
let name = user.name.unwrap_or_else(|| "anonymous".to_string());

// before — the fallback is built on every call, then usually thrown away
let cfg = maybe.unwrap_or(expensive_default());
// after
let cfg = maybe.unwrap_or_else(expensive_default);

// before
if opt.is_some() { let v = opt.unwrap(); use_it(v); }
// after
if let Some(v) = opt { use_it(v); }

// before
let x = match parse(s) { Ok(v) => v, Err(e) => return Err(e) };
// after
let x = parse(s)?;

// before
match find(k) { Some(v) => Some(transform(v)), None => None }
// after
find(k).map(transform)

// before — two spellings of "nothing"
struct Config { retries: Option<Option<u32>> }
// after
enum Retries { Unset, Disabled, Times(u32) }
```

`let … else` is the one to reach for when the failure path diverges:

```rust
let Some(user) = lookup(id) else {
    return Err(Error::NotFound(id));
};
// `user` is in scope below, unindented, for the rest of the function.
```

## Iteration

```rust
// before
for i in 0..items.len() { total += items[i].price; }
// after
let total: u64 = items.iter().map(|item| item.price).sum();

// before
let mut out = Vec::new();
for x in &xs { if x.active { out.push(x.name.clone()); } }
// after
let out: Vec<_> = xs.iter().filter(|x| x.active).map(|x| x.name.clone()).collect();

// before
for i in 0..a.len() { merge(&a[i], &b[i]); }        // panics if b is shorter
// after
for (x, y) in a.iter().zip(b.iter()) { merge(x, y); }

// before
let mut found = None;
for x in &xs { if x.id == target { found = Some(x); break; } }
// after
let found = xs.iter().find(|x| x.id == target);

// before
for maybe in results { if let Some(v) = maybe { use_it(v); } }
// after
for v in results.into_iter().flatten() { use_it(v); }

// before
xs.iter().filter(|x| p(x)).next()
// after
xs.iter().find(|x| p(x))
```

And the reverse direction, which matters just as much: **keep the loop** when a
step depends on the previous one, when the body `.await`s something rate-limited,
when it needs `break` with a value and the adaptor version needs a `try_fold`
nobody will read, or when the imperative form is genuinely shorter.

## Strings and allocation

```rust
String::from("")            →  String::new()
format!("{}", x)            →  x.to_string()
format!("{}", name)         →  format!("{name}")        // 1.58+, in any format macro
s.push_str("x")             →  s.push('x')
v.iter().cloned().collect() →  v.to_vec()
s.len() == 0                →  s.is_empty()
s == ""                     →  s.is_empty()
if c { true } else { false }→  c
```

## Signatures

```rust
fn f(s: &String)            →  fn f(s: &str)
fn f(v: &Vec<T>)            →  fn f(v: &[T])
fn f(p: &PathBuf)           →  fn f(p: &Path)
fn f(b: &Box<T>)            →  fn f(b: &T)
fn get_name(&self) -> &str  →  fn name(&self) -> &str
fn into_bytes(&self)        →  fn to_bytes(&self)   // `into_` must consume
fn valid(&self) -> bool     →  fn is_valid(&self) -> bool
```

The generic version, when a function should take either:

```rust
fn f(name: impl Into<String>)      // callers pass &str or String, one allocation at most
fn f(path: impl AsRef<Path>)       // the standard-library convention for paths
```

## Types

```rust
// before — a truncation that compiles
let n = big_u64 as u32;
// after — a failure you handle
let n = u32::try_from(big_u64)?;

// before — the caller can only print it
fn parse(s: &str) -> Result<Config, String>
// after — the caller can match on it
#[derive(Debug, thiserror::Error)]
enum ParseError {
    #[error("missing field {0}")] Missing(&'static str),
    #[error(transparent)] Io(#[from] std::io::Error),
}
fn parse(s: &str) -> Result<Config, ParseError>

// before — any two arguments can be swapped silently
fn transfer(from: u64, to: u64, amount: u64)
// after — the swap is a compile error, and costs nothing at runtime
struct AccountId(u64);
fn transfer(from: AccountId, to: AccountId, amount: Cents)
```

## Errors

```rust
// applications: anyhow, with context at each layer
let cfg = fs::read_to_string(&path)
    .with_context(|| format!("reading config from {}", path.display()))?;

// libraries: a concrete enum, so downstream code can branch
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("key {0} not found")] NotFound(String),
    #[error("backend unavailable")] Unavailable(#[source] io::Error),
}
```

Never `.map_err(|_| MyError::Whatever)`. The cause is the only part of an error
that helps at 3am; `#[from]` and `#[source]` keep it for free.

## Construction

```rust
// before
Foo { name: name, size: size, tag: tag }
// after
Foo { name, size, tag }

// before, for a struct with many optional fields
Foo::new(a, None, None, Some(x), None)
// after
Foo { a, x, ..Default::default() }
```
