"""
Candidate cache — GitHub profile data with 24h TTL.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Optional

from db.client import get_supabase


TTL_HOURS = 24


def get_cached_candidate(github_username: str) -> Optional[dict]:
    """Return cached GitHub profile if fresh, else None."""
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()

    result = (
        sb.table("candidates")
        .select("github_data, talent_score, fetched_at")
        .eq("github_username", github_username)
        .gt("expires_at", now)
        .maybe_single()
        .execute()
    )

    if result and result.data:
        return {
            "github_data": result.data["github_data"],
            "talent_score": result.data["talent_score"],
            "fetched_at": result.data["fetched_at"],
        }
    return None


def upsert_candidate(github_username: str, github_data: dict, talent_score: dict) -> None:
    """Insert or update a candidate in the cache."""
    sb = get_supabase()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=TTL_HOURS)

    sb.table("candidates").upsert({
        "github_username": github_username,
        "github_data": github_data,
        "talent_score": talent_score,
        "fetched_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    }, on_conflict="github_username").execute()
