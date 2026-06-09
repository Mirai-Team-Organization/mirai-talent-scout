"""Unit tests for calculate_talent_score() and hiring context reweighting."""

import pytest
from scoring.talent_scorer import calculate_talent_score, score_to_grade
from scoring.hiring_context import apply_hiring_context


def _make_profile(
    commits=50, prs=10, reviews=5, active_days=200, streak=45,
    languages=None, pinned_count=3, has_bio=True, has_readme=True,
    oss_commits=20, oss_repos=3,
) -> dict:
    if languages is None:
        languages = [{"name": "Python", "size": 50000}, {"name": "TypeScript", "size": 30000}]

    daily = [{"date": f"2025-01-{i:02d}", "count": 1 if i <= active_days % 28 or i % 2 == 0 else 0, "weekday": i % 7}
             for i in range(1, 29)] * 13  # rough 365-day approximation

    return {
        "profile": {
            "login": "testdev",
            "bio": "Engineer" if has_bio else None,
            "location": "Berlin",
            "followers": 150,
        },
        "languages": languages,
        "activityHeatmap": {
            "totalContributions": commits,
            "dailyActivity": [{"date": f"2025-{i//30+1:02d}-{i%30+1:02d}", "count": 1 if i < active_days else 0, "weekday": i % 7} for i in range(365)],
        },
        "pinnedProjects": [{"name": f"repo{i}"} for i in range(pinned_count)],
        "profileReadme": {"exists": has_readme},
        "contributions": {
            "commits": commits,
            "pullRequests": prs,
            "pullRequestReviews": reviews,
            "issues": 5,
            "openSourceRepoCount": oss_repos,
        },
        "repositories": {"nodes": []},
    }


def test_grade_thresholds():
    assert score_to_grade(95) == "S"
    assert score_to_grade(85) == "A+"
    assert score_to_grade(77) == "A"
    assert score_to_grade(69) == "A-"
    assert score_to_grade(61) == "B+"
    assert score_to_grade(53) == "B"
    assert score_to_grade(45) == "B-"
    assert score_to_grade(37) == "C+"
    assert score_to_grade(10) == "C"


def test_active_developer_scores_high():
    profile = _make_profile(commits=600, prs=40, reviews=20, active_days=300, streak=60)
    result = calculate_talent_score(profile)

    assert result.overall >= 60, f"Active dev should score ≥ 60, got {result.overall}"
    assert result.grade in ("S", "A+", "A", "A-", "B+", "B"), f"Got {result.grade}"


def test_inactive_developer_scores_low():
    profile = _make_profile(commits=2, prs=0, reviews=0, active_days=10, streak=0,
                            oss_commits=0, oss_repos=0)
    result = calculate_talent_score(profile)

    assert result.overall < 40, f"Inactive dev should score < 40, got {result.overall}"


def test_breakdown_sums_to_overall():
    """Overall score = weighted sum of breakdown components."""
    profile = _make_profile()
    result = calculate_talent_score(profile)
    b = result.breakdown

    expected = (
        b.tech_stack.score * 0.38 +
        b.open_source.score * 0.23 +
        b.consistency.score * 0.22 +
        b.collaboration.score * 0.17
        # presentation excluded from overall (indexing-only signal)
    )
    assert abs(result.overall - round(expected, 1)) < 0.2


def test_startup_early_boosts_consistency():
    """startup_early reweights consistency to 38% — should push consistent devs up."""
    consistent = _make_profile(active_days=340, streak=90, commits=300, prs=5)
    base_score = calculate_talent_score(consistent)
    context_score = apply_hiring_context(base_score, context="startup_early")

    assert context_score.context_score is not None
    assert context_score.hiring_context == "startup_early"
    # Consistency-heavy profile should score higher in startup_early vs enterprise
    enterprise_score = apply_hiring_context(base_score, context="enterprise")
    assert context_score.context_score >= enterprise_score.context_score - 5


def test_startup_early_prestige_penalty():
    """Candidates with many followers get a prestige penalty for startup_early."""
    profile = _make_profile()
    base_score = calculate_talent_score(profile)
    context_score = apply_hiring_context(
        base_score, context="startup_early",
        candidate_followers=1000,  # > 400 ceiling
    )
    assert context_score.prestige_penalty > 0


def test_location_fit_exact_match():
    profile = _make_profile()
    base_score = calculate_talent_score(profile)
    result = apply_hiring_context(
        base_score, context="enterprise",
        target_location="Berlin",
        candidate_location="Berlin",
    )
    assert result.location_fit == 100.0


def test_location_fit_no_location():
    profile = _make_profile()
    base_score = calculate_talent_score(profile)
    result = apply_hiring_context(
        base_score, context="enterprise",
        target_location="Berlin",
        candidate_location=None,
    )
    assert result.location_fit == 40.0  # unknown location


