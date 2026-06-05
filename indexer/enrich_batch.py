"""
indexer/enrich_batch.py — Batch LinkedIn enrichment for all talent_index profiles.

Pre-enriches every profile that has a linkedin_url so that searches serve from
cache and never call Apify at query time.  Results land in linkedin_enrichments
(30-day TTL) — the same table enrich_linkedin checks first.

After each enrichment, talent_index.role_signals is updated from LinkedIn position
titles. LinkedIn-derived signals REPLACE GitHub-inferred signals when present
(job titles are more reliable than repo topic heuristics).

Usage:
    python -m indexer.enrich_batch                        # enrich all unenriched profiles
    python -m indexer.enrich_batch --dry-run              # show scope + cost, no API calls
    python -m indexer.enrich_batch --limit 100            # process at most N profiles
    python -m indexer.enrich_batch --force                # re-enrich even if cached
    python -m indexer.enrich_batch --country IT           # Italy only
    python -m indexer.enrich_batch --country CH           # Switzerland only
    python -m indexer.enrich_batch --update-signals-only  # refresh role_signals from cache, no Apify

Environment:
    APIFY_API_TOKEN=...         (required for enrichment; not needed for --update-signals-only)
    SUPABASE_URL=...            (required)
    SUPABASE_SERVICE_KEY=...    (required)

Cost: $0.004/profile (HarvestAPI via Apify).  Run --dry-run first.
--update-signals-only: zero Apify cost — reads existing cached enrichments only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time
from datetime import datetime, timezone

# Lazy imports so --dry-run works without all envvars set
from db.client import get_supabase
from db.linkedin import get_cached_linkedin, upsert_linkedin
from agent.tools.enrich_linkedin import _call_apify, _parse_apify_response
from scoring.linkedin_analyzer import detect_move_signals, parse_harvestapi_response
from indexer.role_signals import infer_role_signals_from_linkedin

APIFY_COST_PER_PROFILE = 0.004
CONCURRENCY = 5           # Apify allows 5 parallel runs for HarvestAPI
BATCH_DELAY  = 1.0        # seconds between batches (be nice to Apify)

_DIM   = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED  = "\033[31m"
_CYAN = "\033[36m"
_RESET = "\033[0m"
_BOLD  = "\033[1m"

_stop_requested = False


def _install_signal_handler() -> None:
    def _handler(sig, frame):
        global _stop_requested
        print(f"\n{_YELLOW}[enrich_batch] Ctrl-C received — will stop after current batch.{_RESET}")
        _stop_requested = True
    signal.signal(signal.SIGINT, _handler)


# ── Role signal update from LinkedIn data ────────────────────────────────────

def _update_role_signals(login: str, enrichment: object) -> bool:
    """
    Derive role signals from a parsed LinkedInEnrichment and write them to
    talent_index.role_signals. LinkedIn signals REPLACE GitHub-inferred signals
    when the LinkedIn inference produces any results (job titles are more reliable
    than repo topic heuristics). No-op when LinkedIn yields no signals.

    Returns True if an update was written, False if skipped.
    """
    li_signals = infer_role_signals_from_linkedin(enrichment)
    if not li_signals:
        return False  # no LinkedIn title matched — leave existing signals unchanged

    sb = get_supabase()
    sb.table("talent_index").update(
        {"role_signals": li_signals}
    ).eq("github_username", login).execute()
    return True


# ── Load unenriched profiles from Supabase ────────────────────────────────────

def load_targets(
    country: str | None,
    force: bool,
    limit: int | None,
) -> list[dict]:
    """
    Return talent_index rows that need LinkedIn enrichment.

    Without --force: only rows with no fresh linkedin_enrichments entry.
    With --force: all rows with a linkedin_url.
    """
    sb = get_supabase()

    q = (
        sb.table("talent_index")
        .select("github_username, linkedin_url, country_code, city")
        .not_.is_("linkedin_url", "null")
        .gt("expires_at", datetime.now(timezone.utc).isoformat())
        .order("activity_score", desc=True)
    )

    if country:
        q = q.eq("country_code", country)

    # Always cap initial fetch to avoid huge payloads; over-fetch so filtering leaves enough
    fetch_limit = (limit * 3) if limit else 2000
    q = q.limit(fetch_limit)

    print("  → querying talent_index…", flush=True)
    result = q.execute()
    rows = result.data or []
    print(f"  → {len(rows)} rows with linkedin_url", flush=True)

    if force:
        targets = rows[:limit] if limit else rows
    else:
        print("  → checking linkedin_enrichments cache…", flush=True)
        cached_result = (
            sb.table("linkedin_enrichments")
            .select("github_username")
            .gt("expires_at", datetime.now(timezone.utc).isoformat())
            .execute()
        )
        cached_set = {r["github_username"] for r in (cached_result.data or [])}
        print(f"  → {len(cached_set)} already cached, filtering…", flush=True)
        targets = [r for r in rows if r["github_username"] not in cached_set]
        if limit:
            targets = targets[:limit]

    return targets


# ── Enrich a single profile ───────────────────────────────────────────────────

async def _enrich_one(
    row: dict,
    semaphore: asyncio.Semaphore,
    idx: int,
    total: int,
) -> tuple[str, str]:
    """
    Enrich a single talent_index row.
    Returns (github_username, status) where status is "ok" | "skip" | "fail:<msg>".
    """
    login       = row["github_username"]
    linkedin_url = row["linkedin_url"]
    location    = f"{row.get('city') or ''} {row.get('country_code') or ''}".strip()

    async with semaphore:
        try:
            raw = await _call_apify(linkedin_url)
            enrichment, _about = _parse_apify_response(login, raw)
            mobility = detect_move_signals(enrichment)

            upsert_linkedin(
                github_username=login,
                linkedin_url=enrichment.linkedin_url or linkedin_url,
                enrichment_data=raw,
                mobility_score=mobility.mobility_score,
                data_completeness=mobility.data_completeness,
            )

            # Update role_signals from LinkedIn job titles (replaces GitHub heuristics)
            _update_role_signals(login, enrichment)

            current = enrichment.current_title or ""
            company = enrichment.current_company or ""
            role_str = f" — {current} @ {company}" if (current or company) else ""
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(
                f"  {_DIM}[{ts}]{_RESET} {_GREEN}✓{_RESET} "
                f"({idx}/{total}) {_BOLD}{login}{_RESET} {_DIM}({location}){role_str}{_RESET}"
            )
            return login, "ok"

        except Exception as e:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            msg = str(e)[:80]
            print(
                f"  {_DIM}[{ts}]{_RESET} {_RED}✗{_RESET} "
                f"({idx}/{total}) {login} {_DIM}— {msg}{_RESET}"
            )
            return login, f"fail:{e}"


# ── Process targets in batches ────────────────────────────────────────────────

async def run_enrichment(targets: list[dict]) -> dict:
    semaphore = asyncio.Semaphore(CONCURRENCY)
    total = len(targets)
    ok = fail = 0

    for batch_start in range(0, total, CONCURRENCY):
        if _stop_requested:
            print(f"\n{_YELLOW}[enrich_batch] Stopped by user after {batch_start} profiles.{_RESET}")
            break

        batch = targets[batch_start : batch_start + CONCURRENCY]
        tasks = [
            _enrich_one(row, semaphore, batch_start + i + 1, total)
            for i, row in enumerate(batch)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                fail += 1
            elif isinstance(r, tuple):
                _, status = r
                if status == "ok":
                    ok += 1
                else:
                    fail += 1

        # Breathe between batches
        if batch_start + CONCURRENCY < total and not _stop_requested:
            await asyncio.sleep(BATCH_DELAY)

    return {"ok": ok, "fail": fail, "total": ok + fail}


# ── Update signals from cached enrichments (no Apify cost) ───────────────────

def _run_update_signals_only(country: str | None, limit: int | None) -> None:
    """
    Read all cached linkedin_enrichments, re-derive role_signals, and update
    talent_index.role_signals for any profile where LinkedIn titles produce signals.
    Zero Apify calls, zero cost. Idempotent.
    """
    sb = get_supabase()

    print(f"\n{_BOLD}{'═' * 70}{_RESET}")
    print(f"  {_BOLD}Mirai — Update Role Signals from LinkedIn Cache{_RESET}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | Zero Apify cost")
    print(f"{'═' * 70}\n")

    print("[1/2] Loading cached enrichments…", flush=True)
    q = (
        sb.table("linkedin_enrichments")
        .select("github_username, enrichment_data")
        .gt("expires_at", datetime.now(timezone.utc).isoformat())
        .not_.is_("enrichment_data", "null")
    )
    if limit:
        q = q.limit(limit)
    result = q.execute()
    rows = result.data or []

    # Filter by country if requested (join needed — skip for simplicity, filter in Python)
    if country:
        ti_result = (
            sb.table("talent_index")
            .select("github_username")
            .eq("country_code", country)
            .execute()
        )
        country_set = {r["github_username"] for r in (ti_result.data or [])}
        rows = [r for r in rows if r["github_username"] in country_set]

    print(f"  → {len(rows)} cached enrichments to process\n")
    print("[2/2] Updating role_signals…\n")

    updated = skipped = 0
    for row in rows:
        login = row["github_username"]
        raw = row.get("enrichment_data")
        if not raw:
            skipped += 1
            continue
        try:
            enrichment, _ = parse_harvestapi_response(login, raw)
            was_updated = _update_role_signals(login, enrichment)
            if was_updated:
                updated += 1
                print(f"  {_GREEN}✓{_RESET} {login}")
            else:
                skipped += 1
        except Exception as e:
            print(f"  {_RED}✗{_RESET} {login} — {str(e)[:60]}")
            skipped += 1

    print(f"\n{'═' * 70}")
    print(f"  Updated: {_GREEN}{updated}{_RESET}   Skipped (no LinkedIn signals): {_DIM}{skipped}{_RESET}")
    print(f"{'═' * 70}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Batch LinkedIn enrichment for talent_index")
    parser.add_argument("--dry-run",             action="store_true", help="Show scope + cost without calling Apify")
    parser.add_argument("--limit",               type=int, default=None, help="Max profiles to enrich")
    parser.add_argument("--force",               action="store_true",   help="Re-enrich even if cached")
    parser.add_argument("--country",             choices=["IT", "CH"],  help="Restrict to one country")
    parser.add_argument("--update-signals-only", action="store_true",
                        help="Refresh role_signals from cached linkedin_enrichments — no Apify calls, zero cost")
    args = parser.parse_args()

    _install_signal_handler()

    print(f"\n{_BOLD}{'═' * 70}{_RESET}")
    print(f"  {_BOLD}Mirai — Batch LinkedIn Enrichment{_RESET}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'═' * 70}\n")

    # ── --update-signals-only: refresh role_signals from cache, no Apify ────────
    if args.update_signals_only:
        _run_update_signals_only(country=args.country, limit=args.limit)
        return

    print("[1/2] Loading unenriched profiles from talent_index…", flush=True)
    targets = load_targets(
        country=args.country,
        force=args.force,
        limit=args.limit,
    )

    if not targets:
        print(f"{_GREEN}✓ Nothing to enrich — all profiles are already cached.{_RESET}\n")
        return

    est_cost = len(targets) * APIFY_COST_PER_PROFILE

    # Country breakdown
    by_country: dict[str, int] = {}
    for t in targets:
        c = t.get("country_code") or "??"
        by_country[c] = by_country.get(c, 0) + 1

    breakdown = "  ".join(f"{c}: {n}" for c, n in sorted(by_country.items()))
    print(f"  Found {_BOLD}{len(targets)}{_RESET} profiles to enrich  ({breakdown})")
    print(f"  Estimated cost: {_BOLD}${est_cost:.2f}{_RESET}  ({len(targets)} × $0.004 HarvestAPI)")
    print(f"  Concurrency: {CONCURRENCY} parallel Apify calls\n")

    if args.dry_run:
        print(f"{_YELLOW}[dry-run] No Apify calls made. Pass without --dry-run to proceed.{_RESET}")
        if len(targets) <= 20:
            for t in targets:
                print(f"  {t['github_username']:30s} {t.get('city') or ''} {t.get('country_code') or ''}")
        else:
            for t in targets[:10]:
                print(f"  {t['github_username']:30s} {t.get('city') or ''} {t.get('country_code') or ''}")
            print(f"  … and {len(targets) - 10} more")
        return

    print(f"[2/2] Enriching {len(targets)} profiles…\n")
    start = time.monotonic()

    summary = asyncio.run(run_enrichment(targets))

    elapsed = time.monotonic() - start
    actual_cost = summary["ok"] * APIFY_COST_PER_PROFILE

    print(f"\n{'═' * 70}")
    print(f"  Enrichment complete in {elapsed:.1f}s")
    print(f"  {_GREEN}✓ OK: {summary['ok']}{_RESET}   {_RED}✗ Failed: {summary['fail']}{_RESET}")
    print(f"  Actual cost: ${actual_cost:.3f}  ({summary['ok']} profiles × $0.004)")
    print(f"  Cache will serve enrichments for the next 90 days — no Apify calls at search time.")
    print(f"{'═' * 70}\n")


if __name__ == "__main__":
    main()
