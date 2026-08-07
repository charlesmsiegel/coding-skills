"""The scoring rubric — the arithmetic behind every letter grade.

These are the claims the documents make about themselves, so they are worth
pinning: the weights add up, the curve behaves, ungraded categories are dropped
rather than counted as zero or a hundred, and a finding from any of the three
doctors lands somewhere sensible.
"""

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "code-overview" / "scripts"


@pytest.fixture
def rubric(load_module):
    return load_module(SCRIPTS, "rubric")


def test_weights_total_one_hundred(rubric):
    assert sum(weight for _, _, weight, _ in rubric.CATEGORIES) == pytest.approx(100.0)


def test_every_category_key_is_unique_and_labelled(rubric):
    keys = [key for key, _, _, _ in rubric.CATEGORIES]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(rubric.CATEGORY_LABELS)


def test_a_category_scores_fifty_at_its_half_life(rubric):
    for _, _, _, half_life in rubric.CATEGORIES:
        assert rubric.score_from_density(half_life, half_life) == pytest.approx(50.0)


def test_the_curve_is_monotone_and_bounded(rubric):
    scores = [rubric.score_from_density(d, 6.0) for d in range(0, 200, 5)]
    assert scores[0] == 100.0
    assert all(0.0 <= s <= 100.0 for s in scores)
    assert all(a >= b for a, b in zip(scores, scores[1:])), "more findings never scores better"
    assert scores[-1] > 0.0, "the curve approaches zero without reaching it"


def test_small_units_are_graded_against_a_floor(rubric):
    """A 40-line file with one finding must not score 0 on the divisor alone."""
    weighted = rubric.severity_weight("medium")
    assert rubric.density(weighted, 40) == rubric.density(weighted, 1000)
    assert rubric.density(weighted, 4000) < rubric.density(weighted, 1000)


def test_grade_bands_are_ordered_and_cover_the_range(rubric):
    thresholds = [t for t, _ in rubric.GRADE_BANDS]
    assert thresholds == sorted(thresholds, reverse=True)
    assert rubric.grade_for(100.0) == "A+"
    assert rubric.grade_for(90.0) == "A-"
    assert rubric.grade_for(89.9) == "B+"
    assert rubric.grade_for(59.9) == "F"
    assert rubric.grade_for(0.0) == "F"
    assert rubric.grade_for(None) == rubric.UNGRADED


def test_ungraded_categories_are_dropped_not_counted(rubric):
    """None means 'not measured'. Zero and a hundred are both lies."""
    everything = {key: 80.0 for key in rubric.CATEGORY_KEYS}
    assert rubric.weighted_overall(everything) == pytest.approx(80.0)

    partial = dict(everything)
    partial["tests"] = None
    assert rubric.weighted_overall(partial) == pytest.approx(80.0), (
        "dropping a category and renormalizing must not move a uniform score"
    )

    as_zero = dict(everything, tests=0.0)
    assert rubric.weighted_overall(as_zero) < 80.0
    as_hundred = dict(everything, tests=100.0)
    assert rubric.weighted_overall(as_hundred) > 80.0


def test_nothing_measured_at_all_is_none_not_zero(rubric):
    assert rubric.weighted_overall({key: None for key in rubric.CATEGORY_KEYS}) is None


def test_severity_weights_order_a_bug_above_a_smell(rubric):
    assert rubric.severity_weight("high") > rubric.severity_weight("medium")
    assert rubric.severity_weight("medium") > rubric.severity_weight("low")
    assert rubric.severity_weight("nonsense") == rubric.severity_weight("medium")


@pytest.mark.parametrize("finding,expected", [
    ({"category": "mutation_hazards"}, "correctness"),
    ({"category": "security"}, "security"),
    ({"category": "untested_modules"}, "tests"),
    ({"category": "duplicates"}, "duplication"),
    ({"category": "import_cycles"}, "design"),
    ({"category": "naming_issues"}, "hygiene"),
    ({"smell_type": "n_plus_one_query"}, "correctness"),
    ({"smell_type": "missing_ownership_filter"}, "security"),
    ({"smell_type": "hardcoded_secret"}, "security"),
    ({"smell_type": "setuptestdata_mutation"}, "tests"),
    ({"smell_type": "fat_view"}, "complexity"),
    ({"smell_type": "single_use_mixin"}, "design"),
    ({"smell_type": "django_end_of_life"}, "hygiene"),
])
def test_findings_from_every_doctor_land_in_a_sensible_category(rubric, finding, expected):
    category, matched = rubric.categorize(finding)
    assert (category, matched) == (expected, True)


def test_an_unknown_type_falls_back_and_is_flagged(rubric):
    category, matched = rubric.categorize({"smell_type": "zzz_brand_new_detector"})
    assert category == rubric.FALLBACK_CATEGORY
    assert matched is False, "an unmapped type must be reportable, not silently absorbed"
    assert rubric.CATEGORY_WEIGHTS[rubric.FALLBACK_CATEGORY] == min(
        rubric.CATEGORY_WEIGHTS.values()
    ), "the fallback is the lowest-weight category so it cannot swing a grade unnoticed"


def test_keyword_fallback_beats_the_default(rubric):
    category, matched = rubric.categorize({"smell_type": "some_new_auth_bypass"})
    assert (category, matched) == ("security", True)


def test_django_alone_does_not_claim_duplication_coverage(rubric):
    django = rubric.DOCTOR_COVERAGE["django-code-doctor"]
    assert "duplication" not in django, (
        "django-code-doctor has no duplication detector; claiming coverage would "
        "hand out a free 100"
    )
    assert rubric.DOCTOR_COVERAGE["python-code-doctor"] == set(rubric.CATEGORY_KEYS)


def test_every_mapped_target_is_a_real_category(rubric):
    targets = (set(rubric.DETECTOR_CATEGORIES.values())
               | set(rubric.SMELL_TYPE_CATEGORIES.values())
               | {category for _, category in rubric.TYPE_KEYWORDS})
    assert targets <= set(rubric.CATEGORY_KEYS)


def test_no_smell_type_is_mapped_to_two_categories(rubric):
    seen = {}
    for category, smells in rubric.SMELL_TYPES.items():
        for smell in smells:
            assert smell not in seen, f"{smell} is in both {seen.get(smell)} and {category}"
            seen[smell] = category
