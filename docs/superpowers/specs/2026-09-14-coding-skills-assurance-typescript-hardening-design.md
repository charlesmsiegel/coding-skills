# Coding Skills Assurance & TypeScript Hardening

**Status:** Proposed (revision 2)
**Date:** 2026-09-14
**Primary repository:** `charlesmsiegel/coding-skills`
**Primary target:** `typescript-code-doctor`
**Secondary targets:** `code-doctor`, `code-overview`, `brutal-review`, `fix-issue`, `fix-pr`, `rust-code-doctor`, `python-code-doctor`, shared CI contracts

## 1. Summary

`coding-skills` already has the right architecture: deterministic analyzers prove
what they can, judgment guides cover what static analysis cannot decide, a
degraded run never reads as a clean repository, and a candidate never scores.
Most of that discipline is already machinery rather than prose — but only in
some of the skills, and only up to the point where an agent stops running
scripts by hand.

This revision of the spec was written after verifying every claim of the first
draft against the repository. The diagnosis survived; the design was overbuilt.
What the repo actually lacks is narrower than a new assurance system:

1. **The finding/candidate contract is enforced in one skill only.** Generic
   `code-doctor` has a frozen, constructor-validated record and ten tests for
   it. `typescript-code-doctor` and `rust-code-doctor` ship an unfrozen record
   with no `kind` field and no validation, so every observation they make is a
   defect with a fix. `python-code-doctor` emits `kind: "candidate"` in plain
   dicts with no class to check them.
2. **Nothing runs the specialists in code.** `route.py` names them and stops;
   `merge_reports.py` unions whatever report files it is handed. The step in
   between is an agent following SKILL.md.
3. **External tools are a side channel.** Each `run_external_tools.py` writes
   its own report shape with no tool versions and no per-tool status; failures
   become synthetic findings; nothing feeds the merged envelope, so
   `code-overview` never sees `tsc`, typed ESLint, coverage, or advisories.
4. **The TypeScript scanner has concrete false-clean and false-positive paths**,
   none covered by a test.
5. **`rust-code-doctor` is not routed or graded.** Packaging, evals, tests, and
   README already include it; routing, the code-overview doctor map, and the
   coverage rubric do not.
6. **External-tool execution has no capability model**, and `npm audit` runs on
   a bare invocation of the TypeScript runner.

The work is therefore a port, three extensions of existing machinery, a set of
test-first bug fixes, and one wiring job. The standalone/offline value of each
language doctor is untouched.

---

## 2. What already exists (and must be preserved)

This section is normative. Later phases extend these mechanisms; none may
reimplement or weaken them.

### 2.1 The record contract — `skills/code-doctor/scripts/common.py:269-335`

One frozen dataclass, `Finding`, with `kind ∈ {"finding", "candidate"}`.
`__post_init__` raises `SchemaError` when:

- `kind` is anything else;
- a finding has a blank `suggestion` or a non-empty `also_caused_by`;
- a candidate has a non-blank `suggestion` or an empty `also_caused_by`.

`also_caused_by` and `related_lines` are coerced to tuples so a validated record
cannot be mutated into an invalid one. `Reporter.finding()` / `.candidate()` are
the sanctioned constructors. Ten tests in `tests/code_doctor/test_common.py`
cover the invariants, freezing, and JSON round-trips; `analyze_all.py:109-113`
re-validates on the merge hop and counts rejected records.

### 2.2 The merged envelope — `skills/code-doctor/scripts/merge_reports.py`

Schema id `code-doctor-merge/1`. Top-level keys:

```
schema, doctors_run, analyzers_run{doctor: [...]}, analyzers_skipped{},
analyzer_errors{}, doctor_errors{}, completeness{}, coverage_unknown[],
findings[], candidates[]
```

Invariants already encoded, each with a test in `tests/code_doctor/test_merge_reports.py`:

- a doctor whose report failed to parse lands in `doctor_errors`, never as zero findings;
- an empty report file is a failure, not a clean run;
- a bare JSON list is accepted but the doctor lands in `coverage_unknown`, and
  its `analyzers_run` is `None` internally rather than `[]`;
- the merger never deduplicates;
- exit 1 when every report failed.

### 2.3 Grading — `skills/code-overview/scripts/rubric.py`, `build_health.py`

- `DOCTOR_COVERAGE` (`rubric.py:286-300`) says which rubric categories each
  doctor can speak to. A category outside a doctor's coverage comes back
  `score=None`, `graded=False`, and is dropped from the weighted mean
  (`build_health.py:50-102`, `rubric.py:399-416`).
- `COMPLETENESS_GATES` ungrades `design`/`tests`/`hygiene` when the producing
  report says `adequate: false` (`rubric.py:424-439`).
- Candidates are split out before scoring (`code-overview/scripts/common.py:234-250`)
  and rendered in their own tab; they are counted, never scored.
- `--assume-full-coverage` yields to a demonstrably crashed doctor.

### 2.4 Preserved invariants

Every phase below must keep these true, and the conformance tests in Phase 1
and Phase 4 exist to prove it:

1. **Failed doctor ≠ zero findings.**
2. **Bare list ≠ known coverage.**
3. **A skipped or failed analyzer ungrades the categories it covers.**
4. **Candidates never score.**
5. **A category nobody measured is ungraded, not 0 and not 100.**

### 2.5 Packaging constraints

- Every script is stdlib-only and avoids PEP 701 f-strings
  (`pyproject.toml:4-9`), because a skill runs on whatever interpreter the user has.
- Each skill directory must install and run alone (`install.sh`,
  `tests/test_standalone_install.py`). A module shared between skills is either
  duplicated byte-identically and pinned by the CI identity check
  (`.github/workflows/ci.yml:48-90`) or lives in exactly one skill. A new shared
  module is not an exception.
- Skills locate siblings by `$(dirname "$SKILL")/<sibling>` and degrade when the
  sibling is absent (`skills/brutal-review/SKILL.md:65-78`).

