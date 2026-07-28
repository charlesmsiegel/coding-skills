---
name: python-simplifier
description: Critically review and simplify Python code — aggressively. Use whenever the user wants to simplify, refactor, clean up, reduce complexity, or improve code quality; find code smells, duplication, dead code, over-engineering, naming issues, resource leaks, security risks, mutable-state or exception-handling bugs, import cycles, weak tests, or dated idioms; judge whether a GoF design pattern is warranted; add type annotations; build a characterization-test safety net before refactoring; review AI-generated / vibe-coded code for hallucinated APIs, stubs, swallowed errors, and tests that cannot fail; or clean up a poorly-written-but-working repo from cold. Triggers on "simplify this", "refactor this", "clean this up", "review my code", "is this over-engineered", "analyze this codebase" — even when the user just pastes Python and asks "what do you think?". Combines deterministic AST detectors (run them) with judgment guides in references/ (load them). For Django, use the django-simplifier skill instead.
---

# Python Code Simplifier

A critical-reviewer skill. Its job is to make Python **simpler, more consistent, and
more correct** — by deleting what isn't needed, flattening what's tangled, fixing
real bugs, and converging the codebase on one good way of doing each thing.

## Reviewer mindset (read this first)

Approach the code as **too complex until proven otherwise.** The default question is
not "is this OK?" but **"why isn't this simpler?"** Be specific and unsparing. Two
hard limits keep the criticism honest:

1. **Behavior is sacred.** Never change what the code does. If it isn't tested, write
   a characterization test that pins current behavior *before* refactoring.
2. **Simpler, not cleverer.** Aim for code a tired developer reads at a glance — not
   a showcase of techniques. A clever line that needs a comment has failed.

Bias toward: **deleting** code, **flattening** structure, the **standard library**
over hand-rolled machinery, **one canonical pattern** applied everywhere, and small
behavior-preserving steps. The burden of proof is on complexity, not on its removal.

## How the skill works: two pronged

- **Deterministic scripts** find what can be found mechanically (AST/tokenize, low
  false-positive). **Run them first** and triage their output before reviewing by hand.
- **Judgment guides** in `references/` cover what needs a reading brain — whether an
  abstraction earns its keep, whether duplication is real, whether a pattern is the
  right one applied consistently. **Load the relevant guide** when doing that review.

## Workflow

**Cleaning up a whole poorly-written repo from cold?** The steps below assume the
code already runs, has some tests, and is roughly formatted. When it doesn't, follow
`references/messy-repo-runbook.md` first — get it running, **build a test safety net
before touching anything** (`references/safety-net-and-testing.md`), normalize
formatting in one behavior-free commit, *then* return here to triage. Refactoring
without a net violates rule #1.

1. **Run the analyzer.** `python scripts/analyze_all.py <path>` (add `--format json`
   for tooling). Triage deterministic findings first — don't spend judgment on what a
   tool already caught.
2. **Find the hot files.** Effort follows *change frequency*, not line count:
   ```bash
   git log --since="1 year ago" --name-only --pretty=format: \
     | grep '\.py$' | sort | uniq -c | sort -rn | head -30
   ```
   High-churn × high-complexity = top priority. **Don't refactor cold code.**
3. **Review the hot files with the judgment guides open** (see the reference index).
4. **Produce a findings artifact.** One smell → one entry → one small PR. Turn any
   script's JSON into a list/cards/JSON file: `python scripts/analyze_all.py .
   --format json | python scripts/format_findings.py`. This is the deliverable — see
   *Output & ticketing* below; never create tickets in a tracker without asking.
5. **Ratchet.** When a whole class of problem is cleared, turn on the check that keeps
   it gone (a Ruff rule, a complexity gate, one of these scripts in CI).

## Output & ticketing

The deliverable is always an **artifact, never a side effect.** Produce one of: a
findings **list** (markdown table), detailed **cards**, a **JSON** array, or the full
report from `analyze_all.py` — saved as a file in the workspace or returned inline.
`scripts/format_findings.py` renders any detector's JSON into these shapes.

This skill does **not** create tickets in any system on its own. When the user wants
findings filed as real tickets, **ask which ticket software or MCP to use** (e.g.
Jira, Linear, GitHub Issues, Asana, or a connected MCP) and create them through that
tool — never assume or fabricate a tracker. Absent that, hand back the artifact and
let the user import it.

## Deterministic scripts

```bash
python scripts/analyze_all.py /path           # Run everything, unified report
python scripts/analyze_all.py . --format json > report.json

