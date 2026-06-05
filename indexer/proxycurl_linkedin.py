"""
indexer/proxycurl_linkedin.py — Resolve LinkedIn URLs from emails via Proxycurl.

Takes talent_index profiles that have an email but no linkedin_url,
calls Proxycurl's person_lookup endpoint, and writes the found URLs back.

Proxycurl person_lookup by email:
  GET https://nubela.co/proxycurl/api/linkedin/profile/resolve/email
  Headers: X-Api-Key: {PROXYCURL_API_KEY}
  Params:  email=..., lookup_depth=superficial
  Cost:    $0.01 / call (1 credit)
  Returns: {"url": "https://linkedin.com/in/...", ...} or 404 if not found

After running this, run enrich_batch.py to pull the full LinkedIn profiles.

Usage:
    python -m indexer.proxycurl_linkedin --dry-run     # show scope + cost
    python -m indexer.proxycurl_linkedin               # resolve all
    python -m indexer.proxycurl_linkedin --limit 50    # resolve first 50
    python -m indexer.proxycurl_linkedin --country IT  # Italy only

Environment:
    PROXYCURL_API_KEY=...     (required — get at nubela.co/proxycurl)
    SUPABASE_URL=...           (required)
    SUPABASE_SERVICE_KEY=...   (required)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import time
from datetime import datetime, timezone

import httpx

from db.client import get_supabase

_DIM    = "\033[2m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_RESET  = "\033[0m"
_BOLD   = "\033[1m"

PROXYCURL_URL  = "https://nubela.co/proxycurl/api/linkedin/profile/resolve/email"
COST_PER_CALL  = 0.01
CONCURRENCY    = 5
BATCH_DELAY    = 0.5   # seconds between batches

_stop_requested = False


def _install_signal_handler() -> None:
    def _handler(sig, frame):
        global _stop_requested
        print(f"\n{_YELLOW}[proxycurl] Ctrl-C — stopping after current batch.{_RESET}")
        _stop_requested = True
    signal.signal(signal.SIGINT, _handler)


# ── Load targets ──────────────────────────────────────────────────────────────

def load_targets(country: str | None, limit: int | None) -> list[dict]:
    """Profiles with an email but no linkedin_url."""
    sb = get_supabase()

    q = (
        sb.table("talent_index")
        .select("github_username, email, location_raw, city, country_code")
        .is_("linkedin_url", "null")
        .not_.is_("email", "null")
        .gt("expires_at", datetime.now(timezone.utc).isoformat())
        .order("activity_score", desc=True)
    )

    if country:
        q = q.eq("country_code", country)
    if limit:
        q = q.limit(limit)

    return q.execute().data or []


# ── Proxycurl call ────────────────────────────────────────────────────────────

async def _resolve_email(
    email: str,
    api_key: str,
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
) -> str | None:
    """Return LinkedIn URL for email, or None if not found."""
    async with semaphore:
        try:
            resp = await client.get(
                PROXYCURL_URL,
                params={"email": email, "lookup_depth": "superficial"},
                headers={"X-Api-Key": api_key},
                timeout=20.0,
            )
            if resp.status_code == 200:
                return resp.json().get("url") or None
            if resp.status_code == 404:
                return None   # legitimate not found
            if resp.status_code == 402:
                print(f"\n{_RED}[proxycurl] Out of credits — top up at nubela.co{_RESET}")
                return None
            print(f"\n{_YELLOW}[proxycurl] HTTP {resp.status_code} for {email}{_RESET}")
            return None
        except Exception as e:
            print(f"\n{_RED}[proxycurl] Error for {email}: {e}{_RESET}")
            return None


def _update_linkedin_url(github_username: str, linkedin_url: str) -> None:
    sb = get_supabase()
    sb.table("talent_index") \
        .update({"linkedin_url": linkedin_url}) \
        .eq("github_username", github_username) \
        .execute()


# ── Batch runner ──────────────────────────────────────────────────────────────

async def run(targets: list[dict], api_key: str, dry_run: bool) -> dict:
    stats = {"found": 0, "not_found": 0, "total": len(targets)}
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(timeout=20.0) as client:
        for batch_start in range(0, len(targets), CONCURRENCY):
            if _stop_requested:
                break

            batch = targets[batch_start : batch_start + CONCURRENCY]

            tasks = [
                _resolve_email(row["email"], api_key, semaphore, client)
                for row in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for row, result in zip(batch, results):
                login    = row["github_username"]
                email    = row["email"]
                location = " ".join(filter(None, [row.get("city"), row.get("country_code")]))
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                idx = batch_start + batch.index(row) + 1

                if isinstance(result, str) and result:
                    print(
                        f"  {_DIM}[{ts}]{_RESET} {_GREEN}✓{_RESET} "
                        f"({idx}/{len(targets)}) {_BOLD}{login}{_RESET} "
                        f"{_DIM}({location}){_RESET}  {result}"
                    )
                    stats["found"] += 1
                    if not dry_run:
                        _update_linkedin_url(login, result)
                else:
                    print(
                        f"  {_DIM}[{ts}]{_RESET} {_DIM}—{_RESET} "
                        f"({idx}/{len(targets)}) {login} "
                        f"{_DIM}({email}) not found{_RESET}"
                    )
                    stats["not_found"] += 1

            if batch_start + CONCURRENCY < len(targets) and not _stop_requested:
                await asyncio.sleep(BATCH_DELAY)

    return stats


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve LinkedIn URLs from emails via Proxycurl")
    parser.add_argument("--dry-run",  action="store_true", help="Don't write to DB")
    parser.add_argument("--limit",    type=int, default=None)
    parser.add_argument("--country",  choices=["IT", "CH"])
    args = parser.parse_args()

    _install_signal_handler()

    api_key = os.environ.get("PROXYCURL_API_KEY", "")
    if not api_key:
        print(f"{_RED}Error: PROXYCURL_API_KEY not set.{_RESET}")
        print("  Sign up at https://nubela.co/proxycurl — $10 buys 1000 lookups.")
        raise SystemExit(1)

    print(f"\n{_BOLD}{'═' * 70}{_RESET}")
    print(f"  {_BOLD}Mirai — Proxycurl Email → LinkedIn URL{_RESET}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'═' * 70}\n")

    print("[1/2] Loading profiles with email but no linkedin_url…")
    targets = load_targets(country=args.country, limit=args.limit)

    if not targets:
        print(f"{_GREEN}✓ All profiles with emails already have a LinkedIn URL.{_RESET}\n")
        return

    by_country: dict[str, int] = {}
    for t in targets:
        c = t.get("country_code") or "??"
        by_country[c] = by_country.get(c, 0) + 1
    breakdown = "  ".join(f"{c}: {n}" for c, n in sorted(by_country.items()))
    est_cost = len(targets) * COST_PER_CALL

    print(f"  {_BOLD}{len(targets)}{_RESET} profiles  ({breakdown})")
    print(f"  Estimated cost: {_BOLD}${est_cost:.2f}{_RESET}  ({len(targets)} × $0.01)")
    if args.dry_run:
        print(f"  {_YELLOW}[dry-run] No Proxycurl calls will be made.{_RESET}")
        return

    print(f"\n[2/2] Resolving via Proxycurl…\n")
    start = time.monotonic()
    stats = asyncio.run(run(targets, api_key, args.dry_run))
    elapsed = time.monotonic() - start

    actual_cost = stats["found"] * COST_PER_CALL
    hit_rate = stats["found"] / stats["total"] * 100 if stats["total"] else 0

    print(f"\n{'═' * 70}")
    print(f"  Done in {elapsed:.0f}s")
    print(f"  {_GREEN}Found: {stats['found']}  ({hit_rate:.0f}% hit rate){_RESET}")
    print(f"  {_DIM}Not found: {stats['not_found']}{_RESET}")
    print(f"  Actual cost: ${actual_cost:.2f}")
    print(f"\n  Next step: python -m indexer.enrich_batch")
    print(f"{'═' * 70}\n")


if __name__ == "__main__":
    main()
