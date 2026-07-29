---
name: pr-visualization
description: Build a single-file, tabbed HTML review report for a pull request, branch, commit range, or uncommitted diff — change footprint with risk-ordered files, contract/signature changes, test delta with measured coverage of changed files, blast radius (untouched callers of changed symbols), plus judgment tabs (behavioral summary independent of the author's description, before/after flow diagrams, review walkthrough with a verdict). The report lands at docs/pr-NUMBER.html. Use whenever the user wants to review, understand, visualize, or assess a PR, diff, branch, or "what changed" — "review this PR", "what does this branch change", "is this change safe", "review my uncommitted changes". For mapping a whole codebase rather than a change, use the code-visualization skill.
---

# PR Visualization — Review Report

Produce **one deliverable**: the PR review report, a self-contained tabbed HTML file at `docs/pr-<number>.html`. Three tabs come from bundled scripts (deterministic diff analysis); three you write after reading the diff (judgment). The report's job is to help a reviewer *zero in on bad choices*: undisclosed behavior changes, untouched callers, missing tests, asymmetries, weakened invariants.

## Workflow

Let `SKILL=/path/to/this/skill`. Pick a scratch working dir for intermediate files (any temp location, e.g. `WORK=<temp>/pr-review-<name>`), with `TABS=$WORK/tabs`. Commands are POSIX-shell shaped; adapt to your shell.

### 1. Establish the diff

You need a local git repo containing both sides of the change:

- **Branch/PR checked out locally**: nothing to do. Scripts auto-detect the base (merge-base with, in order: origin/main, origin/master, main, master, origin/develop, develop). Pass `--base <ref>` when auto-detection is wrong, `--head <ref>` when the head isn't HEAD, or both for a commit range (`--base HEAD~5`). `--worktree` reviews uncommitted changes (untracked files included; on a mainline checkout the base falls back to HEAD itself; `--head` is rejected with it — the worktree IS the head).
- **GitHub PR URL**: clone the repo, then `git fetch origin pull/<N>/head:pr-<N> && git checkout pr-<N>`. If the user pasted the PR description, keep it — the Summary tab compares claims against the actual diff.
- **Patch file only**: `git init` a repo, commit the pre-images if available, apply the patch; if pre-images aren't available, note that blast radius and test-delta analysis will be partial.

### 2. Run the automated analyzers

```bash
mkdir -p $TABS
python $SKILL/scripts/analyze_diff.py         <repo> --tabs-dir $TABS [--base REF] [--worktree]
python $SKILL/scripts/analyze_blast_radius.py <repo> --tabs-dir $TABS [--base REF] [--worktree]
```

Each writes its fragment(s) and prints a JSON summary to stdout. **Read the summaries** — they are the skeleton of your judgment tabs: `analyze_diff` gives the risk-ordered file list with reasons, signature changes, renames, risky added lines (with file:line), and source files lacking test changes; `analyze_blast_radius` lists changed symbols and their *untouched* callers, with a `caveat` field and truncation counts you must respect — repeat its caveat when you cite its numbers. When the repo carries a coverage artifact (coverage.xml / lcov.info / coverage.out), `analyze_diff` also reports the measured coverage of each changed file (`coverage` in the summary; the artifact's age is stated — it predates the change, so it is the coverage the code started from). If it prints `coverage_hints` instead, one conversion command away is a parseable artifact — mention it or ask the user; never run the test suite unprompted. Signature/symbol tracing covers Python, JS/TS, Go, Rust, Ruby, Java, Kotlin, Scala, and C#; for other languages those sections degrade honestly. Both analyzers exit 0 with a JSON `note` on an empty or docs-only diff — that is an answer ("nothing reviewable changed"), not a failure.

### 3. Read the diff

Read the full diff for at least every file in the top of the risk ordering, plus every signature change and every risky added line the summary flagged. Follow untouched callers from the blast-radius summary into the actual code — that's where you check whether they still hold. Read surrounding context (`git show HEAD:<file>`), not just hunks: swallowed errors and missing cleanup live just outside the changed lines.

### 4. Write the judgment tabs

Read `$SKILL/references/llm-tabs.md` first (fragment format, styled primitives incl. `diff-snippet`, Mermaid patterns, per-tab guide). Write into `$TABS`:

| File | Tab | Content in brief |
|---|---|---|
| `01-summary.html` | Summary | Behavioral summary from the diff itself; claimed-vs-actual; what didn't change |
| `05-flow-impact.html` | Flow Impact | Before/after sequence diagrams for affected flows + what moved |
| `06-review-walkthrough.html` | Review Walkthrough | Per-file verdicts, asymmetry checklist, invariant impact, must-fix list |

(02–04 are the script tabs; the number prefix controls order.)

### 5. Verify citations

Judgment tabs assert `file:line` facts; check them before assembling:

```bash
python $SKILL/scripts/verify_citations.py <repo> --tabs-dir $TABS --fragments 01,05,06
```

Fix hard breaks (exit 1: missing/ambiguous paths, out-of-range lines), then skim the `cited_content` the JSON echoes for each citation and confirm the quoted reality still matches your claim. Prefer full paths over bare filenames so citations resolve unambiguously.

### 6. Assemble and deliver

The report's canonical home is **inside the repo at `docs/pr-<number>.html`**, using the PR number (`docs/pr-3637.html`). When there is no PR number — a local branch, commit range, or uncommitted diff — substitute a short slug: `docs/pr-<branch-slug>.html` (e.g. `docs/pr-fix-retry-loop.html`), or ask the user if nothing natural exists. Reports accumulate side by side (one file per PR, unlike the single `docs/codemap.html`), so they double as a review history.

```bash
mkdir -p <repo>/docs
python $SKILL/scripts/assemble.py --tabs-dir $TABS \
  --out <repo>/docs/pr-<number>.html \
  --title "<repo>#<PR or branch> — Review" \
  --subtitle "One-line description of the change" \
  --meta "base <base-ref> @ <sha> → <head> · +<adds>/−<dels> · generated <date>" \
  --footer "Generated by pr-visualization. Automated tabs: Footprint, Contracts &amp; Tests, Blast Radius (heuristic, name-based). Judgment tabs authored from diff reading — verify citations before acting."
```

The report is **part of the PR itself**: it's created on the branch, before the merge, and committed with it, so the merge carries the PR's review record into the mainline. This is safe because the analyzers mechanically exclude `docs/codemap.html` and `docs/pr-*.html` from the diff ("the report covers everything except itself"); the JSON summary lists them under `excluded_generated_docs`. So the analysis order doesn't matter, and re-running after committing the report produces the same result.

Check the assembler output lists all expected tabs. If the repo is the user's working copy (e.g., Claude Code), the file in `docs/` is the deliverable — offer to commit it. In a sandboxed chat environment where the user can't browse the repo, copy it to the user-visible outputs directory and present that copy, mentioning its intended home is `docs/pr-<number>.html`. Either way, present with a 2–4 sentence summary led by the verdict and the top must-fix items. Don't recap every tab.

**Stay out of `docs/codemap.html`.** This skill never writes or refreshes a repo's codebase atlas, even when one exists and this change has clearly made it stale. Updating the atlas is the code-visualization skill's job (its "rerun vs. revise" mode) and is a separate, explicitly requested task — this report is a ready-made list of behavioral changes to fold in when the user asks for that.

## Quality bar

- **Verdicts over summaries.** The reviewer can read the diff; tell them what it means and where it's likely wrong. Every significant claim cites `file:line`, and the most important findings quote the diff in a `diff-snippet`.
- **Independence.** Write the behavioral summary from the diff before consulting the author's description, then diff the two. Undisclosed changes are headline findings.
- **Honest heuristics.** Risk scores, test matching, and caller tracing are name-based heuristics — the tabs say so, and so should you. Never present a heuristic miss ("no callers found") as proof of safety when dynamic dispatch could hide callers.
- **Proportion.** A 10-line docs PR gets a short report (empty fragments are dropped — skip Flow Impact rather than pad it). A 3000-line change gets the full set with a long walkthrough.
- **Fragment protocol**: first line `<!-- tab: Title -->`, then panel HTML using the template's primitives only.
