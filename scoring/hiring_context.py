"""
Python port of gitcheck-webapp/src/lib/agent/hiringContext.ts

Reweights talent scores based on company stage.
"""

from __future__ import annotations

from agent.models import TalentScore, TalentScoreBreakdown

# ── Context weight profiles ────────────────────────────────────────────────────

CONTEXT_WEIGHTS = {
    "startup_early": {
        "tech_stack": 0.30,
        "open_source": 0.12,
        "consistency": 0.38,
        "collaboration": 0.12,
        "presentation": 0.08,
        "prestige_ceiling_followers": 400,
        "prestige_ceiling_stars": 150,
    },
    "startup_growth": {
        "tech_stack": 0.25,
        "open_source": 0.20,
        "consistency": 0.25,
        "collaboration": 0.22,
        "presentation": 0.08,
        "prestige_ceiling_followers": 2000,
        "prestige_ceiling_stars": 800,
    },
    "enterprise": {
        "tech_stack": 0.30,
        "open_source": 0.25,
        "consistency": 0.20,
        "collaboration": 0.15,
        "presentation": 0.10,
        "prestige_ceiling_followers": None,
        "prestige_ceiling_stars": None,
    },
}


def apply_hiring_context(
    talent_score: TalentScore,
    context: str,
    target_location: str | None = None,
    candidate_location: str | None = None,
    candidate_followers: int = 0,
    candidate_stars: int = 0,
) -> TalentScore:
    """
    Apply hiring-stage-aware reweighting to a TalentScore.

    context: "startup_early" | "startup_growth" | "enterprise"
    """
    weights = CONTEXT_WEIGHTS.get(context, CONTEXT_WEIGHTS["enterprise"])
    b = talent_score.breakdown

    context_score = (
        b.tech_stack.score * weights["tech_stack"] +
        b.open_source.score * weights["open_source"] +
        b.consistency.score * weights["consistency"] +
        b.collaboration.score * weights["collaboration"] +
        b.presentation.score * weights["presentation"]
    )

    # Prestige penalty for over-qualified candidates
    prestige_penalty = 0.0
    ceil_followers = weights.get("prestige_ceiling_followers")
    ceil_stars = weights.get("prestige_ceiling_stars")

    if ceil_followers and candidate_followers > ceil_followers:
        prestige_penalty += 5.0
    if ceil_stars and candidate_stars > ceil_stars:
        prestige_penalty += 5.0

    context_score = max(0.0, context_score - prestige_penalty)

    # Location fit adjustment (±15 pts)
    location_fit = _score_location(target_location, candidate_location)
    if location_fit is not None:
        location_adjustment = (location_fit - 50) / 50 * 15  # -15 to +15
        context_score = max(0.0, min(100.0, context_score + location_adjustment))

    return talent_score.model_copy(update={
        "context_score": round(context_score, 1),
        "location_fit": location_fit,
        "prestige_penalty": prestige_penalty,
        "hiring_context": context,
    })


def _score_location(target: str | None, candidate: str | None) -> float | None:
    if not target:
        return None
    if not candidate:
        return 40.0   # location unknown

    t = target.lower().strip()
    c = candidate.lower().strip()

    if t == c:
        return 100.0
    if t in c or c in t:
        return 90.0

    # Same country heuristic — check last token
    t_country = t.split(",")[-1].strip()
    c_country = c.split(",")[-1].strip()
    if t_country and c_country and t_country == c_country:
        return 70.0

    return 10.0
