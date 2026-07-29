# Surveying a codebase

Four axes. Each answers a question the others cannot, and together they cover what
a newcomer needs before they can make a safe change.

These are **briefs, not a mandated fan-out.** If the user has asked for subagents,
they parallelize cleanly — they share no state and their outputs merge by
concatenation. If not, work them yourself in this order; the later axes are easier
once the earlier ones are done.

Whoever runs them, the rule holds: **an axis returns file paths and quoted
evidence, not impressions.** "The API layer looks well-organized" is not a
finding. `api/router.py:1-40 registers 12 routes, all delegating to
services/*.py` is.

---

## 1. Structure

> Map the directory tree three levels deep. For each directory, say what it
> contains — based on the files in it, not its name. Identify: entry points
> (`main`, `cli`, `app`, `server`, `__main__`, `index`); build and packaging files;
> configuration; generated or vendored code that should be excluded from the rest
> of the survey. Return a tree with a one-line purpose per directory, and the
> absolute set of entry points. Where a directory's name and its contents
> disagree, say so — that mismatch is worth documenting.

Generated and vendored code is the most valuable output here: it stops the other
three axes from documenting code nobody maintains.

## 2. Stack and patterns

> Identify the languages, frameworks, and libraries actually in use. Read the
> package manifests (`package.json`, `pyproject.toml`, `requirements*.txt`,
> `go.mod`, `Cargo.toml`, `pom.xml`, `Gemfile`) and then check the imports —
> a manifest lists what is installed, not what is used. Note the architectural
> shape (layered, MVC, event-driven, monorepo, plugin, pipeline) and cite the
> files that make it visible. Flag any dependency in the manifest that nothing
> imports, and any import that is not in the manifest.

That last instruction catches a real problem: the manifest and the imports drift,
and documentation written from the manifest describes a stack the project doesn't
have.

## 3. Data flow

> Trace how data moves. Find HTTP routes / CLI commands / event handlers /
> scheduled jobs — every way execution enters this system. For each entry point,
> follow the call path to where data is persisted or sent onward, and name the
> layers it passes through. Find the database models or schemas and the external
> services called. Return the two or three most important end-to-end paths as
> ordered step lists with `file:line` at each step.

This is the axis that produces the most useful documentation and takes the most
work. A newcomer's first real question is always "where does a request go", and
almost no repository answers it.

## 4. Conventions

> Identify what this project does consistently that a newcomer would otherwise get
> wrong. Naming patterns, file organization rules, error-handling approach (return
> values vs exceptions vs a Result type), logging, configuration access, test
> layout and naming, and how tests get their fixtures. Read `CLAUDE.md`,
> `AGENTS.md`, `CONTRIBUTING.md`, `.editorconfig`, and the linter configuration if
> they exist. Distinguish conventions that are **enforced** (a linter rule, a CI
> check, a type) from ones that are merely **habitual** — the second kind is where
> a newcomer's PR gets rejected for reasons nobody wrote down.

The enforced/habitual distinction is the highest-value thing this axis produces.

---

## After the survey

The survey tells you what to read. **Now read it** — entry points, the modules the
data-flow axis walked through, the schemas, the config. Not the whole repo; enough
that every claim you are about to write has a file behind it.

Two checks before writing:

- **Do the axes contradict each other?** Structure says `workers/` is the job
  runner, data flow found no path reaching it. One of them is wrong, and resolving
  it is usually where the interesting fact lives (it's dead code, or it's invoked
  by cron outside the repo).
- **Did any axis report an impression instead of evidence?** Go get the evidence
  or drop the claim.

## Scoping a large repository

For a monorepo or anything above roughly 100k lines, do not survey it all. Pick
the scope with the user, and say in the docs what was covered.

Reasonable scopes: one service or package; the paths touched in the last 90 days
(`git log --since='90 days ago' --name-only --pretty=format: | sort | uniq -c |
sort -rn`); or whatever the user is about to work on. Churn is usually the best
proxy — it is where documentation pays off and where it goes stale fastest.
