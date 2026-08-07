# Async, background tasks, and transaction boundaries

Three topics in one file because they share a failure mode: **work that happens
somewhere other than where it is written.** An async view yields, a task runs in
another process, a transaction commits after the block ends — and in each case
the bug is a mismatch between when the code *looks* like it runs and when it
actually does.

---

## Async

### Is it worth it at all?

Async pays where a view **waits on the network** — calling three services and
combining the results, streaming, long-polling, websockets.

It does not pay for a database-bound view. The ORM's async methods still hand the
query to a thread; you get the syntax without the concurrency win. A view that
does two queries and renders a template is not faster async, and it is harder to
reason about.

**If the project is entirely sync and performs fine, leave it.** A half-async
Django project is harder to work in than either whole: every function acquires a
colour, and the boundary between them is where the bugs are.

### The three ways it goes wrong

```python
async def dashboard(request):
    order = Order.objects.get(pk=1)          # raises SynchronousOnlyOperation
    order = Order.objects.aget(pk=1)         # returns a coroutine, never runs, raises nothing
    order = await Order.objects.aget(pk=1)   # correct
```

The middle one is the dangerous one. No exception, no query, and `order` is a
coroutine object that will fail later somewhere unrelated — or quietly test as
truthy.

Every sync ORM method has an `a`-prefixed twin: `aget`, `acreate`, `asave`,
`adelete`, `acount`, `aexists`, `afirst`, `aget_or_create`, `abulk_create`,
`arefresh_from_db`. Iteration is `async for`. Evaluating a queryset is
`[o async for o in qs]`.

### Blocking the loop

```python
async def report(request):
    time.sleep(5)                            # blocks EVERY request on this worker
    requests.get("https://slow.example")     # same
```

An async worker serves many requests on one thread. Blocking it does not slow
this request — it stops all of them. Use `await asyncio.sleep(...)` and an async
HTTP client (`httpx.AsyncClient`).

This is how an async rewrite ends up **slower** than the sync version it
replaced: sync workers block one request each, an async worker blocks everything.

### Crossing the boundary

```python
from asgiref.sync import sync_to_async

result = await sync_to_async(legacy_function, thread_sensitive=True)()
```

`thread_sensitive=True` (the default) runs the function in the thread that owns
the database connection, so it can see the caller's open transaction. With
`False` it gets a fresh thread — a different connection, outside your
transaction, unable to see anything uncommitted. State it explicitly for anything
touching the ORM; the reader should not have to know the default.

Note that `sync_to_async` around ORM work is a thread, not concurrency. It stops
the loop blocking; it does not make the query faster.

---

## Background tasks

### Pass a primary key, never an instance

```python
# WRONG
send_receipt.delay(order)

# RIGHT
send_receipt.delay(order.pk)

@shared_task(bind=True, max_retries=3)
def send_receipt(self, order_id):
    order = Order.objects.get(pk=order_id)
```

An instance is serialised whole and the worker then operates on a **snapshot
taken at enqueue time**. Anything that changed in between is invisible, and
saving that snapshot overwrites the newer values. A pk is small, always current,
and safely retryable.

### Enqueue after the commit, not inside it

```python
# WRONG — the worker can start before the transaction commits
with transaction.atomic():
    order = Order.objects.create(...)
    send_receipt.delay(order.pk)          # worker: Order.DoesNotExist

# RIGHT
with transaction.atomic():
    order = Order.objects.create(...)
    transaction.on_commit(lambda: send_receipt.delay(order.pk))
```

The broker is not part of your transaction. A fast worker picks the job up before
the commit lands and queries for a row that does not exist yet. It fails
intermittently and more often under load — which is exactly when it is hardest to
reproduce.

`on_commit` also means the task is **never enqueued at all** if the transaction
rolls back, which is almost always what you want.

Under `TestCase`, `on_commit` callbacks never run at all (the test transaction
rolls back). Use `captureOnCommitCallbacks(execute=True)` to exercise them.

### Idempotence

