#!/usr/bin/env python3
"""Run every detector over one parse of the tree, across several processes.

The detectors could each walk the tree themselves — that is what running them
individually does — but `analyze_all` asks twenty-four questions, and re-parsing
every file twenty-four times is most of the runtime on a large crate. Here the
loop is inverted: parse a file once, then ask every question of that one tree,
with the files split across a process pool. The detectors themselves are
untouched; only when and where they are called changed.

Two kinds of detector, scheduled differently:

  * A **file detector** answers from one file (``analyze(rsfile, ignore)``).
    Files are cut into contiguous chunks, one pool task per chunk, and a task
    parses only its own chunk. Across the pool that is one parse of the tree.
  * A **tree detector** needs the whole project at once (``analyze(root,
    ignore, args)``). They run together in a single extra task so they share
    one ``load_project()`` — every one of them calls it.

Order is preserved exactly. Chunks are contiguous slices of ``find_rs_files()``
order and their results are concatenated in chunk order, so the sequence handed
to the sort is the one a sequential runner produced, and the sort is stable.
"""

import importlib
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from common import (
    SEVERITY_RANK,
    configure_output,
    find_rs_files,
    warn_detector_error,
    warn_unparseable,
)
from rsparse import RustSyntaxError, parse_file


def default_jobs() -> int:
    """Workers to use when the caller did not say. The cores we may actually run on."""
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        return max(1, len(affinity(0)))
    return max(1, os.cpu_count() or 1)


def _sort_records(records: list[dict]) -> list[dict]:
    """The sort `common.emit` applies, on records that are already dicts."""
    records.sort(key=lambda r: (SEVERITY_RANK.get(r.get("severity", "medium"), 1),
                                r.get("file", ""), r.get("line", 0)))
    return records


