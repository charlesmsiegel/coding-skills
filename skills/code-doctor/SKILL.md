---
name: code-doctor
description: Review any codebase for committed credentials, unresolved merge conflicts, oversized files, and TODO debt — in any language, including ones with no dedicated tooling. Use when the user wants a repo checked, audited, triaged, or cleaned up and it is not primarily Python or TypeScript: Go, Rust, Ruby, Java, Kotlin, C#, PHP, Swift, Elixir, or a mixed/polyglot tree. Needs no parser, no build, and no install — it reads text and git, so it works on a fresh clone. Separates defects it can prove from unverified leads, and never recommends a fix on heuristic evidence. Triggers on "review this repo", "what's wrong with this codebase", "any secrets committed", "audit this project". For Python use python-code-doctor, for TypeScript use typescript-code-doctor, for Django use django-code-doctor — this skill defers to them and says so. For architecture and understanding rather than defects, use code-visualization.
---

# Code Doctor

A critical reviewer for **any** codebase. Its job is to find quality problems and
bugs, and it works without a parser, a build, an install, or knowing what
language it is looking at.

Today it ships two detectors: repository hygiene (merge markers, oversized
files, TODO debt, a committed `.env`) and committed secrets (key material,
recognisable cloud credentials). Everything else in the family's design —
duplication, dead-code leads, churn hotspots, a project-toolchain runner — is
landing in later plans and is not part of this skill yet.

## What this skill is, and is not

It measures **quality and bugs**. `code-visualization` explains **architecture**.
If the user wants to understand a codebase, hand off. This skill emits findings,
never maps or diagrams.

## Reviewer mindset (read this first)

Approach the code as **too complex until proven otherwise.** The default question
is not "is this OK?" but **"why isn't this simpler?"** Two hard limits keep the
criticism honest:

1. **Behavior is sacred.** Never change what the code does. If it isn't tested,
   pin current behavior with a characterization test *before* refactoring.
2. **Never assert more than the evidence supports.** This skill has no parser.
   Most of what it observes is heuristic, and saying so is what makes the rest
   worth reading.

## Findings and candidates

Every record is one of two kinds, and the difference is enforced in the schema:

- A **finding** asserts a defect. It carries a concrete fix.
- A **candidate** reports a lead needing verification. It carries the specific
  ways a healthy codebase produces the same observation, and **no fix**.

Never present a candidate as a defect, and never act on one without checking it
first. Recommending an edit on heuristic evidence is how a tool like this talks
someone into deleting live code.

Degrade audibly, too: a detector whose evidence can be incomplete (a file it
could not read, git being unavailable, a shallow clone) says so in the report
rather than staying silent about it. Where completeness cannot be established
at all, the detector suppresses the finding rather than footnoting a guess —
and never asserts a negative ("no problems found") from an index it knows is
incomplete.

## The three layers

| layer | what you get | needs |
|---|---|---|
| **raw** | works on any text-based repo | git + Python 3.11 |
| **the project's own toolchain** | whatever it already configured | its Makefile / npm scripts / pre-commit / CI |
| **specialist handoff** | parser-backed depth | python-code-doctor / typescript-code-doctor installed |

## Defer when a specialist exists

Check before reviewing by hand. A specialist sees types and syntax this skill
structurally cannot:

| the tree is mostly | load |
|---|---|
| Python | `python-code-doctor` |
| TypeScript / TSX | `typescript-code-doctor` |
| Django | `django-code-doctor` |

This skill also ships `scripts/route.py` (decides, from the repo's own manifests
rather than a filename census, which specialists are justified) and
`scripts/merge_reports.py` (unions several doctors' reports into one attributed
envelope). Their run-and-merge protocol is documented separately; for now it is
enough to know they exist and to load the matching specialist yourself when the
table above applies.

## Running the scripts

Let `SKILL=/path/to/this/skill` — the directory holding this SKILL.md. The
commands run from the project being reviewed, so they need that prefix.

Needs **Python 3.11+** and nothing else. The detectors are stdlib-only, so there
is nothing to install and no build to wait for.

```bash
python "$SKILL/scripts/analyze_all.py" .                  # everything, unified report
python "$SKILL/scripts/analyze_all.py" . --format json    # for tooling
python "$SKILL/scripts/analyze_all.py" . --skip secrets   # drop a category
python "$SKILL/scripts/analyze_all.py" . --only hygiene   # run one category alone

python "$SKILL/scripts/find_hygiene_issues.py" .   # merge markers, oversized files, TODO debt, .env
python "$SKILL/scripts/find_secrets.py" .          # key material, cloud credentials
```

All detectors share one interface: `--format text|json`, `--ignore type1,type2`,
🔴/🟡/🟢 severities. They are deliberately conservative — false negatives over
false positives — so the output stays trustworthy, and a file that cannot be read
is named on stderr rather than counted clean.

## Workflow

1. **Establish what the repo is.** Read the manifest and the entry points. If a
   specialist covers the bulk of it, load that skill and use this one for the rest.
2. **Run the analyzer.** Triage deterministic findings before spending judgment.
3. **Separate findings from candidates.** Act on findings. Verify candidates.
4. **Find the hot files.** Effort follows change frequency, not line count:
   ```bash
   git log --since="1 year ago" --name-only --pretty=format: \
     | sort | uniq -c | sort -rn | head -30
   ```
   **Don't refactor cold code.**
5. **Run the project's own checks** — see `references/unknown-language-review.md`.
   Never run one without knowing what it does; a `lint` script may carry `--fix`.
6. **Produce a findings artifact.** `analyze_all.py`'s own text output already
   groups findings ahead of candidates and labels each severity — one smell → one
   entry → one small PR.

## Output & ticketing

The deliverable is always an **artifact, never a side effect.** Produce a findings
list, cards, or JSON. This skill does **not** create tickets in any system on its
own. When the user wants findings filed, **ask which tracker or MCP to use** and
create them through that tool — never assume or fabricate one.

## Reference index (load on demand)

| Load this when… | File |
|---|---|
| Reading critically — the per-unit questions, the triage rubric, how to write a finding | `references/critical-review-guide.md` |
| Reviewing a language you do not know well; what each heuristic can and cannot support | `references/unknown-language-review.md` |

## When NOT to act

- On a **candidate** you have not verified. That is what the kind means.
- Untested code — write a characterization test first, *then* refactor.
- Cold code that never changes and blocks nothing.
- Complexity genuinely forced by an external API or a real present requirement.
