# The code-health rubric

Everything here is implemented in `scripts/rubric.py`. Read this when a grade
looks wrong, when a doctor grows a detector the rubric has never seen, or when
someone asks what a B- means.

## What the grade is a claim about

**A grade measures what the deterministic detectors can see.** It is a density
of mechanically-detectable problems, not a verdict on the design. A package can
score A- and still be architecturally wrong, and a package can score D because
it is 200 lines long with four real bugs in it. Say this out loud when
presenting a grade; the number's usefulness is that it is *comparable over
time*, not that it is a judgment.

The atlas is the other half. A grade with no code map next to it invites exactly
the misreading above, which is why `summary.html` links both and neither ships
alone.

## Categories and weights

| Category | Weight | Half-life | What lands here |
|---|---|---|---|
| Correctness | 25% | 6 | Mutation hazards, exception handling, resource leaks, unawaited coroutines, global state, duplicate definitions, unfinished scaffolding, N+1 queries, migration and transaction bugs |
| Security | 15% | 2 | Injection, secrets, unsafe deserialization, missing authorization, insecure settings, permissive CORS/hosts |
| Tests & safety net | 15% | 8 | Untested modules, test smells, coverage gaps |
| Complexity | 15% | 12 | Cyclomatic/cognitive complexity, nesting, god classes, long functions, everyday smells |
| Design & structure | 12% | 10 | Design smells, over-engineering, coupling, import cycles, data clumps, encapsulation leaks |
| Duplication & dead code | 10% | 10 | Duplicate blocks, dead code, commented-out code |
| Dependencies & hygiene | 8% | 25 | Dependency reconciliation, dated idioms, naming, docstrings, type gaps, debug leftovers |

Weights total 100. They encode a single opinion: **a bug outranks a smell.**
Correctness and security together are 40% of the grade; naming and docstrings
are 8% and cannot sink a package on their own.

## The curve

```
weighted   = Σ severity_weight(finding)          high 10 · medium 3 · low 1
density    = weighted / max(loc, 1000) * 1000    weighted findings per KLOC
score      = 100 × 0.5 ** (density / half_life)
```

Three properties worth knowing:

- **A category scores exactly 50 at its half-life.** Security's half-life is 2,
  so two weighted security findings per KLOC — one medium-and-a-bit — is already
  a failing security score. Hygiene's is 25. The half-life is where the rubric
  states how much of a thing is tolerable, in the units of the thing.
- **It never hits zero and never goes negative.** Doubling the findings always
  halves the remaining score. The 400th finding costs less than the 4th, which
  is right: a package with 400 findings and one with 800 are both simply bad, and
  the difference between them is not four times more interesting.
- **LOC is floored at 1000.** Without it, a 40-line module with one medium
  finding scores 0 — a statement about the divisor, not the code. The floor
  means very small units are graded gently, which is the correct bias: there is
  not enough evidence to grade them harshly.

Blank lines are excluded from LOC; comments are not. Per-language comment
stripping is a parser's job, and the number only has to be stable enough to
divide by.

## Grade bands

`A+ ≥97 · A ≥93 · A- ≥90 · B+ ≥87 · B ≥83 · B- ≥80 · C+ ≥77 · C ≥73 · C- ≥70 ·
D+ ≥67 · D ≥63 · D- ≥60 · F <60`

The overall score is the weight-mean of the graded categories. Ungraded
categories are dropped and the remaining weights renormalized.

## Ungraded is not zero, and not a hundred

A category comes back ungraded in five situations:

1. **No detector covers it for this doctor.** `rubric.DOCTOR_COVERAGE` records
   this. `django-code-doctor` has no general duplication or dead-code detector,
   so duplication is ungraded for a Django app reviewed by it alone — it must
   not collect a free 100.
2. **No known doctor produced the findings at all.** An unrecognized (or empty)
   `--doctor` means *nothing* can be claimed as measured, so every category is
   ungraded and the overall score is null. This is the case that matters most:
   a doctor pointed at a language it cannot parse returns an empty findings
   list, and an empty findings list graded as coverage is an A+. Pass
   `--assume-full-coverage` to override deliberately.
3. **A detector crashed.** The doctors report those under
   `meta.analyzer_errors`, and a zero count from a crashed detector means
   *unknown*. `build_health.py` drops the affected rubric category.
4. **A detector was skipped or never ran.** `meta.analyzers_skipped`, plus any
   category absent from `meta.analyzers_run`. `--skip-duplicates` is advertised
   by the doctors as the way to skip the slowest detector, so being handed a
   report missing a whole category is normal, not a corruption.
5. **The artifact it needs is absent** — no coverage file, no manifest.

All five land in `ungraded` in the metadata and in a callout on the page. The
only dishonest options are the two obvious ones: scoring 0 punishes a repo for
a missing coverage artifact; scoring 100 rewards it for one.

Run a Django app through **both** `django-code-doctor` and `python-code-doctor`
and pass both findings files to `build_health.py` — that is what closes the
coverage gap. Pass `--doctor python-code-doctor` in that case, since the union
covers every category.

## What the grade is divided by

Density is findings per KLOC, so the findings and the lines have to describe the
same code or the number is meaningless in a way that is invisible on the page.
Two mechanisms keep them aligned:

- **`--scope`** (defaulting to `--root-dir`) drops findings about files outside
  the package. The doctors are run from the repo root so they can see manifests,
  tests and settings — without that context they *invent* findings, reporting a
  missing manifest and "no test files found" for a package that has neither of
  its own but sits in a project that has both. Running them where they can see
  everything and partitioning by path afterwards is the only way to get both
  correct findings and per-package numbers. The dropped count is recorded as
  `findings_out_of_scope`.
- **Root sizing from the map.** With `--root --map` and no explicit `--root-dir`,
  the denominator is the union of the mapped packages' roots. When the user
  answers the `unassigned` question with "leave it out", that code contributes no
  findings; measuring the whole checkout anyway would improve the repo's grade in
  direct proportion to how much code was excluded from analysis.

## Mapping a finding to a category

Four steps, first hit wins:

1. `finding["category"]` against `DETECTOR_CATEGORIES` — this is what
   `python-code-doctor` and `typescript-code-doctor` stamp on every finding.
2. The finding's type token against `SMELL_TYPE_CATEGORIES`, the explicit table
   for `django-code-doctor`, which emits a flat list with no category field.
3. Substring keywords against the type token, then against the detector name.
4. Fallback to hygiene — **and the type is recorded in `unmapped_types`**, shown
   in a callout on the page.

The fallback is the lowest-weight category on purpose: an unmapped type should
not be able to swing a grade before anyone notices it is unmapped. When
`unmapped_types` is non-empty, that is a rubric gap to close in
`rubric.py`, not something for the reader to work around.

## Extending it

Adding a detector to a doctor means adding its category key to
`DETECTOR_CATEGORIES`, or its finding types to `SMELL_TYPES`. Adding a whole new
doctor means an entry in `DOCTOR_COVERAGE` naming the categories it can speak
to. Changing weights or half-lives changes every historical grade's meaning —
worth saying in the commit message, because the point of the number is
comparison over time.