---

## 3. Goals

1. A maintainer who does not know TypeScript can trust the mechanical output of
   `typescript-code-doctor`: what is labelled a finding is proved, what is a
   lead is labelled a candidate, and what was not checked is visible.
2. The finding/candidate contract is enforced identically in every doctor.
3. Routed specialists are run by a script, not by an agent reading prose.
4. External-tool evidence — compiler, typed linter, coverage, advisories — enters
   the same merged envelope as detector findings, with attribution, tool
   version, per-check status, and the capability it required.
5. An incomplete required check cannot disappear into a clean health report.
6. Reading files, executing a toolchain, executing target code, using the
   network, and mutating the checkout are distinct capabilities. The default is
   the first only.
7. `rust-code-doctor` is routed and graded like the other specialists, and a
   future specialist cannot be added half-wired.
8. `brutal-review`, `fix-issue`, `fix-pr`, and `code-overview` consume one
   mechanical evidence path instead of four prose workflows.
9. All of §2.4 stays true throughout.

### Success criteria

- A TypeScript report never labels a contextual smell a finding because the
  schema has no alternative.
- A checkout under a directory named `tests`, a TSX-only tree, a `.d.mts`
  file, or a monorepo with eleven tsconfigs cannot silently receive wrong analysis.
- The merged envelope states whether `tsc` ran, which version, which effective
  configs it resolved, whether type-aware ESLint ran, and which checks did not run.
- `code-overview` displays health grade and assurance status separately.
- Adding a `*-code-doctor` without routing, coverage, and tests fails CI.
- No networked, mutating, or target-executing check occurs on a default invocation.

---

## 4. Non-goals

1. Replace the standalone Python TypeScript scanner with a Node dependency.
2. Implement the TypeScript type system in Python.
3. Make every smell a blocking defect.
4. Require a project to adopt a particular ESLint preset.
5. Install missing tools.
6. Run tests, package scripts, advisory lookups, or fixers without the
   applicable capability.
7. Redesign the health score beyond what consuming `checks[]` requires.
8. Introduce a second envelope schema alongside `code-doctor-merge/1`.
9. Add a third record kind. Missing assurance is check metadata, not a finding.
10. Merge the doctors into one package to deduplicate code.
11. Install Node or TypeScript in this repository's blocking CI (see §15).

---

## 5. Design principles

**Evidence before judgment.** A detector emits the strongest claim its evidence
supports. A *finding* asserts a defect and carries a fix. A *candidate* reports a
lead and carries the benign explanations instead. No later layer promotes one to
the other silently.

**Missing evidence is metadata, not a record.** A check that did not run, was
blocked, or errored is recorded in `checks[]` and in the existing
`analyzers_skipped` / `analyzer_errors` / `doctor_errors` machinery. It is never
a finding, never a candidate, and never silence.

**Prefer the authoritative system, narrowly.** `tsc` beats the scanner on type
correctness and effective config; typed ESLint beats syntax heuristics on
promise misuse; advisory tooling beats manifest parsing. But a tool's success
proves only what that tool checked. A clean `tsc` run does not grade
Correctness clean; it adds `typecheck` evidence and its errors map into
Correctness.

**Capability is separate from intelligence.** Every external check declares
what authority it needs, and the envelope records what it was granted.

**Standalone skills stay standalone.** Cross-skill consistency is enforced by
conformance tests, not by a shared installed package.

---

## 6. Phase 1 — Port the record contract to every doctor

This is a port, not a design. The target implementation exists at
`skills/code-doctor/scripts/common.py:262-367` and its tests at
`tests/code_doctor/test_common.py:175-296`.

### 6.1 Current state

| Doctor | `kind` / candidate | `also_caused_by` | Constructor validation | Frozen |
|---|---|---|---|---|
| `code-doctor` | yes | yes | yes, `SchemaError` | yes |
| `python-code-doctor` | dict key only (`find_dead_code.py:435,451`, `find_security_issues.py:219`) | read by `format_findings.py` | **none — no record class** | n/a |
| `typescript-code-doctor` | **no** (`common.py:117-130`) | no | none | no |
| `rust-code-doctor` | **no** (`common.py:158-170`) | no | none | no |

### 6.2 Work

1. **TypeScript and Rust:** replace the `Finding` dataclass with the generic
   one — same fields, same `__post_init__`, `frozen=True`, tuple coercion,
   `SchemaError`, `Reporter.finding()` / `Reporter.candidate()`. Existing
   detectors continue to call `Reporter.finding()` unchanged. `format_findings.py`
   in each skill gains the separate "unverified leads" section the generic
   formatter already has (`code-doctor/common.py:694-705`). `analyze_all.py` in
   each skill re-validates records on the merge hop and reports
   `records_rejected` per category, as `code-doctor/analyze_all.py:109-113` does.
2. **Python:** introduce the same record class and reporter into
   `python-code-doctor/scripts/common.py`. Detectors that build dicts today
   (`find_dead_code.py`, `find_security_issues.py`, and any others the port
   turns up) construct records through the reporter. `django-code-doctor`
   carries `common.py` byte-identically from `python-code-doctor`
   (`ci.yml:75-84`), so it inherits the class; its detectors are converted in the
   same pass. Since `format_findings.py` already reads `also_caused_by`, no
   consumer changes.
3. **Cross-doctor conformance suite:** a single parametrized test module under
   `tests/` that loads each doctor's `common` module via `conftest.load_module`
   and asserts, for every doctor:
   - valid finding accepted; finding with blank suggestion rejected; finding
     with benign explanations rejected;
   - valid candidate accepted; candidate with suggestion rejected; candidate
     with no benign explanation rejected; blank reasons rejected;
   - unknown `kind` rejected; record is frozen; tuples survive `asdict`;
   - serialized shape contains `kind`; JSON round-trip preserves validity;
   - each doctor's `analyze_all.py` keeps candidates in a separate count;
   - `merge_reports.py` routes each doctor's candidates to `candidates[]`.

   The parametrization is discovered from `skills/*-code-doctor/scripts/common.py`
   plus `skills/code-doctor`, so a new doctor is tested the moment it exists.
