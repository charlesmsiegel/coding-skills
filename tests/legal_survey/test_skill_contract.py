"""The judgment half has to stay reachable from the always-loaded body.

Everything the scripts enforce mechanically is tested elsewhere in this directory.
What is tested here is the seam between the prose and the scripts: a stage an agent
is never told to run does not happen, a guide nothing routes to is never loaded, and
a rule stated only in a reference cannot govern the step it is about. Prose is where
this skill does most of its work, so this is the file that fails when a rewrite drops
a load-bearing sentence.
"""

import dataclasses
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "legal-survey"
SCRIPTS = SKILL / "scripts"
REFERENCES = SKILL / "references"


def skill_text() -> str:
    return (SKILL / "SKILL.md").read_text(encoding="utf-8")


def prose() -> str:
    """SKILL.md with wrapping and emphasis removed, so a phrase check survives a reflow.

    Asserting on the raw text would make these tests fail on rewrapping or on a
    word gaining a code span, which teaches people to delete the assertion rather
    than restore the sentence.
    """
    return " ".join(skill_text().replace("**", "").replace("*", "")
                    .replace("`", "").split())


# --- the pipeline is documented in the order it has to run ---------------

def test_every_shipped_script_is_reachable_from_the_body():
    """A stage an agent is never told about is dead weight in the archive."""
    text = skill_text()
    runnable = {p.name for p in SCRIPTS.glob("*.py")} - {"common.py", "sources.py"}

    missing = sorted(name for name in runnable if name not in text)
    assert not missing, f"SKILL.md never names {missing}; those stages cannot be invoked"


def test_the_helper_modules_are_imported_rather_than_run():
    """common.py and sources.py have no CLI, so naming them as commands would misdirect."""
    text = skill_text()

    for helper in ("common.py", "sources.py"):
        assert "$SKILL/scripts/" + helper not in text


def test_the_runnable_commands_appear_in_pipeline_order():
    """Each stage consumes the previous stage's file, so the reading order is the run order.

    Positions of the *commands*, not of every mention: the gate is named in the
    opening paragraphs too, which is the one place it belongs out of order.
    """
    text = skill_text()
    order = ["search_sources.py", "fetch_authorities.py", "check_treatment.py",
             "snowball.py", "corpus_stats.py", "assemble.py", "inject_masthead.py",
             "verify_authorities.py"]

    positions = [text.index("$SKILL/scripts/" + name) for name in order]
    assert positions == sorted(positions), "the documented order is the order the files depend on"


def test_validation_is_documented_before_reading():
    """An overruled case reads exactly like a good one, so the order is load-bearing."""
    text = skill_text()

    assert text.index("$SKILL/scripts/check_treatment.py") < text.index("## 6. Read")


def test_every_reference_is_routed_to_from_the_body():
    text = skill_text()
    guides = {p.name for p in REFERENCES.glob("*.md")}

    missing = sorted(name for name in guides if "references/" + name not in text)
    assert not missing, f"nothing tells the agent to read {missing}"


def test_commands_are_written_to_run_from_the_users_project():
    """An installed skill is not the cwd; a bare `python scripts/x.py` cannot run."""
    assert not re.search(r"(?<![\w/\"$])python3? scripts/", skill_text())


# --- the two failures the skill exists to prevent ------------------------

def test_the_body_names_both_failures_it_exists_to_prevent():
    """Without them the skill reads as a downloader and the discipline looks like overhead."""
    text = prose()

    assert "fabricated citation" in text
    assert "Citing bad law" in text


def test_the_gate_is_described_as_blocking():
    text = prose()

    assert "verify_authorities.py" in text
    assert "blocking" in text.lower()
    assert "non-zero exit" in text
    assert "Fix the claim, not the check" in text


def test_unverifiable_is_documented_as_distinct_from_clean():
    """A green exit over checks that could not be performed is a laundered result."""
    text = prose()

    assert "Unverifiable is not clean" in text
    for case in ("page count cannot be determined", "binary artifact", "section name"):
        assert case in text, f"the gate's unverifiable cases do not mention {case}"


