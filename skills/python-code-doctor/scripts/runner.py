#!/usr/bin/env python3
"""Run every detector over one parse of the tree, across several processes.

The detectors used to be subprocesses, one per category, and each of them
re-walked and re-parsed the whole tree: thirty-odd parses of every file to ask
thirty-odd questions, in sequence, on one core.

Two changes, and neither of them touches what a detector looks for:

* **Parse once.** The loop is inverted. Instead of *for each detector: for each
  file*, it is *for each file: ask every detector*, so `common.cached_parse`
  hands the second and later callers the first one's tree. The cache holds one
  file, which is all file-major order ever needs and all a large repository can
  afford.
* **Run in parallel.** Files are cut into contiguous chunks, one pool task per
  chunk. Detectors that need the whole tree at once cannot be chunked, so they
  run together in one further task alongside the chunks.

Order is preserved by construction. Chunks are contiguous slices of
`find_python_files()` order and their results are concatenated in chunk order,
so the sequence reaching the sort is the one the sequential runner produced —
and every sort here is the detector's own, which is stable.
"""

import importlib
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, is_dataclass
from pathlib import Path

from common import (
    configure_output,
    find_python_files,
    warn_detector_error,
)

def _standard_key(record: dict):
    """common.sort_findings' key, over records that are already dicts.

    A detector that orders its report differently says so with a module-level
    `sort_key`, and one that filters by default with a `default_filter` — the
    same arrangement as `to_record`. Asking the module means the runner holds no
    opinion about any particular detector, and a detector cannot change how its
    CLI reports without the pooled run following.
    """
    return (record.get("severity") != "high", record.get("severity") != "medium",
            record.get("file", ""), record.get("line", 0))


def default_jobs() -> int:
    """Workers to use when the caller did not say: the cores we may actually run on."""
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        return max(1, len(affinity(0)))
    return max(1, os.cpu_count() or 1)


def chunk(items: list, count: int, weight=None) -> list[list]:
    """Split into at most ``count`` contiguous slices of near-equal weight.

    Slices stay **contiguous** because that is what preserves finding order: the
    shards are concatenated in order, so the sequence reaching the sort is the
    one a single sequential pass would have produced.

    ``weight`` balances by something other than item count. Equal counts is the
    wrong split for source files — a shard of four large modules costs several
    times one of four small ones, and the run waits for the slowest shard. The
    imbalance grows with worker count, which is exactly when it hurts: on this
    repository, splitting 161 files by count gave the heaviest of 16 shards 1.5x
    the average, and by bytes it is within a few percent.
    """
    if not items:
        return []
    count = max(1, min(count, len(items)))
    if weight is None:
        size, extra = divmod(len(items), count)
        out, start = [], 0
        for index in range(count):
            stop = start + size + (1 if index < extra else 0)
            out.append(items[start:stop])
            start = stop
        return out

    weights = [max(1, weight(item)) for item in items]
    target = sum(weights) / count
    out, current, carried, placed = [], [], 0, 0
    for index, item in enumerate(items):
        current.append(item)
        carried += weights[index]
        remaining = len(items) - index - 1
        # Close the slice once it has its share, but never leave a later slice
        # with nothing: `remaining` has to cover the shards still unopened.
        if len(out) < count - 1 and carried >= target * (len(out) + 1) - placed \
                and remaining >= count - len(out) - 1:
            out.append(current)
            placed += carried
            current, carried = [], 0
    if current:
        out.append(current)
    return out


def _source_size(path: Path) -> int:
    """Bytes of source, the closest cheap stand-in for what a file costs to analyse."""
    try:
        return path.stat().st_size
    except OSError:  # vanished or unreadable: let the detectors report it
        return 1


def _records(found, module=None) -> list[dict]:
    """Findings as the JSON records their own detector would print.

    A detector that shapes its records itself says so with a module-level
    `to_record`; using it here is what keeps a pooled run and that detector's
    own CLI emitting the same fields.
    """
    to_record = getattr(module, "to_record", None) if module is not None else None
    if to_record is not None:
        return [to_record(f) for f in found]
    return [asdict(f) if is_dataclass(f) else f for f in found]


# --------------------------------------------------------------------------- #
# Pool tasks. Module-level so they survive pickling under a spawning start method.
# --------------------------------------------------------------------------- #

def run_file_shard(paths: list[Path], specs: list[tuple[str, str]],
                   ignore: set[str]) -> dict[str, object]:
    """Ask every file detector about each file in the shard, parsing it once."""
    configure_output()
    detectors, modules, out = [], {}, {}
    for category, module_name in specs:
        out[category] = []
        try:
            module = importlib.import_module(module_name)
            modules[category] = module
            detectors.append((category, module.analyze_file))
        except Exception as exc:  # an unimportable detector must not sink the shard
            out[category] = {"issues": [], "error": f"{type(exc).__name__}: {exc}"}

    for path in paths:
        for category, analyze_file in detectors:
            try:
                out[category].extend(analyze_file(path, ignore))
            except Exception as exc:  # a detector bug must not read as a clean file
                warn_detector_error(path, exc)

    return {category: (found if isinstance(found, dict)
                       else _records(found, modules.get(category)))
            for category, found in out.items()}


