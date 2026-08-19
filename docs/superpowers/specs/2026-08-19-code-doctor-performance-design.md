# Code Doctor Performance: Parse Once, Run in Parallel

**Status:** design
**Date:** 2026-08-19

## Problem

`analyze_all.py` in `python-code-doctor` and `typescript-code-doctor` runs each
detector as its own subprocess, in a sequential loop. Every detector re-walks the
tree, re-reads every file, and re-parses it. Nothing is shared.

Measured on this repository (242 Python files, 74k LOC):

| | |
|---|---|
| `python-code-doctor/analyze_all.py .` | **49.1 s** |
| walk + read + `ast.parse` the whole tree, once | 0.53 s |
| 32 detectors × that parse | ~17 s — **35% of runtime** |

Measured on a 200-file / 15.6k-LOC TypeScript corpus:

| | |
|---|---|
| `typescript-code-doctor/analyze_all.py` | **19.3 s** |
| `tsparse` over the corpus, once | 0.60 s |
| 24 parses (18 file detectors + 6 `load_project` calls) | ~15 s — **78% of runtime** |

TypeScript degrades worse because the hand-rolled lexer costs ~38 µs/LOC against
CPython's ~7 µs/LOC for `ast.parse`.

Both numbers are on a 4-core box where three cores are idle for the whole run.

`django-code-doctor` does not have this problem. `analyze_django.py` builds one
context and hands it to all fifteen detectors, and says why in its docstring:
*"parsing a large Django project fifteen times to ask fifteen questions is most
of the runtime."* This design carries that technique into the other two doctors
and adds the parallelism none of them have.

## Goal and non-goals

**Goal:** the same reports, in materially fewer seconds.

**Non-goals.** The default stays a full-tree scan — no changed-files-only mode,
no hotspot prefilter, no sampling. Detector logic is not touched; no finding
appears, disappears, or changes text or order. Report JSON stays byte-identical
apart from `meta.timestamp`.

## Design

### Invert the loop

Today the shape is *for each detector: for each file: parse, detect*. It becomes
*for each file: parse once, then run every detector on that one tree*.

That alone removes the redundant parsing, and it bounds memory at one parsed file
rather than a whole tree of ASTs — which matters at a million lines, where holding
every AST at once would cost gigabytes.

### Shard files across processes

Files are split into contiguous chunks, one pool task per chunk. Each worker
parses only its own chunk and runs every per-file detector over it. Total parsing
across the run is one full tree, spread over the cores.

Whole-project detectors (5 in Python, 7 in TypeScript) cannot be sharded — they
need the tree at once. All of them run together in **one** additional pool task,
sharing a single tree load, concurrently with the file shards. TypeScript gains
most here: six of its seven tree detectors each call `load_project()` today, so
six full-tree parses collapse into one.

Wall clock becomes roughly `max(slowest file shard, tree-detector task)` instead
of the sum of everything.

### Preserving output exactly

Order is the thing at risk, and three properties protect it:

- Chunks are **contiguous slices** of the file list in `find_*_files()` order,
  and results are concatenated **in chunk order**. The pre-sort sequence is
  therefore identical to the sequential run's.
- Each detector's own sort is applied after the merge, unchanged. Python's sort
  is stable, so equal keys keep that identical order.
- Per-file `try/except` stays exactly where it is, so a detector that crashes on
  one file still loses only that file and still prints the same stderr warning.

A golden-output harness (Stage 0) proves this rather than asserting it.

### Interfaces

**TypeScript** needs almost no detector changes. All 18 file detectors already
expose `analyze(tsfile, ignore)` through `common.run_file_detector`, and all 7
tree detectors expose `analyze(root, ignore, args)` through `run_tree_detector`.
The orchestrator imports the modules and calls those directly. `load_project()`
gains a per-process memo so the tree detectors sharing a task also share a parse.

**Python** needs a uniform seam, because its 28 file detectors each open-code
read + parse inside `analyze_file`. `common.py` gains:

```python
@dataclass(frozen=True)
class ParsedFile:
    path: Path
    source: str
    lines: list[str]
    tree: ast.AST | None      # None when the file does not parse
    error: Exception | None

def parse_file(filepath: Path) -> ParsedFile:
    """Read and parse once. Never raises; an unparseable file carries .error."""
```

and each detector splits its existing `analyze_file` in two:

```python
def analyze_parsed(pf: ParsedFile, ignore) -> list:
    ...          # the body that is there today, minus read_text and ast.parse

def analyze_file(filepath: Path, ignore) -> list:
    return analyze_parsed(parse_file(filepath), ignore)
```

`analyze_file` keeping its signature is a requirement, not a courtesy:
`tests/conftest.py` runs every detector as a subprocess through its CLI, and
those tests must keep passing untouched.

### Degrading

`--jobs/-j` selects worker count, defaulting to `len(os.sched_getaffinity(0))`.
`--jobs 1` runs everything in-process with no pool — still parse-once, and the
fallback when `ProcessPoolExecutor` is unavailable in a restricted sandbox.

One behaviour is deliberately dropped: the per-detector `subprocess` timeout
(300 s in Python, 900 s in TypeScript). In-process detectors have no equivalent.
A timeout firing today *adds* an error record, so losing it can only make a
report more complete, never less — but it is a real change and is named here.

## Stages

0. **Golden-output harness.** Capture every detector CLI and every `analyze_all`
   JSON over fixture corpora on `main`. Every later stage diffs against it.
1. **TypeScript.** Biggest win, smallest edit — orchestrator plus a `load_project`
   memo. Proves the pattern.
2. **Python.** `ParsedFile` in `common.py`, the `analyze_parsed` split across 28
   detectors, the new orchestrator.
3. **code-doctor.** Same orchestrator shape over its 2 detectors.
4. **Docs.** `--jobs` in each SKILL.md; the performance note in each.

`django-code-doctor` already shares its context. Whether its 15 sequential
`collect(ctx)` calls are worth parallelising is a measurement to take after
Stage 2, not a commitment made here.

## Verification

- Golden JSON diffs clean for every detector CLI and every `analyze_all`, on both
  a fixture corpus and this repository, at `--jobs 1` and `--jobs 4`.
- `pytest tests/` passes unchanged.
- Before/after wall clock recorded for both doctors on the same corpora.
