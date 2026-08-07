---
name: code-doctor
description: Under construction — not yet ready for use. This skill will review any codebase for quality problems and bugs without a parser or language tables, but its detectors are still being built. Do not invoke it yet. For Python use python-code-doctor, for TypeScript use typescript-code-doctor, for Django use django-code-doctor.
---

# Code Doctor (under construction)

**This skill is incomplete and should not be invoked.** It is being built task by
task; see `docs/superpowers/plans/2026-08-07-code-doctor-foundation.md`.

Its finished form is a language-agnostic reviewer that measures code quality and
bugs — no parsers, no comment-syntax tables, no framework knowledge — separating
defects it can prove from unverified leads it cannot. Until the detectors land,
use `python-code-doctor`, `typescript-code-doctor`, or `django-code-doctor`.

Task 9 of the plan replaces this file with the real router.
