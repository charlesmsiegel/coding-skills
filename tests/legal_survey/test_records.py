"""The dataclasses carry the skill's confidence discipline, so they are tested first.

Every invariant here exists because the alternative is a report that reads as
finished while asserting something nobody decided: an authority nobody weighed, a
gap nobody explained, dicta filed as a holding, a regulation with no version.
"""

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "legal-survey" / "scripts"


@pytest.fixture
def common(load_module):
    return load_module(SCRIPTS, "common")


# --- Authority: why it is absent, and what it is worth -------------------

def test_a_dropped_authority_must_say_why(common):
    with pytest.raises(ValueError, match="drop_reason"):
        common.Authority(title="Doe v. Agency", status="dropped")


def test_a_kept_authority_may_not_carry_a_drop_reason(common):
    with pytest.raises(ValueError, match="only meaningful on a dropped"):
        common.Authority(title="Doe v. Agency", status="new", drop_reason="stale")


def test_a_selected_authority_must_carry_a_weight(common):
    """Whether it binds the forum is the difference between an argument and a list."""
    with pytest.raises(ValueError, match="must carry a weight"):
        common.Authority(title="Doe v. Agency", status="selected")


def test_a_weight_must_carry_its_reason(common):
    with pytest.raises(ValueError, match="weight must carry its reason"):
        common.Authority(title="Doe v. Agency", status="selected", weight="binding")


def test_a_fully_triaged_authority_is_accepted(common):
    authority = common.Authority(
        title="Doe v. Agency", status="selected", weight="binding",
        weight_reason="9th Cir. published opinion; forum is N.D. Cal.",
    )

    assert authority.weight == "binding"


def test_an_unknown_weight_is_rejected(common):
    with pytest.raises(ValueError, match="weight must be one of"):
        common.Authority(title="x", status="selected", weight="controlling",
                         weight_reason="because")


def test_an_unknown_authority_type_is_rejected(common):
    with pytest.raises(ValueError, match="authority_type must be one of"):
        common.Authority(title="x", authority_type="caselaw")


# --- ManifestEntry: a gap is never also a file --------------------------

def test_a_gap_must_carry_its_reason(common):
    with pytest.raises(ValueError, match="failure_reason"):
        common.ManifestEntry(artifact_id="a", kind="case", url="u", status="paywalled")


def test_a_gap_may_not_claim_a_file_on_disk(common):
    with pytest.raises(ValueError, match="must not claim a path"):
        common.ManifestEntry(artifact_id="a", kind="case", url="u", status="failed",
                             failure_reason="timeout", path="docs/case/a.html")


def test_an_ok_entry_must_be_hashed(common):
    with pytest.raises(ValueError, match="sha256"):
        common.ManifestEntry(artifact_id="a", kind="case", url="u", status="ok",
                             path="docs/case/a.html")


def test_an_archived_regulation_must_carry_a_version_date(common):
    """A code section with no version is a citation to a text nobody can retrieve."""
    with pytest.raises(ValueError, match="as_of"):
        common.ManifestEntry(artifact_id="a", kind="regulation", url="u", status="ok",
                             path="docs/regulation/a.xml", sha256="deadbeef")


def test_an_archived_case_needs_no_version_date(common):
    """A decided case is not a snapshot; requiring one would be noise, not rigor."""
    entry = common.ManifestEntry(artifact_id="a", kind="case", url="u", status="ok",
                                 path="docs/case/a.html", sha256="deadbeef")

    assert entry.as_of == ""


@pytest.mark.parametrize("kind", ("statute", "regulation", "constitution", "treaty"))
def test_every_dated_kind_demands_an_as_of(common, kind):
    with pytest.raises(ValueError, match="as_of"):
        common.ManifestEntry(artifact_id="a", kind=kind, url="u", status="ok",
                             path="p", sha256="h")


# --- Locator: a pincite is not evidence ---------------------------------

def test_a_locator_needs_something_resolvable(common):
    with pytest.raises(ValueError, match="page, a section or a quote"):
        common.Locator(artifact_id="410us113")


def test_a_pincite_alone_does_not_satisfy_the_locator(common):
    """The easiest part of a citation to fabricate must not be the one that passes."""
    with pytest.raises(ValueError, match="easiest part of a citation to fabricate"):
        common.Locator(artifact_id="410us113", pincite="at 118")


def test_a_pincite_rides_along_with_a_real_locator(common):
    locator = common.Locator(artifact_id="410us113", page=7, pincite="at 118")

    assert locator.pincite == "at 118"


# --- Claim and Dictum: holdings and asides are different types ----------

def test_a_claim_must_carry_evidence(common):
    with pytest.raises(ValueError, match="fabricated-citation"):
        common.Claim(text="drafts are exempt", basis="holding")


def test_dicta_is_not_a_basis_for_a_claim(common):
    """The error has to name the other type, or the reader just picks another string."""
    with pytest.raises(ValueError, match="record it as a Dictum"):
        common.Claim(text="might well waive", basis="dictum",
                     locators=[common.Locator(artifact_id="a", page=1)])


