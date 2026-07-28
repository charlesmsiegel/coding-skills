# Reviewing AI-Generated Code

AI-written Python — whole repos, single features, or the diff in a change request —
fails in a recognizable, *different* way from human-written code. Human code tends to
be under-built and idiosyncratic. AI code tends to be **over-built, over-explained,
plausible, and untested**: it compiles, it reads confidently, it looks finished, and
a meaningful fraction of it is subtly wrong or quietly incomplete. Review it with
that asymmetry in mind.

The deterministic detectors catch the mechanical tells — `find_ai_scaffolding.py`
(stubs/placeholders), `find_duplicate_definitions.py` (regeneration artifacts),
`find_redundant_comments.py` (narration), `find_unawaited_coroutines.py`, plus the
AI-tuned additions to `find_security_issues.py` (injection), `find_exception_issues.py`
(swallowed errors), and `find_test_smells.py` (vacuous assertions). This guide is the
reading-brain half: the failure modes no parser can confirm, and the stance for
reviewing a CR.

## The core stance: confident, plausible, untrusted

The danger of AI code is not that it looks bad — it's that it looks **good**. It uses
the right vocabulary, has docstrings, handles obvious edge cases, and carries an air
of having been thought through. None of that is evidence it is correct. Reviewing it,
invert your usual priors:

- **Fluency is not correctness.** Well-named, well-commented code with a wrong
  boundary condition is the typical defect, not the exception. Read the logic, not
  the prose around it.
- **"Looks complete" is not "is complete."** AI fills gaps it can't satisfy with
  stubs, placeholders, and `TODO: implement` — confidently formatted. Assume some of
  what looks done is scaffolding until you've checked (`find_ai_scaffolding.py`).
- **It did what was asked, plus things that weren't.** AI adds unrequested config
  flags, abstraction layers, "robustness," and files. Scope creep is the norm; treat
  every element the request didn't call for as guilty until justified.
- **It was probably never run.** Generated code is frequently submitted without
  execution. The single highest-value review action is to **run it and type-check
  it** — most hallucinations die instantly there (see below).

## Failure modes a parser can't confirm — read for these

### Hallucinated APIs
The signature AI bug: methods, attributes, keyword arguments, or imports that don't
exist, or that exist with a different name/signature. `client.fetch_all()` when the
method is `.list()`; a `timeout=` kwarg the function never accepted; `from utils
import retry` where `retry` was never defined.
**You cannot find these by reading alone** — they look perfectly reasonable. The fix
is mechanical and non-negotiable: **run the code and run a type checker** (pyright/
mypy). A hallucinated API is an `AttributeError`/`ImportError` at runtime and a red
squiggle at check time. This is why `references/safety-net-and-testing.md` and the
typing guide are load-bearing for AI review: the tooling *is* the hallucination
detector.

### Plausible-but-wrong logic
Off-by-one in a slice, an inverted condition, a wrong default, `>=` where `>` was
meant, the edge case handled in the comment but not the code, currency math in floats.
The code reads as if it's right. The only defense is to **pin behavior with a
characterization test and read adversarially** — walk the empty input, the single
element, the boundary, the duplicate, the None. Don't accept "looks correct."

### Over-defensive ceremony
AI hedges: re-validating arguments a caller already validated, `if x is None`
immediately after assigning `x` a non-None value, nested try/except around code that
can't raise, `hasattr`/`getattr(..., default)` guarding attributes that always exist,
`**kwargs` catch-alls that are never read (`find_ai_scaffolding.py` flags the last).
Each check reads as caution; together they bury the actual logic and silence the
typos a real signature would have caught. Strip guards that defend against the
impossible.

### Superfluous abstraction for a simple ask
Asked for a function, AI delivers a class with a factory and a config dataclass;
asked to read a file, it builds a pluggable `LoaderStrategy`. `find_overengineering.py`
catches the structural cases; the AI-specific framing is: **the abstraction was
generated to look thorough, not because a second case exists.** Inline it to the
concrete thing the request actually needed (`references/overengineering-and-abstraction.md`).

