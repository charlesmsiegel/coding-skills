"""SKILL.md is half the routing protocol, so it is tested like code.

route.py names a specialist; only the agent can load a skill. If SKILL.md
stops describing that hand-off, the router still exits zero and the specialists
silently never run — a partial review that reads exactly like a complete one.
"""

import json
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent.parent / "skills" / "code-doctor"


def skill_text() -> str:
    return (SKILL / "SKILL.md").read_text(encoding="utf-8")


def test_skill_md_is_no_longer_the_under_construction_stub():
    assert "Under construction" not in skill_text(), (
        "finish the code-doctor foundation plan's Task 9 before this one"
    )


def test_the_routing_section_names_both_scripts():
    text = skill_text()
    assert "route.py" in text
    assert "merge_reports.py" in text


def test_the_routing_section_names_every_specialist_route_py_can_emit():
    text = skill_text()
    for specialist in ("python-code-doctor", "django-code-doctor", "typescript-code-doctor"):
        assert specialist in text, f"{specialist} is routable but undocumented"


def test_the_protocol_says_the_agent_loads_the_skill():
    text = skill_text().lower()
    assert "load" in text and "route.py" in text, (
        "a script cannot invoke a skill; SKILL.md must tell the agent to do it"
    )


def test_the_evidence_rule_is_stated_where_someone_would_change_it():
    assert "manifest" in skill_text().lower()


def test_evals_cover_routing_and_the_empty_report_trap():
    payload = json.loads((SKILL.parent.parent / "evals" / "code-doctor" / "evals.json")
                         .read_text(encoding="utf-8"))
    prompts = " ".join(case["prompt"] + case["expected_output"] for case in payload["evals"])
    assert "route" in prompts.lower()
    assert "empty" in prompts.lower() or "failed" in prompts.lower()