def test_an_unknown_basis_is_rejected(common):
    with pytest.raises(ValueError, match="basis must be one of"):
        common.Claim(text="x", basis="vibes",
                     locators=[common.Locator(artifact_id="a", page=1)])


def test_a_dictum_must_say_what_makes_it_non_binding(common):
    with pytest.raises(ValueError, match="why_not_binding"):
        common.Dictum(text="might well waive")


def test_a_dictum_may_not_carry_locators(common):
    """Locators would dress non-binding language as established law."""
    with pytest.raises(ValueError, match="must not carry locators"):
        common.Dictum(text="might well waive", why_not_binding=["not decided on these facts"],
                      locators=[common.Locator(artifact_id="a", page=1)])


def test_a_claim_round_trips_through_its_dict(common):
    claim = common.Claim(text="exempt", basis="statutory_text", treatment_ack="",
                         locators=[common.Locator(artifact_id="a", page=3, pincite="at 118")])

    restored = common.Claim.from_dict(claim.to_dict())

    assert restored.basis == "statutory_text"
    assert restored.locators[0].pincite == "at 118"


# --- Treatment: a flag nobody can check is not a flag -------------------

def test_a_negative_treatment_must_name_its_signals(common):
    with pytest.raises(ValueError, match="must name the signals"):
        common.Treatment(artifact_id="a", status="negative")


def test_an_unchecked_treatment_must_say_why(common):
    """'Nothing cited it' and 'the graph was down' are different facts."""
    with pytest.raises(ValueError, match="must say why"):
        common.Treatment(artifact_id="a", status="unchecked")


def test_unchecked_is_not_clean(common):
    record = common.Treatment(artifact_id="a", status="unchecked",
                              reason="nothing has cited it")

    assert not record.is_clean


def test_good_and_not_applicable_are_clean(common):
    assert common.Treatment(artifact_id="a", status="good").is_clean
    assert common.Treatment(artifact_id="a", status="not_applicable").is_clean


# --- citation identity --------------------------------------------------

@pytest.mark.parametrize("rendering", ("410 U.S. 113", "410 US 113", "410 U. S. 113"))
def test_one_citation_normalizes_to_one_key(common, rendering):
    assert common.normalize_citation(rendering) == common.normalize_citation("410 U.S. 113")


def test_parse_citation_splits_volume_reporter_and_page(common):
    assert common.parse_citation("532 F.3d 1099") == ("532", "F.3d", "1099")


def test_parse_citation_returns_none_for_prose(common):
    assert common.parse_citation("a case about drafts") is None


def test_the_artifact_id_prefers_the_reporter_citation(common):
    authority = common.Authority(title="Doe v. Agency", citation="410 U.S. 113",
                                 external_ids={"cl": "12345"})

    assert common.artifact_id(authority) == "410us113"


def test_the_artifact_id_never_contains_a_separator(common):
    """It names docs/notes/<id>.json; a slash silently becomes an unreadable directory."""
    authority = common.Authority(title="x", authority_type="secondary",
                                 external_ids={"doi": "10.2307/1073947"})

    assert "/" not in common.artifact_id(authority)


def test_the_artifact_id_is_stable_without_any_identifier(common):
    authority = common.Authority(title="An agency guidance page", authority_type="secondary")

    assert common.artifact_id(authority) == common.artifact_id(authority)


def test_slugify_does_not_fuse_numbers_across_a_dash(common):
    assert common.slugify("1985–2010 amendments") == "1985-2010-amendments"


def test_two_cfr_subsections_do_not_collide_on_one_artifact_id(common):
    """Parsing a code cite as volume-reporter-page drops the subsection, so
    40 CFR 60.1 and 40 CFR 60.2 became one id -- one file, one note, two
    different regulations."""
    a = common.Authority(title="a", authority_type="regulation", citation="40 CFR 60.1")
    b = common.Authority(title="b", authority_type="regulation", citation="40 CFR 60.2")

    assert common.artifact_id(a) != common.artifact_id(b)


def test_a_reporter_cite_with_a_parenthetical_keys_the_same_as_one_without(common):
    """The court-and-year parenthetical is bibliography, not identity, and two
    sources will disagree about whether to include it."""
    bare = common.Authority(title="Doe", citation="901 F.3d 1099")
    full = common.Authority(title="Doe", citation="901 F.3d 1099 (9th Cir. 2019)")

    assert common.artifact_id(bare) == common.artifact_id(full)


def test_one_code_section_written_two_ways_keys_the_same(common):
    plain = common.Authority(title="a", authority_type="regulation", citation="40 CFR 60.1")
    sectioned = common.Authority(title="a", authority_type="regulation",
                                 citation="40 CFR § 60.1")

    assert common.artifact_id(plain) == common.artifact_id(sectioned)
