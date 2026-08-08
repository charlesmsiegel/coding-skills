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

Severities are folded to `high`/`medium`/`low` as each report is read, anything
else becoming `medium`. The weight table lowercases but every count, icon, sort
and metadata field matches the token exactly, so an unnormalized `"High"` moved
the grade like a high while being reported as zero highs. `--covers` invites
producers this skill has never seen, so this cannot be left to convention.

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

**A report only speaks for the doctor that produced it**, named by labelling the
file `--findings <doctor>:<path>`. Nothing inside a report identifies its author,
and the workflow hands every report to every package, so without the label a
foreign report is read as evidence about this package: with only a TypeScript
report present, a Python package graded Security **100** off `tsconfig`. The
doctor-profile cap cannot catch that, because both doctors cover the same rubric
categories. An unlabelled report is attributed to `--doctor`, which is right for
the ordinary single-doctor run and why the label is only required when more than
one doctor's findings are passed.

**Each report is then resolved on its own, and the results are unioned.** Both
halves matter. Per report, because a failure belongs to the run it happened in —
a crashed `tsconfig` analyzer was ungrading Security on a Python package whose
own security detector had completed cleanly. Unioned, because a category one
report skipped and another measured *is* measured; that is what makes companion
doctors add up.

Within a report, a rubric category usually has several detectors behind it, and
the subtraction is deliberately all-or-nothing: skipping `exception_issues`
ungrades **Correctness** even though `mutation_hazards` ran. A partly-measured category can only ever miss
findings, never invent them, so grading it would systematically flatter the code —
and "mostly measured" is not a thing the score can express.

Evidence is also capped by the doctor's profile. A report whose envelope claims a
duplication analyzer ran, handed over with `--doctor django-code-doctor`, is a
mislabeled run rather than a reason to grade a category that doctor cannot detect.

A zero-byte findings file counts as a report, not as no file at all. It is what a
doctor leaves behind when it fails *after* the shell created the redirect target,
and dropping it silently would leave no report, no contrary evidence, and an A+
built on an empty artifact.

**An empty artifact ungrades everything, even beside a full report.** A failed
run is evidence of a gap, and it outranks the evidence next to it. In the
recommended Python+Django merge, a Django crash leaves the Python report full and
clean — and Django is what sees N+1 queries, missing CSRF tokens and insecure
settings, so the categories Python "covered" were in fact half-measured. Nothing
in the files says which doctor was meant to write the empty one, so the gap
cannot be subtracted from particular categories. Re-run the failed doctor, or use
`--covers` to say what the surviving reports examined.

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
3. **A detector crashed.** `meta.analyzer_errors`, keyed by the doctor whose
   report it crashed in; a zero count from a crashed detector means *unknown*.
4. **A detector was skipped or never ran.** `meta.analyzers_skipped`, keyed the
   same way, plus any category absent from `meta.analyzers_run`.
   `--skip-duplicates` is advertised by the doctors as the way to skip the
   slowest detector, so being handed a report missing a whole category is
   normal, not a corruption.
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

So the denominator's extension set is derived from **what the analysis reached**,
which two signals report:

- **A contributing doctor parses markup.** `rubric.DOCTORS_ANALYZING_TEMPLATES`
  names them — `django-code-doctor` reads templates on every run, so its template
  lines are in the denominator whether or not any of them was faulty.
  *Contributing* means every doctor that supplied a report, read from the
  `--findings <doctor>:<path>` labels — not just the one `--doctor` names. That
  flag caps coverage, and reading it as the whole story left a clean Django run's
  templates out of the divisor in this skill's own recommended merge.
- **A finding points at one.** This still catches what a doctor profile cannot
  know: an unrecognized `--doctor`, or a third-party tool declared via `--covers`.

The first signal is not redundant. Deriving template sizing from findings alone
leaves a package of *clean* templates measured over its Python lines only — and
then the first template finding anyone adds drops thousands of lines into the
divisor and **raises** the grade. A rule under which discovering a bug improves
the score is the wrong shape, whatever it does to any individual number.

