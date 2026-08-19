# Output styles

Seven styles, split by context rather than by intensity. All push toward
shorter, flatter, less ornamented output.

| Style | Use for | Coding instructions |
|---|---|---|
| `blunt` | Conversation, review, advice. Sycophancy control. | kept |
| `peer` | Agentic coding. Execution discipline. | kept |
| `debug` | Narrowing a failure. Hypothesis before edit. | kept |
| `decide` | Recommendations. Answer, three reasons, falsifier. | kept |
| `reference` | Docs, specs, technical writing. | kept |
| `teach` | Explaining something new, under a word budget. | dropped |
| `plain` | Drafting and editing prose. | dropped |

## What an output style is

A Claude Code output style is appended to the **system prompt**, so it applies
to every turn and survives compaction. That is what distinguishes it from
`CLAUDE.md` (a user message) and from a skill (loaded on demand).

By default a custom style *replaces* Claude Code's built-in software
engineering instructions — how it scopes changes, writes comments, and
verifies work. The five styles above that are used while coding set
`keep-coding-instructions: true` to keep them; `teach` and `plain` leave it
out, because neither is a coding style and the built-ins are dead weight
there.

Getting this backwards is the easy mistake: a "better coding style" that
silently deletes the coding instructions.

## Install

    ./install.sh --claude --styles              # all seven
    ./install.sh --claude --styles 'blunt,peer' # a subset

Then `/config` → **Output style**, or set the style name in a settings file:

```json
{ "outputStyle": "Blunt" }
```

Output style is read once at session start, so a change takes effect after
`/clear` or in the next session.

The standalone `/output-style` command was deprecated in Claude Code v2.1.73
and removed in v2.1.91. If you have it in a script or a note, it is gone.

For claude.ai custom styles, paste the body of a file — everything after the
`---` frontmatter — into the style editor.

## Other agents

Neither Kiro nor Codex has output styles. The bodies port; the mechanism does
not.

**Kiro** has steering files, which are injected context rather than a system
prompt, but come close enough to be worth installing:

    ./install.sh --kiro --styles

Each style lands in `.kiro/steering/` as `style-<name>.md` with its
frontmatter rewritten to `inclusion: manual`, so you pull one in with
`#style-blunt` for a conversation. Change it to `inclusion: always` by hand if
you want it permanently on. The `style-` prefix keeps these clear of Kiro's
own `product.md` / `tech.md` / `structure.md`.

**Codex** has no equivalent, and `install.sh` will tell you so rather than
install something that half works. The three near-misses, and why none is
shipped:

- `$CODEX_HOME/prompts/*.md` slash commands — deprecated upstream in favour of
  skills, and a prompt is a user message, so its influence decays over a long
  session.
- `model_instructions_file` in `config.toml`, settable per `[profiles.x]` so
  `codex --profile blunt` would work — but it *replaces* the built-in
  instructions wholesale, which is the `keep-coding-instructions: false`
  failure mode with no way to opt out.
- `~/.codex/AGENTS.md` — genuinely always-on, but global across every project
  and not switchable per session.

If you want one anyway, `~/.codex/AGENTS.md` is the least bad: paste a body
into it and accept that it applies everywhere.

## Composing

One style is active at a time. Safe to concatenate under a single frontmatter
block:

- `blunt` + `peer` — how you talk, how you work.
- `peer` + `debug` — building and narrowing.
- `blunt` + `decide` — brevity and decision shape.

Do not merge `reference` with `teach` or `plain`; they conflict on register.
`decide` and `teach` conflict on purpose — one commits, one explains. When you
concatenate, keep `keep-coding-instructions: true` if *any* component is a
coding style.

## Enforcement

Each style opens with a `Style Active:` marker, mirroring Anthropic's
built-ins. Styles live in the system prompt, so they survive compaction and new
sessions — but that only guarantees the *text* persists, not that every rule
fires.

If you want per-turn reinforcement, add a `UserPromptSubmit` hook that echoes
the two or three rules you most care about. Worth it for dispositional rules,
unnecessary for diction ones.

## Why these rules and not others

Rules about diction and format hold reliably: every sentence is an occasion to
comply. Rules about disposition — push back, check first, reuse before
building, reproduce before fixing — apply only at rare decision points, and
fail silently when skipped.

So each dispositional rule here demands a visible trace: name the option you
rejected, say what was checked and what wasn't, say what the result ruled out,
name what would change the recommendation. A rule with an output signature can
be caught when violated. One without cannot.

Failure modes to watch when tuning:

- Anti-sycophancy drifting into contrarianism — manufactured objections that
  exist to prove independence. `blunt` names this; keep that line if you
  rewrite it.
- "Act first" quietly overriding "design first" whenever the patch is cheaper
  than the model. `peer` sets a threshold (multi-file, hard to reverse, new
  interface). Tune it; don't delete it.
- Word budgets stated as adjectives. `teach` says 200 words, not "concise,"
  because "concise" is unfalsifiable and gets ignored.

## Measuring

Before shipping a change to a style, run one fixed question with and without it
and compare word counts and whether the answer got worse. Most style rules do
nothing; the count tells you which ones.
