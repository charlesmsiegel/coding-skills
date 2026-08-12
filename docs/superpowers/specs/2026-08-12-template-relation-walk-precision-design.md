# Template Relation-Walk Precision and Accounts N+1 Fixes

## Goal

Make `django-code-doctor` report credible template-loop N+1 candidates instead of
every dotted template expression, and remove the five credible N+1 paths found on
the `tg` accounts profile page.

## Detector design

The detector remains a lightweight source scanner; it will not try to reconstruct
Django's complete runtime context. It will classify the shape of an expression
before emitting `relation_walk_in_loop`:

- Preserve model-looking relation chains such as
  `obj.owner.profile.get_absolute_url` and `journal.character.name`.
- Treat a loop variable ending in `_form` as a presentation wrapper. Strip the
  form variable and its first domain-object attribute before judging the remaining
  chain. Thus `freebie_form.character.owner.profile.get_absolute_url` still warns
  about `owner.profile`, while `freebie_form.character.name`, `.gameline`, and
  `.get_absolute_url` do not.
- Ignore known scalar/value transformations such as `type.title` and file-field
  value access such as `image.url`; neither is evidence of an ORM relation.
- Continue reporting explicit query accessors (`all`, `count`, `first`, `last`,
  `exists`) through the existing `query_in_template` rule.

Regression fixtures will reproduce the `accounts` expressions and require the
scanner to retain exactly the five credible candidates while suppressing the
obvious false positives. Existing generic positive cases must remain green.

## Application fixes

The `tg` fixes will eager-load the relations used by those five candidates:

- Add `owner__profile` to the shared pending-approval queryset used by character,
  item, and location approval lists.
- Add `owner__profile` to the freebie-character queryset built by
  `ProfileView.get_context_data()`.
- Add `character` to `Profile.get_updated_journals()` via `select_related`.

Focused database tests will evaluate representative rows and then access the same
attributes the templates use under a zero-additional-query assertion. This proves
the fix at the ORM boundary without coupling the tests to unrelated template
rendering costs. Where practical, a scaling assertion will compare one and two
rows so a per-row regression cannot hide behind a fixed query allowance.

## Verification and installation

1. Add detector tests and observe them fail against the current scanner.
2. Implement the smallest scanner refinement and run the focused and full
   `coding-skills` test suites.
3. Add `tg` query-count tests and observe them fail before eager-loading changes.
4. Implement the three eager-loading changes and run focused accounts/core tests.
5. Run the canonical detector against `tg` and verify that the former 30 findings
   contain no obvious false positives and that the five former candidates are no
   longer actionable N+1s in application code.
6. Run `coding-skills/install.sh --codex`, then verify the installed detector is
   byte-identical to the canonical source and reproduces the refined result.

## Boundaries

- Do not build a full Django template type-inference engine.
- Do not silence uncertain arbitrary chains globally.
- Do not alter unrelated health findings or regenerate overview HTML as part of
  this change.
- Preserve existing uncommitted overview HTML in `tg`.
