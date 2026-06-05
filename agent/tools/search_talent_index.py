"""
search_talent_index — query the pre-built talent index (Mode A).

Only returns profiles that are COMPLETE: have both GitHub data in talent_index
AND a valid (non-expired) LinkedIn enrichment in linkedin_enrichments.
No live GitHub calls, no Apify calls — cached data only.

Search strategy:
  1. Parse the TalentBrief for required languages and role_signals
  2. Call Supabase RPC `search_talent_index_fn` with location + language filters
  3. Batch-fetch LinkedIn enrichments for the returned usernames
  4. Filter to complete profiles (both GitHub + LinkedIn present)
  5. Attach parsed LinkedIn data to each profile (linkedin, mobility, career_signals)
  6. Return profiles ready for score_candidate_rubric() — no enrich_linkedin needed
"""

from __future__ import annotations

from strands import tool

from db.client import get_supabase

# Minimum results before relaxing signal filters
_MIN_RESULTS = 5

# Role signal → preferred programming languages (for pre-filtering)
_ROLE_LANGUAGES: dict[str, list[str]] = {
    "ml_engineer_signal":  ["Python"],
    "devops_signal":       ["Python", "Go", "Shell"],
    "fullstack_signal":    ["TypeScript", "JavaScript"],
    "backend_signal":      ["Go", "Rust", "Java", "Python", "Kotlin"],
    "fde_signal":          ["Python", "TypeScript", "JavaScript"],
}

# Country → cities with sort boost
_PRIORITY_CITIES: dict[str, list[str]] = {
    "IT": ["Milan", "Milano"],
    "CH": ["Zurich", "Zuerich"],
}


def _infer_country(location_hints: list[str]) -> str | None:
    """Derive country_code from location strings in the TalentBrief."""
    it_keywords = {"italy", "italia", "milan", "milano", "rome", "roma", "turin",
                   "torino", "florence", "firenze", "bologna", "naples", "napoli",
                   "genoa", "palermo", "bari"}
    ch_keywords = {"switzerland", "schweiz", "svizzera", "zurich", "zuerich",
                   "zürich", "geneva", "geneve", "genève", "basel", "bern",
                   "lausanne", "lugano"}
    combined = " ".join(location_hints).lower()
    if any(k in combined for k in it_keywords):
        return "IT"
    if any(k in combined for k in ch_keywords):
        return "CH"
    return None


def _row_to_profile(row: dict) -> dict:
    """
    Convert a talent_index row into the unified profile dict shape expected by
    score_candidate_rubric() and build_talent_brief().
    """
    gd = row.get("github_data") or {}
    cc = gd.get("contributionsCollection") or {}
    calendar = cc.get("contributionCalendar") or {}

    # Languages: talent_index stores TEXT[] — reconstruct as list of dicts
    languages = [{"name": l, "size": 0} for l in (row.get("languages") or [])]

    # Pinned items from raw github_data if present
    pinned = gd.get("pinnedItems", {}).get("nodes", []) if gd else []

    oss_count = 0
    if gd:
        login = row.get("github_username", "")
        oss_count = sum(
            1 for c in cc.get("commitContributionsByRepository", [])
            if c["repository"]["owner"]["login"] != login
        )

    return {
        "profile": {
            "login":           row.get("github_username", ""),
            "name":            gd.get("name"),
            "bio":             gd.get("bio"),
            "email":           gd.get("email"),
            "location":        row.get("location_raw"),
            "company":         gd.get("company"),
            "websiteUrl":      gd.get("websiteUrl"),
            "twitterUsername": gd.get("twitterUsername"),
            "createdAt":       gd.get("createdAt"),
            "followers":       row.get("followers", 0),
        },
        "languages":      languages,
        "pinnedProjects": pinned,
        "activityHeatmap": {
            "totalContributions": calendar.get("totalContributions", 0),
        },
        "contributions": {
            "commits":            cc.get("totalCommitContributions", 0),
            "issues":             cc.get("totalIssueContributions", 0),
            "pullRequests":       cc.get("totalPullRequestContributions", 0),
            "pullRequestReviews": cc.get("totalPullRequestReviewContributions", 0),
            "openSourceRepoCount": oss_count,
        },
        "profileReadme": {"exists": False},

        # Contact fields (available when developer made them public on GitHub)
        "email":        row.get("email"),
        "linkedin_url": row.get("linkedin_url"),

        # GitHub talent score (pre-computed at index time)
        "talent_score": row.get("talent_score"),

        # Index-only metadata (used by scoring / UI)
        "source":       row.get("source", "talent_index"),
        "role_signals": row.get("role_signals") or [],
        "signals":      row.get("signals") or [],
        "activity_score": row.get("activity_score", 0),
        "country_code": row.get("country_code"),
        "city":         row.get("city"),
        "indexed_at":   row.get("indexed_at"),
        # github_data intentionally excluded — all useful fields already unpacked above
    }