A Python package that merely ships an HTML fixture matches neither signal and is
sized over its code alone, as it should be. `--include-extension` adds more by
hand; `sized_extensions` in the metadata records what was counted beyond code.

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

  That union is narrowed twice more, on the same principle. A package with **no
  doctor** contributes no findings, so its lines are out. And a package whose
  **health page is missing or ungraded** was not actually analyzed — a doctor
  named in the map is an intention, not a result. A TypeScript package whose
  report never arrived, or arrived empty from a failed run, is pure dilution:
  6000 unexamined lines beside 600 examined ones turned a high-severity secret
  from a failing security score into an overall A. Both exclusions are warned
  about, and both packages stay in the table as ungraded. This is why the rebuild
  order runs `--root` *after* the package pages exist; run before them, there is
  nothing to narrow by, so it falls back to every doctored package and says so.

  One exception, in `--root` mode only: **repo-wide findings** are kept. Two
  shapes qualify. Non-source files sitting directly in the repo root —
  `tsconfig.json`, the root manifest, a CI config — describe the whole tree
  rather than any package, and contribute no lines to any denominator, so
  keeping them costs nothing. A root-level *source* file is not kept unless `.`
  is itself mapped: a `loose.py` the user left unassigned is code they chose not
  to grade, and counting its findings while its lines stay out of the divisor is
  the same asymmetry pointed the other way — it penalizes the repo for code
  outside the scope its own map defines. And findings reported against the
  **repo root directory itself**:
  `find_untested_modules.py` emits `no_tests_in_repo` and
  `find_dependency_issues.py` emits `no_dependency_manifest` with
  `file=str(root)`, because there is no single file to blame. Requiring a *file*
  dropped both — high severity, and about the project as a whole — so a repo with
  no tests and no manifest rolled up to **A+**. An unmapped *sub*directory is
  still out; the exception is for the repository, not for code the user excluded.

- **A partial measurement is never a grade.** If *any* configured root is missing,
  or the roots resolve to zero source files, every category is ungraded. Without
  this the 1000-line floor turned an empty tree into a confident denominator, and
  a clean report over a root that had been renamed away scored A+ for code that
  was not there. One missing root out of several is the same problem and harder
  to see: the surviving roots still measure files, so the page was sized over
  part of the package while the findings covered all of it. `--loc`/`--files` are
  the caller asserting a size this script cannot see, and are left alone.

## Mapping a finding to a category

Four steps, first hit wins:

1. `finding["category"]` against `DETECTOR_CATEGORIES` — this is what
   `python-code-doctor` and `typescript-code-doctor` stamp on every finding.
2. The finding's type token against `SMELL_TYPE_CATEGORIES`, the explicit table
   for `django-code-doctor`, which emits a flat list with no category field.
3. `TYPE_KEYWORDS` against the type token, then against the detector name.
   Keywords match at **word boundaries** — the start of the token or just after a
   non-alphanumeric character — not anywhere in it. Plain substring matching put
   `latest_dependency` under Tests because `test` sits inside `latest`, swapping
   an 8%-weight category for a 15% one *and* reporting the match as successful,
   so the unmapped-type caveat never fired. Boundary matching still lets a
   keyword cover a family by prefix: `complex` reaches `complexity`, `auth`
   reaches `authentication`, `deprecat` reaches both `deprecated` and
   `deprecation`.
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

## The measurement grade

A different question from the health grade, and it divides a different thing:

    score = 100 × Σ(importance × credit) / Σ(importance)     over all measurable things

Health asks *how dense are the detectable defects*. Measurement asks *how much of
what matters is actually measured* — so its denominator is not lines of code but
the set of things that could be measured, whether or not anyone measured them.

| Importance | |
|---|---|
| 3 | gates a ship / release / rollout decision |
| 2 | informs a decision someone actually makes |
| 1 | informational |

| Credit | |
|---|---|
| 1.0 | measured, nothing found against it |
| 0.5 | measured, one confirmed medium finding |
| 0.25 | measured, one confirmed high finding |
| 0.0 | not measured, or structurally unmeasurable with today's data |

**Structurally unmeasurable things stay in the denominator.** Recall with no gold
set, calibration with no outcomes, causal effect with no control arm — dropping
those rows would let a system that measures one easy thing perfectly score 100,
which is precisely how silence gets read as success.

**The letter bands are the health grade's, exactly.** A B- means the same score
range on both pages, because they sit side by side in the nav and a reader
compares them directly. What differs is what each divides — say which when you
present them together. The rubric itself lives in
`science-investigation/scripts/rubric.py`; a CI test pins the band table
identical to this skill's.

## The theory grade

    score = 100 × Σ(weight × median step) / Σ(weight)

A third question again: health asks how dense the detectable defects are,
measurement how much of what matters is measured, theory whether the code
expresses a coherent model of its problem at all.

Its ladder is ordinal — `absent`, `strained`, `partial`, `holds` — and
disagreement between judges is counted in **rungs**, not in the values, because
the values are unevenly spaced on purpose and arithmetic distance would rank the
same disagreement differently depending on where it sat.

Its bands are the same as the other two, imported directly from `rubric.py`
rather than copied — `theory_rubric.py` ships in the same skill, so the
standalone-installability rule that forces the science-investigation copy does
not apply.

**Unlike the other two, this grade is a judgment.** Present it with that said
out loud, and never compare letters produced by different models.