4. **Mutation check:** the suite must fail when any one invariant is removed
   from any one doctor. Verify this once per doctor during the port and record
   the command in the plan.

The contract is the conformance suite, not byte-identity. Byte-identity of the
record code between skills is welcome but not required, because the surrounding
`common.py` files legitimately differ.

### 6.3 First reclassifications

Phase 1 ends by moving the clearest TypeScript heuristics to candidates so the
new kind is exercised immediately: `type_assertion` (ordinary `as T`),
`await_in_loop`, `missing_return_type`, `all_optional_type`,
`single_implementation_interface`, `barrel_file`, `data_clump`,
`primitive_obsession`, `public_mutable_field`. Each gets a healthy fixture that
stays silent and a test asserting `also_caused_by`. The full audit is Phase 8.

---

## 7. Phase 2 — TypeScript scanner and config correctness

Every item is a failing-test-first bug fix. None has an open design question.
None has an existing test.

### A. Root-relative test classification

`skills/typescript-code-doctor/scripts/common.py:78-83` tests `TEST_DIR_NAMES`
against every component of the absolute path. A checkout at `/tmp/tests/repo/`
yields `project.sources == []` and suppresses findings at eighteen call sites.
`common.py:74` already scopes `EXCLUDE_DIRS` below the root; do the same here,
following `rust-code-doctor/scripts/common.py:94-114`, which fixed and documented
this exact bug.

Acceptance: a repo under `/tmp/tests/` classifies nothing as test code by
directory; `tests/`, `__tests__/`, `*.test.ts`, `*.spec.tsx` inside the root
still classify.

### B. Declaration files

`common.py:86-88` recognizes `.d.ts` only; `tsproject.py:24-28` lists `.d.ts`
only among candidate suffixes. Because `TS_EXTENSIONS` includes `.mts`/`.cts`,
a `foo.d.mts` file is collected as source and fed to every code detector and to
`find_untested_modules` / `find_dead_code` as implementation. Recognize
`.d.ts`, `.d.mts`, `.d.cts` in both places.

### C. TSX-only detection

The only broken site is `find_tsconfig_issues.py:123-129`: the directory branch
globs `*.ts`, so a TSX-only (or `.mts`-only) repo with no tsconfig skips the
`no_tsconfig` finding. `analyze_all.py`, `analyze_diff.py`, `code-doctor/route.py`,
and `code-overview` already use the full extension set. Fix the one site and add
the regression test; do not touch the others.

### D. No silent caps

Three silent truncations, not one:

| Site | Cap | What degrades |
|---|---|---|
| `find_tsconfig_issues.py:120` | `[:10]` tsconfigs | strictness audit of configs 11+ |
| `tsproject.py:95` | `[:5]` tsconfigs for `paths` aliases | import resolution → `import_cycle`, `unused_export`, `unreferenced_module` |
| `tsproject.py:180` | `[:3]` `package.json` | dependency reconciliation |

Remove the caps; process every config after directory pruning. If a defensive
cap is retained for pathological trees, exceeding it must be recorded in the
detector's completeness notes so `analyze_all.py` marks the category
incomplete — never a silent slice.

### E. Prune during traversal

`common.py:63-75` does `sorted(path.rglob("*"))`, which materializes the entire
tree including `node_modules` before the first filter. The same shape recurs at
`tsproject.py:93,180`, `find_tsconfig_issues.py:117,126`,
`find_dependency_issues.py:54`, `run_external_tools.py:287`. Replace with
`os.walk` and in-place `subdirectories[:]` pruning as
`rust-code-doctor/scripts/common.py:65-83` does, through one shared walker.

### F. Package-manager audit

`run_external_tools.py:335` registers the tool as `"npm"`, so `_invocation`
resolves the npm binary; `run_audit` (`:234-243`) then re-derives the manager
from lockfiles and labels output with it. Two defects:

- yarn: builds `npm npm audit --json`, which cannot succeed; reported as a
  `yarn-audit` tool error.
- pnpm: runs `npm audit` in a pnpm project and reports the result — usually
  `ENOLOCK` — as pnpm's, down to the suggested `pnpm audit fix`.

Resolve the executable the manager actually names, as `measure_coverage`
(`:356-357`) already does. Add argument-construction tests for all three.

### G. Generated/vendor classification

Add explicit exclusion only where repository evidence is strong: generated
declaration output directories declared in tsconfig `outDir`/`declarationDir`,
files whose first lines carry a recognized generator header. Do not infer
"generated" from style.

---

## 8. Phase 3 — External-tool capability, status, and version metadata

### 8.1 Capability classes

Every external check declares a set from:

| Capability | Meaning | Examples |
|---|---|---|
| `pure-local` | shipped Python reads source/config; no target executable, no network, no writes | doctor scanners, parsing an existing coverage file |
| `executes-toolchain` | runs an installed compiler/linter or loads repository plugins/config that are code | `tsc`, ESLint, Biome, Prettier, `cargo check`, clippy, ruff, mypy |
| `executes-target-code` | runs tests, app code, migrations, package scripts | `npm test`, pytest, coverage runs, `manage.py check` that imports the project |
| `network` | sends data off-machine or depends on remote state | `npm audit`, `pip-audit`, `cargo audit`, `cargo check` fetching crates |
| `mutates` | writes source, lockfiles, build output, or caches that matter | `eslint --fix`, `prettier --write`, `biome --write`, non-dry migrations, emitting builds |

"Check mode" means the tool does not intentionally rewrite source. It does not
mean safe to execute on an untrusted checkout: `_invocation` prefers
`node_modules/.bin/<tool>` over PATH (`run_external_tools.py:60-72`), ESLint and
Prettier configs execute JavaScript, and `cargo check` runs build scripts. Say so
in each runner's SKILL.md.

