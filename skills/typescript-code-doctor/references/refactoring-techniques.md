# Executing the fix

Behavior is sacred. Every technique below is a sequence of steps that each leave
the code compiling and the tests green, so you can stop after any of them.

## The loop

1. `tsc --noEmit` and the tests pass. If not, stop — you are debugging, not
   refactoring.
2. Make **one** change.
3. `tsc --noEmit` and the tests pass again.
4. Commit.

The compiler is doing more for you here than in most languages: rename, extract
and move are verifiable rather than hopeful. Use the IDE's refactorings (F2 for
rename, "Extract to function") for anything mechanical — they are exact, and your
regex is not.

## Extract function

*When:* a block needs a comment to say what it does; a function does two things;
a block appears twice.

1. Copy the block into a new function below.
2. Turn each free variable into a parameter; if more than three, take an options
   object or ask whether the boundary is in the right place.
3. Replace the original block with a call.
4. Check the compiler agrees on the return type; declare it explicitly.

## Replace conditional with discriminated union

*When:* `type_switch`, `all_optional_type`, or repeated `!` on the same field.
The highest-value refactor available in TypeScript, and worth doing in small
steps.

1. Write the union type beside the existing one. Do not delete anything yet.
2. Change **one** construction site to build the union value.
3. Change the consumer to `switch` on the discriminant with
   `default: assertNever(x)`. The compiler now lists every case you have not
   handled.
4. Migrate the remaining construction sites one at a time.
5. Delete the old type. Every `if (x.result)` guard it required disappears with
   it.

## Introduce parameter object

*When:* `data_clump`, `long_parameter_list`, `primitive_obsession`.

1. Declare the type.
2. Add an overload (or a new function) taking the object; implement the old
   signature in terms of it.
3. Migrate callers — the compiler finds them all.
4. Delete the old signature.

## Move method

*When:* `feature_envy`.

1. Copy the method to the target type; make its old receiver a parameter.
2. Turn the original into a one-line delegation.
3. Migrate callers, then delete the delegation.

## Encapsulate field

*When:* `public_mutable_field`.

1. Mark it `private` (or stop exporting it). The compiler lists every external
   user.
2. For each, decide: does the caller want *the field*, or an operation? Add the
   operation.
3. Delete the ones that turn out to be unnecessary.

## Replace assertion with validation

*When:* `as`, `as any`, `non_null_assertion` at a boundary.

1. Write a type guard or a schema for the shape.
2. Apply it immediately at the boundary; throw or return a typed failure.
3. Delete the assertion. Then delete every downstream assertion the compiler no
   longer needs — that cascade is the payoff.

## Replace inheritance with composition

*When:* `refused_bequest`, `deep_inheritance`.

1. Add a field on the subclass holding an instance of the former base.
2. Replace each inherited call with a delegation.
3. Remove `extends`. The compiler lists what is still missing.
4. Delete the delegations nobody uses.

## Split a module

*When:* `god_module`, `low_cohesion`, `high_module_fan_out`.

1. Group the exports by which other exports and imports they use.
2. Move one group to a new file; re-export it from the old one temporarily so no
   caller changes.
3. Migrate importers.
4. Delete the temporary re-export. Skipping step 4 leaves a barrel.

## Break an import cycle

1. Name the shared thing the cycle is really about.
2. Move it to a third module both sides import.
3. If it is only types, `import type` alone may be enough — the cycle vanishes at
   compile time.
4. Verify with `madge --circular` or `find_module_issues.py`.

## Guard clauses

*When:* `deep_nesting`.

```ts
// before
function f(u?: User) {
  if (u) {
    if (u.active) {
      return doWork(u);
    }
  }
  return null;
}
// after
function f(u?: User) {
  if (!u) return null;
  if (!u.active) return null;
  return doWork(u);
}
```

Each inverted condition is one level of nesting removed, and the narrowing means
`u` is `User` for the rest of the body.

## Rename

The cheapest high-value refactor and the most under-used. Use F2, not
find-and-replace: the compiler renames the symbol, not the string, so it will not
touch a different `id` in an unrelated file.

## When a refactor is too big to do safely

If a change cannot be made in behaviour-preserving steps, it is a rewrite. Say so.
Rewrites need a characterization test suite around the old behaviour first (see
`references/safety-net-and-testing.md`), a plan for running both, and an explicit
decision — not a refactor that quietly became one.
