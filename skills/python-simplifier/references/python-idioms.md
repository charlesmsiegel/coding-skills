# Python Idioms — Before/After Swaps

Concrete swaps to suggest in review. Each entry shows the shape a detector flags
(or a reading eye catches) and the idiomatic replacement. For the modernization
set — `%` / `.format()` → f-strings, `typing.List` / `Optional` → builtin
generics and `X | Y`, `super(C, self)` → `super()`, `os.path` → `pathlib` — see
the table in `typing-and-modernization.md`, which pairs each swap with the place
it can bite; those rows are not repeated here.

## Manual index loops → `zip` / `enumerate`

`find_unpythonic.py` flags `range(len(...))` and manual index tracking.

```python
# Before
for i in range(len(names)):
    print(f"{names[i]}: {scores[i]}")

# After
for name, score in zip(names, scores):
    print(f"{name}: {score}")
```

A hand-maintained counter (`i = 0` … `i += 1`) is the same smell — replace it
with `for i, item in enumerate(items, start=1):`.

## Flag loops → `any` / `all`

`find_loop_simplifications.py` flags loops that only compute a boolean.

```python
# Before
found = False
for item in items:
    if item.valid:
        found = True
        break

# After
found = any(item.valid for item in items)
```

`all(...)` replaces the inverted version (start `True`, set `False` on a miss).

## String building with `+=` → `join`

```python
# Before
result = ""
for item in items:
    result += str(item) + ", "

# After
result = ", ".join(str(item) for item in items)
```

## Indexing and temp variables → unpacking

```python
# Before
first = items[0]
last = items[-1]
tmp = a; a = b; b = tmp

# After
first, *_, last = items
a, b = b, a
```

## Copy-then-update → dict unpacking

```python
# Before
config = dict(defaults)
config.update(user_config)

# After
config = {**defaults, **user_config}   # or defaults | user_config (3.9+)
```

## Magic strings → `Enum`

`find_code_smells.py` flags magic values; `find_pattern_issues.py` flags
string-typed state machines.

```python
# Before
if order.status == "aproved":          # typo fails silently at runtime
    ship(order)

# After
class Status(Enum):
    PENDING = auto()
    APPROVED = auto()

if order.status is Status.APPROVED:    # typo is an AttributeError
    ship(order)
```

## `__init__` boilerplate → `dataclass`

```python
# Before
class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y
    def __repr__(self): ...
    def __eq__(self, other): ...

# After
@dataclass(frozen=True)
class Point:
    x: float
    y: float
```

`frozen=True` when instances shouldn't mutate; `__repr__` and `__eq__` come free.

## Manual dict bookkeeping → `collections`

```python
# Before
counts = {}
for word in words:
    counts[word] = counts.get(word, 0) + 1

# After
counts = Counter(words)
```

Likewise `graph.setdefault(src, []).append(dst)` in a loop becomes
`graph = defaultdict(list)` and a plain `graph[src].append(dst)`.

## Repeated setup/teardown → `@contextmanager`

`find_pattern_issues.py` flags try/finally cleanup that wants to be a context
manager. Keep the `finally` inside the generator — it's what guarantees the
teardown runs when the body raises; a bare `yield` would skip it.

```python
# Before (repeated at every call site)
start = time.perf_counter()
try:
    do_work()
finally:
    print(f"operation: {time.perf_counter() - start:.2f}s")

# After
import time
from contextlib import contextmanager

@contextmanager
def timer(name):
    start = time.perf_counter()
    try:
        yield
    finally:
        print(f"{name}: {time.perf_counter() - start:.2f}s")

with timer("operation"):
    do_work()
```

## Lost tracebacks → exception chaining

`find_exception_issues.py` flags `raise` without `from` inside an `except`.

```python
# Before
except ValueError:
    raise ProcessingError("Invalid")        # original cause lost

# After
except ValueError as e:
    raise ProcessingError("Invalid") from e
```

## Intentional ignore → `contextlib.suppress`

```python
# Before
try:
    os.remove("temp.txt")
except FileNotFoundError:
    pass

# After
with suppress(FileNotFoundError):
    os.remove("temp.txt")
```

Only for genuinely ignorable, *narrow* exceptions — `suppress(Exception)` is the
same swallowed-error smell in nicer clothes.
