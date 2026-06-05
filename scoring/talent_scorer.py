"""
Python port of gitcheck-webapp/src/lib/talentScoring.ts

Weights (presentation removed — only technical activity signals count for indexing):
  Tech Stack     33%  (top languages by code volume)
  Open Source    28%  (contributions to repos user doesn't own)
  Consistency    22%  (active days + longest streak, past 12 months)
  Collaboration  17%  (PRs + PR reviews)

Presentation is still computed and stored in the breakdown for display purposes
but does NOT contribute to the overall score. Only profiles scoring B- or above
(overall >= 42) are accepted into the talent index.

IMPORTANT: Any change to weights or scoring logic here MUST be mirrored in the
TypeScript implementation. Run `pytest tests/parity/` to verify alignment.
"""

from __future__ import annotations

import math
from typing import Any

from agent.models import (
    TalentScore, TalentScoreBreakdown,
    TechStackScore, OpenSourceScore, ConsistencyScore,
    CollaborationScore, PresentationScore,
)

# ── Grade thresholds (matches TypeScript exactly) ───────────────────────────
GRADE_THRESHOLDS = [
    (90, "S"),
    (82, "A+"),
    (74, "A"),
    (66, "A-"),
    (58, "B+"),
    (50, "B"),
    (42, "B-"),
    (34, "C+"),
    (0,  "C"),
]

GRADE_ORDER = {"S": 9, "A+": 8, "A": 7, "A-": 6, "B+": 5, "B": 4, "B-": 3, "C+": 2, "C": 1}


def score_to_grade(score: float) -> str:
    for threshold, grade in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "C"


def _cdf_exponential(x: float, rate: float = 1.0) -> float:
    """Exponential CDF: F(x) = 1 - e^(-rate*x). Used for linear metrics."""
    if x <= 0:
        return 0.0
    return 1.0 - math.exp(-rate * x)


def _cdf_lognormal(x: float, mu: float = 0.0, sigma: float = 1.0) -> float:
    """Log-normal CDF approximation for heavy-tailed metrics (stars, followers)."""
    if x <= 0:
        return 0.0
    from math import log, erf, sqrt
    z = (log(x) - mu) / (sigma * sqrt(2))
    return 0.5 * (1 + erf(z))


# ── Tech stack score (30%) ────────────────────────────────────────────────────

def _score_tech_stack(profile: dict) -> TechStackScore:
    languages: list[dict] = profile.get("languages", [])
    top_langs = [lang["name"] for lang in sorted(languages, key=lambda l: l.get("size", 0), reverse=True)[:5]]
    # Score: presence of at least 1 language = base 20, more = higher
    score = min(100.0, 20.0 + len(top_langs) * 16.0)
    return TechStackScore(score=round(score, 1), top_languages=top_langs)


# ── Open source score (28%) ───────────────────────────────────────────────────
# Uses the integer fields from the contributions dict (as returned by _parse_profile):
#   openSourceRepoCount — number of distinct repos the user contributed to but doesn't own
#   commits             — total commit contributions (proxy for depth of activity)

def _score_open_source(profile: dict) -> OpenSourceScore:
    contributions: dict = profile.get("contributions", {})
    repo_count  = contributions.get("openSourceRepoCount", 0)
    commit_count = contributions.get("commits", 0)

    # Normalize: 5 repos → ~50 pts; 50 commits → ~50 pts
    repo_score   = _cdf_exponential(repo_count,   rate=0.15) * 50
    commit_score = _cdf_exponential(commit_count, rate=0.02) * 50
    score = repo_score + commit_score

    return OpenSourceScore(
        score=round(min(score, 100.0), 1),
        repo_count=repo_count,
        commit_count=commit_count,
    )


# ── Consistency score (22%) ───────────────────────────────────────────────────
# dailyActivity is deliberately not fetched (365 records/candidate bloats context).
# We use totalContributions from the contributionCalendar as a proxy.
# Calibration: ~2 contributions per active day → 400 total ≈ 200 active days.

def _score_consistency(profile: dict) -> ConsistencyScore:
    heatmap: dict = profile.get("activityHeatmap", {})
    total = heatmap.get("totalContributions", 0)

    # Estimated active days: contributions / 2, capped at 365
    active_days = min(int(total / 2), 365)

    # Streak proxy: average contributions/day; >= 1/day suggests sustained streaks
    avg_daily = total / 365
    streak = min(30, int(avg_daily * 30))  # maps 1.0 avg → full 30-day streak estimate

    base_score   = (active_days / 365) * 80
    streak_bonus = 20.0 if streak >= 30 else (streak / 30) * 20
    score = min(base_score + streak_bonus, 100.0)

    return ConsistencyScore(
        score=round(score, 1),
        active_days=active_days,
        streak=streak,
    )


# ── Collaboration score (15%) ─────────────────────────────────────────────────

def _score_collaboration(profile: dict) -> CollaborationScore:
    contributions: dict = profile.get("contributions", {})
    prs = contributions.get("pullRequests", 0)
    reviews = contributions.get("pullRequestReviews", 0)

    pr_score = _cdf_exponential(prs, rate=0.05) * 50
    review_score = _cdf_exponential(reviews, rate=0.05) * 50
    score = min(pr_score + review_score, 100.0)

    return CollaborationScore(
        score=round(score, 1),
        prs=prs,
        reviews=reviews,
    )


# ── Presentation score (10%) ──────────────────────────────────────────────────

def _score_presentation(profile: dict) -> PresentationScore:
    points = 0
    prof: dict = profile.get("profile", {})

    if prof.get("bio"):
        points += 3
    if profile.get("profileReadme", {}).get("exists"):
        points += 4
    pinned = profile.get("pinnedProjects", [])
    if len(pinned) >= 3:
        points += 3

    score = (points / 10) * 100
    return PresentationScore(score=round(score, 1))


# ── Main entry point ──────────────────────────────────────────────────────────

def calculate_talent_score(profile: dict, hiring_context: str | None = None) -> TalentScore:
    """
    Calculate talent score from a GitHub profile dict.

    The profile dict matches the shape returned by GitHub GraphQL + profileFetcher.ts.
    This is the Python equivalent of calculateTalentScore() in talentScoring.ts.
    """
    tech = _score_tech_stack(profile)
    oss = _score_open_source(profile)
    consistency = _score_consistency(profile)
    collab = _score_collaboration(profile)
    presentation = _score_presentation(profile)

    # Presentation excluded from overall — only technical activity signals matter.
    # Weights redistributed proportionally (30+25+20+15 → 33+28+22+17).
    overall = (
        tech.score * 0.33 +
        oss.score * 0.28 +
        consistency.score * 0.22 +
        collab.score * 0.17
    )

    grade = score_to_grade(overall)

    return TalentScore(
        overall=round(overall, 1),
        grade=grade,
        breakdown=TalentScoreBreakdown(
            tech_stack=tech,
            open_source=oss,
            consistency=consistency,
            collaboration=collab,
            presentation=presentation,
        ),
        hiring_context=hiring_context,
    )
