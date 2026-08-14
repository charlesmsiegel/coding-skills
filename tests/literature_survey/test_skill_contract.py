"""The judgment half has to stay reachable from the always-loaded body.

Everything the scripts enforce mechanically is tested elsewhere in this
directory. What is tested here is the seam between the prose and the scripts:
a stage an agent is never told to run does not happen, a guide nothing routes to
is never loaded, and a rule stated only in a reference cannot govern the step it
is about. Prose is where this skill does most of its work, so this is the file
that fails when a rewrite drops a load-bearing sentence.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "literature-survey"
SCRIPTS = SKILL / "scripts"
REFERENCES = SKILL / "references"


def skill_text() -> str:
    return (SKILL / "SKILL.md").read_text(encoding="utf-8")


def prose() -> str:
    """SKILL.md with wrapping and emphasis removed, so a phrase check survives a reflow.

    Asserting on the raw text would make these tests fail on `fmt`-style rewrapping,
    which teaches people to delete the assertion rather than restore the sentence.
    """
    return " ".join(skill_text().replace("**", "").replace("*", "").split())


# --- the pipeline is documented in the order it has to run ---------------

def test_every_shipped_script_is_reachable_from_the_body():
    """A detector an agent is never told about is dead weight in the archive."""
    text = skill_text()
    runnable = {p.name for p in SCRIPTS.glob("*.py")} - {"common.py", "sources.py"}

    missing = sorted(name for name in runnable if name not in text)
    assert not missing, f"SKILL.md never names {missing}; those stages cannot be invoked"


def test_the_helper_modules_are_imported_rather_than_run():
    """common.py and sources.py have no CLI, so naming them as commands would misdirect."""
    text = skill_text()

    for helper in ("common.py", "sources.py"):
        assert '$SKILL/scripts/' + helper not in text


def test_the_runnable_commands_appear_in_pipeline_order():
    """Each stage consumes the previous stage's file, so the reading order is the run order.

    Positions of the *commands*, not of every mention: the gate is named in the
    opening paragraph too, which is the one place it belongs out of order.
    """
    text = skill_text()
    order = ["search_sources.py", "fetch_artifacts.py", "snowball.py", "corpus_stats.py",
             "assemble.py", "inject_masthead.py", "verify_locators.py"]

    positions = [text.index("$SKILL/scripts/" + name) for name in order]
    assert positions == sorted(positions), "the documented order is the order the files depend on"


def test_every_reference_is_routed_to_from_the_body():
    text = skill_text()
    guides = {p.name for p in REFERENCES.glob("*.md")}

    missing = sorted(name for name in guides if "references/" + name not in text)
    assert not missing, f"nothing tells the agent to read {missing}"


def test_commands_are_written_to_run_from_the_users_project():
    """An installed skill is not the cwd; a bare `python scripts/x.py` cannot run as written."""
    assert not re.search(r"(?<![\w/\"$])python3? scripts/", skill_text())


# --- the rules that only exist as prose ----------------------------------

def test_the_gate_is_described_as_blocking():
    text = prose()

    assert "verify_locators.py" in text
    assert "blocking" in text.lower()
    assert "non-zero exit" in text
    assert "Fix the claim, not the check" in text


def test_unverifiable_is_documented_as_distinct_from_clean():
    """A green exit code over checks that could not be performed is the shape of a
    laundered result, so the body has to say the caveats are part of the verdict."""
    text = prose()

    assert "Unverifiable is not clean" in text
    for case in ("page count cannot be determined", "binary artifact", "section name"):
        assert case in text, f"the gate's unverifiable cases do not mention {case}"


def test_the_masthead_numbers_are_forbidden_to_be_written_by_hand():
    text = prose()

    assert "Never write one by hand" in text
    assert "corpus_stats.py" in text


def test_the_body_states_the_failure_the_skill_exists_to_prevent():
    """Without it the skill reads as a downloader, and the discipline looks like overhead."""
    assert "fluent synthesis over unread abstracts" in prose()


def test_reading_from_the_abstract_is_forbidden_in_the_body_not_only_in_the_guide():
    """The reading guide is loaded by subagents; the prohibition has to bind the dispatcher too."""
    text = prose()

    assert "Never summarize from the abstract" in text
    assert "locator" in text


def test_readers_are_dispatched_without_each_others_notes():
    text = prose()

    assert "none gets another reader's notes" in text
    assert "serially" in text, "a harness with no subagents still has to be told what to do"


def test_a_dropped_candidate_must_carry_its_reason_in_the_body_and_the_guide():
    body = prose()
    guide = (REFERENCES / "triage-and-selection.md").read_text(encoding="utf-8")

    assert "dropped" in body and "reason it was dropped" in body
    assert "drop" in guide.lower()


def test_the_cap_is_never_allowed_to_read_as_saturation():
    text = prose()

    assert "if the cap stopped it, the report says so" in text
    assert "Silent truncation reads as coverage" in text


def test_the_off_field_homonym_is_carried_as_a_real_example():
    """The failure is abstract until it is 'arXiv returned software transactional memory'."""
    for text in (skill_text(), (REFERENCES / "triage-and-selection.md").read_text(encoding="utf-8")):
        assert "transactional memory" in text


def test_the_acquisition_etiquette_is_stated_where_it_cannot_be_skipped():
    """These are promises to other people's servers, and the body is the only always-read file."""
    text = skill_text()

    for rule in ("robots.txt", "Paywalls", "rate-limited", "concurrency"):
        assert rule.lower() in text.lower(), f"the etiquette section does not mention {rule}"


def test_fetched_text_is_declared_untrusted():
    """A paper or README may contain text shaped like an instruction; it is data, not direction."""
    text = prose()

    assert "untrusted input" in text
    assert "data, not direction" in text


def test_the_body_distinguishes_a_survey_from_a_question():
    """The two shapes produce different reports, and the classification happens before spending."""
    text = prose()

    assert "survey" in text and "question" in text
    assert "counter-evidence" in text


def test_the_non_cs_source_landscape_is_read_before_searching():
    text = skill_text()

    assert text.index("references/source-landscape.md") < text.index("search_sources.py")


# --- the evals cover the judgment the tests cannot reach -----------------

def test_the_evals_exercise_the_stages_pytest_cannot():
    payload = json.loads((ROOT / "evals" / "literature-survey" / "evals.json")
                         .read_text(encoding="utf-8"))
    blob = " ".join(case["prompt"] + " " + case["expected_output"]
                    for case in payload["evals"]).lower()

    for judgment in ("triage", "abstract", "adversarial", "construct", "untrusted"):
        assert judgment in blob, f"no eval exercises {judgment}"


@pytest.mark.parametrize("guide", sorted(p.name for p in REFERENCES.glob("*.md")))
def test_every_guide_opens_with_a_heading(guide):
    text = (REFERENCES / guide).read_text(encoding="utf-8")

    assert text.startswith("# "), f"{guide} has no title, so a loaded guide has no subject line"
