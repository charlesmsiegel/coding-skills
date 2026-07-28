# Skill evals

One directory per skill, each holding an `evals.json` of realistic test prompts
(the schema skill-creator's tooling consumes: `skill_name`, `evals[]` with
`id` / `prompt` / `expected_output` / `files`). The deterministic halves of the
skills are covered by `tests/`; these prompts exercise the **judgment** halves —
the part pytest cannot reach — so a prose edit to a SKILL.md or reference can be
regression-checked by re-running the prompts with and without the change and
comparing outputs against `expected_output`.

To run one: give a fresh agent session the skill plus the prompt, then judge the
output against the eval's `expected_output` criteria (or drive it with
skill-creator's eval loop, which automates the with/without-skill comparison).

These live outside `skills/` on purpose: skill directories ship as-is when
zipped, and evals are development tooling, not skill content.