# Complexity & structure
python scripts/analyze_complexity.py .         # Cyclomatic/cognitive complexity, nesting, size
python scripts/find_duplicates.py .            # AST-normalized duplicate blocks
python scripts/find_coupling_issues.py .       # Feature envy, low cohesion (LCOM), message chains

# Smells, dead code, over-engineering
python scripts/find_code_smells.py .           # Mutable defaults, bare excepts, magic numbers, god classes
python scripts/find_dead_code.py .             # Unused imports/functions/params, unreachable code
python scripts/find_overengineering.py .       # Single-impl interfaces, factories, thin wrappers (YAGNI)
python scripts/find_design_smells.py .         # Classic-catalog smells: type-switches, refused bequest, temporary fields, intimacy
python scripts/find_unpythonic.py .            # range(len), == True/None, manual index tracking

# Correctness bugs (these find real bugs, not style)
python scripts/find_mutation_hazards.py .      # Mutable class attrs, modify-during-iteration, mutated defaults
python scripts/find_exception_issues.py .      # raise-without-from, unreachable except, BaseException, assert-validation
python scripts/find_global_state.py .          # Mutated module globals, global-rebinding functions
python scripts/find_resource_leaks.py .        # open()/socket/tempfile not used as a context manager (fd leaks)
python scripts/find_security_issues.py .       # eval/exec, shell=True, unsafe yaml/pickle, weak hash, hardcoded secrets

# Architecture & repo structure (cross-file)
python scripts/find_import_cycles.py .         # Circular imports, god modules, wildcard imports, logic in __init__
python scripts/find_dependency_issues.py .     # Missing/unused/unpinned third-party deps vs. the manifest

# Safety net (build this BEFORE refactoring — see references/safety-net-and-testing.md)
python scripts/find_untested_modules.py .      # Source modules no test references; "no tests in repo" alarm
python scripts/find_test_smells.py .           # Assertion-less/trivial tests, over-mocking, logic in tests, silent skips

# AI-generated-code tells (see references/ai-generated-code.md)
python scripts/find_ai_scaffolding.py .        # NotImplementedError stubs, pass/... bodies, placeholder values, unused **kwargs
python scripts/find_duplicate_definitions.py . # Same name defined twice (later silently wins); merge-conflict markers
python scripts/find_unawaited_coroutines.py .  # async call created and discarded (silent no-op)
python scripts/find_local_imports.py .         # Imports inside functions / not at top of file (circular-import workarounds)
python scripts/find_redundant_comments.py .    # Comments that just narrate the next line (NOISY — opt-in, not in analyze_all)

# Design & simplification
python scripts/find_pattern_issues.py .        # Design-pattern issues both ways: hand-rolled singletons/builders/iterators/memoize → Python-native form; string state machines, try/finally cleanup → the missing pattern
python scripts/find_parameter_objects.py .     # Data clumps: parameter groups recurring across functions
python scripts/find_boolean_params.py .        # Boolean flag parameters at definitions
python scripts/find_return_issues.py .         # Inconsistent returns, if/else-returns-bool
python scripts/find_loop_simplifications.py .  # Loop→comprehension, += string concat, manual any()/all()
python scripts/find_naming_issues.py .         # Shadowed builtins, non-snake_case funcs, non-PascalCase classes
python scripts/find_comment_smells.py .        # Commented-out code, TODO/FIXME inventory
python scripts/find_debug_leftovers.py .       # pdb.set_trace/breakpoint/ipdb, stray debug prints
python scripts/find_outdated_idioms.py .       # %/format → f-strings, typing.List → list, os.path → pathlib, super(args)
python scripts/find_missing_docstrings.py .    # Public modules/classes/functions with no docstring
python scripts/find_type_gaps.py .             # Missing annotations at API boundaries, Any overuse, broad type:ignore