@tool
def search_talent_index(
    talent_brief: dict,
    limit: int = 1000,
) -> list[dict]:
    """
    Search the pre-built talent index for candidates matching a TalentBrief.

    Queries talent_index by language overlap, role signals, and location.
    Milan and Zurich profiles surface first. Falls back to live GitHub search
    during cold start (< 5 results in the index).

    Args:
        talent_brief: TalentBrief dict from build_talent_brief()
        limit: Max candidates to return (default 1000 — fetch full matching pool, filter for completeness)

    Returns:
        List of profile dicts ready for score_candidate_rubric(). Profiles
        include role_signals, signals, and activity_score from the index.
    """
    sb = get_supabase()
    limit = min(limit, 2000)  # safety cap — avoids unbounded DB scans on very large indices

    # ── Derive filters — prefer structured index_query, fall back to legacy fields ─
    iq: dict = talent_brief.get("index_query") or {}

    location_hints: list[str] = talent_brief.get("locations") or []
    if not location_hints and talent_brief.get("location"):
        location_hints = [talent_brief["location"]]

    required_languages: list[str] = iq.get("languages") or talent_brief.get("language_list") or []
    role_signals: list[str] = iq.get("role_signals") or []
    required_signals: list[str] = iq.get("signals") or []
    seniority: str = talent_brief.get("seniority", "mid")
    country_code = _infer_country(location_hints)

    # Signals to query — use all provided signals (up to 2); fall back to role_type
    query_signals: list[str | None] = (
        role_signals[:2] if role_signals
        else [talent_brief.get("role_type")] if talent_brief.get("role_type")
        else [None]
    )
    rpc_role_signal = query_signals[0]  # used for language hint below

    # Add role-specific language hints if none provided
    if not required_languages and rpc_role_signal and rpc_role_signal in _ROLE_LANGUAGES:
        required_languages = _ROLE_LANGUAGES[rpc_role_signal]

    # ── Minimum language overlap threshold ────────────────────────────────────
    # Require candidates to know at least N of the required languages so we
    # don't surface people with no relevant tech stack. With 3+ required
    # languages we enforce at least 2 matches; otherwise any 1 is enough.
    # Falls back to 1 automatically if the strict threshold yields < _MIN_RESULTS.
    min_lang_overlap = 2 if len(required_languages) >= 3 else 1

    # ── Derive city hint for prioritised ordering ─────────────────────────────
    priority_city: str | None = None
    if country_code:
        for city_name in _PRIORITY_CITIES.get(country_code, []):
            combined = " ".join(location_hints).lower()
            if city_name.lower() in combined:
                priority_city = city_name
                break

    def _rpc_query(overlap: int) -> list[dict]:
        """Run one RPC call per role signal, union and deduplicate results."""
        seen: set[str] = set()
        result_rows: list[dict] = []
        for signal in query_signals:
            try:
                res = sb.rpc(
                    "search_talent_complete_fn",
                    {
                        "p_languages":            required_languages or [],
                        "p_role_signal":          signal,
                        "p_country":              country_code,
                        "p_city":                 priority_city,
                        "p_limit":                limit,
                        "p_min_language_overlap": overlap,
                    },
                ).execute()
                for row in (res.data or []):
                    uname = row.get("github_username", "")
                    if uname not in seen:
                        seen.add(uname)
                        result_rows.append(row)
            except Exception as e:
                print(f"[search_talent_index] RPC failed for signal={signal}: {e}")
        return result_rows

    # ── Query: strict overlap first, relax to 1 if too few results ───────────
    rows = _rpc_query(min_lang_overlap)
    if min_lang_overlap > 1 and len(rows) < _MIN_RESULTS:
        print(
            f"[search_talent_index] Only {len(rows)} results with overlap={min_lang_overlap}, "
            f"relaxing to overlap=1"
        )
        rows = _rpc_query(1)

    # ── Required achievement signals (relax if too few results) ──────────────
    if required_signals:
        sig_set = set(required_signals)
        strict = [r for r in rows if sig_set.issubset(set(r.get("signals") or []))]
        if len(strict) >= _MIN_RESULTS:
            rows = strict

    print(f"[search_talent_index] {len(rows)} complete profiles (GitHub+LinkedIn joined)")

    # Build enrich_map from the joined enrichment_data already in the rows
    enrich_map = {
        r["github_username"]: {
            "enrichment_data":  r.get("enrichment_data"),
            "mobility_score":   r.get("mobility_score"),
            "data_completeness": r.get("data_completeness"),
            "fetched_at":       r.get("fetched_at"),
        }
        for r in rows
        if r.get("enrichment_data")
    }

    profiles = []
    for row in rows:
        profile = _row_to_profile(row)
        if row["github_username"] in enrich_map:
            _attach_linkedin(profile, row["github_username"], enrich_map[row["github_username"]])
        profiles.append(profile)

    return profiles


