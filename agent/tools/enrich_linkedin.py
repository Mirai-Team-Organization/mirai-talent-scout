"""
LinkedIn enrichment tool — uses Apify linkedin-profile-scraper, caches results 30 days.

Uses asyncio.gather for concurrent batch enrichment with a 600s ceiling.
Partial failures (one candidate fails) never abort the whole batch.
If no LinkedIn URL is found on the GitHub profile, enrichment is skipped gracefully.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from datetime import datetime, timezone
from typing import Optional

import httpx
from strands import tool

from agent.models import LinkedInEnrichment, LinkedInPosition
from db.linkedin import get_cached_linkedin, upsert_linkedin
from scoring.mobility_scorer import detect_move_signals

# Apify actor for LinkedIn profile scraping (harvestapi, no cookies required)
# https://apify.com/harvestapi/linkedin-profile-scraper
APIFY_ACTOR = "harvestapi~linkedin-profile-scraper"


def _run_in_thread(coro):
    """Run an async coroutine in a fresh thread+event loop, safe from any calling context."""
    result = None
    exc = None

    def target():
        nonlocal result, exc
        try:
            result = asyncio.run(coro)
        except Exception as e:
            exc = e

    t = threading.Thread(target=target)
    t.start()
    t.join(timeout=25)
    if exc:
        raise exc
    return result


def _extract_linkedin_url(profile: dict) -> Optional[str]:
    """
    Extract a LinkedIn profile URL from a GitHub profile dict.
    Checks websiteUrl and bio for linkedin.com/in/ patterns.
    """
    pattern = re.compile(r"https?://(www\.)?linkedin\.com/in/[^\s\"'>]+", re.IGNORECASE)

    website = profile.get("profile", {}).get("websiteUrl") or ""
    if pattern.search(website):
        return pattern.search(website).group(0).rstrip("/")

    bio = profile.get("profile", {}).get("bio") or ""
    match = pattern.search(bio)
    if match:
        return match.group(0).rstrip("/")

    return None


async def _call_apify(linkedin_url: str) -> dict:
    """
    Run the Apify harvestapi LinkedIn profile scraper and return the first result.
    Docs: https://apify.com/harvestapi/linkedin-profile-scraper
    Input: {"urls": ["https://linkedin.com/in/..."]}
    """
    api_token = os.environ["APIFY_API_TOKEN"]

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
            params={"token": api_token},
            json={"urls": [linkedin_url]},
        )
        resp.raise_for_status()
        items = resp.json()

    if not items:
        raise ValueError(f"Apify returned no results for {linkedin_url}")

    return items[0]


def _parse_apify_response(github_username: str, data: dict) -> LinkedInEnrichment:
    """Normalise harvestapi linkedin-profile-scraper response into LinkedInEnrichment model.

    harvestapi field mapping (differs from old apify~linkedin-profile-scraper):
      firstName + lastName  → full_name
      headline              → current_title fallback
      location.linkedinText → location
      currentPosition[]     → used for current role
      experience[]          → positions (endDate.text == "Present" means is_current)
    """
    _MONTH_MAP = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }

    def _fmt_date(d: dict) -> Optional[str]:
        if not d or not d.get("year"):
            return None
        m = d.get("month", "")
        m_str = _MONTH_MAP.get(str(m).lower()[:3], str(m).zfill(2) if str(m).isdigit() else "01")
        return f"{d['year']}-{m_str}"

    positions = []
    for pos in data.get("experience", []):
        start = pos.get("startDate") or {}
        end = pos.get("endDate") or {}

        start_str = _fmt_date(start)
        is_current = str(end.get("text", "")).strip().lower() == "present"
        end_str = None if is_current else _fmt_date(end)

        positions.append(LinkedInPosition(
            title=pos.get("position", ""),
            company=pos.get("companyName", ""),
            start_date=start_str,
            end_date=end_str,
            is_current=is_current,
        ))

    current = next((p for p in positions if p.is_current), None)

    first = data.get("firstName", "") or ""
    last = data.get("lastName", "") or ""
    full_name = f"{first} {last}".strip() or None

    location_raw = data.get("location") or {}
    if isinstance(location_raw, dict):
        location = location_raw.get("linkedinText") or location_raw.get("text")
    else:
        location = location_raw or None

    return LinkedInEnrichment(
        github_username=github_username,
        linkedin_url=data.get("linkedinUrl") or data.get("url"),
        full_name=full_name,
        current_title=current.title if current else data.get("headline"),
        current_company=current.company if current else None,
        location=location,
        positions=positions,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


async def _enrich_single(
    github_username: str,
    linkedin_url: str,
    semaphore: asyncio.Semaphore,
    force_refresh: bool = False,
) -> LinkedInEnrichment:
    """Enrich a single candidate — cached, with semaphore-controlled concurrency."""
    async with semaphore:
        if not force_refresh:
            cached = get_cached_linkedin(github_username)
            if cached and cached.get("enrichment_data"):
                return _parse_apify_response(github_username, cached["enrichment_data"])

        raw = await _call_apify(linkedin_url)

        enrichment = _parse_apify_response(github_username, raw)
        mobility = detect_move_signals(enrichment)

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
) -> list[LinkedInEnrichment | Exception | None]:
    """
    Enrich a batch of candidates concurrently.
    - Candidates without a LinkedIn URL are skipped (None returned)
    - Max 5 concurrent Apify calls (semaphore)
    - 600s overall timeout — returns partial results on timeout
    - return_exceptions=True — one failure doesn't abort others
    """
    semaphore = asyncio.Semaphore(5)

    async def _safe_enrich(c: dict) -> LinkedInEnrichment | Exception | None:
        login = c["profile"]["login"]
        linkedin_url = _extract_linkedin_url(c)
        if not linkedin_url:
            return None  # no LinkedIn URL on GitHub profile — skip gracefully
        try:
            return await _enrich_single(
                github_username=login,
                linkedin_url=linkedin_url,
                semaphore=semaphore,
                force_refresh=force_refresh,
            )
        except Exception as e:
            print(f"[enrich_linkedin] Failed for {login}: {e}")
            return e

    try:
        async with asyncio.timeout(22):
            results = await asyncio.gather(*[_safe_enrich(c) for c in candidates])
    except TimeoutError:
        print("[enrich_linkedin] Batch timed out after 22s — returning partial results")
        results = []

    return results


@tool
def enrich_linkedin(
    candidates: list[dict],
    force_refresh: bool = False,
) -> list[dict]:
    """
    Enrich a list of GitHub profiles with LinkedIn data and mobility scores.

    Looks for LinkedIn URLs in each candidate's GitHub websiteUrl or bio.
    Candidates without a LinkedIn URL are returned with linkedin=null.

    Args:
        candidates: List of GitHub profile dicts from search_github()
        force_refresh: Skip cache and re-fetch from Apify

    Returns:
        List of dicts combining github profile + linkedin enrichment + mobility score.
        Candidates with failed or missing enrichment are included with linkedin/mobility=null.
    """
    enrichments = _run_in_thread(_enrich_batch(candidates, force_refresh=force_refresh))

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
            linkedin_data = enrichment.model_dump()
            # Cap positions to last 5 — full history can be 15+ roles per candidate
            if linkedin_data.get("positions"):
                linkedin_data["positions"] = linkedin_data["positions"][:5]
            entry["linkedin"] = linkedin_data
            entry["mobility"] = mobility.model_dump()
        else:
            entry["linkedin"] = None
            entry["mobility"] = None

        result.append(entry)

    return result