# Format findings as a portable artifact (does NOT create tickets)
python scripts/format_findings.py report.json                       # markdown list
<any detector> --format json | python scripts/format_findings.py --format cards
<any detector> --format json | python scripts/format_findings.py --format json --min-severity high
```

All detectors share one interface: `--format text|json`, `--ignore type1,type2`, and
🔴/🟡/🟢 severities. JSON output is a flat list of findings; `analyze_all.py`
aggregates them. They are deliberately conservative (false negatives over false
positives) so the output stays trustworthy.

## Use the repo's own tools when they exist

The detectors above are stdlib-only on purpose, but if the target repo's environment
already has the real tools, they are stronger — so use them. `run_external_tools.py`
**detects what is installed in the current environment** (ruff, mypy, black, isort,
bandit, flake8), runs every available one in non-mutating check mode, and merges the
output into this skill's findings shape:

```bash
python scripts/run_external_tools.py .                 # run all available (check only)
python scripts/run_external_tools.py . --format json   # {tools_run, missing_tools, findings}
python scripts/run_external_tools.py . --fix           # also run black/isort/ruff --fix (MUTATES)
```

**When a tool is missing, ask before installing.** The script never installs anything;
it lists each absent tool with a `pip install` hint under `missing_tools`. When that
list is non-empty and the tools would help the task, **ask the user whether to install
them** (e.g. via the AskUserQuestion tool) and only install on confirmation — never
auto-install into someone's environment.

## Reviewing a change request (diff lens)

For an AI-written feature or CR, review *what changed*, not the legacy around it.
`analyze_diff.py` runs the file-level detectors against only the changed files, and by
default only the added/modified lines:

```bash
python scripts/analyze_diff.py                 # working tree vs. merge-base with the default branch
python scripts/analyze_diff.py origin/main     # vs. an explicit base ref
python scripts/analyze_diff.py --format json | python scripts/format_findings.py
```

Whole-repo detectors (import cycles, dependency hygiene, untested modules, duplicate
code) need the full tree — run those with `analyze_all.py` separately. See
`references/ai-generated-code.md` for the AI-CR review stance.

## Reference index (load on demand)

Keep `SKILL.md` lean; pull in depth only when a review needs it.

| Load this when… | File |
|---|---|
| Starting any review — the master stance, workflow, critical-questions checklist, triage | `references/critical-review-guide.md` |
| Deciding whether an abstraction should exist; DRY vs the wrong abstraction; YAGNI | `references/overengineering-and-abstraction.md` |
| Diagnosing design smells — the full classic catalog (bloaters, OO abusers, change preventers, dispensables, couplers) and triaging detector candidates | `references/refactoring-catalog.md` |
| Executing a fix — the named refactoring techniques, safe step-by-step mechanics, Python equivalents of the classic moves | `references/refactoring-techniques.md` |
| Judging names, comments, and function shape; deleting comments that lie | `references/naming-comments-readability.md` |
| Choosing the right Python pattern AND making the codebase use it consistently | `references/patterns-and-consistency.md` |
| Judging design patterns (GoF + Python-specific): when a pattern is warranted, when it's ceremony, the Python-native form of each, smell→pattern and pattern→simpler maps | `references/design-patterns.md` |
| You want concrete before/after idiom swaps | `references/python-idioms.md` |
| Cleaning up a whole poorly-written-but-working repo from cold — the phased campaign (run it, net it, normalize, triage, ratchet) | `references/messy-repo-runbook.md` |
| Building the test safety net before refactoring — coverage maps, characterization & golden-master tests, spotting hollow tests | `references/safety-net-and-testing.md` |
| Adopting type hints incrementally, modernizing dated idioms, and fixing a dishonest dependency manifest | `references/typing-and-modernization.md` |
| Reviewing AI-generated code or vibe-coded features/CRs — hallucinated APIs, plausible-but-wrong logic, scaffolding, fake robustness, tests that can't fail | `references/ai-generated-code.md` |

## Over-engineering anti-patterns (quick reference)

| Pattern | Problem | Fix |
|---|---|---|
| Single-impl interface/ABC | Abstraction over one thing | Merge; a mock is not a second impl |
| Unnecessary factory | Builds one type | Direct instantiation |
| Premature strategy | One strategy | A function |
| Hand-rolled Singleton/Borg | Global state with ceremony | Module-level instance; better, pass it in |
| Stateless strategy/command classes | Classes standing in for functions | Plain functions + dispatch dict |
| Fluent builder | kwargs reimplemented as a class | Keyword arguments / dataclass |
| Thin wrapper / middle man | Only forwards calls | Use the wrapped object |
| Speculative generality | Code for "future needs" | Delete it (YAGNI) |
| Config never varied | A param with one value | Hardcode; drop the param |
| Deep inheritance | 4+ levels / reuse-by-inheritance | Composition |

## Code smells (quick reference → fix)

| Smell | Fix |
|---|---|
| Mutable default `def f(x=[])` | Default `None`, create inside |
| Mutated shared mutable (class attr / global / default) | Encapsulate or make per-instance |
| Bare/broad except, swallowed error, `raise` without `from` | Catch narrow, chain with `from`, never `pass` |
| God class / long function / deep nesting | Extract class/method; guard clauses |
| Feature envy / message chains | Move method; hide delegate |
| Reaching into `other._private` internals | Move method, or expose an intentional API |
| Field that is `None` except in one method | Pass parameters or extract the method object |
| Subclass that no-ops/raises on inherited methods | Composition (refused bequest) |
| Same statement in every if/elif branch | Hoist it out of the conditional |
| Bool flag steering a while loop | `break`/`return` at the decision point |
| Data clump (params travelling together) | Bundle into a dataclass |
| Boolean flag parameter | Split the function or use an enum |
| if/elif type-switch | Dispatch dict or polymorphism |
| String-typed state machine (`self.state == "..."` across methods) | Enum; dispatch/State pattern if behavior branches |
| `get_x()`/`set_x()` accessor pair | Plain attribute (`@property` when logic arrives) |
| Hand-rolled lazy property / memoize dict | `functools.cached_property` / `lru_cache` |
| `__iter__`/`__next__` iterator class | Generator function (`yield`) |
| `__del__` for cleanup; try/finally that only closes | Context manager (`with`) |
| Magic numbers/strings | Named constants / enums |
| Commented-out code | Delete it (git remembers) |
| `f = open(...)` without `with` | Use `with open(...) as f:` (deterministic close) |
| `eval`/`exec`, `shell=True`, `yaml.load`, hardcoded secret | Remove dynamic eval, pass arg lists, `yaml.safe_load`, load secrets from env |
| Circular import / god module / `from x import *` | Break the cycle, split by responsibility, import names explicitly |
| `pdb.set_trace()` / `breakpoint()` / stray `print` | Delete before committing; use logging if needed |
| `"%s" % x`, `"{}".format(x)`, `typing.List`, `super(C, self)` | f-string, builtin generics / `X \| Y`, bare `super()` |
| Missing annotation / docstring on public API | Annotate boundaries (adopt mypy), add an intent-revealing docstring |
| Untested module about to be refactored | Pin behavior with a characterization test first |
| Import inside a function / not at top | Move to module top; fix the real circular import instead of deferring |
| `NotImplementedError`/`pass` stub, placeholder value, unused `**kwargs` | Finish it or delete the scaffolding |
| Same function/class defined twice; `<<<<<<<` markers | Remove the duplicate / resolve the conflict (the later def silently wins) |
| Coroutine called without `await` | `await` it (or wrap in `asyncio.create_task`/`gather`) |
| `except Exception: pass` / `print(e)` then continue | Handle and re-raise, or catch a narrow type — don't swallow |
| Test that asserts nothing or only `assert True`/`assertIsNotNone` | Assert the actual expected value/behavior |

## When NOT to simplify

- Untested code — write a characterization test first, *then* refactor.
- Hot paths — measure before trading clarity for speed.
- Code being replaced or retired soon.
- Complexity genuinely forced by an external API or a real, present requirement.
- Cold code that never changes and blocks nothing — leave it; fix what churns.

## Relationship to Ruff and type checkers

These scripts complement linters; they don't replace them. Some detectors overlap
Ruff rule sets — naming (`N`), flake8-builtins (`A`), commented-out code (`ERA`),
TODOs (`TD`), some return/loop simplifications (`RET`, `SIM`, `PERF`), boolean traps
(`FBT`), debug leftovers (`T10`/`T20`), outdated idioms (`UP` pyupgrade), missing
docstrings (`D`), missing annotations (`ANN`), and security (`S`/bandit). If the repo
already runs those Ruff rules, disable the matching detector via `--ignore` (or skip
it in the analyzer) to avoid double-reporting — or better, run the real tools with
`run_external_tools.py` (above) and lean on the detectors only for what the tools
don't cover. The unique value here is the **bug-finding and design detectors**
(mutation hazards, exception chaining, global state, resource leaks, import cycles,
unawaited coroutines, data clumps, coupling, duplication), the **repo-level checks**
(dependency hygiene, untested-module and test-smell detection that scaffold a safety
net), the **AI-code tells** (scaffolding, duplicate definitions, fake robustness),
plus the **judgment guides** — which no linter provides.
```