def run_tree_detector(path: Path, category: str, module_name: str,
                      ignore: set[str]) -> dict[str, object]:
    """Run one whole-tree detector.

    One task per detector rather than one task for all of them, because these
    six share nothing: each walks the tree with its own passes, and the parse
    cache holds one file, so running them back to back in a single process only
    serialises them. Bundling them made the bundle the floor the whole run
    could not go below — 6.5s of a 9.0s run before this was split.

    `analyze_tree` returns records already in the order its own main() prints,
    so nothing downstream re-sorts them: one detector, one task, no shard merge
    to disturb the order.
    """
    configure_output()
    try:
        module = importlib.import_module(module_name)
        return {category: _records(module.analyze_tree(path, ignore), module)}
    except Exception as exc:  # one failing detector must not sink the report
        return {category: {"issues": [], "error": f"{type(exc).__name__}: {exc}"}}


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def run_detectors(path: str, file_specs: list[tuple[str, str]],
                  tree_specs: list[tuple[str, str]], *, jobs: int | None = None,
                  ignore: set[str] | None = None) -> dict[str, object]:
    """Run every named detector and return category -> records (or an error dict).

    Both spec lists are ``(category, module)``. A file module is entered through
    ``analyze_file(path, ignore)``, a tree module through
    ``analyze_tree(path, ignore)``.
    """
    ignore = set(ignore or ())
    root = Path(path)
    jobs = jobs or default_jobs()
    shards = chunk(list(find_python_files(root)), jobs, weight=_source_size)
    results: dict[str, object] = {}

    if jobs == 1:
        for shard in shards:
            _absorb(results, run_file_shard(shard, file_specs, ignore))
        for category, module_name in tree_specs:
            _absorb(results, run_tree_detector(root, category, module_name, ignore))
        return _finish(results, file_specs, tree_specs)

    # More workers than tasks just costs process startup.
    workers = min(jobs, max(1, len(shards) + len(tree_specs)))
    try:
        pool = ProcessPoolExecutor(max_workers=workers)
    except (OSError, NotImplementedError, ImportError, ValueError):
        # A sandbox with no working process pool. Same work, one process.
        return run_detectors(path, file_specs, tree_specs, jobs=1, ignore=ignore)

    # File shards are the longest tasks, so they are queued first: a worker that
    # finishes one picks up a tree detector rather than the other way round.
    died = None
    with pool:
        try:
            shard_futures = [pool.submit(run_file_shard, shard, file_specs, ignore)
                             for shard in shards]
            tree_futures = [pool.submit(run_tree_detector, root, category, module_name, ignore)
                            for category, module_name in tree_specs]
            for future in shard_futures:  # in submission order: chunk order is finding order
                _absorb(results, future.result())
            for future in tree_futures:
                _absorb(results, future.result())
        except BrokenProcessPool as exc:
            # A worker died outright — killed by the OOM reaper on a big tree, or
            # taken down by a detector that called sys.exit or crashed a C
            # extension. `submit` raises this too once the pool is broken, so it
            # is inside the guard with the waiting.
            died = exc

    # Outside the `with`, so the dead pool is shut down before the retry rather
    # than held open for the length of a second full run.
    if died is not None:
        print(f"⚠️  a worker process died ({died}); re-running single-process. "
              "Pass --jobs 1 to skip the wasted attempt, or --skip to drop a "
              "category that cannot finish.", file=sys.stderr)
        return run_detectors(path, file_specs, tree_specs, jobs=1, ignore=ignore)

    return _finish(results, file_specs, tree_specs)


def _absorb(results: dict, part: dict) -> None:
    """Merge one task's output, keeping an error dict as the category's whole result."""
    for category, found in part.items():
        if isinstance(found, dict):  # an error, which replaces whatever it names
            results[category] = found
        elif isinstance(results.get(category), dict):
            continue  # already failed; nothing to append to
        else:
            results.setdefault(category, []).extend(found)


def _finish(results: dict, file_specs, tree_specs) -> dict:
    """Filter and order each category exactly as its own main() would."""
    for category, module_name in file_specs:
        found = results.setdefault(category, [])
        if not isinstance(found, list):
            continue  # an error dict, whose category has no records to order
        # Reaching here means a worker already imported this module successfully,
        # so the import cannot fail: a module that would not import left the error
        # dict skipped just above.
        module = importlib.import_module(module_name)
        default_filter = getattr(module, "default_filter", None)
        if default_filter is not None:
            found = default_filter(found)
            results[category] = found
        found.sort(key=getattr(module, "sort_key", _standard_key))
    for category, _ in tree_specs:
        results.setdefault(category, [])  # already ordered by its analyze_tree
    return results
