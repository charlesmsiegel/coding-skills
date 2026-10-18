# Evidence for safe deletion

An attractive simplification is a candidate until its premise is established.
Read implementations and contracts, not just names. If the proof depends on an
unsupported assumption, skip the edit and report what evidence is missing.

| Candidate | Evidence that permits simplification | Evidence that is insufficient |
|---|---|---|
| Repeated guard | An earlier check dominates every path to this check; no mutation, callback, concurrency, or re-entry can invalidate it | A type annotation or one caller that already validates |
| Dead private function/branch | All relevant entry points and runtime loading mechanisms are accounted for; branch contradicts an enforced invariant | No textual references, low coverage, or a linter's unused warning |
| Pass-through wrapper | No registration, decoration, identity, dispatch, override, tracing, lazy-loading, or public compatibility role | A body that only calls another function |
| Duplicate implementations | Same preconditions, behavior, ordering, errors, and ownership; existing implementation already fits | Similar-looking loops, or a new helper needing modes/boolean flags |
| Defensive copy or conversion | Ownership and aliasing prove no caller relies on isolation, coercion, iterator consumption, or eager failure | Current tests do not mutate the returned object |
| Exception wrapper/fallback | Its observed exception and recovery behavior is identical and required cleanup remains | The happy path works or the exception is rarely raised |
| Compatibility layer | Its supported versions and consumers are demonstrably retired in the project's actual contract | It looks old or only the newest runtime is installed locally |
| Custom algorithm | An existing primitive matches the edge cases, supported runtime, resource use, and failure behavior | A shorter library call with subtly different semantics |

## Defensive code: prove redundancy

Preserve validation at trust boundaries: user input, files, network responses,
plugins, persisted state, and public APIs. Keep authorization, resource limits,
timeouts, retries, cleanup, and transaction protections unless their redundancy
is proven. Being defensive is not itself a defect. Do not remove a guard simply
because invalid input is absent from local tests.

A private boolean function can often collapse without changing its contract:

```python
# Before: `ready` is a boolean, enforced by the owning class.
def is_ready(self):
    if self.ready:
        return True
    else:
        return False

# After, with the boolean invariant established:
def is_ready(self):
    return self.ready
```

If `ready` can instead hold a truthy object, this changes the return value.
`bool(self.ready)` preserves boolean conversion in that case. Check property
access, exceptions, and evaluation count as well as the happy-path value.

Likewise, `if value is None: raise ValueError(...)` in a public plugin API is
not redundant because three local callers pass non-null values. Removing it
may change both the error type and where failure happens for outside callers.

## Test the seam that disappeared

Target the reason the deleted code existed: null versus absent, empty values,
duplicates, malformed input, exception paths, lazy iteration, repeated calls,
mutation/aliasing, and extension points, selecting the cases this edit affects.
Record existing failures before editing; do not adjust expectations to bless
changed behavior. Keep meaningful coverage and avoid deleting assertions to pay
for implementation. If necessary tests cannot fit within real savings, skip the
refactor. Temporary external probes may supplement, not replace, required
regression coverage.

## Budget traps

| Temptation | Required decision |
|---|---|
| "Still smaller than HEAD" | Compare with the invocation snapshot; pre-existing deletions belong to the user. |
| "The implementation shrank; tests do not count" | Count both. Keep new tests only when the total budget passes. |
| "Removing comments makes the diff negative" | Comments provide no implementation credit. Shrink real code or undo. |
| "The refactor is clearer even though it is longer" | Recommend separately; leave this run within its budget. |
| "The manager expects a deletion" | A verified zero-change run satisfies the contract. Preserve required behavior. |
