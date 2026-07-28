---
name: pr-visualization
description: Build a single-file, tabbed HTML review report for a pull request, branch, commit range, or uncommitted diff — change footprint with risk-ordered files, contract/signature changes, test delta, blast radius (untouched callers of changed symbols), plus judgment tabs (behavioral summary independent of the author's description, before/after flow diagrams, asymmetry-checklist walkthrough with a verdict). The report lands at docs/pr-NUMBER.html, and the skill also refreshes the repo's docs/codemap.html atlas in the same run so it stays current with the merged change. Use whenever the user wants to review, understand, visualize, or assess a PR, diff, branch, changeset, or "what changed" — "review this PR", "what does this branch change", "visualize this diff", "is this change safe", "compare main to my branch", "review my uncommitted changes". For understanding or mapping a whole codebase rather than a change, use the code-visualization skill.
---

# PR Visualization — Review Report

Produce **two deliverables**: the PR review report (one self-contained tabbed HTML file at `docs/pr-<number>.html`) and, when a codemap atlas exists, a refreshed `docs/codemap.html` that absorbs the change (step 7). For the report itself: Three tabs come from bundled scripts (deterministic diff analysis); three you write after reading the diff (judgment). The report's job is to help a reviewer *zero in on bad choices*: undisclosed behavior changes, untouched callers, missing tests, asymmetries, weakened invariants.

## Workflow

Let `SKILL=/path/to/this/skill`, `CVSKILL=/path/to/the/code-visualization/skill` (sibling install, used in step 7), `WORK=/home/claude/pr-review-<name>`, `TABS=$WORK/tabs`.

### 1. Establish the diff

You need a local git repo containing both sides of the change:

- **Branch/PR checked out locally**: nothing to do. Scripts auto-detect the base (merge-base with origin/main, origin/master, main, master, develop). Pass `--base <ref>` when auto-detection is wrong or the diff is a commit range (`--base HEAD~5`), and `--worktree` to include uncommitted changes.
- **GitHub PR URL**: clone the repo, then `git fetch origin pull/<N>/head:pr-<N> && git checkout pr-<N>`. If the user pasted the PR description, keep it — the Summary tab compares claims against the actual diff.
- **Patch file only**: `git init` a repo, commit the pre-images if available, apply the patch; if pre-images aren't available, note that blast radius and test-delta analysis will be partial.

### 2. Run the automated analyzers

```bash
mkdir -p $TABS
python3 $SKILL/scripts/analyze_diff.py         <repo> --tabs-dir $TABS [--base REF] [--worktree]
python3 $SKILL/scripts/analyze_blast_radius.py <repo> --tabs-dir $TABS [--base REF] [--worktree]
```

Each writes its fragment(s) and prints a JSON summary to stdout. **Read the summaries** — they are the skeleton of your judgment tabs: `analyze_diff` gives the risk-ordered file list with reasons, signature changes, risky added lines (with file:line), and source files lacking test changes; `analyze_blast_radius` lists changed symbols and their *untouched* callers. Signature/symbol tracing covers Python, JS/TS, Go, Rust, Ruby, Java-family; for other languages those sections degrade honestly.

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
python3 $SKILL/scripts/verify_citations.py <repo> --tabs-dir $TABS --fragments 01,05,06
```

Fix hard breaks (exit 1: missing/ambiguous paths, out-of-range lines), then skim the `cited_content` the JSON echoes for each citation and confirm the quoted reality still matches your claim. Prefer full paths over bare filenames so citations resolve unambiguously.

### 6. Assemble and deliver

The report's canonical home is **inside the repo at `docs/pr-<number>.html`**, using the PR number (`docs/pr-3637.html`). When there is no PR number — a local branch, commit range, or uncommitted diff — substitute a short slug: `docs/pr-<branch-slug>.html` (e.g. `docs/pr-fix-retry-loop.html`), or ask the user if nothing natural exists. Reports accumulate side by side (one file per PR, unlike the single `docs/codemap.html`), so they double as a review history.

```bash
mkdir -p <repo>/docs
python3 $SKILL/scripts/assemble.py --tabs-dir $TABS \
  --out <repo>/docs/pr-<number>.html \
  --title "<repo>#<PR or branch> — Review" \
  --subtitle "One-line description of the change" \
  --meta "base <base-ref> @ <sha> → <head> · +<adds>/−<dels> · generated <date>" \
  --footer "Generated by pr-visualization. Automated tabs: Footprint, Contracts &amp; Tests, Blast Radius (heuristic, name-based). Judgment tabs authored from diff reading — verify citations before acting."
```

Both generated artifacts are **part of the PR itself**: they're created on the branch, before the merge, and committed with it — the merge then carries a current codemap and the PR's review record into the mainline. This is safe because the analyzers mechanically exclude `docs/codemap.html` and `docs/pr-*.html` from the diff ("the report covers everything except itself"); the JSON summary lists them under `excluded_generated_docs`. So the analysis order doesn't matter, and re-running after committing the docs produces the same result.

Check the assembler output lists all expected tabs. If the repo is the user's working copy (e.g., Claude Code), the file in `docs/` is the deliverable — offer to commit it. In a sandboxed chat environment where the user can't browse the repo, copy it to the user-visible outputs directory and present that copy, mentioning its intended home is `docs/pr-<number>.html`. Either way, present with a 2–4 sentence summary led by the verdict and the top must-fix items. Don't recap every tab.

### 7. Update the codemap (part of the job, not an afterthought)

A PR report and a stale atlas is half-finished work: if `<repo>/docs/codemap.html` exists, bring it current in the same run. The judgment work is already done — the Summary and Walkthrough tabs you just wrote name exactly the behavioral changes and weakened invariants the atlas must absorb.

**Semantics: the codemap tracks the branch's state.** Update it on the branch, as part of the PR, so `docs/codemap.html` always describes the code it sits next to — reviewers of the PR see the atlas of the proposed state, and the merge delivers a current codemap to the mainline with zero post-merge work. Record the branch head sha in the meta line.

**First, check whether the existing codemap can be trusted:**

```bash
python3 $SKILL/scripts/check_codemap_state.py <repo>
```

Act on the verdict:
- `current` / `stale` → normal in-place revision (below). `stale` with `files_changed_since` much larger than this PR means unabsorbed drift from earlier merges — use the code-visualization skill's rerun-and-reconcile mode instead of blaming this PR's revision for all of it.
- `merge-resolution-suspect` → **the codemap was last changed by a merge commit** (a hand-resolved conflict, or git silently auto-splicing two branches' independent atlas revisions — it flags both, since neither splice was ever reviewed as a whole). The codemap must be updated regardless of how small this PR is. Extract it, but re-verify *every* citation (all fragments, not just files this PR touched), and reconcile the judgment tabs against both parents' versions (`git show <parent-sha>:docs/codemap.html`, then `extract_tabs.py` each) — a finding present in either parent must survive into the update or be deliberately closed. When the splice looks badly garbled, rebuild via code-visualization instead.
- `conflict-markers` → the file is corrupt (markers committed); extraction would recover garbage. Rebuild via the code-visualization skill, using both merge parents' versions as findings checklists.
- `missing` → don't build one unprompted; offer to generate it with the code-visualization skill.

**How** — this is the "revise in place" path of the code-visualization skill (a single PR is the definitional small drift); its scripts live in that skill's directory (`CVSKILL`), which should be installed alongside this one:

```bash
ATABS=$WORK/atlas-tabs
python3 $SKILL/scripts/extract_tabs.py <repo>/docs/codemap.html --out-dir $ATABS   # prints old sha in meta
python3 $CVSKILL/scripts/analyze_inventory.py <repo> --tabs-dir $ATABS   # regenerate automated tabs 02-04
python3 $CVSKILL/scripts/analyze_deps.py      <repo> --tabs-dir $ATABS
python3 $CVSKILL/scripts/analyze_hotspots.py  <repo> --tabs-dir $ATABS
```

Then revise the atlas's judgment tabs using your PR findings: fold in new behavior and new risks, close resolved ones ("resolved by <sha>", then remove), re-check every atlas claim that cites a file this PR touched — the Blast Radius and risky-lines summaries tell you which. Verify (`verify_citations.py <repo> --tabs-dir $ATABS --fragments 01,05,06,07,08`), fix breaks, compare `cited_content` for touched files, then reassemble with `$SKILL/scripts/assemble.py` to `<repo>/docs/codemap.html` with `--label "CODEBASE ATLAS"` and `--meta "updated <date> · <old-sha> → <new-sha> · …"`.

If little in the atlas is affected (a docs-only PR, a leaf-module fix), a near-identical codemap with a fresh sha is the correct output — the sha bump is the point, since it's what keeps future updates' diffs small and keeps the `check_codemap_state.py` verdict clean for the next PR. If the code-visualization skill isn't installed, revise only the judgment tabs, leave 02–04 untouched, and add a visible note in the atlas Overview that automated tabs date from the old sha.

Commit both files to the branch and deliver them together, with one line on what changed in the atlas.

## Quality bar

- **Verdicts over summaries.** The reviewer can read the diff; tell them what it means and where it's likely wrong. Every significant claim cites `file:line`, and the most important findings quote the diff in a `diff-snippet`.
- **Independence.** Write the behavioral summary from the diff before consulting the author's description, then diff the two. Undisclosed changes are headline findings.
- **Honest heuristics.** Risk scores, test matching, and caller tracing are name-based heuristics — the tabs say so, and so should you. Never present a heuristic miss ("no callers found") as proof of safety when dynamic dispatch could hide callers.
- **Proportion.** A 10-line docs PR gets a short report (empty fragments are dropped — skip Flow Impact rather than pad it). A 3000-line change gets the full set with a long walkthrough.
- **Fragment protocol**: first line `<!-- tab: Title -->`, then panel HTML using the template's primitives only.
