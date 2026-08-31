# Async and concurrency

Rust makes data races impossible and says nothing about the two failures that
actually happen: a lock held across a suspension point, and a blocking call on
the executor's thread.

## The guard-across-await deadlock

```rust
// deadlocks under contention
async fn refresh(&self) -> Result<()> {
    let mut cache = self.cache.lock().unwrap();   // guard alive from here…
    let fresh = fetch().await?;                   // …across this suspension…
    cache.insert(fresh);                          // …until here
    Ok(())
}
```

The task suspends at the `.await` **while holding the lock**. Another task on
the same executor wants that lock, blocks, and now the worker thread is stuck
waiting for a task that cannot be polled. With `std::sync::Mutex` the guard is
not `Send`, so this often fails to compile — which is the good case. Behind an
`Arc`, or with a `tokio::sync::Mutex`, it compiles and deadlocks in production
under load.

Two fixes:

```rust
// 1. Narrow the scope so the guard drops before the await.
let key = { let cache = self.cache.lock().unwrap(); cache.next_key() };
let fresh = fetch(key).await?;
self.cache.lock().unwrap().insert(fresh);

// 2. Use an async-aware lock, whose guard is safe to hold across .await.
let mut cache = self.cache.lock().await;   // tokio::sync::Mutex
let fresh = fetch().await?;
cache.insert(fresh);
```

Prefer (1). An async mutex held across an await still serialises every task that
wants it; it just does so without deadlocking.

**Which mutex:** `std::sync::Mutex` for short critical sections that never
await — it is faster and its guard being non-`Send` is a feature. `tokio::sync::
Mutex` only where the lock must be held across a suspension.

## Blocking on the executor

```rust
async fn handle(req: Request) -> Response {
    std::thread::sleep(Duration::from_secs(1));       // stops the whole worker
    let data = std::fs::read_to_string("big.json")?;  // same
    let out = expensive_pure_computation(&data);      // same, if it takes >100µs
}
```

An async executor multiplexes many tasks onto few threads. A blocking call stops
*every* task scheduled on that thread — including the timeout that was supposed
to cancel this one. The rule of thumb: anything that could take more than about
100µs and is not `.await`ed does not belong in an async fn.

| Blocking | Async |
|---|---|
| `std::thread::sleep` | `tokio::time::sleep(..).await` |
| `std::fs::*` | `tokio::fs::*`, or `spawn_blocking` |
| `reqwest::blocking` | the async `reqwest::Client` |
| `std::process::Command::output` | `tokio::process::Command` |
| a CPU-bound loop | `tokio::task::spawn_blocking`, or a `rayon` pool |

`block_on` inside async code is the sharpest version: on tokio it panics
("Cannot start a runtime from within a runtime"), and elsewhere it deadlocks.

## Sequential awaits

```rust
for id in ids {
    results.push(fetch(id).await?);   // n round trips, one at a time
}
```

Whether this is a bug depends entirely on the work:

- **Independent, bounded list** → `futures::future::try_join_all(ids.iter().map(fetch))`.
- **Independent, large or unbounded list** → a bounded stream:
  `stream::iter(ids).map(fetch).buffer_unordered(16).try_collect().await?`.
  `join_all` over ten thousand ids opens ten thousand connections.
- **Each step feeds the next, the endpoint rate-limits, or the order of side
  effects matters** → keep the loop, and put a one-line comment saying which,
  because the next reader will ask.

Note that `try_join_all` abandons the remaining futures on the first error. If
partial results matter, `join_all` and inspect each `Result`.

## Spawned tasks

```rust
tokio::spawn(background_work());   // the JoinHandle is dropped here
```

Nothing observes this task again. A panic inside it is silent, and when the
parent returns, the task is detached rather than cancelled. Keep the handle and
`.await` or `.abort()` it, or — if detachment is deliberate — bind it and say so:

```rust
let _worker = tokio::spawn(background_work());   // deliberately detached: …
```

Also: **cancellation in Rust is dropping the future**, at an await point of the
runtime's choosing. Any state a task holds when it is dropped mid-operation must
be consistent. That is what makes "cancellation safety" a property you have to
design for — `tokio::select!` documents which of its branches are safe.

## Threads

- `thread::spawn` requires `'static`; `std::thread::scope` does not, and is
  usually what you wanted.
- A `JoinHandle` that is never joined means a panic in the thread is invisible.
- Prefer channels to shared `Arc<Mutex<T>>`. A channel makes the ownership
  transfer explicit and is far easier to reason about under review.
- Lock poisoning (`.lock().unwrap()`) panics when another thread panicked while
  holding the lock. That is usually correct — but `.expect("cache lock
  poisoned")` says so, and where the data is still usable, `PoisonError::
  into_inner` recovers it.

## Reviewing async code

1. Every `.lock()`/`.borrow_mut()` in an `async fn`: does its guard reach an
   `.await`?
2. Every call in an `async fn`: does it block?
3. Every `.await` in a loop: are the iterations independent?
4. Every `spawn`: is the handle kept?
5. Every `select!`: is each branch cancellation-safe?
6. Every `async fn` with no `.await`: why is it async?
