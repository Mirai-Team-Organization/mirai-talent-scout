"""
score_candidate_rubric — job-posting-aware scoring pipeline.

Given a candidate profile and a TalentBrief, computes:
  1. Dealbreaker pre-filter  → skip expensive scoring if disqualified
  2. rubric_match_score       → Haiku 0-100 "does this CV match the rubric?"
  3. salary_fit               → MATCH | ABOVE_RANGE | BELOW_RANGE | UNKNOWN
  4. location_fit             → 0-100 (reuses _score_location from hiring_context)
  5. combined_score           → stored as fit_score so rank_shortlist() works unchanged

Formula:
  combined_score = rubric_match_score * 0.60
                 + salary_adjustment       (−10 / 0 / +5 based on fit)
                 + location_adjustment     (−15 to +15, (location_fit−50)/50*15)

Result is merged into the candidate dict as fit_score, plus metadata keys
(rubric_match_score, salary_fit, location_fit, dealbreaker_hit).
"""

from __future__ import annotations

import json
import os

import boto3
from strands import tool

from scoring.hiring_context import _score_location
from scoring.salary_benchmarks import benchmark_range

# ── Bedrock client ─────────────────────────────────────────────────────────────

_bedrock = None


def _get_bedrock():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_REGION", "eu-west-1"),
        )
    return _bedrock


def _haiku(system: str, user: str, max_tokens: int = 200) -> str:
    model_id = os.environ.get(
        "BEDROCK_HAIKU_MODEL", "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    resp = _get_bedrock().converse(
        modelId=model_id,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )
    return resp["output"]["message"]["content"][0]["text"].strip()


# ── Dealbreaker pre-filter ─────────────────────────────────────────────────────

_DEALBREAKER_SYSTEM = """You are a strict recruiting screener. Given a list of job dealbreakers and a candidate's CV summary, answer only YES or NO.

YES = the candidate has at least one dealbreaker
NO = the candidate does not have any dealbreakers

Answer with a single word: YES or NO. Nothing else."""


def _check_dealbreakers(dealbreaker_text: str, candidate_summary: str) -> bool:
    """
    Returns True if the candidate hits a dealbreaker.
    Defaults to False (pass) on any error to avoid false-positives.
    """
    if not dealbreaker_text.strip():
        return False
    try:
        user_msg = f"Dealbreakers: {dealbreaker_text}\n\nCandidate: {candidate_summary}"
        answer = _haiku(_DEALBREAKER_SYSTEM, user_msg, max_tokens=10)
        return answer.upper().startswith("YES")
    except Exception as e:
        print(f"[score_candidate_rubric] Dealbreaker check failed: {e}")
        return False  # fail-open


# ── Rubric match scoring ───────────────────────────────────────────────────────

_RUBRIC_MATCH_SYSTEM = """You are a senior recruiter evaluating a candidate against a job rubric.

Score the candidate from 0 to 100 based on how well they match the rubric:
- 90–100: Exceptional match — exceeds requirements
- 70–89:  Strong match — meets all key requirements
- 50–69:  Partial match — meets most but gaps in 1-2 areas
- 30–49:  Weak match — significant skill or experience gaps
- 0–29:   Poor match — fundamental mismatch

Output ONLY an integer between 0 and 100. No explanation, no text, just the number."""


def _score_rubric_match(rubric_text: str, candidate_summary: str) -> int:
    """Returns 0-100 rubric match score. Returns 50 on any error."""
    if not rubric_text.strip():
        return 50  # no rubric = no signal
    try:
        user_msg = f"Job requirement: {rubric_text}\n\nCandidate: {candidate_summary}"
        raw = _haiku(_RUBRIC_MATCH_SYSTEM, user_msg, max_tokens=10)
        # Extract first integer from the response
        import re
        m = re.search(r"\d+", raw)
        if m:
            return max(0, min(100, int(m.group(0))))
        return 50
    except Exception as e:
        print(f"[score_candidate_rubric] Rubric match failed: {e}")
        return 50


# ── Candidate summary builder ─────────────────────────────────────────────────

def _build_candidate_summary(profile: dict) -> str:
    """
    Build a compact text summary of the candidate for Haiku prompts.
    Works for both GitHub profiles and internal Mirai profiles.
    """
    p = profile.get("profile", {})
    parts = []

    name = p.get("name") or p.get("login", "Candidate")
    parts.append(name)

    if p.get("bio"):
        parts.append(p["bio"][:200])

    # Internal profile fields
    job_role = profile.get("job_role")
    if job_role:
        parts.append(f"Role: {job_role}")

    seniority = profile.get("seniority")
    if seniority:
        parts.append(f"Seniority: {seniority}")

    # Skills
    all_skills = profile.get("all_skills") or []
    if all_skills:
        parts.append(f"Skills: {', '.join(all_skills[:10])}")
    else:
        langs = [l["name"] for l in (profile.get("languages") or [])]
        if langs:
            parts.append(f"Languages: {', '.join(langs[:5])}")

    # Work experience — most recent 3
    experiences = profile.get("experiences") or []
    if experiences:
        recent = experiences[:3]
        exp_text = "; ".join(
            f"{e.get('title', '')} at {e.get('company', '')} ({e.get('startDate', '')}–{e.get('endDate', 'present')})"
            for e in recent
            if e.get("title") or e.get("company")
        )
        if exp_text:
            parts.append(f"Experience: {exp_text}")

    # GitHub signals for external candidates
    contrib = profile.get("contributions", {})
    commits = contrib.get("commits", 0)
    if commits > 0:
        oss = contrib.get("openSourceRepoCount", 0)
        parts.append(f"GitHub: {commits} commits, {oss} OSS repos")

    location = p.get("location")
    if location:
        parts.append(f"Location: {location}")

    return ". ".join(parts)


