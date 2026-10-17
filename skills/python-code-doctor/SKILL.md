---
name: python-code-doctor
description: Critically review and simplify Python code — aggressively. Use whenever the user wants to simplify, refactor, clean up, or reduce complexity; find code smells, duplication, dead code, over-engineering, naming issues, resource leaks, security risks, mutable-state or exception-handling bugs, import cycles, weak tests, or dated idioms; judge whether a design pattern is warranted; add type annotations; build a characterization-test safety net before refactoring; review AI-generated / vibe-coded code for hallucinated APIs, stubs, swallowed errors, and tests that cannot fail; or clean up a poorly-written-but-working repo from cold. Triggers on "simplify this", "refactor this", "clean this up", "review my code", "is this over-engineered", "analyze this codebase" — even when the user just pastes Python and asks "what do you think?". Combines deterministic AST detectors (run them) with judgment guides in references/ (load them).
---

# Python Code Doctor

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

## Running the scripts

Let `SKILL=/path/to/this/skill` — the directory holding this SKILL.md. The commands
below run from the project being reviewed, so they need that prefix; `python
scripts/...` would only work if you had already `cd`'d into the skill itself.

Needs **Python 3.11+** and nothing else — the detectors are stdlib-only, so there is
nothing to install. `run_external_tools.py` additionally drives ruff, mypy, black,
isort, bandit, flake8, pip-audit, and coverage *when they are already installed*; it
reports the ones that are missing rather than failing, and no detector depends on it.

`analyze_all.py` parses each file once and asks every detector about that one tree,
across as many processes as there are cores. On a large repository that is the
difference between minutes and seconds, and it changes nothing about what is
reported. `--jobs N` sets the worker count; `--jobs 1` runs everything in one
process, which is what to use when a crash needs a clean traceback, or when a
tree is large enough that N workers will not fit in memory.

Detectors share one parsed tree per file, so **a detector must not mutate the
tree it is handed** — `find_duplicates` normalises identifiers in the trees it
hashes, which is exactly why it parses for itself instead.

```bash
python "$SKILL/scripts/analyze_all.py" .                   # all cores
python "$SKILL/scripts/analyze_all.py" . --jobs 1          # one process
python "$SKILL/scripts/analyze_all.py" . --skip duplicates # drop the slowest category
```

## Project boundaries and evidence

Installed skills under `.codex/skills`, `.claude/skills`, `.kiro/skills`, and
`.agents/skills` are tooling, not application source. Source walks omit those
payloads while retaining neighboring agent configuration; explicitly scanning a
skill still works. The raw doctor's wider credential scan still inspects installed
payloads for credentials; source exclusions do not hide secrets from that scan.

A mutable class declaration alone is a candidate: framework configuration and
read-only registries share that shape. A demonstrated instance mutation of
unshadowed shared state is a finding. Quoted constant SQL identifiers are also
candidates, since identifiers cannot be passed as query parameters; ordinary
interpolated SQL values remain findings.

Direct literal assignments to named attributes, like named local assignments,
already explain the value's role; arithmetic and comparison thresholds still need
review. Required unittest and Django lifecycle names are exempt from snake-case
checks only on proven imported framework bases. Plain classes, custom camelCase
methods and unresolved inheritance retain the normal naming checks.

Declarative Django model, form and admin classes inherit behavior; proven built-in
bases are not data-container refactoring targets. When other inherited behavior
cannot be resolved, a data-only local declaration is a candidate without a
dataclass replacement recommendation. Plain classes retain the normal check.
Immediate `Meta` classes on proven Django Model/ModelForm parents are configuration;
the same name on unrelated or unresolved parents grants no exemption.

## Workflow

**Cleaning up a whole poorly-written repo from cold?** The steps below assume the
code already runs, has some tests, and is roughly formatted. When it doesn't, follow
`references/messy-repo-runbook.md` first — get it running, **build a test safety net
before touching anything** (`references/safety-net-and-testing.md`), normalize
formatting in one behavior-free commit, *then* return here to triage. Refactoring
without a net violates rule #1.

