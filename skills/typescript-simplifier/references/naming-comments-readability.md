# Names, comments and the shape of a function

## Names

The conventions TypeScript codebases converge on, and why each one is not
arbitrary:

| Kind | Convention | Why |
|---|---|---|
| Types, interfaces, enums, classes, components | `PascalCase` | The case is how a reader tells a type from a value with no other context |
| Values, functions, methods, properties | `camelCase` | |
| Module-level true constants | `SCREAMING_SNAKE` | Only for genuinely fixed values, not every `const` |
| Booleans | `isX`, `hasX`, `shouldX`, `canX` | A bare noun leaves `true` ambiguous |
| Type parameters | `T`, `TKey`, or a real word | A descriptive name beats `T2` |
| Files | one convention per repo | `PascalCase.tsx` for components is common; consistency matters more |

**Do not prefix interfaces with `I`.** TypeScript's own libraries and the DOM
typings do not, the prefix stops meaning anything the moment the interface becomes
a type alias, and it encodes the implementation of the type into its name.

**Do not use `_` to mean private.** `private` and `#` are enforced;
a naming convention is not. A `_` prefix on a public member is the worst of both:
it looks protected and is not.

**Do not shadow browser globals.** `name`, `length`, `top`, `self`, `parent`,
`origin`, `closed`, `opener` all exist on `window`, and a local of the same name
silently changes what a bare reference means. (`location`, `history` and `status`
are shadowed so routinely — `const location = useLocation()` — that flagging them
costs more than it saves.)

### The naming questions

1. **Does the name say what, or how?** `getUserById` names the how (the lookup);
   `user` names the what. Prefer what, except where the how is the point.
2. **Does it need the type in the name?** `userArray`, `nameString`, `optionsObj`
   — the type is right there, checked. Delete the suffix.
3. **Would you have to read the body to know what it returns?** Then the name is
   underspecified — or the function does two things.
4. **Does it lie?** `getUser` that also writes a cache entry, `validate` that
   throws, `isReady` that starts something. A name that lies is worse than no
   name, because a reader trusts it and skips the body.
5. **Is it consistent with its neighbours?** `fetchUser` / `getOrder` /
   `loadItems` in one module makes a reader wonder what the difference is. If
   there is none, use one verb.
6. **Is the abbreviation universal?** `id`, `url`, `db`, `req` are. `usr`, `cfgMgr`
   and `tmpVal` are not.

## Comments

The test: **would the code be worse without this comment?**

**Delete:**
- Comments that restate the next line (`// increment i`).
- JSDoc `@param {string} name` in a TypeScript file — a second, unchecked copy of
  the signature that will drift. Keep the prose, drop the braces.
- Commented-out code. Git has it; the comment does not get updated with the code
  around it, so it becomes misleading rather than useful.
- Changelog comments (`// modified by X on 2021-…`) — that is `git blame`.
- Section banners inside a 300-line function. Those are the function names you
  have not extracted yet.

**Keep and write more of:**
- **Why**, when the code cannot say it: why this order, why this workaround, why
  this constant is 300 and not 30.
- **Why not**, when an obvious better approach was tried and failed. This is the
  highest-value comment in any codebase, because it stops the next person
  repeating the experiment.
- **Links** to the issue, RFC, spec section or vendor bug that explains a
  non-obvious constraint.
- **Warnings**: "this runs on every keystroke", "callers must hold the lock",
  "the API returns 200 with an error body".
- **The reason for a suppression.** `@ts-expect-error` and `eslint-disable`
  without an explanation are unreviewable.

**A comment that lies is a bug.** When the code and the comment disagree, the
comment is the one that has to change, and a review that finds one should treat
it as a finding.

## Function shape

- **One level of abstraction per function.** Reading it should not alternate
  between business rules and byte manipulation.
- **Guard clauses at the top.** Handle the absent, invalid and trivial cases and
  return; the rest of the body then runs at one indent with narrowed types.
- **Return early rather than accumulate a result variable**, unless you genuinely
  need the accumulation.
- **The happy path is the last, longest and least indented thing.**
- **A parameter you always pass the same value for is not a parameter.**
- **More than three parameters** → options object, and the call site starts
  reading like documentation.

## Readability items the detectors flag

| Finding | The judgment |
|---|---|
| `boolean_without_predicate_name` | Does `true` mean the obvious thing? If it needs a comment, rename it. |
| `hungarian_interface_prefix` | Drop `I`. |
| `underscore_without_private` | Use the modifier. |
| `shadows_browser_global` | Rename, or accept it with a comment if it is idiomatic in your framework. |
| `todo_marker` | Ticket or delete. A TODO with no owner is a decision deferred forever. |
| `commented_out_code` | Delete. |
| `jsdoc_repeats_types` | Keep the sentence, drop the `{type}`. |
| `default_export_name_mismatch` | Match the file to the export, or use a named export. |
| `magic_number` | Does the value need a *reason*, or just a name? Sometimes `2` is 2. |
