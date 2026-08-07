# Modules, imports and the dependency graph

The module graph is the part of a codebase you cannot see by reading one file, and
it is where the slow builds, the mystery runtime errors and the supply-chain
surface live.

## Import cycles

A cycle **compiles**. It fails at runtime, sometimes, as `undefined is not a
constructor` or a class that is `undefined` at the moment a subclass extends it.
Which module loses depends on which one the bundler reached first, so the same
code works in dev and breaks in the production build, or works on one developer's
machine and not another's.

Three ways out, in order of preference:

1. **Extract the shared thing.** If A and B both need `Order`, `Order` belongs in
   a third module both import. This is the fix nine times out of ten, and the
   shared thing is usually a type or a constant.
2. **Invert one direction.** The low-level module should not know about the
   high-level one. Pass a callback, emit an event, or take the dependency as a
   parameter.
3. **Make one edge type-only.** `import type` is erased at compile time, so a
   cycle that exists only in types is not a runtime cycle at all. This is a
   legitimate fix, not a workaround — but only when the import really is types.

Do not "fix" a cycle by moving an import inside a function. That defers the
failure to a moment further from its cause.

## Barrel files

An `index.ts` that re-exports a folder feels tidy and costs more than it looks:

- **It creates cycles.** A file inside the folder that imports from the barrel
  imports itself, transitively. This is the most common accidental cycle there is.
- **It loads everything.** Importing one symbol pulls the whole folder's module
  bodies, including their imports. In Jest/Vitest this shows up as a test file
  that mysteriously needs a mock for something it never uses; in a bundler, as
  code that will not tree-shake.
- **`export *` hides ownership.** Nothing tells you which module a name comes
  from, and two modules exporting the same name collide silently.
- **It slows the compiler and the editor.** Every barrel is another indirection
  the language service resolves on every keystroke.

Barrels are defensible for the **published entry point of a package** — one file
that defines the public API, with explicit named re-exports, not `export *`.
Inside an application, import from the defining module.

## `import type`

With `verbatimModuleSyntax` (recommended), the distinction is explicit and
enforced. Without it, TypeScript elides imports it decides are type-only, which
surprises bundlers, decorator metadata, and anything with side effects.

```ts
import type { User } from "./types";        // erased entirely
import { formatUser } from "./format";      // kept
import { type User, formatUser } from "./x"; // mixed, also fine
```

Marking type imports also breaks type-only cycles for free.

## Side-effect imports

`import "./setup"` runs a module body for what it does, not what it exports. That
is fine for CSS, polyfills, and a package whose documented interface is exactly
this (`@testing-library/jest-dom`). It is a problem in your own code:

- The order relative to other imports is the bundler's choice, not yours.
- It cannot be tree-shaken.
- "Remove unused imports" deletes it, and something stops working in a way nobody
  connects to the change.

Export a function and call it from the place that owns startup.

## God modules and fan-out

A module with 25+ exports, or one that imports 15+ internal modules, is a hub:
every change to it rebuilds and retests everything, and every change to anything
rebuilds it. Look for a group of exports that never reference each other, or a
group of imports only one exported function uses — that group is a separate
module.

## Deep relative paths

`../../../shared/utils/format` breaks whenever either end moves and says nothing
about what it is importing. Add a tsconfig `paths` alias and import `@shared/format`.
Make sure the bundler/test runner resolves the alias too, or you have moved the
problem to runtime.

## package.json hygiene

**A package you import but do not declare** works today because something else
installed it and the package manager hoisted it. It breaks on a clean install, a
different package manager, a dependency bump, or a stricter node-linker (pnpm's
default). This is the highest-severity dependency finding for a reason.

**A package you declare but do not import** is still installed, still audited,
still in the container. Some are genuinely used without an import — a CLI, a lint
plugin resolved by name, a type package. Check before deleting, and note the ones
that are load-bearing so the next person does not re-litigate it.

**dependencies vs devDependencies.** Anything only tests or the build use belongs
in `devDependencies`: for a published library the distinction decides what
consumers install, and for an application it decides what ships in the image and
what appears in the vulnerability report.

**`@types/*` belongs in devDependencies** — they are erased at build time. The
exception is a published library whose *public types* reference them; then the
consumer needs them and they are real dependencies.

**Unpinned ranges** (`*`, `latest`) mean today's install and tomorrow's are
different software. Pin a range you have tested and let the lockfile do the rest.

**Exactly one lockfile.** Two means two dependency graphs depending on who ran
what, and CI will pick a different one than your laptop.

## What the tools add

The detector reconciles the manifest with the imports. It cannot know whether a
pinned version has a published advisory — that is `npm audit` / `pnpm audit`, via
`run_external_tools.py`. For unused files and exports across a real build graph,
`knip` is stronger than any static scan of import statements, because it
understands entry points; when the project has it, prefer its answer.