A task will run twice. The broker redelivers, the worker is killed after doing
the work but before acknowledging, someone retries by hand. Write every task so
running it twice is harmless — check state before acting, or key the side effect
on something unique.

### Django 6.0's built-in tasks

```python
from django.tasks import task

@task
def send_receipt(order_id): ...

send_receipt.enqueue(order.pk)
```

Configured via the `TASKS` setting, with backends for development and testing
built in. For simple background work this is now worth preferring over adding
Celery and a broker — the same rules apply (pass a pk, enqueue on commit, be
idempotent).

---

## Transactions

### What belongs inside `atomic()`

Database writes that must succeed or fail together. That is the whole list.

```python
# WRONG — the transaction is open for the gateway's whole timeout
with transaction.atomic():
    charge = stripe.Charge.create(...)
    order.mark_paid(charge.id)

# RIGHT
charge = stripe.Charge.create(...)
with transaction.atomic():
    order.mark_paid(charge.id)
```

An `atomic()` block holds a connection and, for anything it writes, holds locks.
A network call inside it hands that duration to a third party — including their
timeout. One slow dependency becomes connection-pool exhaustion across the whole
application.

The awkward case is when the external call must be undone on rollback. It cannot
be: that is a **compensating action** (a refund, a reversal), not a transaction.
Design for it explicitly rather than hoping the block covers it.

### Catching `IntegrityError`

```python
# WRONG — the transaction is already marked for rollback
with transaction.atomic():
    try:
        Order.objects.create(reference=ref)
    except IntegrityError:
        order = Order.objects.get(reference=ref)   # TransactionManagementError

# RIGHT — the risky statement gets its own savepoint
with transaction.atomic():
    try:
        with transaction.atomic():
            Order.objects.create(reference=ref)
    except IntegrityError:
        order = Order.objects.get(reference=ref)
```

Once a database error is raised inside a transaction, Django marks it for
rollback and refuses further queries. The handler cannot recover by querying,
because querying is exactly what it may no longer do. A nested `atomic()` is a
savepoint, and rolling back to it leaves the outer transaction usable.

### Locking

```python
with transaction.atomic():
    product = Product.objects.select_for_update().get(pk=pk)
    product.stock -= 1
    product.save(update_fields=["stock"])
```

`select_for_update()` outside a transaction raises — there is nothing for the
lock to be held by.

Prefer `F()` where it fits, because it needs no lock at all:

```python
Product.objects.filter(pk=pk).update(stock=F("stock") - 1)
```

`F()` handles the single-statement case. `select_for_update` is for when the
decision spans several statements — read the stock, decide, then write.

### Long transactions

A loop of saves inside one `atomic()` holds every row it touches locked until the
last iteration commits, so the lock window grows with the data. That is how a
nightly job blocks the application.

Batch it (`bulk_update`), or commit in chunks with a smaller `atomic()` per
chunk.

### `ATOMIC_REQUESTS`

```python
DATABASES = {"default": {..., "ATOMIC_REQUESTS": True}}
```

Wraps every request in a transaction. Attractive in principle, and it makes every
one of the problems above worse: every external call in every view is now inside
a transaction. Prefer explicit `atomic()` around the parts that need it.

---

## Review checklist

**Async**
- [ ] The view actually waits on the network. If it is database-bound, ask why it is async.
- [ ] Every ORM call inside `async def` uses the `a*` twin and is awaited.
- [ ] No `time.sleep`, no synchronous HTTP client.
- [ ] `sync_to_async(..., thread_sensitive=True)` stated explicitly for ORM work.

**Tasks**
- [ ] Primary keys, not instances.
- [ ] `transaction.on_commit` around every enqueue inside `atomic()`.
- [ ] Running the task twice is harmless.
- [ ] `captureOnCommitCallbacks(execute=True)` in the tests that cover it.

**Transactions**
- [ ] No network call, email, or file write inside `atomic()`.
- [ ] `IntegrityError` caught around an inner `atomic()`, not inside the outer one.
- [ ] `select_for_update()` inside a transaction; `F()` preferred where it fits.
- [ ] No unbounded loop of writes in one block.