# ── Salary fit ────────────────────────────────────────────────────────────────

def _check_salary_fit(
    candidate_salary_expectation: float | None,
    brief_min: float | None,
    brief_max: float | None,
    market: str,
    seniority: str,
) -> tuple[str, float]:
    """
    Returns (verdict, adjustment):
      MATCH        →  +5
      ABOVE_RANGE  → −10  (candidate costs more than the role pays)
      BELOW_RANGE  →   0  (candidate is cheaper — usually fine)
      UNKNOWN      →   0  (no salary data on either side)

    Uses benchmark_range() if the brief has no salary data.
    """
    # Resolve range to compare against
    min_eur = brief_min
    max_eur = brief_max
    if min_eur is None and max_eur is None:
        rng = benchmark_range(market, seniority)
        if rng:
            min_eur, max_eur = rng

    if candidate_salary_expectation is None or (min_eur is None and max_eur is None):
        return "UNKNOWN", 0.0

    if max_eur is not None and candidate_salary_expectation > max_eur:
        return "ABOVE_RANGE", -10.0
    if min_eur is not None and candidate_salary_expectation < min_eur:
        return "BELOW_RANGE", 0.0
    return "MATCH", 5.0


# ── Main tool ─────────────────────────────────────────────────────────────────

@tool
def score_candidate_rubric(
    profile: dict,
    talent_brief: dict,
    candidate_salary_expectation: float | None = None,
) -> dict:
    """
    Score a candidate against a TalentBrief (job-posting-aware scoring).

    Runs dealbreaker pre-filter first — if hit, returns immediately with
    fit_score=0 and dealbreaker_hit=True (no further Haiku calls).

    Args:
        profile: Profile dict from search_internal_pool() or search_github()
        talent_brief: TalentBrief dict from build_talent_brief()
        candidate_salary_expectation: Candidate's expected salary in EUR (optional)

    Returns:
        Profile dict enriched with:
          fit_score           0–100 (used by rank_shortlist)
          rubric_match_score  0–100
          salary_fit          MATCH | ABOVE_RANGE | BELOW_RANGE | UNKNOWN
          location_fit        0–100 | None
          dealbreaker_hit     bool
    """
    dealbreaker_text: str = talent_brief.get("dealbreaker_text", "")
    rubric_text: str = talent_brief.get("rubric_text", "")
    target_location: str = talent_brief.get("location", "")
    seniority: str = talent_brief.get("seniority", "mid")
    market: str = talent_brief.get("salary_market") or "EU"
    brief_min: float | None = talent_brief.get("salary_min")
    brief_max: float | None = talent_brief.get("salary_max")

    candidate_summary = _build_candidate_summary(profile)

    # ── 1. Dealbreaker pre-filter ─────────────────────────────────────────────
    if _check_dealbreakers(dealbreaker_text, candidate_summary):
        return {
            **profile,
            "fit_score":          0,
            "rubric_match_score": 0,
            "salary_fit":         "UNKNOWN",
            "location_fit":       None,
            "dealbreaker_hit":    True,
        }

    # ── 2. Rubric match score ─────────────────────────────────────────────────
    rubric_match_score = _score_rubric_match(rubric_text, candidate_summary)

    # ── 3. Salary fit ─────────────────────────────────────────────────────────
    salary_fit, salary_adjustment = _check_salary_fit(
        candidate_salary_expectation, brief_min, brief_max, market, seniority
    )

    # ── 4. Location fit ───────────────────────────────────────────────────────
    candidate_location = profile.get("profile", {}).get("location")
    location_fit = _score_location(target_location or None, candidate_location)
    if location_fit is None and talent_brief.get("remote_eligible"):
        location_fit = 70.0  # remote role — location matters less

    location_adjustment = 0.0
    if location_fit is not None:
        location_adjustment = (location_fit - 50) / 50 * 15  # −15 to +15

    # ── 5. Combined score → fit_score ─────────────────────────────────────────
    combined = rubric_match_score * 0.60 + salary_adjustment + location_adjustment
    fit_score = max(0, min(100, round(combined)))

    return {
        **profile,
        "fit_score":          fit_score,
        "rubric_match_score": rubric_match_score,
        "salary_fit":         salary_fit,
        "location_fit":       location_fit,
        "dealbreaker_hit":    False,
    }
