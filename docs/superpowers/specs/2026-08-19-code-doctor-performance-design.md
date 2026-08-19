# Code Doctor Performance: Parse Once, Run in Parallel

**Status:** implemented
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

Two behaviours change, and neither is free:

**The per-detector timeout is gone** (300 s in Python, 900 s in TypeScript).
In-process detectors have no equivalent. Where a detector merely runs long this
is an improvement — a timeout firing replaces findings with an error record. But
a detector that *hangs* used to be killed and reported; now it hangs the run.
That is strictly worse, and the earlier framing of this as "can only make a
report more complete" was wrong.

**A worker can die where a subprocess used to fail alone.** A detector calling
`sys.exit`, a crashing C extension, or the OOM reaper on a large tree takes down
a worker rather than one category. `BrokenProcessPool` is caught and the run
redone in-process, so this costs time rather than correctness — but a run that
cannot fit in memory at N workers needs `--jobs 1`, and says so.

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

## Outcome

| | before | `--jobs 1` | 4 cores |
|---|---|---|---|
| python-code-doctor, 161 files / 44k LOC | 32.7 s | 23.0 s | **7.3 s** |
| typescript-code-doctor, 200 files / 15.6k LOC | 18.3 s | 2.9 s | **1.1 s** |

The `--jobs 1` column is parse-once alone — nearly all of the TypeScript win and
about a third of the Python one; the rest is the pool. TypeScript gains more
because its hand-written scanner is the expensive part, and because six of its
tree detectors were each parsing the whole tree.

Stages 3 and 4 resolved by measurement rather than by writing code:

- **code-doctor** was left alone. Its two detectors read text and never parse,
  they finish a 161-file tree in 1.4 s, and the subprocess isolation its
  docstring argues for is worth more than the ~0.6 s a pool would save.
- **django-code-doctor** was left alone. It already builds its context once, and
  it analyses a synthetic 282-file project in 0.6 s. Parallelising fifteen
  `collect(ctx)` calls that total half a second would mean pickling a large
  context to workers to save nothing.

## Verification

- Golden JSON diffs clean for every detector CLI and every `analyze_all`, on both
  a fixture corpus and this repository, at `--jobs 1` and `--jobs 4`.
- `pytest tests/` passes unchanged.
- Before/after wall clock recorded for both doctors on the same corpora.

## What the review caught

The first implementation of this design was wrong in ways worth recording,
because each was a claim made in prose that the code did not honour.

**The TypeScript memo was documented and never written.** The design says the
tree detectors "share one `load_project()`". They did not: all six still built
their own project, serially, in one task. Measured after the fact, one
`load_project` was 0.69 s and the whole tree task 4.20 s of a 4.5 s run — so
~93% of the TypeScript critical path was the redundant parsing this design
exists to remove. With the memo the tree task is 0.86 s.

**One tree task was a floor, not a plan.** Bundling the whole-tree detectors
together was justified by sharing, which in Python does not exist — they walk
the tree independently and the parse cache holds one file. The bundle was 6.5 s
of a 9.0 s run and no number of cores could go below it. Python now runs one
task per tree detector. TypeScript keeps them bundled, because after the memo
they genuinely do share.

**"Output is unchanged" was true only of stdout.** The old `analyze_all`
captured each subprocess's stderr and discarded it on success; the new one lets
it through. One unparseable file went from 0 warnings to 29 — one per detector.
Surfacing it is right (this skill's whole posture is that a file it could not
read is named rather than counted clean) but 29 times is not, so
`warn_unparseable` now says it once per file per process.

**Shards were balanced by file count, which is not what a shard costs.** Four
large modules take several times what four small ones do, and the run waits on
the heaviest shard. The imbalance grows with worker count — precisely when it
matters: on this repository the heaviest of 16 count-split shards carried 1.5x
the average. Shards are now cut on cumulative source bytes, still contiguous so
order is untouched, and the heaviest is within ~12% of the average.

**The runner grew a second source of truth for ordering.** `to_record` and the
confidence floor were shared with the detectors that own them, but the sort key
was re-spelled in the runner. Python gained `common.sort_findings` — which
TypeScript already had — and the detectors and the runner now sort through one
definition.
