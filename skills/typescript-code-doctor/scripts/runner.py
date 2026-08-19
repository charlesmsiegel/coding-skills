#!/usr/bin/env python3
"""Run every detector over one parse of the tree, across several processes.

The detectors used to be subprocesses, one per category, and each of them
re-walked and re-parsed the whole tree: twenty-four parses of every file to ask
twenty-four questions, on one core. On a large repository that re-parsing was
most of the runtime.

Here the loop is inverted — parse a file once, then ask every question of that
one tree — and the files are split across a process pool. The detectors
themselves are untouched; only when and where they are called changed.

Two kinds of detector, scheduled differently:

  * A **file detector** answers from one file (``analyze(tsfile, ignore)``).
    Files are cut into contiguous chunks, one pool task per chunk, and a task
    parses only its own chunk. Across the pool that is one parse of the tree.
  * A **tree detector** needs the whole project at once (``analyze(root,
    ignore, args)``). They run together in a single extra task so they share one
    ``load_project()``; six of the seven call it separately today.

Order is preserved exactly. Chunks are contiguous slices of ``find_ts_files()``
order and their results are concatenated in chunk order, so the sequence handed
to the sort is the one the sequential runner produced, and the sort is stable.
"""

import importlib
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from common import (
    SEVERITY_RANK,
    configure_output,
    find_ts_files,
    is_declaration_file,
    warn_detector_error,
    warn_unparseable,
)
from tsparse import TsSyntaxError, parse_file


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


def chunk(items: list, count: int) -> list[list]:
    """Split into at most ``count`` contiguous, near-equal slices."""
    if not items:
        return []
    count = max(1, min(count, len(items)))
    size, extra = divmod(len(items), count)
    out, start = [], 0
    for index in range(count):
        stop = start + size + (1 if index < extra else 0)
        out.append(items[start:stop])
        start = stop
    return out


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

    for path in paths:
        try:
            tsfile = parse_file(path)
        except (TsSyntaxError, OSError) as exc:
            warn_unparseable(path, exc)
            continue
        for category, analyze in detectors:
            try:
                out[category].extend(analyze(tsfile, ignore))
            except Exception as exc:  # a detector bug must not read as a clean file
                warn_detector_error(path, exc)

    return {category: (found if isinstance(found, dict) else [asdict(f) for f in found])
            for category, found in out.items()}


def run_tree_task(root: Path, specs: list[tuple[str, str]],
                  ignore: set[str]) -> dict[str, list[dict]]:
    """Run the whole-tree detectors, which share this process's one parse."""
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

    files = [p for p in find_ts_files(root) if not is_declaration_file(p)]
    shards = chunk(files, jobs)

    results: dict[str, object] = {}

    if jobs == 1:
        for shard in shards:
            _absorb(results, run_file_shard(shard, file_specs, ignore))
        if not shards:
            _absorb(results, run_file_shard([], file_specs, ignore))
        if tree_specs:
            _absorb(results, run_tree_task(root, tree_specs, ignore))
        return _finish(results, file_specs, tree_specs)

    try:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            # The tree task is the long pole — start it before the shards queue up.
            tree_future = pool.submit(run_tree_task, root, tree_specs, ignore) if tree_specs else None
            shard_futures = [pool.submit(run_file_shard, shard, file_specs, ignore)
                             for shard in shards]
            for future in shard_futures:  # in submission order: chunk order is finding order
                _absorb(results, future.result())
            if not shard_futures:
                _absorb(results, run_file_shard([], file_specs, ignore))
            if tree_future is not None:
                _absorb(results, tree_future.result())
    except (OSError, NotImplementedError, ImportError):
        # A sandbox with no working process pool. Same work, one process.
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
    for category, _ in [*file_specs, *tree_specs]:
        found = results.setdefault(category, [])
        if isinstance(found, list):
            _sort_records(found)
    return results
