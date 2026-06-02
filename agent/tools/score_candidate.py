"""
Score a candidate — wraps talent_scorer + hiring_context with Supabase caching.
"""

from __future__ import annotations

from strands import tool

from scoring.talent_scorer import calculate_talent_score
from scoring.hiring_context import apply_hiring_context
from db.candidates import get_cached_candidate, upsert_candidate


@tool
def score_candidate(
    profile: dict,
    hiring_context: str | None = None,
    target_location: str | None = None,
) -> dict:
    """
    Calculate a talent score for a GitHub profile.

    Args:
        profile: GitHub profile dict from search_github()
        hiring_context: "startup_early" | "startup_growth" | "enterprise"
        target_location: Target city/country for location fit scoring

    Returns:
        TalentScore dict with overall score, grade, and breakdown.
    """
    login = profile.get("profile", {}).get("login", "")

    # Check cache
    cached = get_cached_candidate(login)
    if cached and cached.get("talent_score") and not hiring_context:
        return cached["talent_score"]

    # Compute
    talent_score = calculate_talent_score(profile, hiring_context)

    if hiring_context:
        followers = profile.get("profile", {}).get("followers", 0)
        stars = sum(r.get("stargazerCount", 0) for r in profile.get("repositories", {}).get("nodes", []))
        talent_score = apply_hiring_context(
            talent_score=talent_score,
            context=hiring_context,
            target_location=target_location,
            candidate_location=profile.get("profile", {}).get("location"),
            candidate_followers=followers,
            candidate_stars=stars,
        )

    result = talent_score.model_dump()

    # Cache (only base score without context, so it's reusable across contexts)
    if not hiring_context:
        upsert_candidate(login, profile, result)

    return result