### 8.2 Policy profiles

| Profile | Allows |
|---|---|
| `safe-static` (default) | `pure-local` only |
| `sandboxed-static` | + `executes-toolchain` |
| `sandboxed-full` | + `executes-target-code` |
| `network-audit` | + `network` |
| `fix` | + `mutates` |

Flags on every `run_external_tools.py` and on the orchestrator:
`--policy <profile>`, `--allow-network`, `--allow-target-exec`,
`--allow-mutation`. The effective policy is recorded in output. No skill upgrades
its own profile.

This changes current behavior: today a bare `run_external_tools.py .` in the
TypeScript skill runs `npm audit` (`:380`, `:335`); `cargo audit`, `cargo deny`,
and `pip-audit` likewise run by default in their skills. Under this spec they
require `--allow-network` and otherwise appear as `blocked` checks.

### 8.3 Per-tool check records

Each `run_external_tools.py` (python, typescript, rust, django) emits:

```json
{
  "schema": "external-tools/1",
  "policy": "sandboxed-static",
  "checks": [
    {
      "id": "typescript:tsc",
      "provider": "tsc",
      "version": "5.6.3",
      "status": "passed",
      "capabilities": ["executes-toolchain"],
      "required": true,
      "evidence_classes": ["typecheck", "effective-tsconfig"],
      "detail": "3 projects, 0 errors"
    }
  ],
  "findings": []
}
```

- `status ∈ {passed, failed, unavailable, blocked, error, skipped}`. `passed`
  means the check completed and produced usable evidence, whatever it found;
  finding counts are separate. `failed` means the tool ran and reports the
  subject as failing (e.g. `tsc` errors), `unavailable` means not installed,
  `blocked` means policy denied it, `error` means it did not complete,
  `skipped` means not requested.
- `version` is recorded whenever the tool can report one; `null` otherwise.
- `required` is true for the language's compiler or type checker whenever the
  repository has one to run (`tsc` with a tsconfig, `cargo check` with a
  Cargo.toml, mypy with a mypy configuration, Django `check` with a
  `manage.py`), and for a linter the repository itself configures (a ruff or
  ESLint config file or pyproject section exists). It is false for formatters,
  coverage, and advisories. The orchestrator's `--require <check-id>` flag
  promotes any check for one run.
- The existing `tools_run`, `missing_tools`, and `actions_taken` keys are kept
  as derived fields for one release so current SKILL.md instructions still read.
- Tool failures stop being synthetic `*:tool-error` findings once a check
  record carries the same information; the `_tool_error` path is removed in
  the same change so the two cannot disagree.

### 8.4 Security tests

Add tests proving, per runner:

- the default profile invokes no network, mutating, or target-executing
  command (assert on the argv the runner would build, with subprocess mocked);
- each of those requires its flag and otherwise yields a `blocked` check;
- every argv is a list and `shell=False` (already true; keep it tested);
- the package-manager audit resolves the manager it names (§7F).

---

## 9. Phase 4 — Orchestration and `checks[]` in `code-doctor-merge/1`

### 9.1 What is missing

Exactly three things, in order:

1. a programmatic orchestrator that routes and runs specialists;
2. per-external-tool status/version/capability records (Phase 3);
3. ingestion of external-tool output into the existing merged envelope.

Coverage accounting, error attribution, and candidate separation already exist
in `merge_reports.py` and `code-overview`. They are not reimplemented.

### 9.2 Extend the envelope

`code-doctor-merge/1` gains two optional keys:

```json
"checks": [
  {
    "id": "typescript:tsc",
    "provider": "tsc",
    "doctor": "typescript-code-doctor",
    "version": "5.6.3",
    "status": "passed",
    "capabilities": ["executes-toolchain"],
    "required": true,
    "evidence_classes": ["typecheck", "effective-tsconfig"]
  }
],
"assurance_status": "pass"
```

- `checks[]` is the union of every ingested `external-tools/1` report's
  `checks`, each stamped with the doctor that produced it, plus one synthetic
  record per doctor scanner (`id: "<lang>:scanner"`, `capabilities:
  ["pure-local"]`, `status` derived from the existing `doctor_errors` /
  `coverage_unknown` state so the two views cannot disagree).
- `assurance_status` is derived, never authored:
  - `fail` — any required check has `status: failed`;
  - `incomplete` — otherwise, any required check is `unavailable`, `blocked`,
    `error`, or `skipped`, or any doctor is in `doctor_errors` / `coverage_unknown`;
  - `pass` — every required check passed and every routed doctor ran with
    known coverage.
  A missing required check never yields `pass`.
- The schema id stays `code-doctor-merge/1`. Both keys are additive; no
  existing field changes meaning. A consumer that finds no `checks` key treats
  external evidence as unknown, not clean. If a later change alters an existing
  field's meaning, that is the point to mint `code-doctor-merge/2`.
- Findings from tools are attributed like any other: `doctor` is the producing
  specialist, `smell_type` keeps the existing `tool:code` form
  (`tsc:TS2345`, `eslint:<rule>`, `pnpm-audit:high`, `coverage:uncovered-file`).

`merge_reports.py` learns the `external-tools/1` shape in `read_report`
alongside the shapes it already accepts, and the label syntax stays
`--report DOCTOR:PATH`.

### 9.3 `skills/code-doctor/scripts/assure.py`

An orchestrator, nothing more. Its job:

1. call `route.py` on the subject;
2. run the raw layer (`code-doctor/analyze_all.py`);
3. run each routed specialist's `analyze_all.py`, located at
   `$(dirname skill)/<specialist>`; a routed specialist that is not installed
   becomes a `<lang>:scanner` check with `status: unavailable` and the doctor
   stays out of `doctors_run`;
4. under the given policy, run each present specialist's
   `run_external_tools.py` with the policy flags passed through;
