"""SKILL.md's "Routing to the specialists" section is half the routing
protocol, so its substance — not just its vocabulary — is tested like code.

route.py names a specialist; only the agent can load a skill. A test that
merely checks whether a handful of familiar tokens appear anywhere in
SKILL.md passes on a stub paragraph that mentions the same words, and it also
passes if the section itself is deleted and the words happen to survive
elsewhere in the file (the pre-existing "Defer when a specialist exists"
table already names every specialist and both scripts). Every assertion here
is therefore scoped to the section's own text, and pinned to a distinction
the protocol exists to preserve — not a word count:

- an empty or unreadable report means the doctor *failed*, not that it found
  nothing;
- a bare JSON list grants no coverage, because nothing in that shape tells
  a full run from one detector that found nothing apart;
- the merge attributes every record but never deduplicates, because
  collapsing two reports of one defect changes a count a grader divides by;
- `merge_reports.py` exits 1 when every report failed — that's a merge with
  no input, not a clean repository;
- a route comes from a manifest the repository wrote about itself, never a
  filename census.

The documented flags are checked against the scripts' real `--help` output,
not against the prose describing them, so the CLI cannot drift from the docs
unnoticed.
"""

import re
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"
ROUTE = SKILL / "scripts" / "route.py"
MERGE = SKILL / "scripts" / "merge_reports.py"

SECTION_HEADING = "## Routing to the specialists"


def skill_text() -> str:
    return (SKILL / "SKILL.md").read_text(encoding="utf-8")


def routing_section(text: str | None = None) -> str:
    """The 'Routing to the specialists' section alone, heading to heading.

    Scoped from its own '##' heading up to (not including) the next '##'
    heading, or end of file. A mention anywhere else in SKILL.md — the defer
    table, the workflow steps — cannot satisfy an assertion meant to pin this
    section's own content. Fails loudly if the heading is gone rather than
    letting a `find`-based lookup fall through to matching nothing.
    """
    document = text if text is not None else skill_text()
    start = document.find(SECTION_HEADING)
    assert start != -1, f"{SECTION_HEADING!r} heading is missing from SKILL.md"
    after_heading = start + len(SECTION_HEADING)
    next_heading = re.search(r"^## ", document[after_heading:], re.MULTILINE)
    end = after_heading + (next_heading.start() if next_heading else len(document) - after_heading)
    return document[start:end]


def test_the_section_heading_exists_and_is_a_strict_subset_of_the_file():
    section = routing_section()
    assert 0 < len(section) < len(skill_text())


def test_the_section_names_both_scripts():
    section = routing_section()
    assert "route.py" in section
    assert "merge_reports.py" in section


def test_the_section_names_every_specialist_route_py_can_emit():
    section = routing_section()
    for specialist in ("python-code-doctor", "django-code-doctor", "typescript-code-doctor"):
        assert specialist in section, f"{specialist} is routable but undocumented"


def test_the_section_tells_the_agent_it_must_load_the_routed_skill():
    section = routing_section().lower()
    assert "load" in section and "route.py" in section, (
        "a script cannot invoke a skill; this section must tell the agent to do it"
    )


def test_the_section_states_the_manifest_evidence_rule_by_contrast():
    section = routing_section().lower()
    assert "manifest" in section
    # The rule only means anything stated against the alternative it rules out.
    assert "filename" in section or "file name" in section


def test_the_section_distinguishes_a_failed_report_from_an_empty_one():
    section = routing_section().lower()
    assert "empty" in section
    assert "fail" in section
    assert "found nothing" in section, (
        "the failed-vs-found-nothing contrast must be stated, not just 'empty'"
    )


def test_the_section_states_a_bare_list_grants_no_coverage():
    section = routing_section().lower()
    assert "coverage_unknown" in section
    assert "bare" in section and "list" in section


def test_the_section_states_attribution_without_deduplication_and_why():
    section = routing_section().lower()
    assert "does not deduplicate" in section or "no deduplicat" in section
    # The reason has to be present too, or this reads as an arbitrary rule:
    # collapsing two reports changes a count something downstream divides by.
    assert "count" in section


def test_the_section_states_merge_reports_exits_1_on_total_failure():
    section = routing_section()
    assert "exits 1" in section or "exit 1" in section


def test_the_documented_route_flags_exist_on_the_real_parser(run_script):
    result = run_script(ROUTE, "--help")
    help_text = result.stdout
    for flag in ("--format",):
        assert flag in help_text, f"SKILL.md documents route.py {flag!r}; the real CLI lacks it"


def test_the_documented_merge_flags_exist_on_the_real_parser(run_script):
    result = run_script(MERGE, "--help")
    help_text = result.stdout
    for flag in ("--report", "--out", "--format"):
        assert flag in help_text, (
            f"SKILL.md documents merge_reports.py {flag!r}; the real CLI lacks it"
        )


def test_evals_cover_routing_and_the_empty_report_trap():
    import json

    payload = json.loads((SKILL.parent.parent / "evals" / "code-doctor" / "evals.json")
                         .read_text(encoding="utf-8"))
    prompts = " ".join(case["prompt"] + case["expected_output"] for case in payload["evals"])
    assert "route" in prompts.lower()
    assert "empty" in prompts.lower() or "failed" in prompts.lower()
