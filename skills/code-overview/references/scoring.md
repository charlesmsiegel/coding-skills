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

**Coverage is evidence, not capability.** What a category may be graded on is
what the findings files *demonstrate* was examined — never what the named doctor
could have done in principle. The three report shapes carry different amounts of
evidence, and are read accordingly:

| Shape | What it is | Coverage it grants |
|---|---|---|
| full | `analyze_all.py`'s report — an envelope with `meta.analyzers_run` | the categories whose detectors ran, **minus every category any skipped or crashed detector belonged to**, intersected with the `--doctor` profile |
| partial | anything else — a bare JSON list, one detector's `{"issues": [...]}`, or a zero-byte file | **nothing** |

There is deliberately no "a bare list means the whole doctor ran" rule, because
that shape is ambiguous in a way that cannot be resolved from the file:
`analyze_django.py` prints a bare list, and so does
`find_duplicates.py --format json`, which prints `[]`. Treating it as a full
Django run graded an empty single-detector run as an **A+ in all seven
categories**. When a bare list really does cover part of the rubric, say which
part with `--covers`; nothing in the file can know.

For `django-code-doctor` alone that is:

```
--covers correctness,security,tests,complexity,design,hygiene
```

(no duplication — it has no such detector). The recommended Python+Django merge
needs no flag: the Python report is a full envelope and supplies the evidence.

A rubric category usually has several detectors behind it, and the subtraction is
deliberately all-or-nothing: skipping `exception_issues` ungrades **Correctness**
even though `mutation_hazards` ran. A partly-measured category can only ever miss
findings, never invent them, so grading it would systematically flatter the code —
and "mostly measured" is not a thing the score can express.

Evidence is also capped by the doctor's profile. A report whose envelope claims a
duplication analyzer ran, handed over with `--doctor django-code-doctor`, is a
mislabeled run rather than a reason to grade a category that doctor cannot detect.

A zero-byte findings file counts as a report, not as no file at all. It is what a
doctor leaves behind when it fails *after* the shell created the redirect target,
and dropping it silently would leave no report, no contrary evidence, and an A+
built on an empty artifact.

When any full report is present it is believed and the others add findings
without inflating coverage. That is what makes the recommended Python+Django
merge behave: if the Python report skipped duplicates, the Django list beside it
cannot silently restore duplication, because a bare list is not evidence that
anything was examined.

`--covers a,b,c` names coverage explicitly when nothing in the files can;
`--assume-full-coverage` credits the whole rubric.

Given that, a category comes back ungraded in five situations:

1. **No detector covers it for this doctor.** `rubric.DOCTOR_COVERAGE` records
   this. `django-code-doctor` has no general duplication or dead-code detector,
   so duplication is ungraded for a Django app reviewed by it alone — it must
   not collect a free 100.
2. **Nothing in the findings demonstrates it was examined** — an unrecognized or
   empty `--doctor`, or a single-detector report. This is the case that matters
   most: a doctor pointed at a language it cannot parse returns an empty findings
   list, and an empty findings list graded as coverage is an A+.
3. **A detector crashed.** `meta.analyzer_errors`; a zero count from a crashed
   detector means *unknown*.
4. **A detector was skipped or never ran.** `meta.analyzers_skipped`, plus any
   category absent from `meta.analyzers_run`. `--skip-duplicates` is advertised
   by the doctors as the way to skip the slowest detector, so being handed a
   report missing a whole category is normal, not a corruption.
5. **The artifact it needs is absent** — no coverage file, no manifest.

All five land in `ungraded` in the metadata and in a callout on the page. The
only dishonest options are the two obvious ones: scoring 0 punishes a repo for
a missing coverage artifact; scoring 100 rewards it for one.

A dropped category makes the score **partial, not an upper bound**. Weights are
renormalized over what remains, so restoring the missing category could move the
overall either way — up if it would have scored well, down if badly. The page
says exactly that rather than giving the reader a direction of error that does
not hold.

## Merging companion doctors

Running two doctors over one tree means the same defect can arrive twice — both
security detectors flag a hardcoded `SECRET_KEY` at the same file and line, at
different severities. Counted twice it costs the grade ~13 weighted points
instead of 10. Findings are therefore deduplicated on `(file, line, type)`,
keeping the higher severity, and the merged count is reported in
`duplicates_merged` and on the page.

The key is the **whole path**, not the basename: `src/a/models.py:3` and
`src/b/models.py:3` are two defects, and a monorepo produces that pair
constantly. Two spellings of one path (absolute vs relative) simply fail to
merge, which leaves a duplicate counted twice — the safe direction to be wrong
in.

Deduplication is strictly **across** reports, never within one. A single
detector can legitimately emit two findings sharing file, line and type —
Django's template detector reports one `hardcoded_url_in_template` per link, so
a line with two links yields two findings differing only in description.
An identity's multiplicity in the merged set is therefore the **maximum** any one
report gave it, not the sum: two-from-Django plus one-from-Python stays two,
and one-plus-one becomes one.

## What the grade is divided by, part two: templates

`CODE_EXTENSIONS` deliberately excludes markup, but Django's detectors report
`missing_csrf_token` and `query_in_template` from `.html`. Dividing those by the
Python lines alone makes a template-heavy package look far worse than it is.

So the denominator's extension set is **derived from the findings**: a template
extension is counted exactly when a finding points at a file with it. That keeps
the invariant self-correcting — lines enter the denominator when the analysis
reached them — so a Django package with template findings is sized over its
templates while a Python package that merely ships an HTML fixture is not.
`--include-extension` adds more by hand; `sized_extensions` in the metadata
records what was counted beyond code.

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

  One exception, in `--root` mode only: findings about files sitting **directly
  in the repo root** — `tsconfig.json`, the root manifest, a settings file — are
  kept. They describe the whole tree rather than any package, and the repo grade
  is documented to include repo-wide findings. An unmapped *directory* is still
  out; the exception is for configuration, not for code the user excluded.

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