### Inconsistency across a session
Because the model doesn't hold the whole repo in view, it converges on different
local choices in different places: f-strings here and `.format` there, `pathlib` in
one module and `os.path` in the next, `get_`/`fetch_`/`load_` for the same operation,
a config read three different ways. Within a single CR this shows up as two files
solving the same sub-problem differently. Converge on one way
(`references/patterns-and-consistency.md`).

### Narration instead of intent
AI comments restate the code (`# increment the counter` over `counter += 1`) and
write docstrings that paraphrase the signature. `find_redundant_comments.py` flags
the line-level cases. The deeper problem: these comments document *what* (which the
code already says) and almost never *why* (which is the only thing worth a comment).
Delete the narration; demand a reason where a reason is owed.

### Silent failure and fake robustness
`except Exception: pass`, `except Exception as e: print(e)` then carry on, functions
that return `None`/`{}`/`[]` on error instead of raising. It *looks* robust and is the
opposite: it converts a loud, debuggable failure into a silent wrong answer.
`find_exception_issues.py` (swallowed_exception) flags the broad-catch-and-discard
shapes; in review, insist errors are either handled meaningfully or propagated.

### Tests that can't fail
Generated tests over-mock (asserting the code calls the mocks the test set up — i.e.
pinning the implementation, not the behavior), assert nothing, or assert the vacuous
(`assert True`, `assertIsNotNone(result)` as the sole check). `find_test_smells.py`
catches assertion-less, over-mocked, and trivial-assertion tests. A green AI test
suite is **not** evidence of correctness until you've confirmed the tests would fail
on a wrong answer — change a return value and watch a test go red. If none do, the
suite is theater.

### Regeneration and edit artifacts
When AI rewrites a file it can emit the same function twice (the second silently
shadows the first), leave both an old and new version, or — through a botched merge —
leave `<<<<<<<` conflict markers. `find_duplicate_definitions.py` catches these.
They're pure bugs; there's no judgment call, just delete/resolve.

### Training-cutoff staleness
AI reaches for whatever was common at its training cutoff: deprecated APIs
(`datetime.utcnow()`, `pkg_resources`), old major-version library idioms (pydantic v1
syntax on a v2 install), superseded patterns. `find_outdated_idioms.py` and a type
checker catch some; cross-check anything version-sensitive against the library
version the repo actually pins (`references/typing-and-modernization.md`).

## Reviewing a change request (the diff lens)

When the unit of review is a CR/PR rather than a whole repo, narrow the analysis to
what changed: `scripts/analyze_diff.py` runs the file-level detectors against the
files (and, by default, the *added/modified lines*) of a diff, so you review the AI's
contribution, not the legacy around it.

```bash
python scripts/analyze_diff.py                 # working tree vs. the merge-base
python scripts/analyze_diff.py origin/main     # branch vs. an explicit base
python scripts/analyze_diff.py --format json | python scripts/format_findings.py
```

Architecture and whole-repo checks (`find_import_cycles`, `find_dependency_issues`,
`find_untested_modules`, `find_duplicates`) need the full tree — run those with
`analyze_all.py` separately; the diff lens deliberately covers only the per-file
detectors.

A CR-review checklist for AI code:

1. **Did it run?** Execute it; type-check it. Do this before reading closely — it
   eliminates the hallucination class for free.
2. **Is the diff only what was asked?** Flag unrequested files, flags, abstractions,
   and dependencies. Scope creep is the default; make it justify itself.
3. **Do the tests actually test?** Mutate the implementation and confirm a test goes
   red. Distrust any suite that stays green.
4. **Walk the boundaries by hand.** Empty, one, many, None, duplicate, negative. AI
   handles the middle of the range and misses the ends.
5. **Strip the ceremony.** Delete narration comments, redundant guards, and
   speculative abstraction — then re-read what's left. The real logic is usually a
   third of the diff, and that third is where the bug is.

The throughline: **AI raises the floor on style and lowers the floor on trust.** It
will rarely give you ugly code; it will regularly give you confident, well-dressed
code that was never run and is wrong at the edges. Spend your review budget on
correctness and scope, not on polish the model already handled.
