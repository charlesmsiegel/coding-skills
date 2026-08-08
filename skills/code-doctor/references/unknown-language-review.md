# Reviewing a language you do not know well

This skill is language-blind by design, and that is exactly the situation you
are in whenever the tree in front of you is not one you read fluently. The
discipline is the same either way: orient before judging, prefer the project's
own tools over guessing, and say plainly when a heuristic cannot support the
claim you want to make.

## Orient before judging

Establish three facts before forming any opinion:

1. **Read the manifest.** `go.mod`, `Cargo.toml`, `Gemfile`, `pom.xml`,
   `build.gradle`, `*.csproj`, `composer.json`, `mix.exs` — whatever declares
   dependencies also names the language, the version floor, and often the
   framework. This is also the evidence this skill's own router
   (`scripts/route.py`) insists on: a manifest the repository wrote about
   itself, never a filename census.
2. **Find the entry points.** A `main` function, a binary target, an `app.py`
   equivalent, a Dockerfile `CMD` or `ENTRYPOINT`. An entry point legitimately
   has no internal caller — do not mistake that for dead code.
3. **Identify the test command.** Not "does a `tests/` directory exist" but
   "what command actually runs them and did it recently pass." A repo whose
   test suite is broken on its own `main` branch is a different conversation
   than one whose suite is green.

Three facts before any opinion. Skipping this step is how a reviewer flags an
idiomatic pattern as a defect because it looked unfamiliar.

## The repo knows its own language

Prefer running the project's own configured checks over reconstructing them by
eye. Read before running — a target's *name* is not proof of what it does:

| File | What to look for |
|---|---|
| `Makefile` / `justfile` | Targets like `lint`, `check`, `test`, `fmt`. Read the recipe body, not just the target name. |
| `package.json` | The `scripts` block — `lint`, `test`, `build`, `typecheck` are conventional names but not guaranteed contracts. |
| `.pre-commit-config.yaml` | The hooks a commit is actually gated on. This is frequently the most honest list of what the project itself considers a check. |
| CI workflow (`.github/workflows/*.yml`, `.gitlab-ci.yml`, etc.) | The commands a real merge has to pass. This is the ground truth for "does the project's own tooling catch this," because it runs on every change whether a human remembers to or not. |

**The warning that matters:** a target named `lint` may carry `--fix`, `-w`, or
an equivalent auto-correct flag. Running it changes files. Read the command
before running it, every time — "lint" is a name, not a promise that it's
read-only.

## What each heuristic can and cannot support

Heuristic analysis — whether it comes from this skill's own detectors, the
project's own linter, or a specialist doctor — has known blind spots. Knowing
them is what turns "the tool didn't flag it" into an honest silence instead of
a false all-clear.

| Heuristic | What it can support | What it misses |
|---|---|---|
| Reference graph (tracing imports/calls to find what's used) | "Nothing in the scanned tree references this" | Package-style imports common in Go, the JVM languages, and Rust; aliased or re-exported names; anything loaded dynamically (reflection, plugin registries, string-built import paths) |
| Single-occurrence symbols (a name defined once, referenced nowhere) | A lead worth checking by hand | Anything reached only through reflection, dependency injection, a template, or a build-time code generator |
| Duplication (near-identical text or structure) | "These two blocks look alike" | Whether they encode the *same decision* — code that looks alike but changes for different reasons must stay separate, and a shape match cannot tell the two apart |
| Size and nesting (line counts, indentation depth) | A rough proxy for how hard a unit is to hold in your head | Anything the project's own formatter controls — the same logic reads as "nested" or "flat" depending on style choices no reviewer made |
| Git signals (churn, ownership, blame) | Real answers, when the history is deep enough | A shallow clone (`git clone --depth 1`) or a young/rewritten history — these produce a confident-looking answer that is simply wrong, not a smaller-but-correct one |

Every row has the same shape: the heuristic supports a *lead*, not a *verdict*,
and the specific way it goes wrong is worth naming in the record rather than
hand-waved. That is what separates a candidate from a finding elsewhere in
this skill.

## Say when you are not confident

A review that separates "this is a defect" from "this looks odd and I cannot
tell" gets acted on. One that mixes them gets skimmed, because the reader can
no longer trust any single line without re-verifying it themselves. When a
heuristic's known blind spot (the table above) applies to what you are looking
at, say so in the same sentence as the observation — don't let the caveat live
only in this file, disconnected from the specific claim it qualifies.
