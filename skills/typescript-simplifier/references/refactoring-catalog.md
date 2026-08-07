# The classic smell catalog, in TypeScript

Fowler's catalog, with the TypeScript-specific spelling of each smell, what the
detector calls it, and the fix that actually applies in a structurally-typed
language with a type checker.

## Bloaters

**Long function.** `long_function`, `high_cognitive_complexity`. The seams are
usually already marked by blank lines and comment headers — those are the function
names. Extract until each level of the function is at one level of abstraction.

**Long parameter list.** `long_parameter_list`. Three or more arguments and the
call site stops being readable. Bundle into an options object with a named type:
order stops mattering, the compiler checks the names, and adding a field does not
touch every caller.

**Primitive obsession.** `primitive_obsession`. Adjacent parameters of the same
primitive type are swappable and nothing notices — `move(userId, orgId)` compiles
with the arguments reversed. Fixes, in ascending cost: an options object (names
are checked), a branded type (`type UserId = string & { readonly __brand: unique symbol }`),
or a small value object where behaviour lives with the data.

**Data clump.** `data_clump`. The same three parameters travelling together
through five functions are one concept without a name. Naming it also gives the
next function that needs the group a type to accept.

**God class / god module.** `god_class`, `god_module`. Look at which fields each
method touches (that is what LCOM measures); the groups are the classes.

## Object-orientation abusers

**Type switch.** `type_switch`. An `if/else if` ladder on `typeof x` or
`x.kind === "…"` is the single most TypeScript-specific smell in the catalog,
because the language has a purpose-built answer: a discriminated union plus an
exhaustive `switch` with `default: assertNever(x)`. That fix does not just tidy
the code — it makes the compiler point at every site that needs updating when a
new case appears. See `references/type-system.md`.

**Refused bequest.** `refused_bequest`. A subclass overriding a method only to
throw is saying it is not really a subtype. Use composition: hold an instance,
expose the operations that do apply.

**Temporary field.** `temporary_field`. A field meaningful only during one call
makes every other method's contract depend on call order. Pass it as a parameter,
or extract the method object that owns it.

**Alternative classes with different interfaces.** Two types doing the same job
with different method names. In TypeScript, unify the shape and structural typing
does the rest — you rarely need a shared interface, only shared names.

**Switch on a boolean flag parameter.** `boolean_flag_parameter`. `f(x, true)`
tells a reader nothing. Split the function, or take `{ force: true }`, or a union
of string literals.

## Change preventers

**Divergent change.** One module edited for unrelated reasons. Its exports are
two modules that share a file.

**Shotgun surgery.** One conceptual change touching many files. Usually a data
clump or a leaked internal — the concept has no home, so every consumer
re-implements it.

**Parallel inheritance hierarchies.** Adding a subclass here forces one there.
Collapse one side into data.

## Dispensables

**Dead code.** `unused_export`, `unreachable_code`, `unused_private_member`,
`unreferenced_module`. Delete it. Git remembers, and a `.d.ts` or a dynamic import
is the only reason to hesitate — check, then delete.

**Speculative generality.** `single_implementation_interface`,
`abstract_class_with_one_subclass`, `single_use_type_parameter`. See
`references/overengineering-and-abstraction.md`.

**Data class.** `data_class`. A class with fields and no behaviour is an
`interface` or a `type` — no constructor, no instantiation, structurally typed.
Keep the class only when it has invariants to defend.

**Comments that restate the code.** `jsdoc_repeats_types`. In TypeScript, JSDoc
`@param {string}` is a second, unchecked copy of the signature. Delete the braces,
keep the prose.

**Duplicate code.** `duplicate_block`, `duplicate_type_shape`. Extract only when
the copies encode the same *decision*. See the DRY section of the
over-engineering guide.

## Couplers

**Feature envy.** `feature_envy`. A method that touches another object more than
its own belongs to that object. That is the whole criterion.

**Inappropriate intimacy.** `reaches_into_private`. Two types reaching into each
other's internals. Either move the behaviour, or make the member part of the API.

**Message chains.** `message_chain`. `a.b.c.d()` couples the caller to every type
on the path. Ask the first object for what you want. (Walking a plain data shape
you own is not this smell.)

**Middle man.** `middle_man`, `pass_through_module`. A class or module that only
forwards. Delete it, or give it a reason: translation, an invariant, a narrowed
surface, an anti-corruption layer around something you do not control.

## TypeScript-specific additions to the catalog

These have no classic name because the classic catalog predates the language.

**Type erosion.** `any` / `as` / `!` at a boundary, spreading outward. The
symptom is a function three layers in that has to re-check something the boundary
already knew.

**Stringly-typed state.** A `status: string` field compared against literals
across many files. The union already exists in everyone's head; declare it.

**Optional soup.** `all_optional_type`. An interface where everything is optional
permits `{}`, so it constrains nothing and every consumer writes guards. The legal
combinations are a discriminated union.

**Unchecked index.** `arr[0]`, `map.get(k)` and `Object.entries(o)[i]` treated as
definitely present. `noUncheckedIndexedAccess` makes this a compile error; without
it, it is a code review item on every indexed read.

**Floating promise.** `floating_promise`. No classic equivalent, and the most
common real defect in modern TypeScript. See
`references/async-and-concurrency.md`.