1. **Run the analyzer.** `python "$SKILL/scripts/analyze_all.py" <path>` (add `--format json`
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
   script's JSON into a list/cards/JSON file: `python "$SKILL/scripts/analyze_all.py" .
   --format json | python "$SKILL/scripts/format_findings.py"`. This is the deliverable — see
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
python "$SKILL/scripts/analyze_all.py" /path           # Run everything, unified report
python "$SKILL/scripts/analyze_all.py" . --format json > report.json

# Complexity & structure
python "$SKILL/scripts/analyze_complexity.py" .         # Cyclomatic/cognitive complexity, nesting, size
python "$SKILL/scripts/find_duplicates.py" .            # AST-normalized duplicate blocks
python "$SKILL/scripts/find_coupling_issues.py" .       # Feature envy, low cohesion (LCOM4), message chains, middle man

# Smells, dead code, over-engineering
python "$SKILL/scripts/find_code_smells.py" .           # Mutable defaults, bare excepts, magic numbers, god classes
python "$SKILL/scripts/find_dead_code.py" .             # Unused imports/functions/params, unreachable code
python "$SKILL/scripts/find_overengineering.py" .       # Single-impl interfaces, factories, thin wrappers (YAGNI)
python "$SKILL/scripts/find_design_smells.py" .         # Classic-catalog smells: type-switches, refused bequest, temporary fields, intimacy
python "$SKILL/scripts/find_unpythonic.py" .            # range(len), == True/None, manual index tracking

# Correctness bugs (these find real bugs, not style)
python "$SKILL/scripts/find_mutation_hazards.py" .      # Mutable class attrs, modify-during-iteration, mutated defaults
python "$SKILL/scripts/find_exception_issues.py" .      # Swallowed errors, unreachable except, BaseException traps, assert-validation, raise-without-from
python "$SKILL/scripts/find_global_state.py" .          # Mutated module globals, global-rebinding functions
python "$SKILL/scripts/find_resource_leaks.py" .        # open()/socket/tempfile not used as a context manager (fd leaks)
python "$SKILL/scripts/find_security_issues.py" .       # eval/exec, shell=True, unsafe yaml/pickle, weak hash, hardcoded secrets

# Architecture & repo structure (cross-file)
python "$SKILL/scripts/find_import_cycles.py" .         # Circular imports, god modules, wildcard imports, logic in __init__
python "$SKILL/scripts/find_dependency_issues.py" .     # Missing/unused/unpinned third-party deps vs. the manifest (an `except ImportError` that visibly degrades = optional, not missing)

# Safety net (build this BEFORE refactoring — see references/safety-net-and-testing.md)
python "$SKILL/scripts/find_untested_modules.py" .      # Source modules no test references; "no tests in repo" alarm
python "$SKILL/scripts/find_test_smells.py" .           # Assertion-less/trivial tests, over-mocking, logic in tests, silent skips

# AI-generated-code tells (see references/ai-generated-code.md)
python "$SKILL/scripts/find_ai_scaffolding.py" .        # NotImplementedError stubs, pass/... bodies, placeholder values, unused **kwargs
python "$SKILL/scripts/find_duplicate_definitions.py" . # Same name defined twice (later silently wins); merge-conflict markers
python "$SKILL/scripts/find_unawaited_coroutines.py" .  # async call created and discarded (silent no-op)
python "$SKILL/scripts/find_local_imports.py" .         # Imports inside functions / not at top of file (circular-import workarounds)
python "$SKILL/scripts/find_redundant_comments.py" .    # Comments that just narrate the next line (NOISY — opt-in everywhere: not in analyze_all, --include-redundant-comments in analyze_diff)

# Design & simplification
python "$SKILL/scripts/find_pattern_issues.py" .        # Design-pattern issues both ways: hand-rolled singletons/builders/iterators/memoize → Python-native form; string state machines, try/finally cleanup → the missing pattern
python "$SKILL/scripts/find_parameter_objects.py" .     # Data clumps: parameter groups recurring across functions (whole-tree; not in the diff lens)
python "$SKILL/scripts/find_boolean_params.py" .        # Boolean flag parameters at definitions
python "$SKILL/scripts/find_return_issues.py" .         # Inconsistent returns, if/else-returns-bool
python "$SKILL/scripts/find_loop_simplifications.py" .  # Loop→comprehension, += string concat, manual any()/all()
python "$SKILL/scripts/find_naming_issues.py" .         # Shadowed builtins, non-snake_case funcs, non-PascalCase classes
python "$SKILL/scripts/find_comment_smells.py" .        # Commented-out code, TODO/FIXME inventory
python "$SKILL/scripts/find_debug_leftovers.py" .       # pdb.set_trace/breakpoint/ipdb, stray debug prints
python "$SKILL/scripts/find_outdated_idioms.py" .       # %/format → f-strings, typing.List → list, os.path → pathlib, super(args)
python "$SKILL/scripts/find_missing_docstrings.py" .    # Public modules/classes/functions with no docstring
python "$SKILL/scripts/find_type_gaps.py" .             # Missing annotations at API boundaries, Any overuse, broad type:ignore

# Format findings as a portable artifact (does NOT create tickets)
python "$SKILL/scripts/format_findings.py" report.json                       # markdown list
<any detector> --format json | python "$SKILL/scripts/format_findings.py" --format cards
<any detector> --format json | python "$SKILL/scripts/format_findings.py" --format json --min-severity high
```

All detectors share one interface: `--format text|json`, `--ignore type1,type2`, and
🔴/🟡/🟢 severities. JSON output is a flat list of findings; `analyze_all.py`
aggregates them and can drop whole categories with `--skip cat1,cat2`
(`--skip-duplicates` is shorthand for the slowest one). They are deliberately
conservative (false negatives over false positives) so the output stays
trustworthy; a file that fails to parse or crashes a detector is named on stderr
rather than silently reported clean.

**Findings and candidates.** Most records assert a defect and carry the fix. A
record that instead carries `"kind": "candidate"` is an *unverified lead* — it
names what was observed, not what is wrong, because the detector reads one file
at a time and the evidence is elsewhere:

| Candidate | Why one file cannot settle it |
|---|---|
| `unused_function` / `unused_class` on a public name | any other module may import it |
| `unused_parameter` on a method or a nested callback | the base class or the caller that receives it dictates the signature |
| `assert_for_validation` on `assert x is not None` / `assert isinstance(...)` | that shape usually narrows a type for the checker and records an invariant the caller guarantees |
| `local_import` of a slow-to-import package (torch, transformers, ...) or inside a CLI command | deferral to keep startup and `--help` fast is the usual reason, and only the author knows |
| `missing_dependency` on a module imported only inside `try`/`except ImportError` where no handler visibly degrades (it raises, exits, or ends any other way), or where the `try` imports several modules and the handler replaces none of this one's names | `except ImportError: sys.exit("pip install x")` is how a script says x is required, and also how an optional extra's entry point reports it is missing; only a handler whose last statement visibly degrades (`pass`, `continue`, a falsy `return`, `x = None` / `x = fallback` / a fallback import of the same name, a `warnings.warn` or logging call) makes the import optional; anything else, from `die()` to `return 1`, is a lead |
| `missing_dependency` on a module imported only after its file puts a directory on `sys.path` that the source cannot place inside the repo: it resolves outside the repo, or reads a function parameter, `os.environ`/`os.getenv`, `sys.argv` or a name bound more than once | a harness run under another program's interpreter imports that program's modules from the directory it adds; only the machine it runs on shows what is there. A path the source resolves inside the repo (under any import alias of `os.path`/`pathlib`), one it cannot follow but that reads none of those run-time sources, and an import in a file that never touches `sys.path` stay findings. **Scope:** one such addition demotes every undeclared module imported only after it in that file, not just what the directory provides: a plugin loader's own `import requests`, or a lazy import in any function below a `def add_dir(d): sys.path.append(d)` helper. Confirm each |
| `unused_parameter` on a method, a nested callback, or a function some module hands out as a value | the base class or the caller that receives it dictates the signature |
| `unused_import` of an absolute import in a package's `__init__.py` | the import makes the name a package attribute another module may read (a relative import there is never reported) |
| `sql_injection` in a schema identifier position or `PRAGMA`, on proven quoted constant identifiers, or in a test | identifiers cannot be bound; test code may construct its own values. Confirm the origin and interpolation position. CREATE data queries containing SELECT or VALUES keep the ordinary data-interpolation finding |
| `swallowed_exception` / `catches_baseexception` under a reasoned `# noqa: BLE001 -- why` | the repo's own lint gate adjudicated the line; the reason is what to check, and a bare noqa stays a finding |
| `global_rebind` under a reasoned `# noqa: PLW0603 -- why` | an at-fork or install-once hook has no caller to return a value to |
| `mutable_class_attribute` that nothing in the module mutates | a read-only table shared on purpose looks the same; only another module's code can show a mutation |
| `raise_without_from` on an `HTTPException` | the framework renders it as a response and never prints the chain |
| `assert_for_validation` outside a public function's argument check | an invariant or type narrowing holds under `-O` too; only the call sites show whether input reaches it |
| `feature_envy`, `try_finally_close` | whether the receiver is a strategy's context, or a context manager, is decided where its class is defined |

Others are candidates because the file states a reason the detector cannot
weigh — a reasoned `# noqa: E402`, a docstring documenting an import order, two
copies of a block (below the rule of three), keyword-only flags, one short
type switch. Each record's `also_caused_by` names the readings to rule out.

Confirm one before acting on it — deleting a "dead" public function on a
single-file verdict deletes live code. `format_findings.py` files candidates as
*Investigate*, not *Refactor*; `analyze_all.py` counts them in
`summary.total_candidates` and lists them under their own heading; and
**code-overview excludes them from a code-health score**, so a candidate never
costs a grade.

**Code copied verbatim from another project.** A parity test that rebuilds
upstream code unchanged, so a diff against it stays readable, cannot be
simplified without defeating its purpose. The author can mark it:

```python
# doctor: verbatim-upstream ComfyUI comfy/ldm/anima/lllite.py
```

On its own line before the module's first statement, the marker covers the
whole file. Directly above a `def` or `class` (or its decorators), it covers
that definition. **It is only for code copied verbatim from another project,
and it must name that source** — never a way to quiet the doctor on code
written here. `analyze_all.py` then does not charge the marked code for
complexity, design, hygiene or duplication; correctness and security detectors
still report inside it. Marked code also never counts toward a duplicate group
or a data clump charged to unmarked code: those are judged on the project's
own copies and signatures. Nothing is hidden. Each marked file gets a
`verbatim_upstream` candidate counting the records not charged, the same count
is in `meta.not_charged`, and a marker that names no source, or sits above no
definition, exempts nothing and is reported as a finding.
`grep -rn "doctor: verbatim-upstream"` lists every opt-in.

## Use the repo's own tools when they exist

The detectors above are stdlib-only on purpose, but if the target repo's environment
already has the real tools, they are stronger — so use them. `run_external_tools.py`
**detects what is installed in the current environment** (ruff, mypy, black, isort,
bandit, flake8, pip-audit, coverage), runs every available one in non-mutating check
mode, and merges the output into this skill's findings shape:

```bash
python "$SKILL/scripts/run_external_tools.py" .                 # run all available (check only)
python "$SKILL/scripts/run_external_tools.py" . --format json   # {tools_run, missing_tools, findings}
python "$SKILL/scripts/run_external_tools.py" . --tools pip-audit,coverage
python "$SKILL/scripts/run_external_tools.py" . --fix           # also run black/isort/ruff --fix (MUTATES)
python "$SKILL/scripts/run_external_tools.py" . --run-coverage  # run the test suite first (SLOW, EXECUTES CODE)
```

Two of these answer questions the stdlib detectors structurally **cannot**, so reach for
them rather than assuming the detectors covered it:

- **pip-audit** — known advisories against the pinned dependencies. `find_dependency_issues.py`
  reads the manifest and can tell you a pin is missing or unpinned; only an advisory
  database can tell you the pin you *have* is vulnerable. It audits `requirements*.txt`
  when the repo has one, and otherwise the installed environment — which it labels,
  because that is a different claim.
- **coverage** — what actually executed. `find_untested_modules.py` answers "does any test
  mention this module"; coverage answers "does this code ever run". It reports only the
  unambiguous cases (a module or function with zero covered statements) and reads existing
  data. `--run-coverage` runs the suite first under `--source`, which is what makes a
  never-imported module show up at 0% instead of vanishing from the report.

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
python "$SKILL/scripts/analyze_diff.py"                 # working tree vs. merge-base with the default branch
python "$SKILL/scripts/analyze_diff.py" origin/main     # vs. an explicit base ref
python "$SKILL/scripts/analyze_diff.py" --format json | python "$SKILL/scripts/format_findings.py"
```

Whole-repo detectors (import cycles, dependency hygiene, untested modules, duplicate
code, dead code, over-engineering, coupling, data clumps) need the full tree — run
those with `analyze_all.py` separately. See `references/ai-generated-code.md` for
the AI-CR review stance.

## Reference index (load on demand)

Keep `SKILL.md` lean; pull in depth only when a review needs it.

| Load this when… | File |
|---|---|
| Reading a file critically — the per-function critical-questions checklist and the finding-triage rubric | `references/critical-review-guide.md` |
| Deciding whether an abstraction should exist; DRY vs the wrong abstraction; YAGNI | `references/overengineering-and-abstraction.md` |
| Diagnosing design smells — the full classic catalog (bloaters, OO abusers, change preventers, dispensables, couplers) and triaging detector candidates | `references/refactoring-catalog.md` |
| Executing a fix — the named refactoring techniques, safe step-by-step mechanics, Python equivalents of the classic moves | `references/refactoring-techniques.md` |
| Judging names, comments, and function shape; deleting comments that lie | `references/naming-comments-readability.md` |
| Choosing the right Python pattern AND making the codebase use it consistently | `references/patterns-and-consistency.md` |
| Judging design patterns (GoF + Python-specific): when a pattern is warranted, when it's ceremony, the Python-native form of each, smell→pattern and pattern→simpler maps | `references/design-patterns.md` |
| You want concrete before/after idiom swaps (the modernization set — f-strings, builtin generics, pathlib — lives in the typing guide's table instead) | `references/python-idioms.md` |
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
| Test that asserts nothing, only `assert True`, or only not-None on a value never None | Assert the actual expected value/behavior |

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
docstrings (`D`), missing annotations (`ANN`), and security (`S`/bandit). Dependency
advisories and line coverage are not a linter's job at all — those come from pip-audit
and coverage through `run_external_tools.py`. If the repo
already runs those Ruff rules, disable the matching detector via `--ignore` (or drop
its whole category with `analyze_all.py --skip`) to avoid double-reporting. Where
they overlap, a suppression with its reason written down — `# noqa: A002 -- the
HTTP parameter is named id` — is read as a reviewed decision: `find_naming_issues.py`
reports that shadow as a candidate, not a finding; a bare `# noqa` changes nothing. Or,
better, run the real tools with
`run_external_tools.py` (above) and lean on the detectors only for what the tools
don't cover. The unique value here is the **bug-finding and design detectors**
(mutation hazards, exception chaining, global state, resource leaks, import cycles,
unawaited coroutines, data clumps, coupling, duplication), the **repo-level checks**
(dependency hygiene, untested-module and test-smell detection that scaffold a safety
net), the **AI-code tells** (scaffolding, duplicate definitions, fake robustness),
plus the **judgment guides** — which no linter provides.

Where a detector mirrors a Ruff rule the repo already enforces, a suppression of
that rule **with a stated reason** — `# noqa: BLE001 -- the caller reads None as
unreadable`, on the flagged line or a comment line directly below it — is read as
the repo's own adjudication: the record becomes a candidate, and the reason is
what to check. A bare `# noqa: BLE001` says nothing and stays a finding. The
detectors that read it: `swallowed_exception` and `catches_baseexception` (BLE001),
and `global_rebind` (PLW0603, on the `global` statement).