def test_the_treatment_check_is_never_presented_as_a_citator():
    """The single most consequential overclaim this skill could make."""
    body = prose()
    guide = (REFERENCES / "validating-authority.md").read_text(encoding="utf-8")

    assert "not a citator" in body
    assert "Shepard's" in body and "KeyCite" in body
    assert "not a citator" in guide


def test_an_uncited_authority_is_documented_as_unvalidated_not_clean():
    text = prose()

    assert "unchecked is not clean" in text.lower()
    assert "ignored, not approved" in text


def test_holdings_and_dicta_are_documented_as_different_types():
    body = prose()
    guide = (REFERENCES / "reading-an-authority.md").read_text(encoding="utf-8")

    assert "A holding is a Claim; dicta is a Dictum" in body
    assert "why_not_binding" in body
    assert "deletion test" in guide.lower() or "would the judgment change" in guide


def test_the_masthead_numbers_are_forbidden_to_be_written_by_hand():
    text = prose()

    assert "Never write one by hand" in text
    assert "corpus_stats.py" in text


def test_reading_from_a_headnote_is_forbidden_in_the_body_not_only_in_the_guide():
    """The reading guide is loaded by subagents; the prohibition has to bind the dispatcher."""
    text = prose()

    assert "headnote" in text
    assert "syllabus" in text
    assert "locator" in text


def test_readers_are_dispatched_without_each_others_notes():
    text = prose()

    assert "none gets another reader's notes" in text
    assert "serially" in text, "a harness with no subagents still has to be told what to do"


def test_a_dropped_authority_must_carry_its_reason_in_the_body_and_the_guide():
    body = prose()
    guide = (REFERENCES / "authority-triage.md").read_text(encoding="utf-8")

    assert "reason it was dropped" in body
    assert "drop" in guide.lower()


def test_a_selected_authority_must_carry_its_weight_in_the_body():
    """The dataclass enforces it, but an agent that meets the error mid-run has
    already paid for the corpus."""
    body = prose()

    assert "weight" in body and "weight_reason" in body
    assert "binding, persuasive or secondary" in body


def test_the_forum_and_the_as_of_date_are_settled_before_spending():
    text = prose()

    assert "The forum" in text
    assert "as-of date" in text
    assert "2019" in text, "the superseded-statute trap is carried as a concrete case"


def test_the_cap_is_never_allowed_to_read_as_saturation():
    text = prose()

    assert "if the cap stopped it, the report says so" in text
    assert "Silent truncation reads as coverage" in text


def test_the_jurisdiction_drift_example_is_carried_as_a_real_one():
    """The failure is abstract until it is 'consideration means two different things'."""
    for text in (skill_text(), (REFERENCES / "authority-triage.md").read_text(encoding="utf-8")):
        assert "consideration" in text


def test_the_acquisition_etiquette_is_stated_where_it_cannot_be_skipped():
    """These are promises to other people's servers, and the body is the only always-read file."""
    text = skill_text()

    for rule in ("robots.txt", "Paywalls", "Rate-limited", "concurrency", "Westlaw"):
        assert rule.lower() in text.lower(), f"the etiquette section does not mention {rule}"


def test_the_token_is_documented_as_scoped_to_one_host():
    text = prose()

    assert "COURTLISTENER_TOKEN is sent only to CourtListener" in text


def test_the_body_tells_the_agent_to_ask_for_a_token():
    """The user chose this behaviour explicitly; it has to survive a rewrite."""
    text = prose()

    assert "ask the user for one" in text
    assert "AskUserQuestion" in text


def test_fetched_text_is_declared_untrusted():
    """An opinion or an agency page may contain text shaped like an instruction."""
    text = prose()

    assert "untrusted input" in text
    assert "data, not direction" in text


def test_the_body_distinguishes_a_survey_from_a_question():
    text = prose()

    assert "survey" in text and "question" in text
    assert "counter-authority" in text.lower()


