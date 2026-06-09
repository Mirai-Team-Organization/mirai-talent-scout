"""
search_internal_pool — query Mirai's own talent pool before hitting GitHub.

Joins user_working_profiles → users, filters by jobRole (ILIKE) and seniority,
returns candidates in the same unified profile dict shape used by score_candidate.

Internal candidates get:
  - source = "internal_mirai"
  - experiences, skill_levels, availability from profile_data
  - None for GitHub-specific fields (activityHeatmap, contributions, pinnedProjects)
  - No enrich_linkedin() call needed — CV data is richer than LinkedIn would return

Seniority mapping (profile_data values → TalentBrief seniority):
  junior       → junior
  mid / medior → mid
  senior       → senior
  lead / staff / principal / manager → lead
"""

from __future__ import annotations

import re
from strands import tool

from db.client import get_supabase


# profile_data seniority values → normalised tier
_SENIORITY_MAP = {
    "junior":    "junior",
    "entry":     "junior",
    "graduate":  "junior",
    "intern":    "junior",
    "mid":       "mid",
    "medior":    "mid",
    "middle":    "mid",
    "senior":    "senior",
    "staff":     "lead",
    "principal": "lead",
    "lead":      "lead",
    "manager":   "lead",
    "director":  "lead",
    "head":      "lead",
    "vp":        "lead",
}


def _normalise_seniority(raw: str | None) -> str:
    if not raw:
        return "mid"
    return _SENIORITY_MAP.get(raw.lower().strip(), "mid")


def _to_profile_dict(row: dict) -> dict:
    """
    Map a user_working_profiles + users JOIN row into the unified profile dict
    shape that score_candidate_rubric() and rank_shortlist() expect.
    """
    pd = row.get("profile_data") or {}
    user = row.get("users") or {}

    # Identity
    login = user.get("email", "").split("@")[0] or f"mirai_{row.get('user_id', '')[:8]}"
    nome    = user.get("nome") or ""
    cognome = user.get("cognome") or ""
    full_name = pd.get("fullName") or pd.get("name") or f"{nome} {cognome}".strip() or None
    email = user.get("email")
    avatar_url = user.get("profile_img_url") or pd.get("avatarUrl")

    # Location: prefer profile_data locations[] → first entry, fallback users table
    locations = pd.get("locations") or []
    user_location = " ".join(filter(None, [user.get("location_city"), user.get("location_country")]))
    candidate_location = locations[0] if locations else pd.get("location") or user_location or None

    # Skills: prefer skillLevels keys (dict skill→level), fallback skills[]
    skill_levels: dict = pd.get("skillLevels") or {}
    skills_list: list[str] = pd.get("skills") or list(skill_levels.keys())

    # Languages: derive from skillLevels keys that look like programming languages
    _LANG_KEYWORDS = {
        "python", "javascript", "typescript", "java", "go", "rust", "ruby",
        "c++", "c#", "swift", "kotlin", "php", "scala", "r", "dart", "elixir",
    }
    top_languages = [
        {"name": s, "size": 0}
        for s in skills_list
        if s.lower() in _LANG_KEYWORDS
    ][:5]

    return {
        # ── Standard profile shape ──────────────────────────────────────────────
        "profile": {
            "login":           login,
            "name":            full_name,
            "bio":             pd.get("bio") or pd.get("summary"),
            "email":           email,
            "location":        candidate_location,
            "company":         pd.get("currentCompany") or user.get("azienda"),
            "websiteUrl":      pd.get("websiteUrl") or pd.get("linkedinUrl"),
            "twitterUsername": None,
            "createdAt":       None,
            "followers":       0,   # no GitHub data
            "avatar_url":      avatar_url,
        },
        "languages": top_languages,
        "pinnedProjects": [],
        "activityHeatmap": {"totalContributions": 0},
        "contributions": {
            "commits":           0,
            "issues":            0,
            "pullRequests":      0,
            "pullRequestReviews": 0,
            "openSourceRepoCount": 0,
        },
        "profileReadme": {"exists": False},

        # ── Internal-only fields (used by score_candidate_rubric) ───────────────
        "source":       "internal_mirai",
        "user_id":      row.get("user_id"),
        "experiences":  pd.get("experiences") or [],
        "skill_levels": skill_levels,
        "availability": pd.get("availability"),
        "open_to_work": pd.get("openToWork") or pd.get("open_to_work") or False,
        "seniority":    _normalise_seniority(pd.get("seniority") or pd.get("experienceLevel")),
        "job_role":     pd.get("jobRole"),
        "all_skills":   skills_list,
    }


