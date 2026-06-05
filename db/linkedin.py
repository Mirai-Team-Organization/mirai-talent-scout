"""
LinkedIn enrichment cache — 90-day TTL.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from db.client import get_supabase


TTL_DAYS = 90


def get_cached_linkedin(github_username: str) -> Optional[dict]:
    """Return cached LinkedIn enrichment if fresh, else None."""
    sb = get_supabase()
    now = datetime.now(timezone.utc).isoformat()

    result = (
        sb.table("linkedin_enrichments")
        .select("enrichment_data, mobility_score, data_completeness, fetched_at")
        .eq("github_username", github_username)
        .gt("expires_at", now)
        .maybe_single()
        .execute()
    )

    return result.data if (result and result.data) else None


def upsert_linkedin(
    github_username: str,
    linkedin_url: Optional[str],
    enrichment_data: dict,
    mobility_score: Optional[int],
    data_completeness: float,
) -> None:
    """Insert or update a LinkedIn enrichment in the cache."""
    sb = get_supabase()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=TTL_DAYS)

    sb.table("linkedin_enrichments").upsert({
        "github_username": github_username,
        "linkedin_url": linkedin_url,
        "enrichment_data": enrichment_data,
        "mobility_score": mobility_score,
        "data_completeness": data_completeness,
        "fetched_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    }, on_conflict="github_username").execute()