def _attach_linkedin(profile: dict, username: str, enrich_row: dict) -> None:
    """Parse cached enrichment_data and attach linkedin, mobility, career_signals to profile."""
    from scoring.linkedin_analyzer import (
        parse_harvestapi_response,
        detect_move_signals,
        compute_career_signals,
    )
    try:
        raw = enrich_row["enrichment_data"]
        enrichment, about_text = parse_harvestapi_response(username, raw)
        mobility = detect_move_signals(enrichment)
        career_signals = compute_career_signals(enrichment, about_text)

        # Fill location from GitHub if LinkedIn didn't capture it
        if not enrichment.location:
            enrichment.location = profile.get("profile", {}).get("location")

        linkedin_data = enrichment.model_dump()

        # ── Build compact career_summary for Haiku scoring ───────────────────
        # Descriptions are used here to build the summary string, then stripped
        # from the position objects so the agent context stays small.
        raw_positions = (linkedin_data.get("positions") or [])[:5]
        summary_parts = []
        for pos in raw_positions:
            title   = pos.get("title") or ""
            company = pos.get("company") or ""
            start   = pos.get("start_date") or ""
            end     = pos.get("end_date") or "present"
            desc    = pos.get("description") or ""
            header  = f"{title} at {company} ({start}–{end})"
            summary_parts.append(f"- {header}: {desc}" if desc else f"- {header}")
        linkedin_data["career_summary"] = "\n".join(summary_parts)

        # Strip descriptions from position objects — UI needs title/company/dates,
        # not the full text (which only Haiku needs, and it's in career_summary now).
        linkedin_data["positions"] = [
            {k: v for k, v in pos.items() if k != "description"}
            for pos in raw_positions
        ]
        linkedin_data["education"] = []  # not needed in agent context

        linkedin_data["fetched_at"] = enrich_row.get("fetched_at")
        profile["linkedin"] = linkedin_data
        profile["mobility"] = mobility.model_dump()
        profile["career_signals"] = career_signals.model_dump()
    except Exception as e:
        print(f"[search_talent_index] LinkedIn parse failed for {username}: {e}")
        profile["linkedin"] = None
        profile["mobility"] = None
        profile["career_signals"] = None
