---
name: update-docs
description: Build or refresh a project's own documentation skill — a SKILL.md plus references/ that gives an agent (or a new developer) working knowledge of the codebase. Use when the user says "update the docs", "regenerate the documentation", "refresh the project docs", "document this codebase", "write a CLAUDE.md for this", or "the docs are out of date". Checks the existing docs against the tree first — broken citations, files that changed since, directories churning with no coverage — and revises what actually went stale instead of rewriting from scratch. Every claim it writes must be traceable to a file it read. For a visual codebase atlas with diagrams, use code-visualization instead.
---

# Update the Project Documentation

Produce documentation whose claims are **true of the code as it is now**, and keep
it that way cheaply. Every statement traces to a file that was read; nothing is
inferred from a directory name.

## First: is this a refresh or a first write?

```bash
python scripts/check_doc_staleness.py                       # .claude/skills/documentation
python scripts/check_doc_staleness.py --docs docs/ --repo .
python scripts/check_doc_staleness.py --format json
```

The script reports four mechanically-checkable things:

| Finding | Meaning |
|---|---|
| `missing_path` | A cited path matches nothing in the tree — it moved or was deleted |
| `citation_past_eof` | A `file:line` citation points past the end of that file |
| `changed_since_documented` | A cited file has been committed to since the docs were written |
| `undocumented_churn` | A directory is churning and no doc page mentions it |

The last one is the one worth reading carefully: **missing documentation is
invisible by construction.** You cannot spot it by reading the docs, only by
comparing them against where the work is actually happening.

If there are no docs yet, it says so — that is a first write, and the whole
staleness step is skipped.

**A refresh is not a rewrite.** Findings tell you which pages to revisit. Prose
that is still accurate stays; rewriting it burns tokens and throws away judgment
that was correct. Rewrite from scratch only when the architecture genuinely
changed out from under the docs.

## Explore

For a first write, or for the areas the staleness pass flagged, survey along four
axes — structure, stack and patterns, data flow, and conventions.
`references/exploration-plan.md` has the four briefs and what each must return.

Run them in parallel **only if the user has asked for subagents** — otherwise work
the four axes yourself, in order. They are a checklist, not a mandated fan-out.

Then **read the files the survey flagged.** Entry points, core modules, configs,
schemas. Not everything — enough that every claim you are about to write is one
you have evidence for.

## Write

Structure and the SKILL.md/references split are in `references/doc-structure.md`.
The rules that matter most:

- **Cite.** Every non-obvious claim carries a `path/to/file.py:88`. This is what
  makes the next staleness pass possible — an uncited claim can never be checked,
  only re-derived.
- **Prefer paths to line numbers** where the file is small or the symbol is
  unique. Line numbers rot fastest.
- **Write what is not in the code.** Why this structure, what the constraints
  were, which alternative was rejected, where the sharp edges are. A reader can
  read the code; they cannot read the reasoning.
- **Say what you did not cover.** A documentation set claiming no gaps is lying.

## Verify before finishing

Re-run the staleness check against what you just wrote:

```bash
python scripts/check_doc_staleness.py --format json
```

`missing_path` and `citation_past_eof` must be empty. If you wrote a citation that
does not resolve, you wrote a claim you did not check — fix it now rather than
shipping documentation that is wrong on its first day.

Then spot-check two or three substantive claims by opening the file and confirming
the code says what you said it says.

## The failure this exists to prevent

Documentation generated from directory names and file names — plausible,
well-organized, and wrong. It reads as authoritative and costs more than no
documentation, because a reader trusts it instead of the code.

Three habits prevent it:

1. **Never describe a file you did not open.** A name is a hypothesis.
2. **Never write a claim you cannot cite.** If you can't point at the line, you
   are guessing.
3. **When you are unsure, say so in the text.** "The retry path here is not
   obvious to me; see `worker.py:120`" is worth more than a confident wrong
   sentence, and it tells the next reader where to look.

## Boundaries

- **Don't invent conventions.** Document what the code does, not what it should
  do. A style critique belongs in a review, not in documentation.
- **Don't delete reference pages just because they weren't regenerated.** Confirm
  the subject is actually gone first.
- **Commit as its own change** (`docs: refresh project documentation`), never
  mixed with code — a docs diff mixed into a feature diff gets skimmed.

## Reference index (load on demand)

| Load this when… | File |
|---|---|
| Surveying the codebase — the four briefs and what each must return | `references/exploration-plan.md` |
| Deciding what goes in SKILL.md vs references/, and how to write each page | `references/doc-structure.md` |
