# Typing, Modernization, and Dependencies

Three kinds of rot accumulate in an old-but-working Python repo that the in-file
smell detectors don't cover: **no type annotations**, **dated idioms** the language
has since outgrown, and a **dishonest dependency manifest**. This guide is the
judgment for closing those gaps with the detectors `find_type_gaps.py`,
`find_outdated_idioms.py`, and `find_dependency_issues.py`.

The stance is the same as everywhere else in this skill: change must be incremental
and behavior-preserving. Typing and modernization are *easy* to do destructively —
a wrong annotation that silences a real error, a "modernize" that changes semantics.
Move in small, verified steps.

## Part 1 — Adopting types incrementally

Types are the cheapest durable documentation and the only check that catches a whole
class of bug before runtime. But you do not annotate a messy repo all at once, and
you never turn on `--strict` over untyped code — you'd drown in errors and disable
the checker out of spite. Adopt it as a ratchet.

**The order:**

1. **Turn on the checker in permissive mode.** Add mypy or pyright with a lax
   config (no `disallow_untyped_defs` yet). Get it to run green over the repo —
   mostly by ignoring, not fixing, at first. That establishes the baseline.
2. **Annotate boundaries first.** Public functions, public methods, module-level
   APIs — the surfaces other code calls. That's exactly what `find_type_gaps.py`
   flags as `missing_return_annotation` / `missing_param_annotation`. Internal
   one-line helpers can wait; their callers will infer fine.
3. **Tighten file by file.** Use per-module overrides to enable
   `disallow_untyped_defs` on modules you've fully typed, leaving the rest lax.
   Each cleaned module ratchets one notch; the baseline can only shrink.
4. **Hold the line in CI.** New code is type-checked; old code is paid down as it's
   touched. (See `references/messy-repo-runbook.md`, Phase 5.)

**`Any` is not a type — it's the absence of one** (`any_overuse`). An `Any`
annotation looks like progress while disabling every check that annotation was for.
Reach for a real type, a `Protocol`, a `TypeVar`, or a union before `Any`. If a
value genuinely is dynamic, `object` (which forces callers to narrow) is usually
more honest than `Any` (which lets anything through silently).

**A bare `# type: ignore` hides the next bug too** (`broad_type_ignore`). It
silences *every* error on that line forever, including ones introduced later.
Always scope it: `# type: ignore[assignment]`. Better, fix the underlying cause —
a bare ignore is often a real type bug wearing a blindfold.

**Don't let annotations lie.** A wrong type is worse than no type: it asserts a
falsehood the checker now trusts. When you can't easily express the real type,
leave it unannotated rather than annotate it wrong. And annotating untested code
doesn't make it safe to refactor — types check shapes, not behavior. The
characterization test still comes first (`references/safety-net-and-testing.md`).

## Part 2 — Modernizing dated idioms

`find_outdated_idioms.py` flags constructs the language has moved past. These are
mostly mechanical and mostly behind Ruff's `UP` (pyupgrade) ruleset — if the repo
runs `ruff check --select UP --fix`, let it do the work and `--ignore` the
overlapping detector. Where it doesn't, here's the *why* and the one place each can
bite:

| Dated (`smell_type`) | Modern | Watch out for |
|---|---|---|
| `"%s" % x` (`percent_format`) | f-string | `%` on a non-literal, or a logging call `log.info("%s", x)` — **leave logging alone**, its lazy `%` formatting is deliberate. |
| `"{}".format(x)` (`str_format_call`) | f-string | `.format` with reused/positional args, or a stored template string used later — an f-string interpolates *now*. |
| `typing.List[int]`, `Optional[x]` (`old_typing_alias`) | `list[int]`, `x \| None` | Builtin generics need 3.9+; `X \| Y` needs 3.10+ (or `from __future__ import annotations`). Check the target version first. |
| `super(Cls, self)` (`super_with_args`) | `super()` | Only equivalent inside a normal method of `Cls`; bare `super()` relies on `__class__`, so it's not valid outside a class body. |
| `os.path.join(...)` (`os_path_join`) | `pathlib.Path` | Migrate a whole call site at once — half `os.path`, half `Path` is worse than either. `Path` returns objects, not `str`; check consumers that do string ops. |

Modernization is **behavior-preserving by definition** — if a swap changes
behavior, it wasn't the right swap. Verify against the safety net like any other
refactor, and don't bundle a version bump (which *enables* some of these) into the
same PR as the rewrites that depend on it.

## Part 3 — Dependency and packaging honesty

A repo that runs on its author's machine often has a manifest that lies — deps that
aren't declared (so a fresh checkout fails), deps that are declared but unused (so
the install is bloated and the audit surface is wider than it needs to be), and
versions that float (so "works today" doesn't mean "works tomorrow").
`find_dependency_issues.py` reconciles declared-vs-imported.

- **`missing_dependency` (imported, not declared).** The most dangerous: the code
  works only because the package happens to be installed. A clean install breaks.
  Add it to the manifest. (Mind the import-vs-distribution name gap — `import yaml`
  ↔ `PyYAML`, `import bs4` ↔ `beautifulsoup4`, `import cv2` ↔ `opencv-python`; the
  detector knows the common ones and stays quiet when unsure.)
- **`unused_dependency` (declared, never imported).** Usually cruft from deleted
  code; remove it. But confirm by reading first — some deps are real without an
  `import`: build/runtime plugins, CLI tools, things imported via entry points or
  string paths. The detector already skips the usual tooling suspects (pytest,
  ruff, mypy…); for anything else, check before deleting.
- **`unpinned_dependency` / `no_dependency_manifest`.** Floating versions make
  builds irreproducible — the same commit installs different code on different
  days. Pin versions (at minimum a floor) and consolidate everything onto one
  `pyproject.toml` with a lockfile. A single source of truth for deps is itself a
  simplification.

Beyond what the detector sees: check for **known-vulnerable or abandoned packages**
(an audit tool like `pip-audit`), a sane **Python-version floor** that matches what
the code actually uses, and **duplicate manifests** (`setup.py` *and*
`requirements.txt` *and* `pyproject.toml` disagreeing). Converge them. As with
everything here: declare, pin, and ratchet it shut in CI so the manifest can't drift
back out of sync.
