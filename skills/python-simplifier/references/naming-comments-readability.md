# Naming, Comments, and Readability

Most code is read far more often than it is written, and most of the time it is
read it is by someone trying to change something nearby. Names and comments are the
interface to that reader. `find_naming_issues.py` catches mechanical problems
(shadowed builtins, casing). This file is about whether the names and comments tell
the truth.

## Names must reveal intent

A name should answer *what is this and why does it exist* without the reader having
to read the implementation. If a name needs a comment to explain it, the name is
wrong.

Interrogate names with these prompts:

- **Does the name say what it is, or what type it is?** `data`, `info`, `obj`,
  `item`, `value`, `temp`, `result`, `the_list`, `d`, `x2` carry no meaning. What is
  in the list? `pending_invoices`, not `data`. (Single letters are fine *only* for
  tight loop indices and math, where convention supplies the meaning.)
- **Would I find every use by searching this name?** Names should be searchable.
  A function called `process` or a constant `7` can't be grepped meaningfully;
  `retry_after_seconds = 7` can.
- **Does the name lie or mislead?** A function named `get_user` that also writes to
  the database, a `count` that's actually a list, an `is_valid` that mutates — these
  are worse than vague names. The name must match what the code does.
- **Is one concept named one way everywhere?** Pick one word per concept and keep
  it: don't mix `fetch` / `get` / `retrieve` / `load` for the same operation, or
  `user` / `account` / `member` for the same entity. Inconsistent vocabulary forces
  readers to wonder whether the difference is meaningful.
- **Are there encodings or noise words?** Drop Hungarian-style prefixes
  (`strName`, `lstItems`), redundant context (`user.user_name`), and filler
  (`*Manager`, `*Data`, `*Info`, `do_*`) that add length without meaning.
- **Does the length match the scope?** A loop variable can be short; a module-level
  constant or public function deserves a full, descriptive name. Long names for tiny
  scopes and cryptic names for wide scopes are both wrong.

A renaming is one of the safest, highest-leverage simplifications there is. When a
good name makes a comment redundant, rename and delete the comment.

## Comments explain *why*, not *what*

Good code says *what* and *how* by itself. Comments are for what the code cannot
say: the reason a choice was made, a non-obvious constraint, a link to a bug or
spec, a warning about a sharp edge.

- **Delete comments that restate the code.** `# increment i` above `i += 1` is
  noise; `# loop over users` above `for user in users` is noise. They rot the moment
  the code changes and the reader stops trusting all comments.
- **Delete commented-out code.** Always. Version control remembers it; a graveyard
  of dead code in the file only confuses. (`find_comment_smells.py` finds these.)
- **A comment that explains *what* a block does is a missing function.** Extract the
  block into a well-named function and the comment becomes the name. "This needs a
  comment to be understood" is a refactoring signal, not a documentation task.
- **Stale comments are lies.** A comment that contradicts the code is worse than
  none. When you change code, fix or delete the comments around it.
- **Keep the warnings and the why.** "// must run before auth middleware or sessions
  leak", "// O(n²) but n < 10 in practice", "// workaround for upstream bug #1234"
  earn their place. So do real docstrings on public functions stating contract,
  params, returns, and raises.

## Function shape

Readability is mostly a property of function shape:

- **Small and one thing.** A function should do one thing, at one level of
  abstraction. If it mixes high-level orchestration with low-level fiddling, extract
  the fiddling.
- **One level of abstraction per function.** Don't interleave "charge the customer"
  with byte-twiddling. Each layer reads as a short story in its own vocabulary.
- **Few parameters.** Zero–two is comfortable, three is a stretch, four-plus is a
  smell (often a missing parameter object — see `find_parameter_objects.py`).
  Boolean parameters usually mean two functions (`find_boolean_params.py`).
- **Command/query separation.** A function should either do something (a command,
  returns None) or answer something (a query, no side effects) — not both. A getter
  that mutates surprises everyone.
- **No surprises.** A function should do what its name says and nothing more. Hidden
  side effects (writing files, mutating globals, sending requests) inside something
  that looks like a pure computation are a readability and correctness hazard.

## The readability test

After a change, ask: **could a tired developer who has never seen this file
understand it at a glance and change it safely?** If understanding it requires you,
its author, to be in the room, it isn't done. Prefer the version with fewer
concepts, plainer names, and flatter structure — even when a denser version would
have impressed you.
```