5. feed every report file into `merge_reports.merge`;
6. append `checks[]` and derive `assurance_status`;
7. emit one extended `code-doctor-merge/1` envelope and exit
   0 / 1 / 2 for `pass` / `fail` / `incomplete`.

It does not install tools, grant capabilities, mutate, or rewrite source. It
accepts `--scope repository|diff --base-ref <ref>` so `brutal-review` and
`fix-pr` can use `analyze_diff.py` where a specialist ships one.

Because `code-doctor` must work installed alone, `assure.py` with no siblings
present degrades to: raw layer + routed-but-unavailable checks +
`assurance_status: incomplete`. That is the correct answer, and a test asserts it.

### 9.4 Tests

- extended-envelope round trip: `checks[]` and `assurance_status` survive
  `code-overview/scripts/common.load_merged`;
- verdict derivation table, including "required check missing → never pass";
- every routed doctor is invoked (subprocess mocked; argv asserted);
- an uninstalled specialist becomes an `unavailable` check;
- a blocked capability becomes a `blocked` check and `incomplete`;
- §2.4 invariants re-asserted through `assure.py` end to end.

---

## 10. Phase 5 — Authoritative TypeScript tooling on the orchestrated path

### 10.1 `tsc` as the source of effective compiler semantics

When a project-local `tsc` is present and the policy allows
`executes-toolchain`:

1. record `tsc --version` in the check;
2. resolve effective configuration per project with `--showConfig`;
3. `find_tsconfig_issues` analyzes effective options, keeping the source
   config path for locations;
4. unresolvable or unsupported project shapes become a check with
   `status: error` and detail, not a finding.

Without `tsc`, the shipped JSONC parser remains the fallback with these rules:

- an explicitly dangerous setting whose meaning is version-independent may
  still be a finding;
- absence of an option whose default depends on compiler version is never a finding;
- an unresolved `extends` chain yields a candidate, not a high-confidence defect;
- the `typescript:tsc` check is `unavailable`, so the envelope is `incomplete`.

### 10.2 Typechecking without mutation

Order of preference:

1. a repository-declared typecheck script that is clearly non-mutating
   (`tsc --noEmit`, `tsc -b --noEmit` variants), if policy allows;
2. `tsc -p <config> --noEmit` for simple non-reference projects;
3. for project-reference layouts where a non-mutating check cannot be built
   confidently, a `typescript:tsc` check with `status: skipped` and a detail
   naming the repo's own command.

The doctor never emits build artifacts to claim a typecheck unless `mutates`
was granted.

### 10.3 Type-aware ESLint as a distinct check

