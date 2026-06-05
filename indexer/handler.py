"""
indexer/handler.py — Lambda entry point for the talent indexer.

EventBridge payload: {"shard": "it_milan" | "it_north" | "it_rest" |
                                "ch_zurich" | "ch_rest" | "oss_topics" |
                                "hackathon_refresh"}

Each invocation:
  1. Picks up pending (location, language) combos for its shard
  2. Fetches pages and upserts profiles until 60s before Lambda timeout
  3. Self-invokes asynchronously to continue if work remains

Self-chaining: after each invocation, if there are still pending combos, the
Lambda invokes itself with InvocationType=Event (async). This chains invocations
until the shard is complete — no orchestrator needed.
"""

from __future__ import annotations

import json
import os
import time

import boto3

from indexer.core import (
    TokenPool, fetch_profile, get_pending_combos, github_search_users_page,
    index_summary, mark_progress, upsert_profile,
)

# ── Shard definitions ─────────────────────────────────────────────────────────

_LANGUAGES = [
    "Python", "TypeScript", "JavaScript", "Go", "Rust",
    "Java", "Kotlin", "Swift", "C++",
]

_SHARDS: dict[str, list[str]] = {
    "it_milan":         ["Milan", "Milano"],
    "it_north":         ["Rome", "Roma", "Turin", "Torino"],
    "it_rest":          ["Florence", "Firenze", "Bologna", "Naples", "Napoli", "Genoa", "Palermo", "Bari", "Italy", "Italia"],
    "ch_zurich":        ["Zurich", "Zuerich"],
    "ch_rest":          ["Geneva", "Geneve", "Basel", "Bern", "Lausanne", "Lugano", "Switzerland", "Schweiz"],
    "oss_topics":       [],   # handled separately
    "hackathon_refresh": [],  # handled separately
}

# Lambda timeout: context.get_remaining_time_in_millis() used when available;
# otherwise fall back to env var or default 900s.
_DEFAULT_TIMEOUT_S = int(os.environ.get("FUNCTION_TIMEOUT", "900"))
_BUFFER_S = 90   # stop fetching 90s before timeout to allow final DB writes


def lambda_handler(event: dict, context=None) -> dict:
    shard = event.get("shard", "it_milan")
    locations = _SHARDS.get(shard, [])

    if shard == "oss_topics":
        result = _run_oss_topics(context)
    elif shard == "hackathon_refresh":
        result = _run_hackathon_refresh(context)
    else:
        result = _run_broad_shard(shard, locations, context)

    print(f"[handler] Shard={shard} done. {result}")

    # Self-invoke if there's more work to do
    if result.get("remaining_combos", 0) > 0:
        _self_invoke({"shard": shard})

    summary = index_summary()
    print(f"[handler] Index summary: {summary}")

    return {"shard": shard, **result, "index_summary": summary}


def _deadline(context) -> float:
    """Return the wall-clock time (time.monotonic()) at which we must stop."""
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        remaining_ms = context.get_remaining_time_in_millis()
        return time.monotonic() + (remaining_ms / 1000) - _BUFFER_S
    return time.monotonic() + _DEFAULT_TIMEOUT_S - _BUFFER_S


def _run_broad_shard(shard: str, locations: list[str], context) -> dict:
    pool = TokenPool.from_env()
    deadline = _deadline(context)

    pending = get_pending_combos(locations, _LANGUAGES)
    print(f"[{shard}] {len(pending)} combos pending.")

    total_upserted = 0
    seen: set[str] = set()

    for location, language in pending:
        if time.monotonic() >= deadline:
            print(f"[{shard}] Approaching timeout — stopping.")
            break

        combo_upserted = 0
        pages_fetched = 0

        for page in range(1, 11):  # GitHub max 10 pages × 100 = 1,000 per combo
            if time.monotonic() >= deadline:
                break

            token, _ = pool.acquire()
            logins = github_search_users_page(location, language, page, token)

            if not logins:
                break  # no more results

            pages_fetched += 1

            for login in logins:
                if login in seen:
                    continue
                seen.add(login)

                if time.monotonic() >= deadline:
                    break

                token, _ = pool.acquire()
                profile = fetch_profile(login, token)
                if profile:
                    upsert_profile(profile, source="github_broad")
                    combo_upserted += 1
                    total_upserted += 1

        mark_progress(location, language, pages_fetched, combo_upserted, completed=(pages_fetched > 0 and len(logins) < 100))

        print(f"[{shard}] {location} × {language}: {combo_upserted} profiles ({pages_fetched} pages)")

    remaining = get_pending_combos(locations, _LANGUAGES)
    return {"upserted": total_upserted, "remaining_combos": len(remaining)}


def _run_oss_topics(context) -> dict:
    """
    Find top repos by topic, extract contributors, filter to IT/CH.
    """
    from indexer.shards.oss_topics import run
    return run(context)


def _run_hackathon_refresh(context) -> dict:
    """
    Index hackathon org members + clean up expired profiles.
    """
    from indexer.shards.hackathon_refresh import run
    return run(context)


def _self_invoke(payload: dict) -> None:
    """Async self-invocation to continue the shard after this invocation ends."""
    fn_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "mirai-talent-indexer")
    try:
        client = boto3.client("lambda", region_name=os.environ.get("AWS_REGION", "eu-west-1"))
        client.invoke(
            FunctionName=fn_name,
            InvocationType="Event",   # async — fire and forget
            Payload=json.dumps(payload).encode(),
        )
        print(f"[handler] Self-invoked for continuation: {payload}")
    except Exception as e:
        print(f"[handler] Self-invoke failed (non-fatal): {e}")