def chunk(items: list, count: int,
          weight: Callable[[object], int] | None = None) -> list[list]:
    """Split into at most ``count`` contiguous slices of near-equal weight.

    Slices stay **contiguous** because that is what preserves finding order: the
    shards are concatenated in order, so the sequence reaching the sort is the
    one a single sequential pass would have produced.

    ``weight`` balances by something other than item count. Equal counts is the
    wrong split for source files — a shard of four large modules costs several
    times one of four small ones, and the run waits for the slowest shard.
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


# --------------------------------------------------------------------------- #
# Pool tasks. Module-level so they survive pickling under a spawning start method.
# --------------------------------------------------------------------------- #

def run_file_shard(paths: list[Path], specs: list[tuple[str, str]],
                   ignore: set[str]) -> dict[str, list[dict]]:
    """Parse each file in the shard once, then run every file detector over it."""
    configure_output()
    detectors = []
    out: dict[str, list] = {}
    for category, module_name in specs:
        out[category] = []
        try:
            detectors.append((category, importlib.import_module(module_name).analyze))
        except Exception as exc:  # an unimportable detector must not sink the shard
            out[category] = {"issues": [], "error": f"{type(exc).__name__}: {exc}"}

    # category -> the first per-file failure, so the report can say the category
    # is incomplete rather than letting a zero count read as clean.
    failures: dict[str, str] = {}
    for path in paths:
        try:
            rsfile = parse_file(path)
        except (RustSyntaxError, OSError) as exc:
            warn_unparseable(path, exc)
            continue
        for category, analyze in detectors:
            try:
                out[category].extend(analyze(rsfile, ignore))
            except Exception as exc:  # a detector bug must not read as a clean file
                warn_detector_error(path, exc)
                failures.setdefault(
                    category, f"{type(exc).__name__} on {path.name}: {exc}")

    result: dict[str, object] = {}
    for category, found in out.items():
        if isinstance(found, dict):
            result[category] = found
        elif category in failures:
            # Keep what was collected *and* the error: the findings are real,
            # and the category is incomplete.
            result[category] = {"issues": [asdict(f) for f in found],
                                "error": failures[category]}
        else:
            result[category] = [asdict(f) for f in found]
    return result


def run_tree_task(root: Path, specs: list[tuple[str, str]],
                  ignore: set[str]) -> dict[str, list[dict]]:
    """Run the whole-tree detectors, which share this process's one parse.

    One task for all of them: every tree detector goes through
    `rsproject.load_project`, which returns the same Project for a repeated
    root. Splitting them across processes would buy parallelism at the price of
    a full tree parse each — the very cost this is here to avoid.
    """
    configure_output()
    # run_tree_detector hands its detectors the parsed argv; none of them read it.
    args = SimpleNamespace(path=str(root), format="json", ignore="")
    out: dict[str, list[dict]] = {}
    for category, module_name in specs:
        try:
            analyze = importlib.import_module(module_name).analyze
            out[category] = [asdict(f) for f in analyze(Path(root), ignore, args)]
        except Exception as exc:  # one failing detector must not sink the report
            out[category] = {"issues": [], "error": f"{type(exc).__name__}: {exc}"}
    return out


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

def run_detectors(path: str, file_specs: list[tuple[str, str]],
                  tree_specs: list[tuple[str, str]], *, jobs: int | None = None,
                  ignore: set[str] | None = None) -> dict[str, object]:
    """Run every named detector and return category -> records (or an error dict).

    ``file_specs`` and ``tree_specs`` are ``(category, module_name)`` pairs.
    """
    ignore = set(ignore or ())
    root = Path(path)
    jobs = jobs or default_jobs()

    files = list(find_rs_files(root))
    shards = chunk(files, jobs, weight=_source_size)

    results: dict[str, object] = {}

    if jobs == 1:
        for shard in shards:
            _absorb(results, run_file_shard(shard, file_specs, ignore))
        if tree_specs:
            _absorb(results, run_tree_task(root, tree_specs, ignore))
        return _finish(results, file_specs, tree_specs)

    # More workers than tasks just costs process startup.
    workers = min(jobs, max(1, len(shards) + (1 if tree_specs else 0)))
    try:
        pool = ProcessPoolExecutor(max_workers=workers)
    except (OSError, NotImplementedError, ImportError, ValueError):
        # A sandbox with no working process pool. Same work, one process.
        return run_detectors(path, file_specs, tree_specs, jobs=1, ignore=ignore)

    died = None
    with pool:
        try:
            # The tree task is the long pole — start it before the shards queue up.
            tree_future = pool.submit(run_tree_task, root, tree_specs, ignore) if tree_specs else None
            shard_futures = [pool.submit(run_file_shard, shard, file_specs, ignore)
                             for shard in shards]
            for future in shard_futures:  # in submission order: chunk order is finding order
                _absorb(results, future.result())
            if tree_future is not None:
                _absorb(results, tree_future.result())
        except BrokenProcessPool as exc:
            # A worker died outright — the OOM reaper on a big tree, or a detector
            # that crashed the interpreter. `submit` raises this too once the pool
            # is broken, so it is inside the guard with the waiting.
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
    """Merge one task's output.

    A category that failed carries both its error and whatever it collected
    before failing — dropping the findings would lose real results, and dropping
    the error would let an incomplete category read as a clean one.
    """
    for category, found in part.items():
        incoming = found if isinstance(found, dict) else {"issues": found, "error": None}
        existing = results.get(category)
        if existing is None:
            results[category] = list(incoming["issues"]) if not incoming.get("error") \
                else {"issues": list(incoming["issues"]), "error": incoming["error"]}
            continue
        if isinstance(existing, dict):
            existing["issues"].extend(incoming["issues"])
            if incoming.get("error") and not existing.get("error"):
                existing["error"] = incoming["error"]
        elif incoming.get("error"):
            results[category] = {"issues": existing + list(incoming["issues"]),
                                 "error": incoming["error"]}
        else:
            existing.extend(incoming["issues"])


def _finish(results: dict, file_specs, tree_specs) -> dict:
    for category, _ in [*file_specs, *tree_specs]:
        found = results.setdefault(category, [])
        if isinstance(found, list):
            _sort_records(found)
        elif isinstance(found, dict):
            _sort_records(found.setdefault("issues", []))
    return results