@tool
def search_internal_pool(
    talent_brief: dict,
    limit: int = 20,
) -> list[dict]:
    """
    Search Mirai's internal talent pool for candidates matching a TalentBrief.

    Queries user_working_profiles by jobRole (ILIKE on first title word) and
    seniority. Internal candidates skip LinkedIn enrichment — their CV data is
    already richer than LinkedIn would return.

    Args:
        talent_brief: TalentBrief dict from build_talent_brief()
        limit: Max candidates to return (default 20)

    Returns:
        List of profile dicts with source="internal_mirai", ready for
        score_candidate_rubric(). Empty list if no internal matches.
    """
    sb = get_supabase()
    limit = min(limit, 50)

    seniority: str = talent_brief.get("seniority", "mid")
    skills: list[str] = talent_brief.get("skills") or []
    internal_role_slugs: list[str] = talent_brief.get("internal_role_slugs") or []

    # ── Build seniority whitelist ─────────────────────────────────────────────
    # Include adjacent tier so "senior" also catches "lead" (and "mid" catches "medior")
    _ADJACENT: dict[str, set[str]] = {
        "junior": {"junior", "entry", "graduate", "intern"},
        "mid":    {"mid", "medior", "middle"},
        "senior": {"senior", "mid", "medior"},
        "lead":   {"lead", "staff", "principal", "manager", "director", "senior"},
    }
    accepted_tiers = _ADJACENT.get(seniority, {"mid"})

    # ── Build LLM-driven OR filter ────────────────────────────────────────────
    # Role slugs query jobRole; skills query skillLevels and skills fields.
    # Max 9 OR conditions (3 slugs × 1 field + 3 skills × 2 fields).
    or_parts: list[str] = []
    for slug in internal_role_slugs[:3]:
        safe_slug = re.sub(r"[^a-z0-9\- ]", "", slug.lower()).strip()
        if safe_slug:
            or_parts.append(f"profile_data->>jobRole.ilike.%{safe_slug}%")
    for sk in skills[:3]:
        safe_sk = re.sub(r"[^a-z0-9\-+# ]", "", sk.lower()).strip()
        if safe_sk:
            or_parts.append(f"profile_data->>skillLevels.ilike.%{safe_sk}%")
            or_parts.append(f"profile_data->>skills.ilike.%{safe_sk}%")

    _select = "user_id, profile_data, users(email, nome, cognome, profile_img_url, location_city, location_country, azienda)"

    try:
        if not or_parts:
            # No filters available — fetch entire pool, let scoring handle relevance
            print("[search_internal_pool] No slugs or skills — fetching full pool")
            result = (
                sb.table("user_working_profiles")
                .select(_select)
                .limit(limit * 3)
                .execute()
            )
        else:
            or_filter = ",".join(or_parts)
            result = (
                sb.table("user_working_profiles")
                .select(_select)
                .or_(or_filter)
                .limit(limit * 3)
                .execute()
            )
        rows = result.data or []
    except Exception as e:
        print(f"[search_internal_pool] Query failed: {e}")
        rows = []

    # ── Client-side seniority filter ──────────────────────────────────────────
    filtered = []
    for row in rows:
        pd = row.get("profile_data") or {}
        raw_seniority = pd.get("seniority") or pd.get("experienceLevel") or ""
        if raw_seniority.lower().strip() in accepted_tiers or not raw_seniority:
            filtered.append(row)

    # ── Map to unified profile shape ──────────────────────────────────────────
    profiles = []
    for row in filtered[:limit]:
        try:
            profiles.append(_to_profile_dict(row))
        except Exception as e:
            print(f"[search_internal_pool] Failed to map row {row.get('user_id')}: {e}")

    return profiles
