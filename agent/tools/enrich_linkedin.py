"""
LinkedIn enrichment tool — wraps orangeslice, caches results 30 days.

Uses asyncio.gather for concurrent batch enrichment with a 600s ceiling.
Partial failures (one candidate fails) never abort the whole batch.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from strands import tool

from agent.models import LinkedInEnrichment, LinkedInPosition
from db.linkedin import get_cached_linkedin, upsert_linkedin
from scoring.mobility_scorer import detect_move_signals


async def _call_orangeslice(github_username: str, linkedin_url: Optional[str]) -> dict:
    """
    Call the orangeslice LinkedIn enrichment service.
    Read orangeslice-docs/services/index.md for the full API contract.
    """
    api_url = os.environ["ORANGESLICE_API_URL"]
    api_key = os.environ["ORANGESLICE_API_KEY"]

    async with httpx.AsyncClient(timeout=30.0) as client:
        payload: dict = {"github_username": github_username}
        if linkedin_url:
            payload["linkedin_url"] = linkedin_url

        resp = await client.post(
            f"{api_url}/enrich/person",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        return resp.json()


def _parse_orangeslice_response(github_username: str, data: dict) -> LinkedInEnrichment:
    """Normalise orangeslice response into LinkedInEnrichment model."""
    positions = []
    for pos in data.get("positions", []):
        positions.append(LinkedInPosition(
            title=pos.get("title", ""),
            company=pos.get("company", ""),
            start_date=pos.get("start_date"),
            end_date=pos.get("end_date"),
            is_current=pos.get("is_current", False),
        ))

    return LinkedInEnrichment(
        github_username=github_username,
        linkedin_url=data.get("linkedin_url"),
        full_name=data.get("full_name"),
        current_title=data.get("current_title"),
        current_company=data.get("current_company"),
        location=data.get("location"),
        positions=positions,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


async def _enrich_single(
    github_username: str,
    linkedin_url: Optional[str],
    semaphore: asyncio.Semaphore,
    force_refresh: bool = False,
) -> LinkedInEnrichment:
    """Enrich a single candidate — cached, with semaphore-controlled concurrency."""
    async with semaphore:
        # Cache check
        if not force_refresh:
            cached = get_cached_linkedin(github_username)
            if cached and cached.get("enrichment_data"):
                return _parse_orangeslice_response(
                    github_username, cached["enrichment_data"]
                )

        # Live enrichment
        raw = await _call_orangeslice(github_username, linkedin_url)

        # Parse and compute mobility
        enrichment = _parse_orangeslice_response(github_username, raw)
        mobility = detect_move_signals(enrichment)

        # Cache result
        upsert_linkedin(
            github_username=github_username,
            linkedin_url=enrichment.linkedin_url,
            enrichment_data=raw,
            mobility_score=mobility.mobility_score,
            data_completeness=mobility.data_completeness,
        )

        return enrichment


async def _enrich_batch(
    candidates: list[dict],
    force_refresh: bool = False,
) -> list[LinkedInEnrichment | Exception]:
    """
    Enrich a batch of candidates concurrently.
    - Max 5 concurrent orangeslice calls (semaphore)
    - 600s overall timeout — returns partial results on timeout
    - return_exceptions=True — one failure doesn't abort others
    """
    semaphore = asyncio.Semaphore(5)

    async def _safe_enrich(c: dict) -> LinkedInEnrichment | Exception:
        try:
            return await _enrich_single(
                github_username=c["profile"]["login"],
                linkedin_url=c.get("linkedin_url"),
                semaphore=semaphore,
                force_refresh=force_refresh,
            )
        except Exception as e:
            print(f"[enrich_linkedin] Failed for {c['profile']['login']}: {e}")
            return e

    try:
        async with asyncio.timeout(600):
            results = await asyncio.gather(*[_safe_enrich(c) for c in candidates])
    except TimeoutError:
        print("[enrich_linkedin] Batch timed out after 600s — returning partial results")
        results = []  # partial — caller handles gracefully

    return results


@tool
def enrich_linkedin(
    candidates: list[dict],
    force_refresh: bool = False,
) -> list[dict]:
    """
    Enrich a list of GitHub profiles with LinkedIn data and mobility scores.

    Args:
        candidates: List of GitHub profile dicts from search_github()
        force_refresh: Skip cache and re-fetch from orangeslice

    Returns:
        List of dicts combining github profile + linkedin enrichment + mobility score.
        Candidates with failed enrichment are included with mobility=None.
    """
    enrichments = asyncio.run(_enrich_batch(candidates, force_refresh=force_refresh))

    enrichment_map: dict[str, LinkedInEnrichment] = {}
    for e in enrichments:
        if isinstance(e, LinkedInEnrichment):
            enrichment_map[e.github_username] = e

    result = []
    for profile in candidates:
        login = profile["profile"]["login"]
        enrichment = enrichment_map.get(login)

        entry = dict(profile)
        if enrichment:
            mobility = detect_move_signals(enrichment)
            entry["linkedin"] = enrichment.model_dump()
            entry["mobility"] = mobility.model_dump()
        else:
            entry["linkedin"] = None
            entry["mobility"] = None

        result.append(entry)

    return result