Two checks, not one: `typescript:eslint` (the project's configured lint ran) and
`typescript:type-aware-lint` (typed linting with `@typescript-eslint` and
`parserOptions.project`/`projectService` was in effect for representative TS
files, determined with `eslint --print-config`). The second records which
high-value rules were enabled where determinable: floating promises, misused
promises, unsafe-any family, unnecessary condition, switch exhaustiveness.

A clean untyped ESLint run passes `typescript:eslint` and leaves
`typescript:type-aware-lint` `unavailable`. `required` for the typed check
defaults to false; `--require typescript:type-aware-lint` promotes it.

### 10.4 Coverage and advisories

`coverage` requires `executes-target-code`; advisories require `network`. Both
are `required: false`. Their findings enter the envelope with attribution like
any other.

### 10.5 What external evidence grades

External findings map into rubric categories through the existing smell-type
mapping: `tsc:*` → correctness, `*-audit:*` → security, `coverage:*` → tests,
lint families → hygiene unless the rule is already mapped. A tool's presence
never widens a doctor's `DOCTOR_COVERAGE`. A passing `tsc` proves the compiler
accepted the checked projects; it does not grade Correctness clean.

---

## 11. Phase 6 — Rust integration and the specialist CI invariant

### 11.1 Where Rust actually drifts

Verified state, with the places that are already fine omitted (README, evals,
release packaging, and `validate_skills.py` are all generic and include Rust):

| Place | State |
|---|---|
| `code-doctor/scripts/route.py:38-40` | no `RUST_DOCTOR`; `_gather` (`:137-157`) never looks for `Cargo.toml` |
| `code-doctor/SKILL.md:3,70-74,140` | description routes Rust *away*; defer table omits it |
| `code-overview/scripts/discover_packages.py:51-54` | `DOCTOR_BY_LANGUAGE` lacks `rust` although `Cargo.toml` and workspaces are discovered (`:58,218-229`); Rust packages get `doctor: ""` |
| `code-overview/scripts/rubric.py:286-300` | no `DOCTOR_COVERAGE["rust-code-doctor"]` → every category ungraded |
| `code-overview/scripts/common.py:351` | `split_doctor_label` only accepts labels in `DOCTOR_COVERAGE`, so `rust-code-doctor:report.json` is read as a path |
| `code-overview/SKILL.md:347-349`, `references/scoring.md` | lists Rust as "no specialist" |
| `tests/code_doctor/test_route.py`, `test_skill_contract.py:94`, `tests/code_overview/test_discover_packages.py` | no Rust cases |
| `brutal-review/SKILL.md:65-78` | delegates to Python only — shared with TypeScript and Django, all of which ship `analyze_diff.py` |

`merge_reports.py` is label-agnostic and already attributes Rust reports correctly.

### 11.2 Work

1. `RUST_DOCTOR`, `Cargo.toml` evidence, and a route in `route.py`; SKILL.md
   defer table and description updated; `test_skill_contract.py` tuple extended.
2. `DOCTOR_BY_LANGUAGE["rust"]`, `DOCTOR_COVERAGE["rust-code-doctor"]`
   (full category set — the skill ships duplication, dead-code, and test
   detectors), docs updated.
3. Regression tests: a Rust-only repository routes to `rust-code-doctor` and
   does not fall to raw-only; a Cargo workspace member gets a doctor; a
   `rust-code-doctor:` label parses; a Rust merged report grades.

### 11.3 New-specialist CI invariant

A test discovers `skills/*-code-doctor`. SKILL.md frontmatter is locked to
`name` and `description` by `tools/validate_skills.py:34`, so the marker lives
in `scripts/specialist.json` inside each doctor: `{"language": "rust"}` for a
primary language specialist, `{"companion_of": "python-code-doctor"}` for a
framework companion such as Django. A `*-code-doctor` directory with neither
fails the test. For each primary specialist the test asserts presence in:

- `route.py` constants and evidence gathering;
- `code-doctor/SKILL.md` defer table;
- `DOCTOR_BY_LANGUAGE` and `DOCTOR_COVERAGE`;
- `tests/code_doctor/test_route.py` (a routing case naming the skill);
- the Phase 1 conformance parametrization (automatic);
- `brutal-review` delegation when `scripts/analyze_diff.py` exists.

README, evals, and release packaging are already enforced generically by
`validate_skills.py` and the release workflow; they are not re-checked here.

---

## 12. Phase 7 — Workflow adoption

- **`code-overview`:** `build_health.py --merged` reads `checks[]` and
  `assurance_status`. The health page gains an assurance block: status, the
  required checks and their state, tool versions, and blocked/unavailable
  checks. The summary page shows `PASS` / `FAIL` / `INCOMPLETE` next to the
  grade, with "incomplete because …" when required checks did not run.
  Candidate count is displayed separately from finding count (already true;
  keep the test).
- **`brutal-review`:** replace the Python-only delegation with one call to
  `assure.py --scope diff --base-ref <base>`. Its existing rule that "ran
  clean, ran and failed, never ran must not look alike" (`SKILL.md:85-88`)
  becomes a reading of `checks[]`.
- **`fix-issue`:** step 6 "Verify" (`SKILL.md:88-96`) runs `assure.py` after
  the new test and the reproduction pass, and pastes the status line and any
  non-`passed` required checks into the PR. A PR is not called verified while
  `assurance_status` is `incomplete`.
- **`fix-pr`:** step 5 (`SKILL.md:59-61`) uses the same call before claiming
  feedback resolved.

Delete duplicated verification prose only where the script now replaces it.

---

## 13. Phase 8 — TypeScript detector audit

166 smell types across 25 detector scripts (inventory in Appendix A). For each,
fill:

| Smell | Current kind | New kind | Evidence | Authoritative alternative | False-positive modes |
|---|---|---|---|---|---|

Rules:

1. If a healthy program can intentionally contain the pattern, it is a
   candidate unless the detector also proves harmful context.
2. If type information is needed, the type-aware tool is authoritative and the
   scanner emits a candidate.
3. Pure style stays low severity and non-blocking, or is removed.
4. A detector that duplicates a stronger compiler/linter rule with no offline
   value is narrowed or deleted.
5. Every retained finding needs an adversarial healthy fixture that stays silent.
6. Every candidate needs a fixture asserting at least one concrete
   `also_caused_by`.

Likely hotspots: `find_type_gaps.py`, `find_async_issues.py`,
`find_design_smells.py`, `find_overengineering.py`,
`find_encapsulation_issues.py`, `find_module_issues.py`,
`find_outdated_idioms.py`, `find_naming_issues.py`. Security detectors with
concrete dangerous sinks stay findings where the evidence proves them.

The Python-side parser regression corpus is expanded in this phase: TS, TSX,
`.mts`, `.cts`, all three declaration forms, decorators, template literals,
regex/division ambiguity, JSX fragments, generic arrows in TSX, `satisfies`,
import attributes, and intentionally invalid inputs — all as pytest fixtures
with no Node dependency.

---

## 14. CI changes

- **Python matrix:** `ci.yml:17-19` tests 3.11 and 3.12 while
  `requires-python` says `>=3.11` and README says "3.11+". Test the floor, a
  middle release, and current stable; update README to match.
- **Contract tests** added by the phases above: record conformance across
  doctors; specialist registry completeness; no candidate scoring; no silent
  cap; default policy performs no escalated capability; every external runner
  emits `external-tools/1` with capability metadata.
- **Eval canaries:** the judgment evals stay outside pytest (`evals/README.md`).
  Add a small scheduled, initially non-blocking, workflow that runs a canary
  subset: candidate vs finding discipline; TypeScript review runs mechanical
  checks before judgment; missing authoritative tools are reported not
  implied; no blanket `await` → `Promise.all` rewrite; type-safety claims
  require compiler evidence; blocked capabilities appear as `blocked`. There is
  no scheduled workflow today; this adds the first.

---

## 15. Follow-up project: TypeScript scanner differential validation

Out of scope for this spec. It is the only work that needs Node and TypeScript
installed in this repository's CI, so it must not block the phases above.

Scope, for the record: an optional integration lane installs the minimum
supported and current stable TypeScript versions (plus a non-blocking
prerelease lane) and verifies over the Appendix corpus that every file the
compiler accepts and the scanner claims to support tokenizes, that deliberately
invalid files are rejected or marked unsupported, and that new syntax cannot
become "skipped but clean". It changes no released skill's runtime dependencies.

---

## 16. Testing rules

1. **Failing test first** for every bug fix in §7 and §11: checkout under
   `/tests/`; TSX-only tree; eleventh tsconfig; `.d.mts`; yarn and pnpm audit
   argv; Rust repo routing.
2. **Positive and negative fixtures** for every reclassified smell; candidate
   tests assert `also_caused_by`.
3. **No assertion by prose.** A SKILL.md claim that a tool is required, a
   candidate never scores, a doctor routes automatically, or a missing check
   makes analysis incomplete has a test behind it.
4. **Mutation checks** for the conformance, registry, coverage, and capability
   tests: each must fail when its invariant is deliberately broken.
5. **Output compatibility.** `code-doctor-merge/1` keeps every existing field's
   meaning; `external-tools/1` keeps `tools_run` / `missing_tools` /
   `actions_taken` as derived fields for one release.
6. **Security tests** per §8.4.

---

## 17. Phase order and dependencies

1. **Port and enforce the finding/candidate contract** in TypeScript, Rust,
   and Python; conformance suite; first reclassifications. Everything else
   needs to represent uncertainty first.
2. **TypeScript scanner/config bug fixes** (§7). Independent of 1; can run in
   parallel.
3. **External-tool capability, status, and version metadata** (§8) in all
   four runners.
4. **Orchestration and `checks[]`** (§9). Depends on 3 for ingestion.
5. **Authoritative TypeScript tooling** on that path (§10). Depends on 4.
6. **Rust routing, code-overview integration, new-specialist CI invariant**
   (§11). Depends on 1 for the conformance parametrization; otherwise independent.
7. **Workflow adoption** (§12). Depends on 4.
8. **TypeScript detector audit and parser corpus** (§13). Depends on 1.
9. **Follow-up project:** compiler-differential scanner CI (§15). Separate spec.

Suggested grouping into plans: {1, 2}, {3, 4}, {5, 6}, {7, 8}, with 9 as its
own project.

---

## 18. Acceptance criteria

### Schema / epistemics

- [ ] TypeScript, Rust, and Python records enforce the finding/candidate
      invariants at construction; the conformance suite passes for every doctor.
- [ ] Invalid finding/candidate shapes fail construction and fail tests.
- [ ] Candidates never affect health scores (preserved; tested through `assure.py`).
- [ ] Analyzer or tool failure is represented in `checks[]` / `analyzer_errors`,
      never as silence or as a finding.

### Envelope / orchestration

- [ ] `code-doctor-merge/1` gains `checks[]` and derived `assurance_status`
      without changing any existing field.
- [ ] External-tool output is attributed and merged into the same envelope.
- [ ] Tool version, status, and capabilities are recorded per check.
- [ ] An incomplete required check cannot disappear into a clean health report.
- [ ] Routed specialists are run programmatically by `assure.py`; an
      uninstalled specialist is an `unavailable` check.
- [ ] §2.4 invariants hold after every phase.

### TypeScript

- [ ] Checkout path containing `tests` does not change source classification.
- [ ] `.d.ts`, `.d.mts`, `.d.cts` are handled alike.
- [ ] TSX-only repo without tsconfig is detected.
- [ ] No silent tsconfig, alias, or manifest cap.
- [ ] Excluded directories are pruned before traversal.
- [ ] Package-manager audit uses the manager's own executable.
- [ ] Effective config uses the real compiler when available; version recorded.
- [ ] Unresolved inheritance and version-dependent defaults are never findings.
- [ ] Type-aware ESLint is a separate check from ordinary ESLint.
- [ ] Project-reference layouts never cause unapproved mutation.
- [ ] Parser regression corpus substantially expanded, Python-only.

### Security / capabilities

- [ ] Default invocation performs no network, mutation, or target execution.
- [ ] Toolchain execution is explicit in policy and recorded in evidence.
- [ ] Tests/coverage, advisories, and fixers each require their capability.
- [ ] A blocked check appears as `blocked` and yields `incomplete`.

### Orchestration of specialists

- [ ] Rust repos route to `rust-code-doctor`; code-overview maps and grades Rust.
- [ ] A new primary language doctor fails CI until fully wired.
- [ ] `brutal-review` reaches Python, TypeScript, Rust, and Django diff doctors
      through one route.
- [ ] `fix-issue`, `fix-pr`, and `code-overview` consume the extended envelope.

### Confidence

- [ ] Every false-positive class found in real-project use has a regression test.
- [ ] Scheduled judgment canaries cover the epistemic and security behaviors.
- [ ] CI Python matrix covers floor, middle, and current stable.

---

## 19. Migration and compatibility

- **Users:** fewer findings, more candidates, and a visible assurance line.
  Call it out in release notes.
- **Report consumers:** `code-doctor-merge/1` is extended additively. Absent
  `checks` means unknown external evidence. Absent `kind` on a record is treated
  as legacy `finding` only when the producing doctor and version are known;
  candidate status is never inferred from missing fields.
- **`external-tools/1`:** old keys retained one release as derived fields.
- **Hooks:** hooks that only regenerate code-overview keep working. Hooks that
  read health JSON should also read `assurance_status`, `checks[]`, and
  candidate counts.

---

## 20. Risks

| Risk | Mitigation |
|---|---|
| Too many candidates make reports vague | every candidate carries concrete benign explanations; non-actionable candidates are deleted; local evidence may promote |
| Authoritative tooling reduces portability | scanners stay the dependency-free baseline; tools are additive; missing tools are `unavailable` checks, not failures |
| Capability policy is cumbersome | named profiles, sane default, no per-command prompts once a profile grants a class |
| TypeScript changes faster than tests | compiler-resolved config, version recorded per run, §15 lane when funded |
| Assurance mistaken for correctness proof | explicit evidence classes; health grade and assurance status shown separately; tools never widen `DOCTOR_COVERAGE` |
| The port drifts across three `common.py` copies | the conformance suite is parametrized by discovery, and a mutation check per doctor is recorded in the plan |

---

## 21. Guiding rule

> Do not trust an intelligent reviewer to remember the right process when the
> system can make the process unavoidable.

For TypeScript in particular:

> Use the shipped scanner to stay safe, offline, and useful on any checkout.
> Use the real toolchain, when available and permitted, for claims that need
> TypeScript semantics. Record the difference in `checks[]`.

---

## Appendix A — TypeScript smell-type inventory (166)

| Script | smell_types |
|---|---|
| `analyze_complexity.py` | `high_cyclomatic_complexity`, `high_cognitive_complexity`, `deep_nesting`, `long_function`, `long_parameter_list`, `long_file` |
| `find_ai_scaffolding.py` | `not_implemented_stub`, `placeholder_return`, `ignored_options_parameter`, `duplicate_definition`, `merge_conflict_marker`, `placeholder_value`, `handler_placeholder_comment` |
| `find_async_issues.py` | `async_callback_in_foreach`, `async_callback_in_predicate`, `async_promise_executor`, `promise_constructor_antipattern`, `floating_promise`, `swallowed_rejection`, `await_in_loop`, `async_without_await`, `nested_then_chain` |
| `find_code_smells.py` | `magic_number`, `loose_equality`, `var_declaration`, `nested_ternary`, `switch_without_default`, `boolean_blindness`, `parseint_without_radix`, `array_constructor`, `god_class`, `data_class`, `inconsistent_returns`, `boolean_return_conditional`, `empty_function_body` |
| `find_comment_smells.py` | `commented_out_code`, `todo_marker`, `jsdoc_repeats_types` |
| `find_coupling_issues.py` | `low_cohesion`, `feature_envy`, `middle_man`, `high_module_fan_out` |
| `find_dead_code.py` | `unreachable_code`, `unreferenced_module`, `unused_export`, `unused_import`, `unused_private_member` |
| `find_debug_leftovers.py` | `debugger_statement`, `console_leftover`, `browser_dialog`, `blanket_line_suppression`, `file_wide_lint_suppression` |
| `find_dependency_issues.py` | `missing_dependency`, `unused_dependency`, `unpinned_dependency`, `test_only_runtime_dependency`, `types_in_runtime_dependencies`, `multiple_lockfiles`, `no_lockfile`, `no_manifest`, `unparseable_manifest` |
| `find_design_smells.py` | `type_switch`, `boolean_flag_parameter`, `data_clump`, `primitive_obsession`, `temporary_field`, `refused_bequest`, `large_inline_options` |
| `find_duplicates.py` | `duplicate_block`, `duplicate_type_shape`, `repeated_string_literal` |
| `find_encapsulation_issues.py` | `public_mutable_field`, `missing_readonly`, `pass_through_accessors`, `exposes_internal_collection`, `exported_mutable_binding`, `module_level_mutable_state`, `global_object_write`, `reaches_into_private`, `message_chain`, `mutates_parameter` |
| `find_exception_issues.py` | `empty_catch`, `catch_logs_and_continues`, `swallowed_error`, `rethrow_without_cause`, `throw_non_error`, `control_flow_in_finally`, `reject_non_error` |
| `find_loop_simplifications.py` | `index_loop_over_array`, `for_in_loop`, `loop_building_array`, `string_concat_in_loop`, `filter_length_instead_of_some`, `filter_first_instead_of_find`, `map_filter_boolean`, `foreach_building_array`, `object_keys_then_lookup` |
| `find_module_issues.py` | `import_cycle`, `barrel_file`, `deep_relative_import`, `god_module`, `missing_import_type`, `side_effect_import` |
| `find_mutation_hazards.py` | `parameter_reassigned`, `mutates_argument_property`, `mutates_imported_object`, `mutation_during_iteration`, `mutates_props`, `mutable_exported_constant`, `object_assign_onto_argument` |
| `find_naming_issues.py` | `non_pascal_case_type`, `hungarian_interface_prefix`, `non_camel_case_function`, `shadows_browser_global`, `boolean_without_predicate_name`, `underscore_without_private`, `default_export_name_mismatch` |
| `find_outdated_idioms.py` | `commonjs_require`, `commonjs_exports`, `typescript_namespace`, `angle_bracket_cast`, `arguments_object`, `let_never_reassigned`, `manual_optional_chaining`, `falsy_default_with_or`, `legacy_has_own_property`, `indexof_membership_test`, `array_from_workaround`, `apply_instead_of_spread`, `json_clone`, `date_gettime_for_now`, `react_fc_annotation`, `mixed_array_type_style` |
| `find_overengineering.py` | `abstract_class_with_one_subclass`, `all_static_class`, `deep_inheritance`, `pass_through_module`, `single_implementation_interface`, `single_use_type_parameter`, `stateless_single_method_class`, `useless_constructor` |
| `find_resource_leaks.py` | `effect_without_cleanup`, `unreleased_resource`, `uncancelled_request_in_effect`, `uncleared_timeout_in_effect` |
| `find_security_issues.py` | `dynamic_code_execution`, `shell_injection_risk`, `html_injection_sink`, `weak_hash_algorithm`, `insecure_randomness`, `hardcoded_secret`, `insecure_transport`, `tls_verification_disabled`, `postmessage_wildcard_origin`, `target_blank_without_noopener` |
| `find_test_smells.py` | `empty_test`, `test_without_assertion`, `weak_assertion_only`, `focused_test`, `skipped_test`, `unawaited_async_assertion`, `async_test_never_awaits`, `logic_in_test`, `over_mocked_test_file` |
| `find_tsconfig_issues.py` | `strict_mode_off`, `strict_flag_disabled`, `checking_suppressed`, `missing_strict_extra`, `outdated_target`, `unchecked_js`, `no_tsconfig`, `unparseable_tsconfig` |
| `find_type_gaps.py` | `as_any`, `any_index_signature`, `explicit_any`, `double_assertion`, `type_assertion`, `non_null_assertion`, `ts_nocheck`, `ts_ignore`, `unexplained_suppression`, `unsafe_builtin_type`, `empty_object_type`, `untyped_parameter`, `missing_return_type`, `catch_typed_any`, `all_optional_type` |
| `find_untested_modules.py` | `no_tests_at_all`, `thin_test_coverage`, `untested_module` |
| `run_external_tools.py` | `tsc:TS####`, `eslint:<rule>`, `biome:*`, `prettier:*`, `madge:*`, `knip:unused-<kind>`, `{npm,pnpm,yarn}-audit:<severity>`, `coverage:uncovered-file`, `coverage:no-data` |