def test_the_report_is_labelled_research_rather_than_advice():
    body = prose()
    guide = (REFERENCES / "report-structure.md").read_text(encoding="utf-8")

    assert "not legal advice" in body
    assert "not legal advice" in guide.lower()


def test_the_non_us_source_landscape_is_read_before_searching():
    text = skill_text()

    assert text.index("references/source-landscape.md") < text.index("search_sources.py")


def test_every_flag_the_docs_use_is_a_flag_some_cli_accepts():
    """A documented flag no parser knows fails at the point of use -- mid-run, after
    the corpus has been paid for."""
    text = skill_text() + " ".join(p.read_text(encoding="utf-8")
                                   for p in REFERENCES.glob("*.md"))
    cited = set(re.findall(r"(--[a-z][a-z-]{2,})", text))
    accepted = " ".join(
        subprocess.run([sys.executable, str(script), "--help"],
                       capture_output=True, text=True, timeout=60).stdout
        for script in sorted(SCRIPTS.glob("*.py")))

    unknown = sorted(flag for flag in cited if flag not in accepted)
    assert not unknown, f"documented flags no CLI accepts: {unknown}"


# --- the note schema readers follow is the one the loader reads ----------

def documented_note(load_module) -> dict:
    """The worked example from the reading guide, parsed as a reader would copy it."""
    text = (REFERENCES / "reading-an-authority.md").read_text(encoding="utf-8")
    block = text.split("```json", 1)[1].split("```", 1)[0]
    return json.loads(block)


def test_the_worked_example_is_valid_json(load_module):
    """It is fenced as json and is the thing a reader copies; a syntax error there
    means the first note of every run starts broken."""
    assert documented_note(load_module)["artifact_id"]


def test_every_documented_note_field_is_a_field_the_loader_reads(load_module):
    """Note.from_dict ignores what it does not recognise, so a guide that drifts from
    the dataclass costs each reader a field that silently never reaches synthesis."""
    common = load_module(SCRIPTS, "common")
    known = {f.name for f in dataclasses.fields(common.Note)}

    assert set(documented_note(load_module)) - known == set()


def test_the_worked_example_loads_into_the_records_it_documents(load_module):
    common = load_module(SCRIPTS, "common")

    note = common.Note.from_dict(documented_note(load_module))

    assert note.claims and note.claims[0].locators, "a claim must carry its evidence"
    assert note.claims[0].basis in common.CLAIM_BASES
    assert note.dicta and note.dicta[0].why_not_binding, "a dictum must say why it does not bind"
    assert note.posture, "posture decides how far a holding travels"
    assert note.limits, "the field the reader is there to fill"


def test_the_documented_pincite_is_paired_with_a_real_locator(load_module):
    """A pincite alone does not resolve, and the example is what readers copy."""
    note = documented_note(load_module)
    locators = note["claims"][0]["locators"]

    assert any(loc.get("pincite") for loc in locators)
    assert all(loc.get("page") or loc.get("section") or loc.get("quote") for loc in locators)


# --- the evals cover the judgment the tests cannot reach -----------------

def test_the_evals_exercise_the_stages_pytest_cannot():
    payload = json.loads((ROOT / "evals" / "legal-survey" / "evals.json")
                         .read_text(encoding="utf-8"))
    blob = " ".join(case["prompt"] + " " + case["expected_output"]
                    for case in payload["evals"]).lower()

    for judgment in ("triage", "dicta", "treatment", "overruled", "headnote", "pincite",
                     "posture", "untrusted", "adversarial", "jurisdiction", "citator"):
        assert judgment in blob, f"no eval exercises {judgment}"


@pytest.mark.parametrize("guide", sorted(p.name for p in REFERENCES.glob("*.md")))
def test_every_guide_opens_with_a_heading(guide):
    text = (REFERENCES / guide).read_text(encoding="utf-8")

    assert text.startswith("# "), f"{guide} has no title, so a loaded guide has no subject line"
