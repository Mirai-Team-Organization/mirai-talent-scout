"""
Quick end-to-end pipeline test for a single candidate.
Usage:
    python scripts/test_pipeline.py
"""

import json
import os
import sys
from pathlib import Path

# Load .env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

sys.path.insert(0, str(Path(__file__).parent.parent))

from scoring.linkedin_analyzer import (
    parse_harvestapi_response,
    detect_move_signals,
    compute_career_signals,
)

# _call_apify inline (avoids pulling in strands which isn't installed outside the agent runtime)
import httpx

async def _call_apify(linkedin_url: str) -> dict:
    APIFY_ACTOR = "harvestapi~linkedin-profile-scraper"
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


GITHUB_USERNAME = "teddykabg"
LINKEDIN_URL = "https://www.linkedin.com/in/teddykabg"


def separator(label: str):
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print('─' * 60)


async def main():
    import asyncio

    separator("1 · Fetching LinkedIn profile via Apify harvestapi")
    print(f"   URL: {LINKEDIN_URL}")

    try:
        raw = await _call_apify(LINKEDIN_URL)
    except Exception as e:
        print(f"   ERROR: {e}")
        return

    separator("2 · Raw Apify response (trimmed)")
    safe = {k: v for k, v in raw.items() if k not in ("experience", "education", "languages")}
    print(json.dumps(safe, indent=2, default=str))
    print(f"\n   experience entries : {len(raw.get('experience', []))}")
    print(f"   education entries  : {len(raw.get('education', []))}")
    print(f"   languages entries  : {len(raw.get('languages', []))}")
    print(f"   about (first 200)  : {str(raw.get('about', '') or '')[:200]!r}")

    separator("3 · Parsed LinkedInEnrichment")
    enrichment, about_text = parse_harvestapi_response(GITHUB_USERNAME, raw)
    print(json.dumps(enrichment.model_dump(), indent=2, default=str))
    print(f"\n   about_text (first 200): {about_text[:200]!r}")

    separator("4 · Mobility score")
    mobility = detect_move_signals(enrichment)
    print(json.dumps(mobility.model_dump(), indent=2, default=str))

    separator("5 · Career signals")
    signals = compute_career_signals(enrichment, about_text)
    print(json.dumps(signals.model_dump(), indent=2, default=str))

    separator("Summary")
    print(f"   Name              : {enrichment.full_name}")
    print(f"   Current role      : {enrichment.current_title} @ {enrichment.current_company}")
    print(f"   Location          : {enrichment.location}")
    print(f"   Open to work      : {enrichment.open_to_work}")
    print(f"   Languages spoken  : {enrichment.languages_spoken}")
    print(f"   Education         : {[(e.school, e.degree, e.year) for e in enrichment.education]}")
    print(f"   Positions stored  : {len(enrichment.positions)}")
    print(f"   Mobility score    : {mobility.mobility_score} (completeness={mobility.data_completeness})")
    print(f"   Missing signals   : {mobility.missing_signals}")
    print(f"   Years experience  : {signals.years_of_experience}")
    print(f"   Seniority level   : {signals.seniority_level}")
    print(f"   Career trajectory : {signals.career_trajectory}")
    print(f"   Quantified outcomes: {signals.has_quantified_outcomes}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
