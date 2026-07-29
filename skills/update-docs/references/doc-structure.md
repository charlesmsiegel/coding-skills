# What to write, and where

## The split

**SKILL.md is the index and the orientation.** It is loaded every time, so it pays
rent on every single interaction. Keep it under roughly 200 lines.

**`references/*.md` is the depth.** Loaded only when a task needs it, so length
costs little — but a page nobody can tell they need is a page nobody loads. That
is why the index table's one-line descriptions matter more than they look.

The test for whether something belongs in SKILL.md: **would a reader need this
before knowing which task they're on?** Orientation, yes. The schema of the
`orders` table, no.

## SKILL.md

```markdown
---
name: documentation
description: Use when working in this repository — what it does, how it is laid
  out, how to build/run/test it, and where to find deeper reference material.
---

# <Project>

One paragraph: what this does and for whom. The thing a README's first line
should say and usually doesn't.

## Key concepts

The three-to-six domain nouns whose meaning is not obvious from their names.
A reader who doesn't know what a "run" is here cannot read anything else.

## Architecture

How the pieces fit, in a paragraph or a small diagram. Name the layers and
cite an entry point for each.

## Getting started

Install, build, run, test — as literal commands, copied from CI or the
Makefile rather than remembered.

## Layout

The directory tree, one line each, with the generated/vendored dirs marked
so nobody documents or edits them.

## Reference index

| Load this when… | File |
|---|---|
| Tracing a request end to end | `references/data-flow.md` |
| Changing a model or a migration | `references/data-model.md` |
```

## Reference pages

Create only what the project actually has. Common shapes:

| Page | Contents |
|---|---|
| `data-flow.md` | The two or three end-to-end paths, as ordered steps with `file:line` |
| `components.md` | Per-module deep dives: responsibility, public interface, dependencies |
| `conventions.md` | Enforced vs habitual patterns; how to not get a PR rejected |
| `data-model.md` | Schemas, relationships, migrations, invariants the DB does not enforce |
| `api.md` | Endpoints, request/response shapes, auth, versioning |
| `infrastructure.md` | Deploy, environments, secrets, CI |
| `gotchas.md` | The things that surprise everyone. Often the most-read page |

`gotchas.md` earns its place more often than it gets written. The
undocumented invariant, the function that must be called before another, the
config that silently does nothing in dev — that is exactly the knowledge that
lives only in people's heads, and it is the first thing lost.

## Writing rules

**Cite everything non-obvious.** `path/to/file.py:88`. A claim without a citation
can never be checked by the staleness pass — only re-derived by hand, which nobody
will do.

**Prefer a path plus a symbol name to a line number** when the symbol is unique.
`api/router.py, register_routes()` survives edits that `api/router.py:88` does not.
Use line numbers when the target is a specific statement.

**Write what the code cannot say.** Why this shape, what was rejected, which
constraint forced the awkward part. The code states the what; the reader has it
already.

**Show, with real snippets.** Copy the actual code, don't paraphrase it into
pseudocode. Paraphrase is where the errors enter.

**Name the sharp edges.** "Changing this enum requires a migration and a deploy
ordering — see `migrations/0042`." That sentence prevents an outage.

**Mark uncertainty as uncertainty.** "I could not determine why this retries
twice; see `worker.py:120`" is an honest, useful line. A confident wrong sentence
is a trap, and it will be believed.

## What not to write

- **Anything you did not open.** A filename is a hypothesis, not a fact.
- **Restated code.** "`get_user()` gets a user" is noise that dilutes the useful
  lines around it.
- **Aspirational description.** Document what is, not what should be. If the code
  is a mess, say the code is a mess and where.
- **A full API dump** the reader can generate. Link to the generator instead.
- **Style critique.** That is a review, not documentation.

## Maintaining it

- Commit docs on their own (`docs: refresh project documentation`). A docs diff
  buried in a feature diff gets skimmed and lands wrong.
- Re-run `check_doc_staleness.py` after writing. `missing_path` and
  `citation_past_eof` must be empty before you finish — those mean you wrote a
  claim you never verified.
- When removing a reference page, confirm the subject is genuinely gone rather
  than merely absent from this pass's survey.
